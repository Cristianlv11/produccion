from fastapi import FastAPI, Request, Form, UploadFile, File, Depends  # type: ignore
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse  # type: ignore
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi.templating import Jinja2Templates  # type: ignore
from starlette.middleware.sessions import SessionMiddleware
from fastapi.middleware.gzip import GZipMiddleware  # type: ignore
from datetime import datetime
from zoneinfo import ZoneInfo
from datetime import timedelta
from fastapi.staticfiles import StaticFiles  # type: ignore
from dotenv import load_dotenv
from auth import login_user, require_admin, require_operario, hash_password
from database import db
from apscheduler.schedulers.background import BackgroundScheduler
from notificaciones import notificar_ausencias_operarios
from backup_db import ejecutar_backup_completo
from routers.ordenes import router as ordenes_router
from routers.usuarios import router as usuarios_router
from routers.android import router as android_router, guardar_registro_android, usuario_android_habilitado
from routers.metricas import router as metricas_router, metricas_semanales
from routers.bonos import router as bonos_router
from routers.admin_tools import router as admin_tools_router
from routers import planificador
from routers import configuracion
from routers.tiempos import router as tiempos_router
from routers import admin_panel
from routers.tickets import router as tickets_router
from routers.excel_ops import router as excel_ops_router
from routers.inventario import router as inventario_router
from routers.auth_views import router as auth_views_router
import os
import pandas as pd  # type: ignore
from urllib.parse import urlencode

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ADMIN_USER = os.getenv("ADMIN_USER")
ADMIN_PASS = os.getenv("ADMIN_PASS")

from limiter import limiter
from csrf import verify_csrf
app = FastAPI(dependencies=[Depends(verify_csrf)])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse("static/sw.js", media_type="application/javascript")

RUTAS_PUBLICAS_EXACTAS = {
    "/admin",
    "/logout",
    "/registro_android",
    "/android/login",
    "/android/me",
    "/android/cambiar_password",
    "/operarios",
    "/maquinas",
    "/ordenes",
    "/sw.js",
    "/favicon.ico",
}

RUTAS_PUBLICAS_PREFIJOS = (
    "/static/",
    "/procesos/",
    "/actividades/",
)

def ruta_publica(path: str):
    return path in RUTAS_PUBLICAS_EXACTAS or any(
        path.startswith(prefijo) for prefijo in RUTAS_PUBLICAS_PREFIJOS
    )

def ruta_operario(path: str):
    return (path in {"/registro_web", "/registro_web/registro", "/cambiar_password", "/inicio_operario"} 
            or path.startswith("/tickets/mis_tickets") 
            or path.startswith("/tickets/actualizar_estado")
            or path.startswith("/tickets/detalle")
            or path.startswith("/tickets/add_nota")
            or path.startswith("/tickets/cerrar_operario"))

def ruta_jefe_tickets(path: str):
    return (path.startswith("/tickets/admin") 
            or path.startswith("/tickets/crear") 
            or path.startswith("/tickets/eliminar")
            or path.startswith("/tickets/detalle")
            or path.startswith("/tickets/kanban_update")
            or path.startswith("/tickets/actualizar_vencimiento")
            or path.startswith("/tickets/actualizar_tiempo")
            or path.startswith("/tickets/dashboard")
            or path.startswith("/tickets/") and "/actividades/" in path)

from database import _active_connections

@app.middleware("http")
async def db_connection_leak_middleware(request: Request, call_next):
    token = _active_connections.set([])
    try:
        response = await call_next(request)
        return response
    finally:
        conns = _active_connections.get()
        if conns is not None:
            for c in conns:
                try:
                    c.close()
                except Exception:
                    pass
        _active_connections.reset(token)

@app.middleware("http")
async def proteger_rutas_administrativas(request: Request, call_next):
    path = request.url.path
    
    # Excepción para rutas públicas
    if ruta_publica(path):
        return await call_next(request)

    from auth import require_jefe_tickets
    
    es_operario_ruta = ruta_operario(path)
    es_jefe_ruta = ruta_jefe_tickets(path)
    
    # Si la ruta no está ni en operario ni en jefe_tickets, es de admin puro
    if not es_operario_ruta and not es_jefe_ruta:
        if request.session.get("username") and request.session.get("role") == "admin":
            return await call_next(request)
        if request.method in ("GET", "HEAD"):
            destino = path.strip("/") or ""
            return RedirectResponse(f"/admin?next={destino}", status_code=303)
        return JSONResponse({"detail": "Debe iniciar sesion como admin"}, status_code=401)

    es_operario = require_operario(request)
    es_jefe = require_jefe_tickets(request)
    
    can_access = False
    if es_operario_ruta and es_operario:
        can_access = True
    if es_jefe_ruta and es_jefe:
        can_access = True
    
    # Los jefes de tickets y admins siempre pueden acceder a las rutas de operarios que traten sobre tickets
    if es_operario_ruta and path.startswith("/tickets/") and es_jefe:
        can_access = True

    if can_access:
        if es_operario and request.session.get("debe_cambiar_password") and path != "/cambiar_password":
            if request.method in ("GET", "HEAD"):
                return RedirectResponse("/cambiar_password", status_code=303)
            return JSONResponse({"detail": "Debe cambiar su password antes de continuar"}, status_code=403)
            
        return await call_next(request)

    # Si no tiene acceso, redirigir al login
    if request.method in ("GET", "HEAD"):
        destino = path.strip("/") or ""
        return RedirectResponse(f"/admin?next={destino}", status_code=303)
        
    return JSONResponse({"detail": "No autorizado para esta accion"}, status_code=401)

# Configurar SessionMiddleware con flags de seguridad
app.add_middleware(
    SessionMiddleware, 
    secret_key=SECRET_KEY,
    https_only=False,
    same_site="lax",
    max_age=86400  # 24 horas
)
app.add_middleware(GZipMiddleware, minimum_size=500)

templates = Jinja2Templates(directory="templates")
app.state.templates = templates

app.include_router(usuarios_router)
app.include_router(android_router)
app.include_router(metricas_router)
app.include_router(bonos_router)
app.include_router(admin_tools_router)
app.include_router(configuracion.router)
app.include_router(tiempos_router)
from routers.dashboard import router as dashboard_router
app.include_router(dashboard_router)
app.include_router(admin_panel.router)
app.include_router(planificador.router)
app.include_router(tickets_router)
app.include_router(inventario_router)
app.include_router(excel_ops_router)
app.include_router(auth_views_router)

# ================= CREAR TABLAS =================

@app.on_event("startup")
def startup():
    conn = db()
    c = conn.cursor()
    
    # Fix ticket states from previous discrepancy
    c.execute("UPDATE tickets SET estado = 'EN_PROGRESO' WHERE estado = 'EN PROGRESO'")
    c.execute("UPDATE tickets SET estado = 'CERRADO' WHERE estado = 'COMPLETADO'")

    c.execute("""
    CREATE TABLE IF NOT EXISTS maquinas(
        id SERIAL PRIMARY KEY,
        nombre TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS procesos(
        id SERIAL PRIMARY KEY,
        maquina_id INTEGER REFERENCES maquinas(id) ON DELETE CASCADE,
        nombre TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS actividades(
        id SERIAL PRIMARY KEY,
        proceso_id INTEGER REFERENCES procesos(id) ON DELETE CASCADE,
        nombre TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS operarios(
        id SERIAL PRIMARY KEY,
        nombre TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS ordenes(
        id SERIAL PRIMARY KEY,
        maquina_id INTEGER REFERENCES maquinas(id) ON DELETE SET NULL,
        cantidad INTEGER,
        estado TEXT,
        porcentaje REAL DEFAULT 0,
        cerrado_en TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS orden_actividades(
        id SERIAL PRIMARY KEY,
        orden_id INTEGER REFERENCES ordenes(id) ON DELETE CASCADE,
        actividad_id INTEGER REFERENCES actividades(id) ON DELETE CASCADE,
        cantidad_total INTEGER,
        cantidad_realizada INTEGER DEFAULT 0
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS registros_produccion(
        id SERIAL PRIMARY KEY,
        operario_id INTEGER REFERENCES operarios(id) ON DELETE SET NULL,
        orden_id INTEGER REFERENCES ordenes(id) ON DELETE CASCADE,
        actividad_id INTEGER REFERENCES actividades(id) ON DELETE CASCADE,
        cantidad INTEGER,
        inicio TEXT,
        fin TEXT,
        tiempo INTEGER
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS auditoria_registros_produccion(
        id SERIAL PRIMARY KEY,
        registro_id INTEGER,
        admin_id INTEGER,
        fecha_edicion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        valores_anteriores JSONB,
        valores_nuevos JSONB
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS bonos(
        id SERIAL PRIMARY KEY,
        operario_id INTEGER REFERENCES operarios(id) ON DELETE CASCADE,
        actividad_id INTEGER REFERENCES actividades(id) ON DELETE CASCADE,
        unidades INTEGER,
        horas REAL,
        rendimiento REAL,
        porcentaje REAL,
        valor REAL,
        fecha TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS cierre_bonos(
        id SERIAL PRIMARY KEY,
        mes INTEGER,
        anio INTEGER,
        datos_json TEXT,
        cerrado_por INTEGER REFERENCES users(id),
        fecha_cierre TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(mes, anio)
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS estandares_actividad(
        id SERIAL PRIMARY KEY,
        actividad_id INTEGER REFERENCES actividades(id) ON DELETE CASCADE,
        unidades_por_hora REAL,
        costo_mo_unidad REAL,
        costo_mo_hora REAL
    )""")
    
    conn.commit()  # <-- FIX: Commit table creations before attempting ALTERS

    c.execute("CREATE INDEX IF NOT EXISTS idx_regprod_operario_inicio ON registros_produccion(operario_id, inicio)")
    conn.commit()

    # Intentar añadir llaves foráneas a BDs existentes (ignorar si falla por huérfanos)
    for alter_cmd in [
        "ALTER TABLE procesos ADD CONSTRAINT fk_proc_maquina FOREIGN KEY (maquina_id) REFERENCES maquinas(id) ON DELETE CASCADE",
        "ALTER TABLE actividades ADD CONSTRAINT fk_act_proceso FOREIGN KEY (proceso_id) REFERENCES procesos(id) ON DELETE CASCADE",
        "ALTER TABLE ordenes ADD CONSTRAINT fk_ord_maquina FOREIGN KEY (maquina_id) REFERENCES maquinas(id) ON DELETE SET NULL",
        "ALTER TABLE orden_actividades ADD CONSTRAINT fk_oa_orden FOREIGN KEY (orden_id) REFERENCES ordenes(id) ON DELETE CASCADE",
        "ALTER TABLE registros_produccion ADD CONSTRAINT fk_reg_operario FOREIGN KEY (operario_id) REFERENCES operarios(id) ON DELETE SET NULL",
        "ALTER TABLE registros_produccion ADD CONSTRAINT fk_reg_orden FOREIGN KEY (orden_id) REFERENCES ordenes(id) ON DELETE CASCADE",
        "ALTER TABLE bonos ADD CONSTRAINT fk_bono_operario FOREIGN KEY (operario_id) REFERENCES operarios(id) ON DELETE CASCADE"
    ]:
        try:
            c.execute(alter_cmd)
        except Exception:
            conn.rollback()
        else:
            conn.commit()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL,
        operario_id INTEGER,
        debe_cambiar_password BOOLEAN DEFAULT FALSE,
        telefono TEXT
    )""")
    
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS telefono TEXT")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS debe_cambiar_password BOOLEAN DEFAULT FALSE")
    c.execute("ALTER TABLE operarios ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE")
    c.execute("ALTER TABLE maquinas ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE")
    c.execute("ALTER TABLE procesos ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE")
    c.execute("ALTER TABLE actividades ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE")
    c.execute("ALTER TABLE actividades ADD COLUMN IF NOT EXISTS herramienta TEXT")
    c.execute("ALTER TABLE actividades ADD COLUMN IF NOT EXISTS operario_responsable_id INTEGER")

    c.execute("""
    CREATE TABLE IF NOT EXISTS tickets(
        id SERIAL PRIMARY KEY,
        titulo TEXT NOT NULL,
        descripcion TEXT,
        estado TEXT DEFAULT 'PENDIENTE',
        prioridad TEXT DEFAULT 'MEDIA',
        fecha_vencimiento TIMESTAMP,
        minutos_invertidos INTEGER DEFAULT 0,
        asignado_a INTEGER REFERENCES users(id),
        creado_por INTEGER REFERENCES users(id),
        fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    c.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS prioridad TEXT DEFAULT 'MEDIA'")
    c.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS fecha_vencimiento TIMESTAMP")
    c.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS minutos_invertidos INTEGER DEFAULT 0")

    c.execute("""
    CREATE TABLE IF NOT EXISTS ticket_adjuntos(
        id SERIAL PRIMARY KEY,
        ticket_id INTEGER REFERENCES tickets(id) ON DELETE CASCADE,
        nombre_original TEXT,
        ruta_archivo TEXT
    )""")

    c.execute("""
    ALTER TABLE ticket_adjuntos 
    ADD COLUMN IF NOT EXISTS subido_por INTEGER REFERENCES users(id),
    ADD COLUMN IF NOT EXISTS fecha_subida TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS bonos_operarios (
        id SERIAL PRIMARY KEY,
        bono_id INTEGER REFERENCES bonos(id) ON DELETE CASCADE,
        operario_id INTEGER REFERENCES operarios(id) ON DELETE CASCADE,
        monto_otorgado NUMERIC(10,2) DEFAULT 0,
        fecha_asignacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    conn.commit()

    c.execute("""
    CREATE TABLE IF NOT EXISTS ticket_notas(
        id SERIAL PRIMARY KEY,
        ticket_id INTEGER REFERENCES tickets(id) ON DELETE CASCADE,
        usuario_id INTEGER REFERENCES users(id),
        nota TEXT,
        fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS ticket_actividades(
        id SERIAL PRIMARY KEY,
        ticket_id INTEGER REFERENCES tickets(id) ON DELETE CASCADE,
        descripcion TEXT NOT NULL,
        estado TEXT DEFAULT 'PENDIENTE',
        asignado_a INTEGER REFERENCES users(id),
        creado_por INTEGER REFERENCES users(id),
        fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS operario_id INTEGER")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS debe_cambiar_password BOOLEAN DEFAULT FALSE")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE")
    c.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS notas_operario TEXT")

    # Índices para mejorar rendimiento (Fase 2)
    c.execute("CREATE INDEX IF NOT EXISTS idx_tickets_estado ON tickets(estado)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tickets_asignado ON tickets(asignado_a)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tickets_creacion ON tickets(fecha_creacion)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ordenes_estado ON ordenes(estado)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ordenes_maquina ON ordenes(maquina_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_regprod_orden ON registros_produccion(orden_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_regprod_operario ON registros_produccion(operario_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")

    # Crear admin inicial si no existe
    c.execute("SELECT * FROM users WHERE username = %s", ("admin",))
    existe = c.fetchone()

    if not existe:
        hashed = hash_password("1234")
        c.execute(
            "INSERT INTO users (username, password, role) VALUES (%s, %s, %s)",
            ("admin", hashed, "admin")
        )

    conn.commit()
    conn.close()

    # Start apscheduler
    scheduler = BackgroundScheduler(timezone="America/Bogota")
    scheduler.add_job(notificar_ausencias_operarios, 'cron', day_of_week='tue-fri', hour=7, minute=0)
    scheduler.add_job(ejecutar_backup_completo, 'cron', hour=3, minute=0, kwargs={'tipo': 'AUTOMATICO'})
    scheduler.start()

# ================= HOME =================

# ================= REGISTRO =================

# ================= EXPORTAR =================

# ================= ELIMINAR ORDEN =================

# ================= CERRAR ORDEN =================

# ================= IMPORTAR EXCEL =================

META_MENSUAL = 5000
TARIFA_HH = 10000

