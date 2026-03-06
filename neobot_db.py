"""
neobot_db.py — CRM Database Layer NeoLife Bot v2
Columnas alineadas con Google Sheets:
  NUMERO | NOMBRE DEL CLIENTE | TIPO DE OPERACIÓN | TIPO DE PROPIEDAD |
  TIPO DE FINANCIAMIENTO | PLAZO DE BÚSQUEDA | FECHA DE SEGUIMIENTO |
  NOTAS | PRIORIDAD

FECHA DE SEGUIMIENTO = última vez que se interactuó con el cliente (auto-update)
PRIORIDAD            = calculada automáticamente por plazo + nivel del lead
"""

import os
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "neolife_crm.db")


# ─────────────────────────────────────────
# PRIORIDAD AUTOMÁTICA
# ─────────────────────────────────────────
def calcular_prioridad(plazo: str, lead_nivel: str) -> str:
    """
    ALTA  → CALIENTE, o plazo ≤ 1 mes
    MEDIA → TIBIO, o plazo 2-3 meses
    BAJA  → FRIO o plazo largo / sin datos
    """
    plazo_lower = (plazo or "").lower()
    nivel       = (lead_nivel or "FRIO").upper()
    corto = any(p in plazo_lower for p in ["1 mes","inmediato","urgente","ya","ahora","semana","15 día","15 dia"])
    medio = any(p in plazo_lower for p in ["2 mes","3 mes","dos mes","tres mes"])

    if nivel == "CALIENTE":        return "ALTA"
    if corto:                      return "ALTA"
    if nivel == "TIBIO" or medio:  return "MEDIA"
    return "BAJA"


# ─────────────────────────────────────────
# CONEXIÓN
# ─────────────────────────────────────────
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────
# INIT — tablas
# ─────────────────────────────────────────
def init_db():
    with get_conn() as conn:
        conn.executescript("""

        CREATE TABLE IF NOT EXISTS asesores (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre      TEXT NOT NULL,
            email       TEXT UNIQUE NOT NULL,
            telefono    TEXT,
            calendar_id TEXT DEFAULT 'primary',
            activo      INTEGER DEFAULT 1,
            creado_en   TEXT DEFAULT (datetime('now'))
        );

        -- Tabla principal alineada con el Sheets
        CREATE TABLE IF NOT EXISTS leads (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id           TEXT UNIQUE NOT NULL,

            -- ── Columnas exactas del Google Sheets ──────────
            numero               TEXT,        -- NUMERO (teléfono/contacto)
            nombre_cliente       TEXT,        -- NOMBRE DEL CLIENTE
            tipo_operacion       TEXT,        -- TIPO DE OPERACIÓN: RENTA | VENTA
            tipo_propiedad       TEXT,        -- TIPO DE PROPIEDAD: CASA | DEPA...
            tipo_financiamiento  TEXT,        -- TIPO DE FINANCIAMIENTO: CONTADO | CRÉDITO...
            plazo_busqueda       TEXT,        -- PLAZO DE BÚSQUEDA: "2 meses"
            fecha_seguimiento    TEXT,        -- FECHA DE SEGUIMIENTO (último contacto, auto)
            notas                TEXT,        -- NOTAS: resumen de necesidad
            prioridad            TEXT DEFAULT 'BAJA',  -- ALTA | MEDIA | BAJA (auto)

            -- ── Campos internos CRM ──────────────────────────
            email                TEXT,
            canal                TEXT DEFAULT 'whatsapp',
            estado               TEXT DEFAULT 'NUEVO',
            lead_score           INTEGER DEFAULT 0,
            lead_nivel           TEXT DEFAULT 'FRIO',
            asesor_id            INTEGER REFERENCES asesores(id),
            creado_en            TEXT DEFAULT (datetime('now')),
            actualizado_en       TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS conversaciones (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id   INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            rol       TEXT NOT NULL,
            mensaje   TEXT NOT NULL,
            canal     TEXT DEFAULT 'whatsapp',
            creado_en TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS citas (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id          INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            asesor_id        INTEGER REFERENCES asesores(id),
            fecha            TEXT,
            hora             TEXT,
            modalidad        TEXT,
            estado           TEXT DEFAULT 'PENDIENTE',
            google_event_id  TEXT,
            google_meet_link TEXT,
            notas            TEXT,
            creado_en        TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS seguimientos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id     INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            asesor_id   INTEGER REFERENCES asesores(id),
            tipo        TEXT,
            descripcion TEXT,
            creado_en   TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_leads_session   ON leads(session_id);
        CREATE INDEX IF NOT EXISTS idx_leads_estado    ON leads(estado);
        CREATE INDEX IF NOT EXISTS idx_leads_prioridad ON leads(prioridad);
        CREATE INDEX IF NOT EXISTS idx_conv_lead       ON conversaciones(lead_id);
        CREATE INDEX IF NOT EXISTS idx_citas_fecha     ON citas(fecha);
        """)
        print("✅ DB inicializada")
    _seed_asesor_default()


def _seed_asesor_default():
    with get_conn() as conn:
        existe = conn.execute("SELECT id FROM asesores WHERE email='asesor@neolife.mx'").fetchone()
        if not existe:
            conn.execute("""
                INSERT INTO asesores (nombre, email, telefono, calendar_id)
                VALUES ('Asesor NeoLife','asesor@neolife.mx','','miguelro.blescr@gmail.com')
            """)
            print("✅ Asesor por defecto creado (calendar: miguelro.blescr@gmail.com)")


# ─────────────────────────────────────────
# LEADS — CRUD
# ─────────────────────────────────────────
def _hoy() -> str:
    return datetime.now().strftime("%d/%m/%Y")


def upsert_lead(session_id: str, datos: dict) -> int:
    """Crea o actualiza lead. Siempre actualiza fecha_seguimiento."""
    now = datetime.now().isoformat()

    # Recalcular prioridad con lo que tengamos
    with get_conn() as conn:
        row_prev = conn.execute(
            "SELECT plazo_busqueda, lead_nivel FROM leads WHERE session_id=?", (session_id,)
        ).fetchone()

    plazo = datos.get("plazo_busqueda") or (row_prev["plazo_busqueda"] if row_prev else None)
    nivel = datos.get("lead_nivel")     or (row_prev["lead_nivel"]     if row_prev else "FRIO")
    if plazo or nivel:
        datos["prioridad"] = calcular_prioridad(plazo or "", nivel or "FRIO")

    # Siempre actualizar fecha de seguimiento
    datos["fecha_seguimiento"] = _hoy()
    datos["actualizado_en"]    = now

    with get_conn() as conn:
        row = conn.execute("SELECT id FROM leads WHERE session_id=?", (session_id,)).fetchone()

        if row:
            lead_id = row["id"]
            campos_ok = [
                "numero","nombre_cliente","tipo_operacion","tipo_propiedad",
                "tipo_financiamiento","plazo_busqueda","fecha_seguimiento","notas",
                "prioridad","email","canal","estado","lead_score","lead_nivel",
                "asesor_id","actualizado_en"
            ]
            campos, vals = [], []
            for k in campos_ok:
                if k in datos and datos[k] is not None:
                    campos.append(f"{k}=?"); vals.append(datos[k])
            if campos:
                vals.append(lead_id)
                conn.execute(f"UPDATE leads SET {', '.join(campos)} WHERE id=?", vals)

        else:
            asesor = conn.execute("""
                SELECT a.id FROM asesores a
                LEFT JOIN leads l ON l.asesor_id=a.id AND l.estado NOT IN ('CIERRE','PERDIDO')
                WHERE a.activo=1
                GROUP BY a.id ORDER BY COUNT(l.id) ASC LIMIT 1
            """).fetchone()
            asesor_id = datos.get("asesor_id") or (asesor["id"] if asesor else None)

            conn.execute("""
                INSERT INTO leads
                (session_id, numero, nombre_cliente, tipo_operacion, tipo_propiedad,
                 tipo_financiamiento, plazo_busqueda, fecha_seguimiento, notas, prioridad,
                 email, canal, estado, lead_score, lead_nivel, asesor_id, creado_en, actualizado_en)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                session_id,
                datos.get("numero"),
                datos.get("nombre_cliente"),
                datos.get("tipo_operacion"),
                datos.get("tipo_propiedad"),
                datos.get("tipo_financiamiento"),
                datos.get("plazo_busqueda"),
                datos.get("fecha_seguimiento"),
                datos.get("notas"),
                datos.get("prioridad","BAJA"),
                datos.get("email"),
                datos.get("canal","whatsapp"),
                datos.get("estado","NUEVO"),
                datos.get("lead_score",0),
                datos.get("lead_nivel","FRIO"),
                asesor_id,
                now, now,
            ))
            lead_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            print(f"✅ Nuevo lead #{lead_id} — sesión {session_id}")

    return lead_id


def get_lead_by_session(session_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE session_id=?", (session_id,)).fetchone()
        return dict(row) if row else None


def get_lead_by_id(lead_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT l.*, a.nombre as asesor_nombre, a.email as asesor_email,
                   a.calendar_id as asesor_calendar
            FROM leads l LEFT JOIN asesores a ON l.asesor_id=a.id
            WHERE l.id=?
        """, (lead_id,)).fetchone()
        return dict(row) if row else None


def actualizar_estado_lead(lead_id: int, estado: str):
    estados = ["NUEVO","CONTACTADO","CITA","CIERRE","PERDIDO"]
    if estado not in estados:
        raise ValueError(f"Estado inválido: {estado}")
    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET estado=?, fecha_seguimiento=?, actualizado_en=? WHERE id=?",
            (estado, _hoy(), datetime.now().isoformat(), lead_id)
        )


def listar_leads(estado=None, asesor_id=None, nivel=None, prioridad=None, limit=100, offset=0) -> list[dict]:
    q = "SELECT l.*, a.nombre as asesor_nombre FROM leads l LEFT JOIN asesores a ON l.asesor_id=a.id WHERE 1=1"
    params = []
    if estado:    q += " AND l.estado=?";     params.append(estado)
    if asesor_id: q += " AND l.asesor_id=?";  params.append(asesor_id)
    if nivel:     q += " AND l.lead_nivel=?"; params.append(nivel)
    if prioridad: q += " AND l.prioridad=?";  params.append(prioridad)
    q += """ ORDER BY
        CASE l.prioridad WHEN 'ALTA' THEN 1 WHEN 'MEDIA' THEN 2 ELSE 3 END,
        l.actualizado_en DESC LIMIT ? OFFSET ?"""
    params += [limit, offset]
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def stats_crm() -> dict:
    with get_conn() as conn:
        estados     = conn.execute("SELECT estado,    COUNT(*) n FROM leads GROUP BY estado").fetchall()
        niveles     = conn.execute("SELECT lead_nivel,COUNT(*) n FROM leads GROUP BY lead_nivel").fetchall()
        prioridades = conn.execute("SELECT prioridad, COUNT(*) n FROM leads GROUP BY prioridad").fetchall()
        citas_hoy   = conn.execute("SELECT COUNT(*) n FROM citas WHERE fecha=date('now') AND estado IN ('PENDIENTE','CONFIRMADA')").fetchone()
        total       = conn.execute("SELECT COUNT(*) n FROM leads").fetchone()
        asesores_c  = conn.execute("""
            SELECT a.nombre, COUNT(l.id) leads_activos
            FROM asesores a
            LEFT JOIN leads l ON l.asesor_id=a.id AND l.estado NOT IN ('CIERRE','PERDIDO')
            WHERE a.activo=1 GROUP BY a.id
        """).fetchall()
    return {
        "total_leads":    total["n"],
        "por_estado":     {r["estado"]:     r["n"] for r in estados},
        "por_nivel":      {r["lead_nivel"]: r["n"] for r in niveles},
        "por_prioridad":  {r["prioridad"]:  r["n"] for r in prioridades},
        "citas_hoy":      citas_hoy["n"],
        "carga_asesores": [dict(r) for r in asesores_c],
    }


# ─────────────────────────────────────────
# CONVERSACIONES
# ─────────────────────────────────────────
def guardar_mensaje(lead_id: int, rol: str, mensaje: str, canal: str = "whatsapp"):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO conversaciones (lead_id, rol, mensaje, canal) VALUES (?,?,?,?)",
            (lead_id, rol, mensaje, canal)
        )
        if rol == "user":
            # Actualizar fecha de seguimiento al recibir mensaje del cliente
            conn.execute(
                "UPDATE leads SET fecha_seguimiento=?, actualizado_en=? WHERE id=?",
                (_hoy(), datetime.now().isoformat(), lead_id)
            )


def obtener_historial(lead_id: int, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT rol, mensaje, canal, creado_en FROM conversaciones
            WHERE lead_id=? ORDER BY creado_en DESC LIMIT ?
        """, (lead_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]


# ─────────────────────────────────────────
# CITAS
# ─────────────────────────────────────────
def crear_cita(lead_id: int, datos: dict) -> int:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO citas (lead_id, asesor_id, fecha, hora, modalidad, estado,
             google_event_id, google_meet_link, notas)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            lead_id, datos.get("asesor_id"),
            datos.get("fecha"), datos.get("hora"),
            datos.get("modalidad","presencial"),
            datos.get("estado","PENDIENTE"),
            datos.get("google_event_id"), datos.get("google_meet_link"),
            datos.get("notas"),
        ))
        cita_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "UPDATE leads SET estado='CITA', fecha_seguimiento=?, actualizado_en=? WHERE id=?",
            (_hoy(), datetime.now().isoformat(), lead_id)
        )
    return cita_id


def actualizar_cita(cita_id: int, datos: dict):
    with get_conn() as conn:
        campos, vals = [], []
        for k in ["estado","google_event_id","google_meet_link","notas"]:
            if k in datos:
                campos.append(f"{k}=?"); vals.append(datos[k])
        if campos:
            vals.append(cita_id)
            conn.execute(f"UPDATE citas SET {', '.join(campos)} WHERE id=?", vals)


def citas_del_asesor(asesor_id: int, fecha: str = None) -> list[dict]:
    q = """SELECT c.*, l.nombre_cliente, l.numero as lead_tel
           FROM citas c JOIN leads l ON c.lead_id=l.id
           WHERE c.asesor_id=? AND c.estado IN ('PENDIENTE','CONFIRMADA')"""
    params = [asesor_id]
    if fecha:
        q += " AND c.fecha=?"; params.append(fecha)
    q += " ORDER BY c.fecha, c.hora"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


# ─────────────────────────────────────────
# SEGUIMIENTOS
# ─────────────────────────────────────────
def agregar_seguimiento(lead_id: int, asesor_id: int, tipo: str, descripcion: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO seguimientos (lead_id, asesor_id, tipo, descripcion) VALUES (?,?,?,?)",
            (lead_id, asesor_id, tipo, descripcion)
        )
        conn.execute(
            "UPDATE leads SET fecha_seguimiento=?, actualizado_en=? WHERE id=?",
            (_hoy(), datetime.now().isoformat(), lead_id)
        )


def obtener_seguimientos(lead_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT s.*, a.nombre as asesor_nombre FROM seguimientos s
            LEFT JOIN asesores a ON s.asesor_id=a.id
            WHERE s.lead_id=? ORDER BY s.creado_en DESC
        """, (lead_id,)).fetchall()
        return [dict(r) for r in rows]


# ─────────────────────────────────────────
# ASESORES
# ─────────────────────────────────────────
def listar_asesores() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM asesores WHERE activo=1 ORDER BY nombre").fetchall()]


def crear_asesor(nombre: str, email: str, telefono: str = "", calendar_id: str = "primary") -> int:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO asesores (nombre, email, telefono, calendar_id) VALUES (?,?,?,?)",
            (nombre, email, telefono, calendar_id)
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── Auto-init
init_db()
