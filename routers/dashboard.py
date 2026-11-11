from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from auth import require_admin, require_operario
from routers.android import guardar_registro_android
from database import db, sincronizar_actividades_ordenes_abiertas
from datetime import datetime
from limiter import limiter

router = APIRouter()

@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not require_admin(request):
        return RedirectResponse("/admin", 303)

    rango = request.query_params.get("rango", "hoy")
    
    # Determinar filtros de fecha (asumiendo PostgreSQL, casteamos el texto a timestamp)
    if rango == 'ayer':
        date_filter = "inicio::timestamp >= CURRENT_DATE - INTERVAL '1 day' AND inicio::timestamp < CURRENT_DATE"
        prev_date_filter = "inicio::timestamp >= CURRENT_DATE - INTERVAL '2 days' AND inicio::timestamp < CURRENT_DATE - INTERVAL '1 day'"
    elif rango == '7dias':
        date_filter = "inicio::timestamp >= CURRENT_DATE - INTERVAL '7 days'"
        prev_date_filter = "inicio::timestamp >= CURRENT_DATE - INTERVAL '14 days' AND inicio::timestamp < CURRENT_DATE - INTERVAL '7 days'"
    elif rango == 'mes':
        date_filter = "inicio::timestamp >= DATE_TRUNC('month', CURRENT_DATE)"
        prev_date_filter = "inicio::timestamp >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month') AND inicio::timestamp < DATE_TRUNC('month', CURRENT_DATE)"
    else: # hoy
        rango = 'hoy'
        date_filter = "inicio::timestamp >= CURRENT_DATE"
        prev_date_filter = "inicio::timestamp >= CURRENT_DATE - INTERVAL '1 day' AND inicio::timestamp < CURRENT_DATE"

    conn = db()
    c = conn.cursor()

    # 1. Órdenes en producción (en este periodo)
    c.execute(f"SELECT COUNT(DISTINCT orden_id) FROM registros_produccion WHERE {date_filter}")
    ordenes_activas = c.fetchone()[0] or 0
    
    c.execute(f"SELECT COUNT(DISTINCT orden_id) FROM registros_produccion WHERE {prev_date_filter}")
    ordenes_prev = c.fetchone()[0] or 0
    
    tendencia_ordenes = 0
    if ordenes_prev > 0:
        tendencia_ordenes = round(((ordenes_activas - ordenes_prev) / ordenes_prev) * 100, 1)
    else:
        tendencia_ordenes = 100 if ordenes_activas > 0 else 0

    # 2. Producción del periodo
    c.execute(f"SELECT COALESCE(SUM(cantidad), 0) FROM registros_produccion WHERE {date_filter}")
    produccion_hoy = c.fetchone()[0]

    c.execute(f"SELECT COALESCE(SUM(cantidad), 0) FROM registros_produccion WHERE {prev_date_filter}")
    produccion_prev = c.fetchone()[0]

    tendencia_prod = 0
    if produccion_prev > 0:
        tendencia_prod = round(((produccion_hoy - produccion_prev) / produccion_prev) * 100, 1)
    else:
        tendencia_prod = 100 if produccion_hoy > 0 else 0

    # 3. Avance Global de Órdenes Abiertas
    c.execute("""
        SELECT COALESCE(SUM(cantidad_realizada), 0), COALESCE(SUM(cantidad_total), 0)
        FROM orden_actividades
        WHERE orden_id IN (SELECT id FROM ordenes WHERE estado != 'CERRADA')
    """)
    row_avance = c.fetchone()
    if row_avance and row_avance[1] > 0:
        avance_promedio = round((row_avance[0] / row_avance[1]) * 100, 1)
    else:
        avance_promedio = 0

    # 4. Tickets Abiertos (en lugar de Operarios Activos o como complemento)
    c.execute("SELECT COUNT(*) FROM tickets WHERE estado != 'Cerrado'")
    tickets_abiertos = c.fetchone()[0] or 0

    # Gráfico: Últimos 7 días
    c.execute("""
        SELECT DATE(inicio::timestamp) as fecha, COALESCE(SUM(cantidad), 0)
        FROM registros_produccion 
        WHERE inicio::timestamp >= CURRENT_DATE - INTERVAL '6 days'
        GROUP BY DATE(inicio::timestamp)
        ORDER BY fecha
    """)
    chart_rows = c.fetchall()
    # Preparar datos para Chart.js
    fechas = [r[0].strftime('%d/%m') for r in chart_rows]
    cantidades = [r[1] for r in chart_rows]

    # Alertas y Notificaciones Contextuales
    # Última orden
    c.execute("""
        SELECT o.id, m.nombre, o.cantidad
        FROM registros_produccion rp
        JOIN ordenes o ON o.id = rp.orden_id
        JOIN maquinas m ON m.id = o.maquina_id
        ORDER BY rp.fin DESC NULLS LAST
        LIMIT 1
    """)
    ultima_orden = c.fetchone()
    if ultima_orden:
        ultima_orden = {"id": ultima_orden[0], "maquina": ultima_orden[1], "cantidad": ultima_orden[2]}

    # Tickets recientes
    c.execute("""
        SELECT id, titulo, prioridad 
        FROM tickets 
        WHERE estado != 'Cerrado' 
        ORDER BY CASE WHEN prioridad = 'Alta' THEN 1 ELSE 2 END, id DESC 
        LIMIT 3
    """)
    tickets_recientes = [{"id": r[0], "titulo": r[1], "prioridad": r[2]} for r in c.fetchall()]

    # 5. Top Operarios
    c.execute(f"""
        SELECT o.nombre, COALESCE(SUM(rp.cantidad), 0) as total_prod
        FROM registros_produccion rp
        JOIN operarios o ON o.id = rp.operario_id
        WHERE {date_filter}
        GROUP BY o.nombre
        ORDER BY total_prod DESC
        LIMIT 5
    """)
    top_operarios = [{"nombre": r[0], "produccion": r[1]} for r in c.fetchall()]

    # 6. Órdenes con Mayor Avance (en el periodo)
    c.execute(f"""
        SELECT o.id, m.nombre, o.cantidad, COALESCE(SUM(rp.cantidad), 0) as avance_periodo
        FROM registros_produccion rp
        JOIN ordenes o ON o.id = rp.orden_id
        JOIN maquinas m ON m.id = o.maquina_id
        WHERE {date_filter}
        GROUP BY o.id, m.nombre, o.cantidad
        HAVING o.cantidad > 0
        ORDER BY (SUM(rp.cantidad)*1.0 / o.cantidad) DESC
        LIMIT 5
    """)
    top_ordenes = []
    for row in c.fetchall():
        pct = round((row[3] / row[2]) * 100, 1) if row[2] > 0 else 0
        top_ordenes.append({
            "id": row[0],
            "maquina": row[1],
            "cantidad": row[2],
            "realizada": row[3],
            "porcentaje": pct
        })

    conn.close()

    return request.app.state.templates.TemplateResponse(
        request=request, name="home.html", context={
        "request": request,
        "rango": rango,
        "ordenes_activas": ordenes_activas,
        "tendencia_ordenes": tendencia_ordenes,
        "produccion_hoy": produccion_hoy,
        "tendencia_prod": tendencia_prod,
        "avance_promedio": avance_promedio,
        "tickets_abiertos": tickets_abiertos,
        "chart_fechas": fechas,
        "chart_cantidades": cantidades,
        "ultima_orden": ultima_orden,
        "tickets_recientes": tickets_recientes,
        "top_operarios": top_operarios,
        "top_ordenes": top_ordenes
    })


# ================= PANEL =================

@router.get("/panel", response_class=HTMLResponse)
def panel(request: Request):

    if not require_admin(request):
        return RedirectResponse("/admin", 303)

    conn = db()
    c = conn.cursor()
    sincronizar_actividades_ordenes_abiertas(c)
    conn.commit()

    c.execute("SELECT id, nombre FROM maquinas")
    maquinas = c.fetchall()

    c.execute("""
        SELECT o.id, m.nombre, o.cantidad, o.estado, o.cerrado_en, o.nota
        FROM ordenes o
        JOIN maquinas m ON m.id = o.maquina_id
        ORDER BY o.id DESC
    """)
    ordenes_sql = c.fetchall()

    ordenes = []

    for o in ordenes_sql:
        oid = o[0]

        c.execute("""
            SELECT SUM(cantidad_realizada), SUM(cantidad_total)
            FROM orden_actividades
            WHERE orden_id = %s
        """, (oid,))

        row_pct = c.fetchone()

        if row_pct and row_pct[1] and row_pct[1] > 0:
            porcentaje_general = round((row_pct[0] / row_pct[1]) * 100, 2)
        else:
            porcentaje_general = 0

        c.execute("""
            SELECT
                p.nombre,
                a.nombre,
                oa.cantidad_realizada,
                oa.cantidad_total
            FROM orden_actividades oa
            JOIN actividades a ON a.id = oa.actividad_id
            JOIN procesos p ON p.id = a.proceso_id
            WHERE oa.orden_id = %s
            ORDER BY p.id
        """, (oid,))
        acts = c.fetchall()

        procesos = {}
        for pr, act, real, total in acts:
            if pr not in procesos:
                procesos[pr] = []
            pct = int((real/total)*100) if total else 0
            procesos[pr].append({
                "nombre": act,
                "realizada": real,
                "total": total,
                "porcentaje": pct
            })

        ordenes.append({
            "id": oid,
            "producto": o[1],
            "cantidad": o[2],
            "estado": o[3],
            "porcentaje": porcentaje_general,
            "cerrado_en": o[4],
            "nota": o[5] or "",
            "procesos": [
                {"nombre": k, "actividades": v}
                for k, v in procesos.items()
            ]
        })

    conn.close()

    return request.app.state.templates.TemplateResponse(
        request=request, name="panel.html", context={
        "request": request,
        "maquinas": maquinas,
        "ordenes": ordenes
    })


# ================= METRICAS OPERARIOS =================

@router.get("/metricas_operarios", response_class=HTMLResponse)
def metricas_operarios(request: Request):

    conn = db()
    c = conn.cursor()

    semanas = metricas_semanales(c)

    conn.close()

    return request.app.state.templates.TemplateResponse(
        request=request, name="Metricas.html", context={
        "request": request,
        "semanas": semanas
    })


# ================= CERRAR ORDEN =================

@router.get("/cerrar/{orden_id}")
def cerrar_orden(orden_id: int, request: Request):

    if not require_admin(request):
        return RedirectResponse("/admin", 303)

    conn = db()
    c = conn.cursor()

    c.execute("""
        UPDATE orden_actividades
        SET cantidad_realizada = cantidad_total
        WHERE orden_id=%s
    """, (orden_id,))

    c.execute("""
        UPDATE ordenes
        SET estado='CERRADA',
            cerrado_en=%s
        WHERE id=%s
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), orden_id))
    
    conn.commit()
    conn.close()
    
    return RedirectResponse("/panel", 303)

# ================= ELIMINAR ORDEN =================

@router.post("/eliminar/{id}")
def eliminar(id: int):

    conn = db()
    c = conn.cursor()

    c.execute("DELETE FROM orden_actividades WHERE orden_id = %s", (id,))
    c.execute("DELETE FROM ordenes WHERE id = %s", (id,))

    conn.commit()
    conn.close()

    return RedirectResponse("/panel", 303)


@router.post("/crear_orden_web")
def crear_orden_web(cantidad: int = Form(...), maquina: int = Form(...), nota: str = Form("")):

    conn = db()
    c = conn.cursor()

    # La nota distingue órdenes del mismo producto (p. ej. "Exportación"); se ve en la app de cortes
    c.execute("""
        INSERT INTO ordenes(maquina_id, cantidad, estado, nota)
        VALUES (%s, %s, 'ABIERTA', %s) RETURNING id
    """, (maquina, cantidad, nota.strip()[:80] or None))

    orden_id = c.fetchone()[0]

    c.execute("""
        SELECT a.id
        FROM actividades a
        JOIN procesos p ON a.proceso_id = p.id
        WHERE p.maquina_id = %s
          AND COALESCE(p.activo, TRUE) = TRUE AND COALESCE(a.activo, TRUE) = TRUE
    """, (maquina,))
    acts = c.fetchall()

    for a in acts:
        c.execute("""
            INSERT INTO orden_actividades
            (orden_id, actividad_id, cantidad_total, cantidad_realizada)
            VALUES (%s, %s, %s, 0)
        """, (orden_id, a[0], cantidad))

    conn.commit()
    conn.close()

    return RedirectResponse("/panel", 303)


@router.get("/sincronizar_ordenes_abiertas")
def sincronizar_ordenes_abiertas_web(request: Request):

    if not require_admin(request):
        return RedirectResponse("/admin", 303)

    conn = db()
    c = conn.cursor()
    insertadas = sincronizar_actividades_ordenes_abiertas(c)
    conn.commit()
    conn.close()

    return RedirectResponse(f"/panel?sync={insertadas}", 303)


@router.get("/inicio_operario", response_class=HTMLResponse)
def inicio_operario(request: Request):
    if request.session.get("role") != "operario":
        return RedirectResponse("/admin", 303)
    return request.app.state.templates.TemplateResponse(
        request=request, name="inicio_operario.html", context={"request": request}
    )


@router.get("/registro_web", response_class=HTMLResponse)
def registro_web(request: Request):
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="registro_web.html",
        context={
            "request": request,
            "operario_actual": {
                "id": request.session.get("operario_id"),
                "nombre": request.session.get("operario_nombre") or request.session.get("username"),
            },
        },
    )


@router.post("/registro_web/registro")
def registro_web_guardar(data: dict, request: Request):
    if not require_operario(request):
        return JSONResponse(
            {"detail": "Debe iniciar sesion como operario"},
            status_code=401,
        )

    return guardar_registro_android(data, request.session["operario_id"])


# ================= REGISTRO =================

@router.post("/registro")
def registro(data: dict):

    inicio = datetime.fromisoformat(data["inicio"])
    fin = datetime.fromisoformat(data["fin"])
    tiempo = int((fin - inicio).total_seconds())

    conn = db()
    c = conn.cursor()

    c.execute("""
        INSERT INTO registros_produccion
        (operario_id, orden_id, actividad_id, cantidad, inicio, fin, tiempo)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (
        data["operario_id"],
        data["orden_id"],
        data["actividad_id"],
        data["cantidad"],
        data["inicio"],
        data["fin"],
        tiempo
    ))

    c.execute("""
        UPDATE orden_actividades
        SET cantidad_realizada = cantidad_realizada + %s
        WHERE orden_id = %s AND actividad_id = %s
    """, (data["cantidad"], data["orden_id"], data["actividad_id"]))

    from database import recalcular_porcentaje_orden
    porcentaje = recalcular_porcentaje_orden(c, data["orden_id"])

    conn.commit()
    conn.close()

    return {"ok": True, "porcentaje": porcentaje}


# ================= EXPORTAR =================

