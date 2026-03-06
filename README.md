# 🏠 NeoLife Bot — CRM Inmobiliario

Bot de WhatsApp con IA para captura y seguimiento de leads inmobiliarios.  
Integra Google Calendar, Google Sheets y Tokko Broker.

## Stack

| Componente | Tecnología |
|---|---|
| Bot IA | Python + FastAPI + GPT-4o-mini |
| WhatsApp | Node.js + whatsapp-web.js |
| Sesión WA | Redis |
| CRM | SQLite + Google Sheets |
| Calendar | Google Calendar API |
| Inventario | Tokko Broker API |
| Deploy | Render (beta gratis) |

---

## 🚀 Deploy en Render (5 pasos)

### 1. Fork o sube este repo a GitHub

### 2. Conecta en Render

- Ve a [render.com](https://render.com) → **New** → **Blueprint**
- Conecta tu repo de GitHub
- Render detecta el `render.yaml` y crea automáticamente:
  - `neolife-api` — Python API
  - `neolife-wa` — WhatsApp Bridge
  - `neolife-redis` — Redis (gratis)

### 3. Configura las variables de entorno

En Render → **neolife-api** → Environment:

| Variable | Valor |
|---|---|
| `TOKKO_KEY` | Tu clave de Tokko Broker |
| `OPENAI_KEY` | `sk-proj-...` |
| `TELEGRAM_BOT_TOKEN` | Token de BotFather (opcional) |
| `GOOGLE_CALENDAR_ID` | `miguelro.blescr@gmail.com` |
| `ASESOR_EMAIL` | `miguelro.blescr@gmail.com` |
| `SHEET_NAME` | `CLIENTES NEO` |
| `GOOGLE_CREDENTIALS_JSON` | *(contenido completo de credentials.json)* |

En Render → **neolife-wa** → Environment:

| Variable | Valor |
|---|---|
| `REDIS_URL` | *(auto — viene del servicio neolife-redis)* |
| `PYTHON_API` | *(auto — viene del servicio neolife-api)* |

### 4. Escanear QR de WhatsApp (solo una vez)

Una vez deployado:

1. Ve a `https://neolife-wa.onrender.com/status`
2. Si ves `"connected": false` → hay un QR pendiente en el campo `"qr"`
3. Copia el texto del QR y genéralo en [qr-code-generator.com](https://www.qr-code-generator.com)
4. Escanéalo con WhatsApp → **Dispositivos vinculados**
5. La sesión se guarda en Redis — no necesitas repetir esto

### 5. Configura auto-deploy desde GitHub

En Render → cada servicio → **Settings** → **Deploy Hook** → copia la URL.

En GitHub → tu repo → **Settings** → **Secrets** → **Actions**:
- `RENDER_DEPLOY_HOOK_API` → URL del hook de neolife-api
- `RENDER_DEPLOY_HOOK_WA` → URL del hook de neolife-wa

Ahora cada `git push` a `main` hace deploy automático. ✅

---

## 💻 Correr localmente

```bash
# 1. Clonar
git clone https://github.com/tu-usuario/neolife-bot.git
cd neolife-bot

# 2. Configurar secrets
cp NeoBot.env.example NeoBot.env
# edita NeoBot.env con tus claves reales
# copia tu credentials.json aquí

# 3. Python
python3 -m venv venv
source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn neobot_main:app --reload --port 8000

# 4. WhatsApp bridge (en otra terminal)
npm install
node whatsapp_bridge.js
# → escanea el QR
```

---

## 📡 URLs

| URL | Descripción |
|---|---|
| `GET  /` | Dashboard CRM |
| `GET  /crm/stats` | Estadísticas generales |
| `GET  /crm/leads` | Lista de leads |
| `GET  /crm/leads/{id}` | Detalle + historial |
| `PATCH /crm/leads/{id}/estado` | Cambiar estado |
| `POST /crm/leads/{id}/seguimiento` | Agregar nota |
| `GET  /calendar/agenda` | Agenda del día |
| `GET  /calendar/slots` | Slots disponibles |
| `POST /webhook` | Chat web/widget |
| `POST /whatsapp/incoming` | Webhook WhatsApp bridge |

---

## 📋 Estructura del repo

```
neolife-bot/
├── neobot_main.py          # API principal + lógica del bot
├── neobot_db.py            # Base de datos SQLite + CRM
├── neobot_calendar.py      # Google Calendar integration
├── crm_dashboard.html      # Dashboard visual
├── whatsapp_bridge.js      # Bridge Node.js WhatsApp
├── requirements.txt        # Deps Python
├── package.json            # Deps Node.js
├── Dockerfile              # Container Python API
├── Dockerfile.whatsapp     # Container WhatsApp bridge
├── render.yaml             # Infrastructure as code (Render)
├── NeoBot.env.example      # Template de variables de entorno
└── .github/
    └── workflows/
        └── deploy.yml      # Auto-deploy en push a main
```

---

## ⚠️ Notas importantes

- **Beta / testing**: SQLite usa `/tmp` → se resetea en cada deploy. Los datos reales persisten en Google Sheets.
- **Producción**: cambia `plan: free` → `plan: starter` en `render.yaml` y agrega un Disk para SQLite.
- **credentials.json**: nunca lo subas a GitHub. Usa la variable `GOOGLE_CREDENTIALS_JSON` en Render.
