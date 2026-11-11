"""
Registro de producción por cortes.

La supervisora pasa por los puestos en cada corte del día (desayuno, almuerzo, salida)
y registra cuánto hizo cada operario desde el corte anterior. Los registros se guardan
en registros_produccion, igual que los de la app de operarios, para que métricas, bonos
y avance de órdenes sigan funcionando.
"""
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import require_admin
from database import db
from routers.android import leer_token_android

router = APIRouter()

TZ = ZoneInfo("America/Bogota")
ROLES_SUPERVISION = ("admin", "supervisor")
HORA_VALIDA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Horario de planta: 6:00 a 15:50, desayuno 8:00-8:20 y almuerzo 12:00-13:00 = 510 min de trabajo
# (la misma jornada de la hoja de balanceo del Excel)
CORTES_INICIALES = [
    ("Entrada", "06:00", 0),
    ("Desayuno", "08:00", 20),
    ("Almuerzo", "12:00", 60),
    ("Salida", "15:50", 0),
]


def asegurar_schema_cortes(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cortes_horario(
            id SERIAL PRIMARY KEY,
            nombre TEXT NOT NULL,
            hora TEXT NOT NULL,
            descanso_min INTEGER DEFAULT 0,
            activo BOOLEAN DEFAULT TRUE
        )""")
    cursor.execute("ALTER TABLE registros_produccion ADD COLUMN IF NOT EXISTS registrado_por INTEGER")
    cursor.execute("ALTER TABLE registros_produccion ADD COLUMN IF NOT EXISTS corte_id INTEGER")
    cursor.execute("ALTER TABLE registros_produccion ADD COLUMN IF NOT EXISTS fecha_corte DATE")
    cursor.execute("SELECT COUNT(*) FROM cortes_horario")
    if cursor.fetchone()[0] == 0:
        for nombre, hora, descanso in CORTES_INICIALES:
            cursor.execute(
                "INSERT INTO cortes_horario (nombre, hora, descanso_min) VALUES (%s, %s, %s)",
                (nombre, hora, descanso),
            )


def hoy():
    return datetime.now(TZ).date()


def a_minutos(hora: str) -> int:
    h, m = hora.split(":")
    return int(h) * 60 + int(m)


def cortes_activos(cursor):
    cursor.execute("""
        SELECT id, nombre, hora, COALESCE(descanso_min, 0)
        FROM cortes_horario WHERE COALESCE(activo, TRUE) = TRUE
    """)
    cortes = [{"id": r[0], "nombre": r[1], "hora": r[2], "descanso_min": r[3]} for r in cursor.fetchall()]
    cortes.sort(key=lambda x: a_minutos(x["hora"]))
    # Cada corte cierra el tramo que empezó al terminar el descanso del corte anterior
    for i, corte in enumerate(cortes):
        if i == 0:
            corte["desde"] = None
            corte["minutos_tramo"] = 0
        else:
            previo = cortes[i - 1]
            inicio = a_minutos(previo["hora"]) + previo["descanso_min"]
            corte["desde"] = f"{inicio // 60:02d}:{inicio % 60:02d}"
            corte["minutos_tramo"] = max(0, a_minutos(corte["hora"]) - inicio)
    return cortes


def corte_sugerido(cortes, ahora: datetime):
    """El corte que cierra el tramo en curso: el primero cuya hora aún no pasó hace más de 90 min."""
    minutos = ahora.hour * 60 + ahora.minute
    cierres = [c for c in cortes if c["desde"]]
    for corte in cierres:
        if minutos <= a_minutos(corte["hora"]) + 90:
            return corte["id"]
    return cierres[-1]["id"] if cierres else None


def supervisor_android(payload: dict = Depends(leer_token_android)):
    conn = db()
    c = conn.cursor()
    c.execute(
        "SELECT id, username, role FROM users WHERE id = %s AND COALESCE(activo, TRUE) = TRUE",
        (payload.get("user_id"),),
    )
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="Usuario no encontrado o inactivo")
    if row[2] not in ROLES_SUPERVISION:
        raise HTTPException(status_code=403, detail="Solo supervisión puede registrar por cortes")
    return {"user_id": row[0], "username": row[1], "role": row[2]}


def etiqueta_orden(orden_id, maquina):
    return f"Orden #{orden_id} — {maquina or 'sin máquina'}"


def aplicar_avance(cursor, orden_id: int, actividad_id: int, delta: int):
    """Suma (o resta) unidades a la actividad de la orden y recalcula su porcentaje, como el registro Android."""
    cursor.execute("""
        UPDATE orden_actividades
        SET cantidad_realizada = GREATEST(0, cantidad_realizada + %s)
        WHERE orden_id = %s AND actividad_id = %s
    """, (delta, orden_id, actividad_id))
    cursor.execute("""
        SELECT SUM(cantidad_realizada), SUM(cantidad_total)
        FROM orden_actividades WHERE orden_id = %s
    """, (orden_id,))
    row = cursor.fetchone()
    porcentaje = round((row[0] / row[1]) * 100, 2) if row and row[1] else 0
    cursor.execute("""
        UPDATE ordenes
        SET porcentaje = %s,
            estado = CASE WHEN %s >= 100 THEN 'CERRADA' ELSE estado END
        WHERE id = %s
    """, (porcentaje, porcentaje, orden_id))


def registros_del_dia(cursor, fecha: date):
    cursor.execute("""
        SELECT r.id, r.operario_id, r.corte_id, r.orden_id, m.nombre, a.nombre, p.nombre,
               r.cantidad, r.inicio, r.fin, COALESCE(r.tiempo, 0)
        FROM registros_produccion r
        JOIN ordenes o ON o.id = r.orden_id
        LEFT JOIN maquinas m ON m.id = o.maquina_id
        LEFT JOIN actividades a ON a.id = r.actividad_id
        LEFT JOIN procesos p ON p.id = a.proceso_id
        WHERE r.fecha_corte = %s AND r.registrado_por IS NOT NULL
        ORDER BY r.id
    """, (fecha,))
    return [{
        "id": r[0], "operario_id": r[1], "corte_id": r[2], "orden_id": r[3],
        "orden": etiqueta_orden(r[3], r[4]), "actividad": r[5] or "", "proceso": r[6] or "",
        "cantidad": r[7], "inicio": (r[8] or "")[11:16], "fin": (r[9] or "")[11:16],
        "minutos": round(r[10] / 60),
    } for r in cursor.fetchall()]


# ================= API PARA LA APP (SUPERVISIÓN) =================

@router.get("/supervisor/jornada")
def jornada(fecha: str = "", supervisor=Depends(supervisor_android)):
    dia = date.fromisoformat(fecha) if fecha else hoy()
    conn = db()
    c = conn.cursor()
    try:
        asegurar_schema_cortes(c)
        conn.commit()
        cortes = cortes_activos(c)
        c.execute("SELECT id, nombre FROM operarios WHERE COALESCE(activo, TRUE) = TRUE ORDER BY nombre")
        operarios = [{"id": r[0], "nombre": r[1]} for r in c.fetchall()]
        registros = registros_del_dia(c, dia)
    finally:
        conn.close()
    return {
        "fecha": dia.isoformat(),
        "supervisor": supervisor["username"],
        "cortes": cortes,
        "corte_sugerido_id": corte_sugerido(cortes, datetime.now(TZ)) if dia == hoy() else None,
        "operarios": operarios,
        "registros": registros,
    }


@router.get("/supervisor/ordenes")
def ordenes_abiertas(supervisor=Depends(supervisor_android)):
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT o.id, m.nombre, o.cantidad, COALESCE(o.porcentaje, 0)
        FROM ordenes o LEFT JOIN maquinas m ON m.id = o.maquina_id
        WHERE o.estado != 'CERRADA'
        ORDER BY o.id DESC
    """)
    datos = [{"id": r[0], "etiqueta": etiqueta_orden(r[0], r[1]), "cantidad": r[2],
              "porcentaje": float(r[3])} for r in c.fetchall()]
    conn.close()
    return datos


@router.get("/supervisor/actividades/{orden_id}")
def actividades_orden(orden_id: int, supervisor=Depends(supervisor_android)):
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT a.id, a.nombre, p.nombre, oa.cantidad_realizada, oa.cantidad_total
        FROM orden_actividades oa
        JOIN actividades a ON a.id = oa.actividad_id
        JOIN procesos p ON p.id = a.proceso_id
        WHERE oa.orden_id = %s
          AND COALESCE(a.activo, TRUE) = TRUE AND COALESCE(p.activo, TRUE) = TRUE
        ORDER BY p.id, a.id
    """, (orden_id,))
    datos = [{"id": r[0], "nombre": r[1], "proceso": r[2], "hechas": r[3] or 0, "total": r[4] or 0}
             for r in c.fetchall()]
    conn.close()
    return datos


@router.post("/supervisor/registro")
def registrar(data: dict, supervisor=Depends(supervisor_android)):
    try:
        operario_id = int(data.get("operario_id"))
        orden_id = int(data.get("orden_id"))
        actividad_id = int(data.get("actividad_id"))
        cantidad = int(data.get("cantidad"))
        corte_id = int(data.get("corte_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Faltan datos: operario, orden, actividad, cantidad y corte")
    if cantidad < 0:
        raise HTTPException(status_code=400, detail="La cantidad no puede ser negativa")
    dia = date.fromisoformat(data["fecha"]) if data.get("fecha") else hoy()
    minutos_pedidos = data.get("minutos")

    conn = db()
    c = conn.cursor()
    try:
        asegurar_schema_cortes(c)
        corte = next((x for x in cortes_activos(c) if x["id"] == corte_id), None)
        if not corte or not corte["desde"]:
            raise HTTPException(status_code=400, detail="Ese corte no cierra ningún tramo")

        c.execute("SELECT 1 FROM operarios WHERE id = %s AND COALESCE(activo, TRUE) = TRUE", (operario_id,))
        if not c.fetchone():
            raise HTTPException(status_code=400, detail="El operario no existe o está inactivo")

        c.execute("""
            SELECT 1 FROM orden_actividades oa
            JOIN ordenes o ON o.id = oa.orden_id
            WHERE oa.orden_id = %s AND oa.actividad_id = %s AND o.estado != 'CERRADA'
        """, (orden_id, actividad_id))
        if not c.fetchone():
            raise HTTPException(status_code=400, detail="La actividad no pertenece a esa orden abierta")

        c.execute("""
            SELECT id FROM registros_produccion
            WHERE operario_id = %s AND fecha_corte = %s AND corte_id = %s
              AND orden_id = %s AND actividad_id = %s
        """, (operario_id, dia, corte_id, orden_id, actividad_id))
        if c.fetchone():
            raise HTTPException(status_code=409, detail="Esa actividad ya está registrada para este operario en este corte. Bórrela y regístrela de nuevo para corregirla.")

        # El tiempo se reparte dentro del tramo: cada actividad empieza donde terminó la anterior
        c.execute("""
            SELECT COALESCE(SUM(tiempo), 0) FROM registros_produccion
            WHERE operario_id = %s AND fecha_corte = %s AND corte_id = %s
        """, (operario_id, dia, corte_id))
        usados = round(c.fetchone()[0] / 60)
        disponibles = corte["minutos_tramo"] - usados
        if disponibles <= 0:
            raise HTTPException(status_code=400, detail="Este operario ya tiene todo el tiempo del tramo asignado")
        if minutos_pedidos in (None, ""):
            minutos = disponibles
        else:
            minutos = int(minutos_pedidos)
            if minutos <= 0 or minutos > disponibles:
                raise HTTPException(status_code=400, detail=f"Los minutos deben estar entre 1 y {disponibles}")

        inicio = datetime.combine(dia, datetime.strptime(corte["desde"], "%H:%M").time()) + timedelta(minutes=usados)
        fin = inicio + timedelta(minutes=minutos)
        c.execute("""
            INSERT INTO registros_produccion
                (operario_id, orden_id, actividad_id, cantidad, inicio, fin, tiempo,
                 registrado_por, corte_id, fecha_corte)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (operario_id, orden_id, actividad_id, cantidad,
              inicio.strftime("%Y-%m-%d %H:%M:%S"), fin.strftime("%Y-%m-%d %H:%M:%S"), minutos * 60,
              supervisor["user_id"], corte_id, dia))
        registro_id = c.fetchone()[0]
        aplicar_avance(c, orden_id, actividad_id, cantidad)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "id": registro_id, "inicio": inicio.strftime("%H:%M"), "fin": fin.strftime("%H:%M"),
            "minutos": minutos}


@router.delete("/supervisor/registro/{registro_id}")
def borrar_registro(registro_id: int, supervisor=Depends(supervisor_android)):
    conn = db()
    c = conn.cursor()
    try:
        c.execute("""
            SELECT orden_id, actividad_id, cantidad, fecha_corte FROM registros_produccion
            WHERE id = %s AND registrado_por IS NOT NULL
        """, (registro_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Registro no encontrado")
        orden_id, actividad_id, cantidad, fecha = row
        if (hoy() - fecha).days > 1:
            raise HTTPException(status_code=400, detail="Solo se pueden borrar registros de hoy o de ayer. Para otros, use Tools > Editar Registros en la web.")
        c.execute("DELETE FROM registros_produccion WHERE id = %s", (registro_id,))
        aplicar_avance(c, orden_id, actividad_id, -(cantidad or 0))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True}


# ================= WEB: HORARIO DE CORTES =================

@router.get("/configuracion/cortes", response_class=HTMLResponse)
def vista_cortes(request: Request):
    if not require_admin(request):
        return RedirectResponse("/admin?next=configuracion/cortes", 303)
    return request.app.state.templates.TemplateResponse(
        request=request, name="config_cortes.html", context={"request": request}
    )


@router.get("/config/cortes")
def listar_cortes(request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    conn = db()
    c = conn.cursor()
    asegurar_schema_cortes(c)
    conn.commit()
    c.execute("SELECT id, nombre, hora, COALESCE(descanso_min, 0), COALESCE(activo, TRUE) FROM cortes_horario")
    datos = [{"id": r[0], "nombre": r[1], "hora": r[2], "descanso_min": r[3], "activo": r[4]} for r in c.fetchall()]
    conn.close()
    datos.sort(key=lambda x: a_minutos(x["hora"]))
    return {"cortes": datos}


def validar_corte(data: dict):
    nombre = str(data.get("nombre", "")).strip()
    hora = str(data.get("hora", "")).strip()
    if len(hora) == 4:
        hora = "0" + hora
    if not nombre:
        raise HTTPException(status_code=400, detail="El nombre del corte es obligatorio")
    if not HORA_VALIDA.match(hora):
        raise HTTPException(status_code=400, detail="La hora debe tener el formato HH:MM (24 horas)")
    try:
        descanso = int(data.get("descanso_min") or 0)
    except ValueError:
        raise HTTPException(status_code=400, detail="El descanso debe ser un número de minutos")
    if descanso < 0 or descanso > 180:
        raise HTTPException(status_code=400, detail="El descanso debe estar entre 0 y 180 minutos")
    return nombre, hora, descanso


@router.post("/config/cortes")
def crear_corte(data: dict, request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    nombre, hora, descanso = validar_corte(data)
    conn = db()
    c = conn.cursor()
    asegurar_schema_cortes(c)
    c.execute("INSERT INTO cortes_horario (nombre, hora, descanso_min) VALUES (%s, %s, %s) RETURNING id",
              (nombre, hora, descanso))
    nuevo = c.fetchone()[0]
    conn.commit()
    conn.close()
    return {"ok": True, "id": nuevo}


@router.put("/config/cortes/{corte_id}")
def editar_corte(corte_id: int, data: dict, request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    nombre, hora, descanso = validar_corte(data)
    activo = bool(data.get("activo", True))
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE cortes_horario SET nombre=%s, hora=%s, descanso_min=%s, activo=%s WHERE id=%s",
              (nombre, hora, descanso, activo, corte_id))
    conn.commit()
    conn.close()
    return {"ok": True}


# ================= WEB: PRODUCTO EN PROCESO =================

def producto_en_proceso(cursor):
    cursor.execute("""
        SELECT o.id, m.nombre, o.cantidad, COALESCE(o.porcentaje, 0),
               p.id, p.nombre, a.id, a.nombre, oa.cantidad_total, oa.cantidad_realizada
        FROM ordenes o
        LEFT JOIN maquinas m ON m.id = o.maquina_id
        JOIN orden_actividades oa ON oa.orden_id = o.id
        JOIN actividades a ON a.id = oa.actividad_id
        JOIN procesos p ON p.id = a.proceso_id
        WHERE o.estado != 'CERRADA'
          AND COALESCE(a.activo, TRUE) = TRUE AND COALESCE(p.activo, TRUE) = TRUE
        ORDER BY o.id, p.id, a.id
    """)
    ordenes = {}
    for r in cursor.fetchall():
        orden = ordenes.setdefault(r[0], {
            "id": r[0], "etiqueta": etiqueta_orden(r[0], r[1]), "cantidad": r[2],
            "porcentaje": float(r[3]), "etapas": [],
        })
        orden["etapas"].append({
            "proceso": r[5], "actividad": r[7], "total": r[8] or 0, "hechas": r[9] or 0,
        })
    for orden in ordenes.values():
        etapas = orden["etapas"]
        for i, etapa in enumerate(etapas):
            etapa["faltan"] = max(0, etapa["total"] - etapa["hechas"])
            siguiente = etapas[i + 1]["hechas"] if i + 1 < len(etapas) else None
            # Piezas que ya pasaron por esta etapa y esperan la siguiente
            etapa["en_espera"] = max(0, etapa["hechas"] - siguiente) if siguiente is not None else 0
        orden["en_proceso"] = sum(e["en_espera"] for e in etapas)
    return list(ordenes.values())


@router.get("/produccion_en_proceso", response_class=HTMLResponse)
def vista_en_proceso(request: Request):
    if request.session.get("role") not in ROLES_SUPERVISION:
        return RedirectResponse("/admin?next=produccion_en_proceso", 303)
    conn = db()
    c = conn.cursor()
    try:
        ordenes = producto_en_proceso(c)
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(
        request=request, name="en_proceso.html", context={"request": request, "ordenes": ordenes}
    )
