"""
neobot_main.py — NeoLife Bot v2
Canal principal: WhatsApp (whatsapp-web.js via Node.js bridge)
CRM: SQLite + Google Sheets (mismo formato)
Calendar: Google Calendar (miguelro.blescr@gmail.com)

Requisitos extra:
  pip install fastapi uvicorn openai gspread google-auth google-api-python-client python-dotenv requests python-telegram-bot
  npm install whatsapp-web.js qrcode-terminal express  (en carpeta /whatsapp-bridge)

Variables NeoBot.env:
  TOKKO_KEY, OPENAI_KEY, TELEGRAM_BOT_TOKEN
  GOOGLE_CALENDAR_ID=miguelro.blescr@gmail.com
  ASESOR_EMAIL=miguelro.blescr@gmail.com
  SHEET_NAME=CLIENTES NEO
  WA_BRIDGE_URL=http://localhost:3001   ← servidor Node.js WhatsApp
"""

import os
import json
import time
import asyncio
import requests
import openai
import gspread
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from neobot_db import (
    upsert_lead, get_lead_by_session, get_lead_by_id,
    guardar_mensaje, obtener_historial,
    crear_cita, actualizar_cita, citas_del_asesor,
    actualizar_estado_lead, listar_leads, stats_crm,
    agregar_seguimiento, obtener_seguimientos,
    listar_asesores, crear_asesor, calcular_prioridad,
)
from neobot_calendar import (
    crear_evento_cita, cancelar_evento,
    proponer_slots, slots_disponibles, eventos_del_dia,
    verificar_disponibilidad,
)

# =============================
# CONFIG
# =============================
load_dotenv("NeoBot.env")

TOKKO_KEY      = os.getenv("TOKKO_KEY")
OPENAI_KEY     = os.getenv("OPENAI_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_NAME     = os.getenv("SHEET_NAME", "CLIENTES NEO")
CALENDAR_ID    = os.getenv("GOOGLE_CALENDAR_ID", "miguelro.blescr@gmail.com")
ASESOR_EMAIL   = os.getenv("ASESOR_EMAIL",        "miguelro.blescr@gmail.com")
WA_BRIDGE_URL  = os.getenv("WA_BRIDGE_URL",       "http://localhost:3001")
TOKKO_URL      = "https://www.tokkobroker.com/api/v1/property/"

openai_client = openai.OpenAI(api_key=OPENAI_KEY)
executor      = ThreadPoolExecutor()

# =============================
# LÍMITES
# =============================
MAX_TURNOS    = 6
MAX_COSTO_USD = 0.01
COSTO_TOKEN   = 0.00000015

# =============================
# ESTADO EN MEMORIA
# =============================
session_state: dict[str, dict] = {}

# =============================
# CACHE TOKKO
# =============================
CACHE_TTL       = 1800
_tokko_cache    = []
_tokko_cache_ts = 0.0

TIPO_MAP = {
    "House":"Casa","Apartment":"Departamento","Office":"Oficina",
    "Local":"Local","Warehouse":"Bodega","Land":"Terreno","Building":"Edificio","PH":"PH",
}
OPERACION_MAP = {"Sale":"Venta","Rent":"Renta","Temporary rent":"Renta temporal"}

# =============================
# INJECTION GUARD
# =============================
INJECTION_KW = [
    "ignore previous","ignore all","system prompt","jailbreak",
    "forget instructions","new instructions","bypass","ignora todo",
    "ignora las instrucciones","olvida","actúa como","actua como",
]
def es_inyeccion(msg: str) -> bool:
    return any(k in msg.lower() for k in INJECTION_KW)


# =============================
# GOOGLE SHEETS — formato exacto
# =============================
SHEETS_HEADER = [
    "NUMERO","NOMBRE DEL CLIENTE","TIPO DE OPERACIÓN","TIPO DE PROPIEDAD",
    "TIPO DE FINANCIAMIENTO","PLAZO DE BÚSQUEDA","FECHA DE SEGUIMIENTO",
    "NOTAS","PRIORIDAD"
]

def _get_google_creds(scopes):
    """
    Beta/Render: lee desde GOOGLE_CREDENTIALS_JSON (env var).
    Local: lee desde credentials.json.
    """
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw:
        info = json.loads(raw)
        return Credentials.from_service_account_info(info, scopes=scopes)
    return Credentials.from_service_account_file("credentials.json", scopes=scopes)


def guardar_en_sheets(lead: dict, event_data: dict = None):
    """
    Sincroniza un lead al Sheets.
    Si la fila del lead ya existe (busca por NUMERO), la actualiza.
    Si no, la agrega.
    """
    if not lead:
        return
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    try:
        creds = _get_google_creds(scopes)
        gc    = gspread.authorize(creds)
        ws    = gc.open(SHEET_NAME).sheet1

        # Crear cabecera si el sheet está vacío
        if ws.row_count < 1 or not ws.row_values(1):
            ws.append_row(SHEETS_HEADER)

        fila = [
            lead.get("numero",               ""),
            lead.get("nombre_cliente",        ""),
            lead.get("tipo_operacion",        ""),
            lead.get("tipo_propiedad",        ""),
            lead.get("tipo_financiamiento",   ""),
            lead.get("plazo_busqueda",        ""),
            lead.get("fecha_seguimiento",     datetime.now().strftime("%d/%m/%Y")),
            lead.get("notas",                 ""),
            lead.get("prioridad",             "BAJA"),
        ]

        # Buscar si ya existe la fila por NUMERO
        numero = lead.get("numero", "")
        if numero:
            celdas = ws.findall(numero)
            if celdas:
                row_num = celdas[0].row
                ws.update(f"A{row_num}:I{row_num}", [fila])
                print(f"✅ Sheets actualizado fila {row_num}")
                return

        ws.append_row(fila)
        print("✅ Sheets — nueva fila agregada")
    except Exception as e:
        print("❌ Sheets error:", e)


# =============================
# TOKKO
# =============================
def _fetch_tokko() -> list:
    todas, limit, offset = [], 100, 0
    while True:
        r = requests.get(TOKKO_URL, params={"key": TOKKO_KEY, "limit": limit, "offset": offset}, timeout=30)
        r.raise_for_status()
        data = r.json()
        todas.extend(data.get("objects", []))
        if offset + limit >= data.get("meta", {}).get("total_count", 0):
            break
        offset += limit
        time.sleep(0.3)
    return todas

def obtener_inventario() -> list:
    global _tokko_cache, _tokko_cache_ts
    if not _tokko_cache or time.time() - _tokko_cache_ts > CACHE_TTL:
        try:
            data = _fetch_tokko()
            if data:
                _tokko_cache    = data
                _tokko_cache_ts = time.time()
        except Exception as e:
            print("❌ Tokko:", e)
    return _tokko_cache

def normalizar(p: dict) -> dict:
    ops  = p.get("operations", [])
    op   = OPERACION_MAP.get(ops[0].get("operation_type",""), "") if ops else ""
    prec = ops[0]["prices"][0]["price"] if ops and ops[0].get("prices") else 0
    mon  = ops[0]["prices"][0].get("currency","MXN") if ops else "MXN"
    tipo = TIPO_MAP.get(p.get("type",{}).get("name",""), p.get("type",{}).get("name",""))
    loc  = p.get("location",{})
    return {"titulo":p.get("publication_title",""),"tipo":tipo,"operacion":op,
            "precio":prec,"moneda":mon,"zona":loc.get("name",""),"link":p.get("public_url","")}

def inventario_resumido(zona_hint="", tipo_hint="", limit=5) -> list[dict]:
    props = [normalizar(p) for p in obtener_inventario()]
    if zona_hint: props = [p for p in props if zona_hint.lower() in p["zona"].lower()]
    if tipo_hint:  props = [p for p in props if tipo_hint.lower() in p["tipo"].lower()]
    return props[:limit]


# =============================
# WHATSAPP — envío por bridge Node.js
# =============================
def wa_send(numero: str, mensaje: str) -> bool:
    """
    Envía mensaje via whatsapp-web.js bridge (Node.js).
    El número debe ser formato internacional sin +: 528341234567
    """
    try:
        r = requests.post(
            f"{WA_BRIDGE_URL}/send",
            json={"number": numero, "message": mensaje},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"❌ WA Bridge error: {e}")
        return False


# =============================
# CITA AUTOMÁTICA EN BACKGROUND
# =============================
async def procesar_cita_automatica(lead_id: int, ext: dict):
    lead     = get_lead_by_id(lead_id)
    if not lead: return

    asesores = listar_asesores()
    asesor   = next((a for a in asesores if a["id"] == lead.get("asesor_id")), asesores[0] if asesores else None)
    if not asesor: return

    cal_id = asesor.get("asesor_calendar") or CALENDAR_ID
    fecha  = ext.get("fecha_cita","")
    hora   = ext.get("hora_cita","")
    if not fecha or not hora: return

    if not verificar_disponibilidad(cal_id, fecha, hora):
        print(f"⚠️ Slot {fecha} {hora} ocupado")
        return

    resultado = crear_evento_cita(
        calendar_id    = cal_id,
        lead_nombre    = lead.get("nombre_cliente") or "Cliente",
        lead_email     = lead.get("email"),
        lead_telefono  = lead.get("numero") or ext.get("numero"),
        asesor_email   = asesor.get("asesor_email") or ASESOR_EMAIL,
        fecha          = fecha,
        hora           = hora,
        modalidad      = ext.get("modalidad_cita") or "presencial",
        zona_propiedad = lead.get("zona") or "",
        presupuesto    = lead.get("tipo_financiamiento") or "",
        notas          = lead.get("notas") or "",
    )

    cita_id = crear_cita(lead_id, {
        "asesor_id":        asesor["id"],
        "fecha":            fecha,
        "hora":             hora,
        "modalidad":        ext.get("modalidad_cita") or "presencial",
        "estado":           "CONFIRMADA" if resultado["event_id"] else "PENDIENTE",
        "google_event_id":  resultado["event_id"],
        "google_meet_link": resultado["meet_link"],
    })

    # Actualizar lead y sincronizar Sheets
    lead_actualizado = get_lead_by_id(lead_id)
    if lead_actualizado:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(executor, guardar_en_sheets, lead_actualizado, resultado)

    # Notificar al cliente por WhatsApp si tenemos número
    numero = lead.get("numero","").replace("+","").replace("-","").replace(" ","")
    if numero and resultado.get("meet_link"):
        msg = (
            f"✅ ¡Cita confirmada!\n"
            f"📅 {fecha} a las {hora}\n"
            f"🔗 Google Meet: {resultado['meet_link']}\n\n"
            f"Un asesor de NeoLife te atenderá. ¡Hasta pronto!"
        )
        wa_send(numero, msg)

    print(f"✅ Cita #{cita_id} — Evento {resultado.get('event_id','N/A')}")


# =============================
# MENSAJE CORE
# =============================
async def procesar_mensaje(
    session_id: str,
    user_msg:   str,
    user_name:  str,
    numero:     str = "",
    canal:      str = "whatsapp",
) -> dict:

    if len(user_msg.strip()) < 3:
        return {"reply": "¿Me das un poco más de info para ayudarte mejor? 🙂", "extraction": {}}
    if es_inyeccion(user_msg):
        return {"reply": "Solo puedo ayudarte con temas inmobiliarios. 😊", "extraction": {}}

    state = session_state.get(session_id, {
        "turnos":0, "costo":0.0, "fase":"NORMAL",
        "intento_cita":False, "score":0, "lead_id":None,
    })

    # Upsert lead
    lead_id = state.get("lead_id")
    if not lead_id:
        lead_id = upsert_lead(session_id, {
            "canal": canal,
            "numero": numero or None,
            "nombre_cliente": user_name if user_name != "Cliente" else None,
        })
        state["lead_id"] = lead_id
    else:
        if numero or user_name != "Cliente":
            upsert_lead(session_id, {
                "numero": numero or None,
                "nombre_cliente": user_name if user_name != "Cliente" else None,
            })

    # Historial desde DB
    historial_db = obtener_historial(lead_id, limit=20)
    history      = [{"role": h["rol"], "content": h["mensaje"]} for h in historial_db]
    history.append({"role": "user", "content": user_msg})
    guardar_mensaje(lead_id, "user", user_msg, canal)

    # Señales de intención
    señales = any(k in user_msg.lower() for k in [
        "busco","quiero","necesito","presupuesto","millones","pesos",
        "zona","colonia","comprar","rentar","arrendar","casa","depa",
        "departamento","oficina","terreno","recámaras","recamaras","m2",
        "contado","crédito","credito","infonavit","fovissste",
    ])

    # Fases
    if (state["turnos"] >= MAX_TURNOS - 1 or state["costo"] >= MAX_COSTO_USD * 0.8
            or (state["score"] >= 2 and señales)):
        if state["fase"] == "NORMAL":
            state["fase"] = "CIERRE"

    if state["fase"] == "CIERRE" and not state["intento_cita"]:
        state["fase"]         = "CITA"
        state["intento_cita"] = True

    if state["fase"] == "HUMANO":
        return {"reply": "Perfecto 🙌 Un asesor de NeoLife te contactará enseguida.", "extraction": {}}

    # Slots disponibles del calendario
    slots_texto = ""
    if state["fase"] == "CITA":
        try:
            lead     = get_lead_by_id(lead_id)
            asesores = listar_asesores()
            asesor   = next((a for a in asesores if a["id"] == (lead or {}).get("asesor_id")), asesores[0] if asesores else None)
            cal_id   = (asesor.get("asesor_calendar") or CALENDAR_ID) if asesor else CALENDAR_ID
            opciones = proponer_slots(cal_id, dias_adelante=3, n_opciones=2)
            if opciones:
                slots_texto = "\n\nSLOTS LIBRES EN AGENDA DEL ASESOR (usa exactamente estos):\n"
                for s in opciones:
                    slots_texto += f"- {s['fecha']} a las {s['hora']}\n"
        except Exception as e:
            print(f"⚠️ Slots: {e}")

    # Propiedades
    ctx_props = ""
    if state["fase"] == "NORMAL":
        props = inventario_resumido(limit=4)
        if props:
            ctx_props = "\n\nPROPIEDADES DISPONIBLES:\n"
            for p in props:
                ctx_props += f"- {p['tipo']} en {p['zona']}: {p['moneda']} {p['precio']:,} ({p['operacion']}) → {p['link']}\n"

    system_prompt = f"""
Eres Neo, asesor inmobiliario de NeoLife.
Cliente: {user_name} | Canal: {canal.upper()}

FASE: {state["fase"]}

OBJETIVO POR FASE:
- NORMAL: orientar, escuchar necesidades, mencionar propiedades si aplica.
- CIERRE: confirmar necesidad, solicitar número de teléfono si no lo tenemos.
- CITA: proponer exactamente los slots indicados abajo. Preguntar modalidad: presencial | videollamada | telefónica.
- Al confirmar cita → extraer fecha_cita, hora_cita, modalidad_cita.

Extracción obligatoria (rellena todo lo que el cliente mencione):
- tipo_operacion: RENTA o VENTA
- tipo_propiedad: CASA | DEPARTAMENTO | OFICINA | LOCAL | TERRENO | BODEGA
- tipo_financiamiento: CONTADO | CRÉDITO BANCARIO | INFONAVIT | FOVISSSTE | OTRO
- plazo_busqueda: "2 meses", "inmediato", etc.

Reglas: Sé breve y cálido. No presiones. No menciones propiedades en CIERRE/CITA.
{ctx_props}{slots_texto}

Responde SOLO en JSON (sin texto fuera):
{{
  "reply": "...",
  "extraction": {{
    "nombre_cliente":      null,
    "numero":              null,
    "tipo_operacion":      null,
    "tipo_propiedad":      null,
    "tipo_financiamiento": null,
    "plazo_busqueda":      null,
    "notas":               null,
    "fecha_cita":          null,
    "hora_cita":           null,
    "modalidad_cita":      null
  }}
}}
"""

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":system_prompt}] + history[-10:],
            response_format={"type":"json_object"},
            timeout=15,
        )
    except openai.APITimeoutError:
        return {"reply":"Disculpa, problema de conexión. ¿Puedes repetir? 🙏","extraction":{}}
    except openai.APIError as e:
        print("❌ OpenAI:", e)
        return {"reply":"Error inesperado. Intenta de nuevo.","extraction":{}}

    contenido = json.loads(resp.choices[0].message.content)
    guardar_mensaje(lead_id, "assistant", contenido["reply"], canal)

    ext = contenido.get("extraction", {})

    # Completar nombre y número si no llegaron en la extracción
    if not ext.get("nombre_cliente") and user_name != "Cliente":
        ext["nombre_cliente"] = user_name
    if not ext.get("numero") and numero:
        ext["numero"] = numero

    # Scoring
    if ext.get("numero"):              state["score"] += 2
    if ext.get("fecha_cita"):          state["score"] += 2
    if ext.get("tipo_financiamiento"): state["score"] += 1
    if ext.get("plazo_busqueda"):      state["score"] += 1
    if ext.get("tipo_operacion"):      state["score"] += 1

    nivel      = "CALIENTE" if state["score"] >= 4 else "TIBIO" if state["score"] >= 2 else "FRIO"
    prioridad  = calcular_prioridad(ext.get("plazo_busqueda",""), nivel)

    ext["lead_score"] = state["score"]
    ext["lead_nivel"] = nivel
    ext["prioridad"]  = prioridad

    # Mapeo a columnas DB/Sheets
    datos_update = {
        "nombre_cliente":      ext.get("nombre_cliente"),
        "numero":              ext.get("numero"),
        "tipo_operacion":      ext.get("tipo_operacion"),
        "tipo_propiedad":      ext.get("tipo_propiedad"),
        "tipo_financiamiento": ext.get("tipo_financiamiento"),
        "plazo_busqueda":      ext.get("plazo_busqueda"),
        "notas":               ext.get("notas"),
        "prioridad":           prioridad,
        "lead_score":          state["score"],
        "lead_nivel":          nivel,
    }
    datos_update = {k: v for k, v in datos_update.items() if v is not None}
    upsert_lead(session_id, datos_update)

    # Cita detectada → crear en Calendar + Sheets (background)
    if ext.get("fecha_cita") and ext.get("hora_cita"):
        asyncio.create_task(procesar_cita_automatica(lead_id, ext))
        state["fase"] = "HUMANO"
    elif ext.get("numero"):
        actualizar_estado_lead(lead_id, "CONTACTADO")
        state["fase"] = "HUMANO"
        # Sync Sheets al obtener número
        lead_upd = get_lead_by_id(lead_id)
        if lead_upd:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(executor, guardar_en_sheets, lead_upd, None)

    # Contadores
    tokens = resp.usage.total_tokens if resp.usage else 0
    state["turnos"] += 1
    state["costo"]  += tokens * COSTO_TOKEN
    session_state[session_id] = state

    contenido["extraction"] = ext
    contenido["lead_id"]    = lead_id
    return contenido


# =============================
# TELEGRAM HANDLER
# =============================
async def telegram_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    result = await procesar_mensaje(
        session_id = f"tg_{user.id}",
        user_msg   = update.message.text,
        user_name  = user.first_name or "Cliente",
        numero     = "",
        canal      = "telegram",
    )
    await update.message.reply_text(result["reply"])


# =============================
# LIFESPAN
# =============================
telegram_app = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    if TELEGRAM_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_handler))
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling()
        print("🤖 Telegram activo")
    yield
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        print("🛑 Telegram detenido")


# =============================
# FASTAPI
# =============================
app = FastAPI(lifespan=lifespan, title="NeoLife CRM API v2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Dashboard CRM
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Sirve el CRM dashboard (crm_dashboard.html)."""
    html_path = os.path.join(os.path.dirname(__file__), "crm_dashboard.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        raise HTTPException(404, "crm_dashboard.html no encontrado — asegúrate de que el archivo esté en el mismo directorio que neobot_main.py")


# ── WhatsApp webhook (recibe mensajes del bridge Node.js)
@app.post("/whatsapp/incoming")
async def wa_incoming(req: Request):
    """
    El bridge Node.js (whatsapp-web.js) hace POST aquí
    cuando llega un mensaje de WhatsApp.
    Body: {"from": "528341234567@c.us", "body": "Hola", "name": "Marco"}
    """
    data    = await req.json()
    wa_from = data.get("from","")                          # "528341234567@c.us"
    numero  = wa_from.replace("@c.us","").replace("@s.whatsapp.net","")
    mensaje = data.get("body","").strip()
    nombre  = data.get("name","Cliente")

    if not mensaje:
        return {"ok": True}

    result = await procesar_mensaje(
        session_id = f"wa_{numero}",
        user_msg   = mensaje,
        user_name  = nombre,
        numero     = numero,
        canal      = "whatsapp",
    )

    # Responder al cliente por WhatsApp
    wa_send(numero, result["reply"])
    return {"ok": True, "reply": result["reply"]}


# ── Web/widget webhook
@app.post("/webhook")
async def webhook(req: Request):
    data   = await req.json()
    result = await procesar_mensaje(
        session_id = data.get("session_id","default"),
        user_msg   = data.get("message",""),
        user_name  = data.get("name","Cliente"),
        numero     = data.get("numero",""),
        canal      = data.get("canal","web"),
    )
    return result


# ── CRM endpoints
@app.get("/crm/leads")
async def api_leads(estado=None, asesor_id=None, nivel=None, prioridad=None, limit:int=50, offset:int=0):
    return listar_leads(estado=estado, asesor_id=asesor_id, nivel=nivel, prioridad=prioridad, limit=limit, offset=offset)

@app.get("/crm/leads/{lead_id}")
async def api_lead_detalle(lead_id: int):
    lead = get_lead_by_id(lead_id)
    if not lead: raise HTTPException(404, "Lead no encontrado")
    return {
        "lead":         lead,
        "historial":    obtener_historial(lead_id),
        "seguimientos": obtener_seguimientos(lead_id),
    }

@app.patch("/crm/leads/{lead_id}/estado")
async def api_estado(lead_id: int, req: Request):
    body = await req.json()
    estado = body.get("estado")
    if not estado: raise HTTPException(400, "Campo 'estado' requerido")
    actualizar_estado_lead(lead_id, estado)
    # Sync Sheets
    lead = get_lead_by_id(lead_id)
    if lead:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(executor, guardar_en_sheets, lead, None)
    return {"ok": True}

@app.post("/crm/leads/{lead_id}/seguimiento")
async def api_seguimiento(lead_id: int, req: Request):
    body = await req.json()
    agregar_seguimiento(lead_id, body.get("asesor_id",1), body.get("tipo","nota"), body.get("descripcion",""))
    return {"ok": True}

@app.get("/crm/stats")
async def api_stats():
    return stats_crm()

@app.get("/openai/usage")
async def api_openai_usage():
    """
    Consulta el uso y créditos disponibles de la cuenta OpenAI.
    Usa la API de billing de OpenAI (requiere OPENAI_KEY con permisos de billing).
    """
    import httpx
    from datetime import date

    if not OPENAI_KEY:
        raise HTTPException(503, "OPENAI_KEY no configurada")

    headers = {"Authorization": f"Bearer {OPENAI_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Suscripción / límite
            sub_r = await client.get(
                "https://api.openai.com/dashboard/billing/subscription",
                headers=headers,
            )
            # Uso del mes actual
            today      = date.today()
            start_date = today.replace(day=1).isoformat()
            end_date   = today.isoformat()
            uso_r = await client.get(
                "https://api.openai.com/dashboard/billing/usage",
                headers=headers,
                params={"start_date": start_date, "end_date": end_date},
            )

        if sub_r.status_code != 200:
            # Puede ocurrir en cuentas API puras (sin billing dashboard)
            return {
                "error": "No se pudo obtener info de billing",
                "detalle": sub_r.text,
                "status": sub_r.status_code,
                "hint": "Las cuentas API puras (sin tarjeta) no exponen el endpoint de billing. Verifica en platform.openai.com/usage",
            }

        sub  = sub_r.json()
        uso  = uso_r.json() if uso_r.status_code == 200 else {}

        limite_total  = sub.get("hard_limit_usd", sub.get("system_hard_limit_usd", 0))
        usado_mes     = round(uso.get("total_usage", 0) / 100, 4)   # viene en centavos
        disponible    = round(max(limite_total - usado_mes, 0), 4)
        porcentaje    = round((usado_mes / limite_total * 100), 1) if limite_total else 0

        return {
            "plan":            sub.get("plan", {}).get("title", "N/A"),
            "limite_usd":      limite_total,
            "usado_mes_usd":   usado_mes,
            "disponible_usd":  disponible,
            "porcentaje_uso":  porcentaje,
            "periodo":         f"{start_date} → {end_date}",
            "acceso_activo":   sub.get("access_until", ""),
        }

    except Exception as e:
        raise HTTPException(500, f"Error consultando OpenAI: {str(e)}")

@app.get("/crm/asesores")
async def api_asesores():
    return listar_asesores()

@app.post("/crm/asesores")
async def api_crear_asesor(req: Request):
    body = await req.json()
    aid  = crear_asesor(body["nombre"], body["email"], body.get("telefono",""), body.get("calendar_id","primary"))
    return {"id": aid}

@app.get("/calendar/slots")
async def api_slots(fecha: str = None, asesor_id: int = None):
    asesores = listar_asesores()
    asesor   = next((a for a in asesores if a["id"] == asesor_id), asesores[0] if asesores else None)
    cal_id   = (asesor.get("calendar_id") or CALENDAR_ID) if asesor else CALENDAR_ID
    return {"slots": slots_disponibles(cal_id, fecha) if fecha else proponer_slots(cal_id, n_opciones=4)}

@app.get("/calendar/agenda")
async def api_agenda(fecha: str = None, asesor_id: int = None):
    asesores = listar_asesores()
    asesor   = next((a for a in asesores if a["id"] == asesor_id), asesores[0] if asesores else None)
    cal_id   = (asesor.get("calendar_id") or CALENDAR_ID) if asesor else CALENDAR_ID
    return {"eventos": eventos_del_dia(cal_id, fecha)}

@app.delete("/calendar/citas/{cita_id}")
async def api_cancelar_cita(cita_id: int):
    from neobot_db import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM citas WHERE id=?", (cita_id,)).fetchone()
    if not row: raise HTTPException(404)
    cita = dict(row)
    if cita.get("google_event_id"):
        asesores = listar_asesores()
        asesor   = next((a for a in asesores if a["id"] == cita.get("asesor_id")), None)
        cal_id   = (asesor.get("calendar_id") or CALENDAR_ID) if asesor else CALENDAR_ID
        cancelar_evento(cal_id, cita["google_event_id"])
    actualizar_cita(cita_id, {"estado":"CANCELADA"})
    return {"ok": True}


# =============================
# RUN
# =============================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
