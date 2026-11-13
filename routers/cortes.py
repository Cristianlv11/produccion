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
from database import db, crear_orden, sincronizar_actividades_ordenes_abiertas, actividades_del_producto
from routers.android import leer_token_android

router = APIRouter()

TZ = ZoneInfo("America/Bogota")
ROLES_SUPERVISION = ("admin", "supervisor")
HORA_VALIDA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Horario de planta: 6:00 a 15:50, desayuno 8:00-8:20 y almuerzo 12:00-13:00 = 510 min de trabajo
# (la misma jornada de la hoja de balanceo del Excel). Los cortes son las pasadas de la supervisora;
# los descansos van aparte y se descuentan del tramo en que caen.
CORTES_INICIALES = [
    ("Entrada", "06:00"),
    ("Desayuno", "08:00"),
    ("Almuerzo", "12:00"),
    ("Salida", "15:50"),
]
PAUSAS_INICIALES = [
    ("Desayuno", "08:00", "08:20"),
    ("Almuerzo", "12:00", "13:00"),
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pausas_horario(
            id SERIAL PRIMARY KEY,
            nombre TEXT NOT NULL,
            inicio TEXT NOT NULL,
            fin TEXT NOT NULL,
            activo BOOLEAN DEFAULT TRUE
        )""")
    cursor.execute("ALTER TABLE registros_produccion ADD COLUMN IF NOT EXISTS registrado_por INTEGER")
    cursor.execute("ALTER TABLE registros_produccion ADD COLUMN IF NOT EXISTS corte_id INTEGER")
    cursor.execute("ALTER TABLE registros_produccion ADD COLUMN IF NOT EXISTS fecha_corte DATE")
    cursor.execute("ALTER TABLE registros_produccion ADD COLUMN IF NOT EXISTS observacion TEXT")
    cursor.execute("ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS nota TEXT")
    # Actividades indirectas (calibración, limpieza, esperas…): cuentan como tiempo trabajado
    # pero no entran en la eficiencia ni en los bonos
    cursor.execute("ALTER TABLE actividades ADD COLUMN IF NOT EXISTS indirecta BOOLEAN DEFAULT FALSE")
    # Área del operario (Soldadura, Pintura…): solo agrupa la lista en la app; puede registrar en cualquier proceso
    cursor.execute("ALTER TABLE operarios ADD COLUMN IF NOT EXISTS area TEXT")
    # Lo que se hizo en un tramo cuando la supervisora no sabe a qué orden va: guarda el tiempo
    # del operario y luego, en la web, se asigna a la orden y actividad correctas
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS registros_por_clasificar(
            id SERIAL PRIMARY KEY,
            operario_id INTEGER NOT NULL,
            corte_id INTEGER NOT NULL,
            fecha_corte DATE NOT NULL,
            cantidad INTEGER DEFAULT 0,
            inicio TEXT,
            fin TEXT,
            tiempo INTEGER NOT NULL,
            nota TEXT,
            registrado_por INTEGER,
            creado TIMESTAMP DEFAULT NOW()
        )""")
    # Actividades creadas desde la app: no tienen estándar hasta revisarlas en la web
    cursor.execute("ALTER TABLE actividades ADD COLUMN IF NOT EXISTS creada_en_app BOOLEAN DEFAULT FALSE")
    from costos import asegurar_parametros
    asegurar_parametros(cursor)

    cursor.execute("SELECT COUNT(*) FROM cortes_horario")
    if cursor.fetchone()[0] == 0:
        for nombre, hora in CORTES_INICIALES:
            cursor.execute("INSERT INTO cortes_horario (nombre, hora) VALUES (%s, %s)", (nombre, hora))

    cursor.execute("SELECT COUNT(*) FROM pausas_horario")
    if cursor.fetchone()[0] == 0:
        # Paso del modelo anterior (descanso pegado al corte) a descansos aparte, sin cambiar los tramos
        cursor.execute("SELECT id, nombre, hora, COALESCE(descanso_min, 0) FROM cortes_horario WHERE COALESCE(descanso_min, 0) > 0")
        viejos = cursor.fetchall()
        if viejos:
            for id_, nombre, hora, descanso in viejos:
                fin = a_minutos(hora) + descanso
                cursor.execute("INSERT INTO pausas_horario (nombre, inicio, fin) VALUES (%s, %s, %s)",
                               (nombre, hora, f"{fin // 60:02d}:{fin % 60:02d}"))
                cursor.execute("UPDATE cortes_horario SET descanso_min = 0 WHERE id = %s", (id_,))
        else:
            for nombre, inicio, fin in PAUSAS_INICIALES:
                cursor.execute("INSERT INTO pausas_horario (nombre, inicio, fin) VALUES (%s, %s, %s)",
                               (nombre, inicio, fin))


def hoy():
    return datetime.now(TZ).date()


def a_minutos(hora: str) -> int:
    h, m = hora.split(":")
    return int(h) * 60 + int(m)


def pausas_activas(cursor):
    cursor.execute("SELECT nombre, inicio, fin FROM pausas_horario WHERE COALESCE(activo, TRUE) = TRUE")
    return [(r[0], a_minutos(r[1]), a_minutos(r[2])) for r in cursor.fetchall()]


def cortes_activos(cursor):
    cursor.execute("""
        SELECT id, nombre, hora
        FROM cortes_horario WHERE COALESCE(activo, TRUE) = TRUE
    """)
    cortes = [{"id": r[0], "nombre": r[1], "hora": r[2], "descanso_min": 0} for r in cursor.fetchall()]
    cortes.sort(key=lambda x: a_minutos(x["hora"]))
    pausas = pausas_activas(cursor)
    # Cada corte cierra el tramo desde el corte anterior; los descansos que caen dentro se descuentan
    for i, corte in enumerate(cortes):
        if i == 0:
            corte["desde"] = None
            corte["minutos_tramo"] = 0
            corte["pausas"] = []
            continue
        desde, hasta = a_minutos(cortes[i - 1]["hora"]), a_minutos(corte["hora"])
        dentro = [(n, max(desde, ini), min(hasta, fin)) for n, ini, fin in pausas if min(hasta, fin) > max(desde, ini)]
        corte["desde"] = cortes[i - 1]["hora"]
        corte["minutos_tramo"] = max(0, hasta - desde - sum(f - i_ for _, i_, f in dentro))
        corte["pausas"] = [n for n, _, _ in dentro]
    return cortes


def corte_sugerido(cortes, ahora: datetime):
    """El corte que cierra el tramo en curso: el primero cuya hora no pasó hace más de 30 min
    (margen para la pasada que se está haciendo; con pasadas cada hora y media, más margen propondría la anterior)."""
    minutos = ahora.hour * 60 + ahora.minute
    cierres = [c for c in cortes if c["desde"]]
    for corte in cierres:
        if minutos <= a_minutos(corte["hora"]) + 30:
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


def etiqueta_orden(orden_id, maquina, nota=None):
    texto = f"Orden #{orden_id} — {maquina or 'sin máquina'}"
    return f"{texto} · {nota.strip()}" if nota and nota.strip() else texto


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
    # No se cierra sola: con un producto a medio configurar (o unidades por pieza distintas
    # a las de la orden) llegaba al 100 % y se cerraba en plena producción. Se cierra en Producción.
    cursor.execute("UPDATE ordenes SET porcentaje = %s WHERE id = %s", (porcentaje, orden_id))


def minutos_usados(cursor, operario_id: int, dia: date, corte_id: int) -> int:
    """Minutos del tramo ya asignados al operario, contando los registros por clasificar."""
    cursor.execute("""
        SELECT COALESCE((SELECT SUM(tiempo) FROM registros_produccion
                         WHERE operario_id = %s AND fecha_corte = %s AND corte_id = %s), 0)
             + COALESCE((SELECT SUM(tiempo) FROM registros_por_clasificar
                         WHERE operario_id = %s AND fecha_corte = %s AND corte_id = %s), 0)
    """, (operario_id, dia, corte_id, operario_id, dia, corte_id))
    return round(cursor.fetchone()[0] / 60)


def registros_del_dia(cursor, fecha: date):
    cursor.execute("""
        SELECT r.id, r.operario_id, r.corte_id, r.orden_id, m.nombre, a.nombre, p.nombre,
               r.cantidad, r.inicio, r.fin, COALESCE(r.tiempo, 0), o.nota, r.observacion
        FROM registros_produccion r
        JOIN ordenes o ON o.id = r.orden_id
        LEFT JOIN maquinas m ON m.id = o.maquina_id
        LEFT JOIN actividades a ON a.id = r.actividad_id
        LEFT JOIN procesos p ON p.id = a.proceso_id
        WHERE r.fecha_corte = %s AND r.registrado_por IS NOT NULL
        ORDER BY r.id
    """, (fecha,))
    salida = [{
        "id": r[0], "operario_id": r[1], "corte_id": r[2], "orden_id": r[3],
        "orden": etiqueta_orden(r[3], r[4], r[11]), "actividad": r[5] or "", "proceso": r[6] or "",
        "cantidad": r[7], "inicio": (r[8] or "")[11:16], "fin": (r[9] or "")[11:16],
        "minutos": round(r[10] / 60), "observacion": r[12] or "",
    } for r in cursor.fetchall()]
    # Los por clasificar van con id negativo y orden 0: la app los muestra y los borra por su propia ruta
    cursor.execute("""
        SELECT id, operario_id, corte_id, cantidad, inicio, fin, tiempo, nota
        FROM registros_por_clasificar WHERE fecha_corte = %s ORDER BY id
    """, (fecha,))
    salida += [{
        "id": -r[0], "operario_id": r[1], "corte_id": r[2], "orden_id": 0,
        "orden": "Por clasificar", "actividad": "Por clasificar", "proceso": "",
        "cantidad": r[3] or 0, "inicio": (r[4] or "")[11:16], "fin": (r[5] or "")[11:16],
        "minutos": round(r[6] / 60), "observacion": r[7] or "",
    } for r in cursor.fetchall()]
    return salida


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
        c.execute("SELECT id, nombre, COALESCE(NULLIF(trim(area), ''), 'Sin área') FROM operarios WHERE COALESCE(activo, TRUE) = TRUE ORDER BY nombre")
        operarios = [{"id": r[0], "nombre": r[1], "area": r[2]} for r in c.fetchall()]
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
        SELECT o.id, m.nombre, o.cantidad, COALESCE(o.porcentaje, 0), o.nota
        FROM ordenes o LEFT JOIN maquinas m ON m.id = o.maquina_id
        WHERE o.estado != 'CERRADA'
        ORDER BY o.id DESC
    """)
    datos = [{"id": r[0], "etiqueta": etiqueta_orden(r[0], r[1], r[4]), "maquina": r[1] or "",
              "nota": r[4] or "", "cantidad": r[2], "porcentaje": float(r[3])} for r in c.fetchall()]
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


def ubicar_en_tramo(c, operario_id, dia, corte, minutos_pedidos):
    """El tiempo se reparte dentro del tramo: cada actividad empieza donde terminó la anterior."""
    usados = minutos_usados(c, operario_id, dia, corte["id"])
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
    return inicio, inicio + timedelta(minutes=minutos), minutos


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
    # Observación libre: pieza, cliente, "exportación"…
    observacion = str(data.get("observacion") or "").strip()[:200] or None

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
              AND lower(COALESCE(observacion, '')) = lower(%s)
        """, (operario_id, dia, corte_id, orden_id, actividad_id, observacion or ""))
        if c.fetchone():
            raise HTTPException(status_code=409, detail="Esa actividad ya está registrada para este operario en este corte. Bórrela y regístrela de nuevo para corregirla, o escriba una observación distinta si es otra pieza.")

        inicio, fin, minutos = ubicar_en_tramo(c, operario_id, dia, corte, minutos_pedidos)
        c.execute("""
            INSERT INTO registros_produccion
                (operario_id, orden_id, actividad_id, cantidad, inicio, fin, tiempo,
                 registrado_por, corte_id, fecha_corte, observacion)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (operario_id, orden_id, actividad_id, cantidad,
              inicio.strftime("%Y-%m-%d %H:%M:%S"), fin.strftime("%Y-%m-%d %H:%M:%S"), minutos * 60,
              supervisor["user_id"], corte_id, dia, observacion))
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


# ---- Desde la app: orden nueva, actividad que falta y registros por clasificar ----

def _entero(data, campo, nombre):
    try:
        return int(data.get(campo))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Falta {nombre}")


def _texto(data, campo, nombre, largo=80):
    valor = str(data.get(campo) or "").strip()[:largo]
    if not valor:
        raise HTTPException(status_code=400, detail=f"Escriba {nombre}")
    return valor


@router.get("/supervisor/productos")
def productos_supervision(supervisor=Depends(supervisor_android)):
    conn = db()
    c = conn.cursor()
    try:
        c.execute("SELECT id, nombre FROM maquinas WHERE COALESCE(activo, TRUE) = TRUE ORDER BY nombre")
        return [{"id": r[0], "nombre": r[1]} for r in c.fetchall()]
    finally:
        conn.close()


@router.post("/supervisor/orden")
def crear_orden_supervision(data: dict, supervisor=Depends(supervisor_android)):
    """Orden nueva desde la app, para el trabajo que llegó a planta sin orden (p. ej. despulpadoras cortadas ayer)."""
    maquina_id = _entero(data, "maquina_id", "el producto")
    cantidad = _entero(data, "cantidad", "la cantidad")
    if cantidad <= 0 or cantidad > 100000:
        raise HTTPException(status_code=400, detail="La cantidad debe ser mayor que 0")
    conn = db()
    c = conn.cursor()
    try:
        c.execute("SELECT nombre FROM maquinas WHERE id = %s AND COALESCE(activo, TRUE) = TRUE", (maquina_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="El producto no existe o está inactivo")
        # «incluir» (1.8): solo las actividades que faltan; sin él, todas (apps anteriores)
        incluir = data.get("incluir")
        if incluir is not None and not isinstance(incluir, list):
            raise HTTPException(status_code=400, detail="Lista de actividades inválida")
        orden_id = crear_orden(c, maquina_id, cantidad, data.get("nota"), incluir)
        c.execute("SELECT nota FROM ordenes WHERE id = %s", (orden_id,))
        nota = c.fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"id": orden_id, "etiqueta": etiqueta_orden(orden_id, row[0], nota), "maquina": row[0],
            "nota": nota or "", "cantidad": cantidad, "porcentaje": 0.0}


@router.get("/supervisor/actividades_producto/{maquina_id}")
def actividades_producto_supervision(maquina_id: int, supervisor=Depends(supervisor_android)):
    """Actividades del producto, para elegir las que faltan al crear la orden."""
    conn = db()
    try:
        return [{"id": r[0], "nombre": r[1], "proceso": r[2]} for r in actividades_del_producto(conn.cursor(), maquina_id)]
    finally:
        conn.close()


@router.get("/supervisor/procesos/{orden_id}")
def procesos_orden(orden_id: int, supervisor=Depends(supervisor_android)):
    """Procesos del producto de la orden (aunque todavía no tengan actividades), para agregar la que falta."""
    conn = db()
    c = conn.cursor()
    try:
        c.execute("""
            SELECT p.nombre FROM procesos p JOIN ordenes o ON o.maquina_id = p.maquina_id
            WHERE o.id = %s AND COALESCE(p.activo, TRUE) = TRUE ORDER BY p.id
        """, (orden_id,))
        return [r[0] for r in c.fetchall()]
    finally:
        conn.close()


@router.post("/supervisor/actividad")
def crear_actividad_supervision(data: dict, supervisor=Depends(supervisor_android)):
    """Actividad que falta en el producto de la orden. Queda sin estándar (no cuenta en la eficiencia
    ni en los bonos) hasta que se le pongan los minutos en Revisión de estándares."""
    orden_id = _entero(data, "orden_id", "la orden")
    proceso_nombre = _texto(data, "proceso", "el proceso")
    nombre = _texto(data, "nombre", "el nombre de la actividad")
    conn = db()
    c = conn.cursor()
    try:
        from routers.configuracion import sincronizar_secuencia
        asegurar_schema_cortes(c)
        c.execute("SELECT maquina_id FROM ordenes WHERE id = %s AND estado != 'CERRADA'", (orden_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="La orden no existe o está cerrada")
        maquina_id = row[0]
        c.execute("SELECT id, COALESCE(activo, TRUE) FROM procesos WHERE maquina_id = %s AND lower(trim(nombre)) = lower(%s) ORDER BY id LIMIT 1",
                  (maquina_id, proceso_nombre))
        proc = c.fetchone()
        if proc:
            proceso_id = proc[0]
            if not proc[1]:
                c.execute("UPDATE procesos SET activo = TRUE WHERE id = %s", (proceso_id,))
        else:
            sincronizar_secuencia(c, "procesos")
            c.execute("INSERT INTO procesos (maquina_id, nombre) VALUES (%s, %s) RETURNING id", (maquina_id, proceso_nombre))
            proceso_id = c.fetchone()[0]
        c.execute("SELECT id, COALESCE(activo, TRUE) FROM actividades WHERE proceso_id = %s AND lower(trim(nombre)) = lower(%s) ORDER BY id LIMIT 1",
                  (proceso_id, nombre))
        act = c.fetchone()
        if act:
            actividad_id = act[0]
            if not act[1]:
                c.execute("UPDATE actividades SET activo = TRUE WHERE id = %s", (actividad_id,))
        else:
            sincronizar_secuencia(c, "actividades")
            c.execute("INSERT INTO actividades (proceso_id, nombre, creada_en_app) VALUES (%s, %s, TRUE) RETURNING id",
                      (proceso_id, nombre))
            actividad_id = c.fetchone()[0]
        # Si se había dejado como «no aplica» en esta orden, se pidió explícitamente: vuelve a la orden
        c.execute("DELETE FROM orden_actividades_omitidas WHERE orden_id = %s AND actividad_id = %s", (orden_id, actividad_id))
        sincronizar_actividades_ordenes_abiertas(c)
        c.execute("SELECT nombre FROM procesos WHERE id = %s", (proceso_id,))
        proceso_real = c.fetchone()[0]
        c.execute("SELECT nombre FROM actividades WHERE id = %s", (actividad_id,))
        nombre_real = c.fetchone()[0]
        c.execute("SELECT cantidad_realizada, cantidad_total FROM orden_actividades WHERE orden_id = %s AND actividad_id = %s",
                  (orden_id, actividad_id))
        oa = c.fetchone() or (0, 0)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"id": actividad_id, "nombre": nombre_real, "proceso": proceso_real, "hechas": oa[0] or 0, "total": oa[1] or 0}


@router.post("/supervisor/por_clasificar")
def registrar_por_clasificar(data: dict, supervisor=Depends(supervisor_android)):
    """Guarda el tiempo del operario cuando no se sabe a qué orden va; se asigna después en la web."""
    operario_id = _entero(data, "operario_id", "el operario")
    corte_id = _entero(data, "corte_id", "la pasada")
    try:
        cantidad = int(data.get("cantidad") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Cantidad inválida")
    if cantidad < 0:
        raise HTTPException(status_code=400, detail="La cantidad no puede ser negativa")
    nota = _texto(data, "nota", "qué estaba haciendo (por ejemplo «soldando piezas de despulpadora»)", 200)
    dia = date.fromisoformat(data["fecha"]) if data.get("fecha") else hoy()
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
        inicio, fin, minutos = ubicar_en_tramo(c, operario_id, dia, corte, data.get("minutos"))
        c.execute("""
            INSERT INTO registros_por_clasificar
                (operario_id, corte_id, fecha_corte, cantidad, inicio, fin, tiempo, nota, registrado_por)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (operario_id, corte_id, dia, cantidad, inicio.strftime("%Y-%m-%d %H:%M:%S"),
              fin.strftime("%Y-%m-%d %H:%M:%S"), minutos * 60, nota, supervisor["user_id"]))
        nuevo = c.fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "id": -nuevo, "inicio": inicio.strftime("%H:%M"), "fin": fin.strftime("%H:%M"), "minutos": minutos}


@router.delete("/supervisor/por_clasificar/{registro_id}")
def borrar_por_clasificar(registro_id: int, supervisor=Depends(supervisor_android)):
    conn = db()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM registros_por_clasificar WHERE id = %s", (abs(registro_id),))
        if c.rowcount == 0:
            raise HTTPException(status_code=404, detail="Registro no encontrado")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ================= WEB: REGISTROS POR CLASIFICAR =================

def contar_por_clasificar(cursor):
    try:
        cursor.execute("SELECT COUNT(*) FROM registros_por_clasificar")
        return cursor.fetchone()[0]
    except Exception:
        cursor.connection.rollback()
        return 0


@router.get("/configuracion/por_clasificar", response_class=HTMLResponse)
def vista_por_clasificar(request: Request):
    if "username" not in request.session:
        return RedirectResponse("/admin?next=configuracion/por_clasificar", 303)
    if request.session.get("role") != "admin":
        return RedirectResponse("/", 303)
    conn = db()
    c = conn.cursor()
    try:
        asegurar_schema_cortes(c)
        conn.commit()
        c.execute("""
            SELECT r.id, op.nombre, r.fecha_corte, ch.nombre, r.inicio, r.fin, r.tiempo, r.cantidad, r.nota, u.username
            FROM registros_por_clasificar r
            JOIN operarios op ON op.id = r.operario_id
            LEFT JOIN cortes_horario ch ON ch.id = r.corte_id
            LEFT JOIN users u ON u.id = r.registrado_por
            ORDER BY r.fecha_corte, r.inicio, r.id
        """)
        filas = [{"id": r[0], "operario": r[1], "fecha": r[2].strftime("%d/%m"), "pasada": r[3] or "",
                  "horas": f"{(r[4] or '')[11:16]}–{(r[5] or '')[11:16]}", "minutos": round(r[6] / 60),
                  "cantidad": r[7] or 0, "nota": r[8] or "", "por": r[9] or ""} for r in c.fetchall()]
        c.execute("""
            SELECT o.id, m.nombre, o.nota FROM ordenes o LEFT JOIN maquinas m ON m.id = o.maquina_id
            WHERE o.estado != 'CERRADA' ORDER BY o.id DESC
        """)
        ordenes = [{"id": r[0], "etiqueta": etiqueta_orden(r[0], r[1], r[2])} for r in c.fetchall()]
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(
        request=request, name="por_clasificar.html",
        context={"request": request, "filas": filas, "ordenes": ordenes},
    )


@router.get("/config/por_clasificar/actividades/{orden_id}")
def actividades_para_clasificar(orden_id: int, request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=403)
    conn = db()
    c = conn.cursor()
    try:
        c.execute("""
            SELECT a.id, p.nombre, a.nombre, oa.cantidad_realizada, oa.cantidad_total
            FROM orden_actividades oa
            JOIN actividades a ON a.id = oa.actividad_id
            JOIN procesos p ON p.id = a.proceso_id
            WHERE oa.orden_id = %s AND COALESCE(a.activo, TRUE) = TRUE AND COALESCE(p.activo, TRUE) = TRUE
            ORDER BY p.id, a.id
        """, (orden_id,))
        return [{"id": r[0], "proceso": r[1], "nombre": r[2], "hechas": r[3] or 0, "total": r[4] or 0} for r in c.fetchall()]
    finally:
        conn.close()


@router.post("/config/por_clasificar/{registro_id}/asignar")
def asignar_por_clasificar(registro_id: int, data: dict, request: Request):
    """Pasa el registro a la orden y actividad elegidas: queda como un registro normal de esa pasada."""
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=403)
    orden_id = _entero(data, "orden_id", "la orden")
    actividad_id = _entero(data, "actividad_id", "la actividad")
    cantidad = _entero(data, "cantidad", "la cantidad")
    if cantidad < 0:
        raise HTTPException(status_code=400, detail="La cantidad no puede ser negativa")
    conn = db()
    c = conn.cursor()
    try:
        c.execute("""
            SELECT operario_id, corte_id, fecha_corte, inicio, fin, tiempo, nota, registrado_por
            FROM registros_por_clasificar WHERE id = %s FOR UPDATE
        """, (registro_id,))
        r = c.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Ese registro ya fue clasificado o borrado")
        c.execute("""
            SELECT 1 FROM orden_actividades oa JOIN ordenes o ON o.id = oa.orden_id
            WHERE oa.orden_id = %s AND oa.actividad_id = %s AND o.estado != 'CERRADA'
        """, (orden_id, actividad_id))
        if not c.fetchone():
            raise HTTPException(status_code=400, detail="La actividad no pertenece a esa orden abierta")
        c.execute("""
            INSERT INTO registros_produccion
                (operario_id, orden_id, actividad_id, cantidad, inicio, fin, tiempo,
                 registrado_por, corte_id, fecha_corte, observacion)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (r[0], orden_id, actividad_id, cantidad, r[3], r[4], r[5], r[7] or 0, r[1], r[2], (r[6] or "")[:200] or None))
        nuevo = c.fetchone()[0]
        aplicar_avance(c, orden_id, actividad_id, cantidad)
        c.execute("DELETE FROM registros_por_clasificar WHERE id = %s", (registro_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "id": nuevo}


@router.delete("/config/por_clasificar/{registro_id}")
def borrar_por_clasificar_web(registro_id: int, request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=403)
    conn = db()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM registros_por_clasificar WHERE id = %s", (registro_id,))
        conn.commit()
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
    c.execute("SELECT id, nombre, hora, COALESCE(activo, TRUE) FROM cortes_horario")
    cortes = [{"id": r[0], "nombre": r[1], "hora": r[2], "activo": r[3]} for r in c.fetchall()]
    c.execute("SELECT id, nombre, inicio, fin, COALESCE(activo, TRUE) FROM pausas_horario")
    pausas = [{"id": r[0], "nombre": r[1], "inicio": r[2], "fin": r[3], "activo": r[4]} for r in c.fetchall()]
    tramos = cortes_activos(c)
    conn.close()
    cortes.sort(key=lambda x: a_minutos(x["hora"]))
    pausas.sort(key=lambda x: a_minutos(x["inicio"]))
    return {"cortes": cortes, "pausas": pausas,
            "tramos": [{"id": t["id"], "desde": t["desde"], "minutos": t["minutos_tramo"], "pausas": t["pausas"]}
                       for t in tramos]}


def hora_valida(valor, campo="La hora"):
    hora = str(valor or "").strip()
    if len(hora) == 4:
        hora = "0" + hora
    if not HORA_VALIDA.match(hora):
        raise HTTPException(status_code=400, detail=f"{campo} debe tener el formato HH:MM (24 horas)")
    return hora


def validar_corte(data: dict):
    nombre = str(data.get("nombre", "")).strip()
    if not nombre:
        raise HTTPException(status_code=400, detail="El nombre del corte es obligatorio")
    return nombre, hora_valida(data.get("hora"))


def validar_pausa(data: dict):
    nombre = str(data.get("nombre", "")).strip()
    if not nombre:
        raise HTTPException(status_code=400, detail="El nombre del descanso es obligatorio")
    inicio = hora_valida(data.get("inicio"), "La hora de inicio")
    fin = hora_valida(data.get("fin"), "La hora de fin")
    if a_minutos(fin) <= a_minutos(inicio):
        raise HTTPException(status_code=400, detail="El descanso debe terminar después de empezar")
    return nombre, inicio, fin


@router.post("/config/cortes")
def crear_corte(data: dict, request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    nombre, hora = validar_corte(data)
    conn = db()
    c = conn.cursor()
    asegurar_schema_cortes(c)
    c.execute("INSERT INTO cortes_horario (nombre, hora) VALUES (%s, %s) RETURNING id", (nombre, hora))
    nuevo = c.fetchone()[0]
    conn.commit()
    conn.close()
    return {"ok": True, "id": nuevo}


@router.put("/config/cortes/{corte_id}")
def editar_corte(corte_id: int, data: dict, request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    nombre, hora = validar_corte(data)
    activo = bool(data.get("activo", True))
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE cortes_horario SET nombre=%s, hora=%s, activo=%s WHERE id=%s",
              (nombre, hora, activo, corte_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.post("/config/pausas")
def crear_pausa(data: dict, request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    nombre, inicio, fin = validar_pausa(data)
    conn = db()
    c = conn.cursor()
    asegurar_schema_cortes(c)
    c.execute("INSERT INTO pausas_horario (nombre, inicio, fin) VALUES (%s, %s, %s) RETURNING id", (nombre, inicio, fin))
    nuevo = c.fetchone()[0]
    conn.commit()
    conn.close()
    return {"ok": True, "id": nuevo}


@router.put("/config/pausas/{pausa_id}")
def editar_pausa(pausa_id: int, data: dict, request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    nombre, inicio, fin = validar_pausa(data)
    activo = bool(data.get("activo", True))
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE pausas_horario SET nombre=%s, inicio=%s, fin=%s, activo=%s WHERE id=%s",
              (nombre, inicio, fin, activo, pausa_id))
    conn.commit()
    conn.close()
    return {"ok": True}


# ================= WEB: PRODUCTO EN PROCESO =================

def producto_en_proceso(cursor):
    cursor.execute("""
        SELECT o.id, m.nombre, o.cantidad, COALESCE(o.porcentaje, 0),
               p.id, p.nombre, a.id, a.nombre, oa.cantidad_total, oa.cantidad_realizada, o.nota
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
            "id": r[0], "etiqueta": etiqueta_orden(r[0], r[1], r[10]), "cantidad": r[2],
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
