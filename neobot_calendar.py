"""
neobot_calendar.py — Google Calendar Integration para NeoLife Bot

Requiere en credentials.json los scopes de Calendar además de Sheets.
O usa una service account con acceso delegado al calendario del asesor.

Variables de entorno adicionales en NeoBot.env:
  GOOGLE_CALENDAR_ID=primary   (o el ID del calendario del asesor)
  ASESOR_EMAIL=asesor@neolife.mx
"""

import os
import json
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

TIMEZONE    = "America/Mexico_City"
CREDENTIALS = "credentials.json"
SCOPES      = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ─────────────────────────────────────────────
# CLIENT
# ─────────────────────────────────────────────
def _get_google_creds():
    """Lee credenciales desde env var (Render) o archivo local."""
    import json as _json
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw:
        info = _json.loads(raw)
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    return Credentials.from_service_account_file(CREDENTIALS, scopes=SCOPES)


def _get_calendar_service():
    return build("calendar", "v3", credentials=_get_google_creds(), cache_discovery=False)


# ─────────────────────────────────────────────
# 1. VERIFICAR DISPONIBILIDAD
# ─────────────────────────────────────────────
def verificar_disponibilidad(
    calendar_id: str,
    fecha: str,          # "2025-08-15"
    hora_inicio: str,    # "10:00"
    duracion_min: int = 60,
) -> bool:
    """
    Retorna True si el slot está libre en el calendario del asesor.
    """
    try:
        service = _get_calendar_service()
        tz      = ZoneInfo(TIMEZONE)

        dt_inicio = datetime.strptime(f"{fecha} {hora_inicio}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        dt_fin    = dt_inicio + timedelta(minutes=duracion_min)

        body = {
            "timeMin": dt_inicio.isoformat(),
            "timeMax": dt_fin.isoformat(),
            "timeZone": TIMEZONE,
            "items": [{"id": calendar_id}],
        }
        result = service.freebusy().query(body=body).execute()
        busy   = result["calendars"].get(calendar_id, {}).get("busy", [])
        return len(busy) == 0

    except HttpError as e:
        print(f"❌ Calendar API error (disponibilidad): {e}")
        return True   # Asumir disponible si falla la consulta


def slots_disponibles(
    calendar_id: str,
    fecha: str,
    horas_candidatas: list[str] = None,
    duracion_min: int = 60,
) -> list[str]:
    """
    Retorna lista de horas disponibles para una fecha dada.
    """
    if horas_candidatas is None:
        horas_candidatas = [
            "09:00", "10:00", "11:00", "12:00",
            "14:00", "15:00", "16:00", "17:00", "18:00"
        ]
    disponibles = []
    for hora in horas_candidatas:
        if verificar_disponibilidad(calendar_id, fecha, hora, duracion_min):
            disponibles.append(hora)
    return disponibles


def proponer_slots(
    calendar_id: str,
    dias_adelante: int = 3,
    horas_candidatas: list[str] = None,
    n_opciones: int = 2,
) -> list[dict]:
    """
    Busca los próximos N slots libres empezando desde mañana.
    Retorna lista de dicts: [{"fecha": "2025-08-15", "hora": "10:00"}, ...]
    """
    opciones  = []
    hoy       = datetime.now(ZoneInfo(TIMEZONE)).date()

    for delta in range(1, dias_adelante + 7):   # hasta 10 días adelante
        if len(opciones) >= n_opciones:
            break
        dia  = hoy + timedelta(days=delta)
        # Saltar domingos
        if dia.weekday() == 6:
            continue
        fecha_str = dia.strftime("%Y-%m-%d")
        slots     = slots_disponibles(calendar_id, fecha_str, horas_candidatas)
        for hora in slots:
            if len(opciones) >= n_opciones:
                break
            opciones.append({"fecha": fecha_str, "hora": hora})

    return opciones


# ─────────────────────────────────────────────
# 2. CREAR EVENTO + INVITACIÓN AL CLIENTE
# ─────────────────────────────────────────────
def crear_evento_cita(
    calendar_id: str,
    lead_nombre: str,
    lead_email: Optional[str],
    lead_telefono: Optional[str],
    asesor_email: str,
    fecha: str,        # "2025-08-15"
    hora: str,         # "10:00"
    modalidad: str,    # presencial | videollamada | telefonica
    zona_propiedad: str = "",
    presupuesto: str = "",
    duracion_min: int = 60,
    notas: str = "",
) -> dict:
    """
    Crea evento en Google Calendar con invitación al cliente y asesor.
    Retorna {"event_id": ..., "meet_link": ..., "html_link": ...}
    """
    try:
        service = _get_calendar_service()
        tz      = ZoneInfo(TIMEZONE)

        dt_inicio = datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        dt_fin    = dt_inicio + timedelta(minutes=duracion_min)

        # Descripción con contexto del lead
        descripcion = f"""
🏠 Asesoría Inmobiliaria — NeoLife

Cliente:     {lead_nombre}
Teléfono:    {lead_telefono or 'N/A'}
Zona:        {zona_propiedad or 'Por definir'}
Presupuesto: {presupuesto or 'Por definir'}
Modalidad:   {modalidad}

{f'Notas: {notas}' if notas else ''}

---
Generado automáticamente por NeoBot
        """.strip()

        # Modalidad → conferencia o ubicación
        ubicacion = ""
        conference_data = None

        if modalidad == "videollamada":
            conference_data = {
                "createRequest": {
                    "requestId": f"neolife-{fecha}-{hora}-{lead_nombre[:5]}".replace(" ", ""),
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
        elif modalidad == "presencial":
            ubicacion = "Oficinas NeoLife — Confirmar dirección exacta"
        elif modalidad == "telefonica":
            ubicacion = f"Llamada al {lead_telefono or 'teléfono del cliente'}"

        # Asistentes
        attendees = [{"email": asesor_email, "displayName": "Asesor NeoLife"}]
        if lead_email:
            attendees.append({"email": lead_email, "displayName": lead_nombre})

        # Recordatorios
        reminders = {
            "useDefault": False,
            "overrides": [
                {"method": "email",  "minutes": 24 * 60},   # 1 día antes
                {"method": "popup",  "minutes": 30},         # 30 min antes
            ],
        }
        if lead_email:
            # Recordatorio extra por email al cliente
            reminders["overrides"].append({"method": "email", "minutes": 60})

        event_body = {
            "summary":     f"🏠 Cita: {lead_nombre} — NeoLife",
            "description": descripcion,
            "location":    ubicacion,
            "start":       {"dateTime": dt_inicio.isoformat(), "timeZone": TIMEZONE},
            "end":         {"dateTime": dt_fin.isoformat(),   "timeZone": TIMEZONE},
            "attendees":   attendees,
            "reminders":   reminders,
            "colorId":     "2",   # verde
        }

        kwargs = {"calendarId": calendar_id, "body": event_body, "sendUpdates": "all"}
        if conference_data:
            kwargs["conferenceDataVersion"] = 1
            event_body["conferenceData"]    = conference_data

        evento = service.events().insert(**kwargs).execute()

        meet_link  = ""
        entry_pts  = evento.get("conferenceData", {}).get("entryPoints", [])
        for ep in entry_pts:
            if ep.get("entryPointType") == "video":
                meet_link = ep.get("uri", "")
                break

        print(f"✅ Evento creado: {evento['id']} | Meet: {meet_link or 'N/A'}")
        return {
            "event_id":  evento["id"],
            "meet_link": meet_link,
            "html_link": evento.get("htmlLink", ""),
        }

    except HttpError as e:
        print(f"❌ Calendar API error (crear evento): {e}")
        return {"event_id": None, "meet_link": "", "html_link": ""}


# ─────────────────────────────────────────────
# 3. CANCELAR / ACTUALIZAR EVENTO
# ─────────────────────────────────────────────
def cancelar_evento(calendar_id: str, event_id: str) -> bool:
    try:
        service = _get_calendar_service()
        service.events().delete(calendarId=calendar_id, eventId=event_id, sendUpdates="all").execute()
        print(f"✅ Evento {event_id} cancelado")
        return True
    except HttpError as e:
        print(f"❌ Error al cancelar evento: {e}")
        return False


def actualizar_evento(calendar_id: str, event_id: str, cambios: dict) -> bool:
    """
    cambios puede incluir: {"summary": ..., "description": ..., "start": ..., "end": ...}
    """
    try:
        service = _get_calendar_service()
        evento  = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        evento.update(cambios)
        service.events().update(
            calendarId=calendar_id,
            eventId=event_id,
            body=evento,
            sendUpdates="all"
        ).execute()
        print(f"✅ Evento {event_id} actualizado")
        return True
    except HttpError as e:
        print(f"❌ Error al actualizar evento: {e}")
        return False


# ─────────────────────────────────────────────
# 4. LISTAR EVENTOS DEL DÍA
# ─────────────────────────────────────────────
def eventos_del_dia(calendar_id: str, fecha: str = None) -> list[dict]:
    """
    Retorna los eventos del calendario para una fecha (default: hoy).
    """
    try:
        service = _get_calendar_service()
        tz      = ZoneInfo(TIMEZONE)

        if fecha:
            dia = datetime.strptime(fecha, "%Y-%m-%d").replace(tzinfo=tz)
        else:
            dia = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)

        time_min = dia.isoformat()
        time_max = (dia + timedelta(days=1)).isoformat()

        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        eventos = []
        for ev in result.get("items", []):
            start = ev.get("start", {})
            eventos.append({
                "id":       ev["id"],
                "titulo":   ev.get("summary", "Sin título"),
                "inicio":   start.get("dateTime", start.get("date", "")),
                "asistentes": [a["email"] for a in ev.get("attendees", [])],
                "meet":     ev.get("hangoutLink", ""),
            })
        return eventos

    except HttpError as e:
        print(f"❌ Error al listar eventos: {e}")
        return []
