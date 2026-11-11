from collections import OrderedDict
from datetime import date, datetime, time, timedelta
from tempfile import gettempdir
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from database import db
from routers.cortes import cortes_activos, hoy as hoy_local, TZ

router = APIRouter()


def _normalizar_fecha(fecha):
    if isinstance(fecha, str):
        return datetime.fromisoformat(fecha[:10]).date()
    return fecha


def _fecha_corta(fecha):
    return fecha.strftime("%d/%m/%Y") if hasattr(fecha, "strftime") else str(fecha)


def _parsear_fecha_periodo(periodo, fecha):
    hoy = hoy_local()

    if periodo not in ("diario", "semanal", "mensual"):
        raise HTTPException(status_code=400, detail="Periodo no valido")

    try:
        if not fecha:
            base = hoy
        elif periodo == "mensual" and len(fecha) == 7:
            base = datetime.strptime(f"{fecha}-01", "%Y-%m-%d").date()
        else:
            base = datetime.strptime(fecha[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Fecha no valida") from exc

    if periodo == "diario":
        inicio = base
        fin = inicio + timedelta(days=1)
        etiqueta = _fecha_corta(inicio)
    elif periodo == "semanal":
        inicio = base - timedelta(days=base.weekday())
        fin = inicio + timedelta(days=7)
        etiqueta = f"{_fecha_corta(inicio)} - {_fecha_corta(fin - timedelta(days=1))}"
    else:
        inicio = base.replace(day=1)
        if inicio.month == 12:
            fin = inicio.replace(year=inicio.year + 1, month=1)
        else:
            fin = inicio.replace(month=inicio.month + 1)
        etiqueta = inicio.strftime("%m/%Y")

    return {
        "periodo": periodo,
        "inicio": datetime.combine(inicio, time.min),
        "fin": datetime.combine(fin, time.min),
        "etiqueta": etiqueta,
    }


def _agregar_fila(sheet, row, bold=False, fill=None):
    sheet.append(row)
    if bold or fill:
        for cell in sheet[sheet.max_row]:
            if bold:
                cell.font = Font(bold=True)
            if fill:
                cell.fill = fill


def _ajustar_columnas(sheet):
    for col in sheet.columns:
        max_len = 0
        letter = get_column_letter(col[0].column)
        for cell in col:
            value = "" if cell.value is None else str(cell.value)
            max_len = max(max_len, len(value))
            cell.alignment = Alignment(vertical="top")
        sheet.column_dimensions[letter].width = min(max(max_len + 2, 12), 42)


def _aplicar_estilo_tabla(sheet):
    header_fill = PatternFill("solid", fgColor="DDE7EE")
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    sheet.freeze_panes = "A2"
    _ajustar_columnas(sheet)


NOMBRES_DIA = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


def lunes_de(dia):
    return dia - timedelta(days=dia.weekday())


def minutos_jornada(cursor):
    """Minutos de trabajo de un día según el horario de pasadas (sin descansos)."""
    try:
        total = sum(c["minutos_tramo"] for c in cortes_activos(cursor))
    except Exception:
        total = 0
    return total or 480


def semanas_con_datos(cursor):
    cursor.execute("""
        SELECT DISTINCT DATE_TRUNC('week', inicio::timestamp)::date AS semana
        FROM registros_produccion
        ORDER BY semana DESC
        LIMIT 60
    """)
    return [_normalizar_fecha(r[0]) for r in cursor.fetchall()]


def _pct(parte, total):
    return round(parte / total * 100, 1) if total else None


def metricas_semana(cursor, lunes):
    """Métricas de una semana (lunes a domingo).

    - Horas: tiempo real de cada registro (fin - inicio).
    - Eficiencia: horas estándar / horas reales, solo sobre actividades que tienen
      tiempo estándar (las demás no se cuentan, para no bajarla injustamente).
    - Tiempo registrado: horas registradas / horas disponibles de los días que el operario trabajó
      (la jornada sale del horario de pasadas, sin descansos).
    """
    inicio = datetime.combine(lunes, time.min)
    fin = inicio + timedelta(days=7)
    cursor.execute("""
        SELECT r.id, op.id, op.nombre, a.nombre, COALESCE(m.nombre, ''), r.cantidad,
               r.inicio::timestamp, r.fin::timestamp, e.unidades_por_hora,
               COALESCE(r.observacion, ''), COALESCE(ch.nombre, ''), r.orden_id
        FROM registros_produccion r
        JOIN operarios op ON op.id = r.operario_id
        JOIN actividades a ON a.id = r.actividad_id
        LEFT JOIN LATERAL (
            SELECT unidades_por_hora FROM estandares_actividad
            WHERE actividad_id = a.id ORDER BY id LIMIT 1
        ) e ON TRUE
        LEFT JOIN ordenes o ON o.id = r.orden_id
        LEFT JOIN maquinas m ON m.id = o.maquina_id
        LEFT JOIN cortes_horario ch ON ch.id = r.corte_id
        WHERE r.inicio::timestamp >= %s AND r.inicio::timestamp < %s
        ORDER BY r.inicio::timestamp, r.id
    """, (inicio, fin))
    filas = cursor.fetchall()
    jornada_h = minutos_jornada(cursor) / 60

    def acumulador():
        return {"unidades": 0, "horas": 0.0, "horas_con_std": 0.0, "horas_std": 0.0, "registros": 0}

    def sumar(acc, cantidad, horas, h_std):
        acc["unidades"] += cantidad
        acc["horas"] += horas
        acc["registros"] += 1
        if h_std is not None:
            acc["horas_con_std"] += horas
            acc["horas_std"] += h_std

    total = acumulador()
    dias = OrderedDict((lunes + timedelta(days=i), acumulador()) for i in range(7))
    operarios, actividades = {}, {}
    sin_estandar = set()

    for (rid, op_id, op_nombre, actividad, maquina, cantidad, ini, fin_r, uph,
         observacion, pasada, orden_id) in filas:
        cantidad = int(cantidad or 0)
        horas = max((fin_r - ini).total_seconds() / 3600, 0) if ini and fin_r else 0
        h_std = cantidad / float(uph) if uph and float(uph) > 0 else None
        if h_std is None:
            sin_estandar.add(actividad)
        sumar(total, cantidad, horas, h_std)
        sumar(dias[ini.date()], cantidad, horas, h_std)

        op = operarios.setdefault(op_id, {"nombre": op_nombre, "dias": set(), "detalle": [], **acumulador()})
        sumar(op, cantidad, horas, h_std)
        op["dias"].add(ini.date())
        op["detalle"].append({
            "dia": f"{NOMBRES_DIA[ini.weekday()]} {ini:%d/%m}",
            "hora": f"{ini:%H:%M}–{fin_r:%H:%M}",
            "pasada": pasada,
            "actividad": actividad,
            "maquina": maquina,
            "orden_id": orden_id,
            "cantidad": cantidad,
            "horas": round(horas, 2),
            "eficiencia": _pct(h_std, horas) if h_std is not None else None,
            "observacion": observacion,
        })

        act = actividades.setdefault(actividad, {"nombre": actividad, "maquina": maquina, **acumulador()})
        sumar(act, cantidad, horas, h_std)

    def cerrar(acc, disponibles=None):
        acc["horas"] = round(acc["horas"], 1)
        acc["eficiencia"] = _pct(acc["horas_std"], acc["horas_con_std"])
        acc["cobertura"] = _pct(acc["horas_con_std"], acc["horas"])
        acc["productividad"] = round(acc["unidades"] / acc["horas"], 2) if acc["horas"] else 0
        if disponibles is not None:
            acc["disponibles"] = round(disponibles, 1)
            acc["registrado"] = _pct(acc["horas"], disponibles)
        return acc

    lista_ops = []
    disponibles_total = 0
    for op in operarios.values():
        disp = len(op["dias"]) * jornada_h
        disponibles_total += disp
        op["dias_trabajados"] = len(op.pop("dias"))
        lista_ops.append(cerrar(op, disp))
    lista_ops.sort(key=lambda o: (-o["horas"], o["nombre"]))

    serie = []
    for i, (dia, acc) in enumerate(dias.items()):
        if i >= 5 and not acc["registros"]:
            continue  # fin de semana sin trabajo: no se muestra
        cerrar(acc)
        serie.append({"etiqueta": f"{NOMBRES_DIA[i]} {dia:%d/%m}", **acc})

    lista_act = sorted(actividades.values(), key=lambda a: -a["horas"])
    for a in lista_act:
        cerrar(a)

    return {
        "lunes": lunes,
        "domingo": lunes + timedelta(days=6),
        "etiqueta": f"{_fecha_corta(lunes)} – {_fecha_corta(lunes + timedelta(days=6))}",
        "total": cerrar(total, disponibles_total),
        "dias": serie,
        "operarios": lista_ops,
        "actividades": lista_act[:10],
        "sin_estandar": sorted(sin_estandar),
        "jornada_h": round(jornada_h, 1),
    }


def _horas_productividad(segundos, unidades):
    segundos = float(segundos or 0)
    unidades = int(unidades or 0)
    horas = round(segundos / 3600, 2) if segundos else 0
    productividad = round(unidades / horas, 2) if horas else 0
    return horas, productividad


def _crear_excel_metricas(cursor, filtro):
    inicio = filtro["inicio"]
    fin = filtro["fin"]
    params = (inicio, fin)

    cursor.execute("""
        SELECT
            op.nombre,
            COALESCE(SUM(r.cantidad), 0) AS unidades,
            COALESCE(SUM(EXTRACT(EPOCH FROM (r.fin::timestamp - r.inicio::timestamp))), 0) AS segundos,
            COUNT(r.id) AS operaciones
        FROM registros_produccion r
        JOIN operarios op ON op.id = r.operario_id
        WHERE r.inicio::timestamp >= %s
        AND r.inicio::timestamp < %s
        GROUP BY op.id, op.nombre
        ORDER BY unidades DESC, op.nombre
    """, params)
    resumen_operarios = cursor.fetchall()

    cursor.execute("""
        SELECT
            r.inicio::date AS fecha,
            op.nombre,
            COALESCE(SUM(r.cantidad), 0) AS unidades,
            COALESCE(SUM(EXTRACT(EPOCH FROM (r.fin::timestamp - r.inicio::timestamp))), 0) AS segundos,
            COUNT(r.id) AS operaciones
        FROM registros_produccion r
        JOIN operarios op ON op.id = r.operario_id
        WHERE r.inicio::timestamp >= %s
        AND r.inicio::timestamp < %s
        GROUP BY r.inicio::date, op.id, op.nombre
        ORDER BY fecha, op.nombre
    """, params)
    resumen_diario = cursor.fetchall()

    cursor.execute("""
        SELECT
            a.nombre,
            op.nombre,
            COALESCE(SUM(r.cantidad), 0) AS unidades,
            COALESCE(SUM(EXTRACT(EPOCH FROM (r.fin::timestamp - r.inicio::timestamp))), 0) AS segundos,
            COUNT(r.id) AS operaciones
        FROM registros_produccion r
        JOIN operarios op ON op.id = r.operario_id
        JOIN actividades a ON a.id = r.actividad_id
        WHERE r.inicio::timestamp >= %s
        AND r.inicio::timestamp < %s
        GROUP BY a.id, a.nombre, op.id, op.nombre
        ORDER BY a.nombre, unidades DESC, op.nombre
    """, params)
    resumen_actividad = cursor.fetchall()

    cursor.execute("""
        SELECT
            r.id,
            r.inicio::date AS fecha,
            op.nombre AS operario,
            COALESCE(m.nombre, '') AS maquina,
            r.orden_id,
            COALESCE(p.nombre, '') AS proceso,
            a.nombre AS actividad,
            r.cantidad,
            r.inicio,
            r.fin,
            ROUND((EXTRACT(EPOCH FROM (r.fin::timestamp - r.inicio::timestamp)) / 3600.0)::numeric, 2) AS horas,
            CASE WHEN e.unidades_por_hora > 0 THEN ROUND((r.cantidad::numeric / e.unidades_por_hora::numeric), 2) END AS horas_estandar,
            COALESCE(ch.nombre, '') AS pasada,
            COALESCE(r.observacion, '') AS observacion
        FROM registros_produccion r
        JOIN operarios op ON op.id = r.operario_id
        JOIN actividades a ON a.id = r.actividad_id
        LEFT JOIN LATERAL (
            SELECT unidades_por_hora FROM estandares_actividad
            WHERE actividad_id = a.id ORDER BY id LIMIT 1
        ) e ON TRUE
        LEFT JOIN cortes_horario ch ON ch.id = r.corte_id
        LEFT JOIN procesos p ON p.id = a.proceso_id
        LEFT JOIN ordenes o ON o.id = r.orden_id
        LEFT JOIN maquinas m ON m.id = o.maquina_id
        WHERE r.inicio::timestamp >= %s
        AND r.inicio::timestamp < %s
        ORDER BY r.inicio::timestamp, op.nombre, r.id
    """, params)
    registros = cursor.fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Resumen"

    total_unidades = sum(int(row[1] or 0) for row in resumen_operarios)
    total_segundos = sum(float(row[2] or 0) for row in resumen_operarios)
    total_operaciones = sum(int(row[3] or 0) for row in resumen_operarios)
    total_horas, total_productividad = _horas_productividad(total_segundos, total_unidades)

    titulo = f"Metricas {filtro['periodo']} - {filtro['etiqueta']}"
    ws.append([titulo])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["Desde", inicio.strftime("%Y-%m-%d %H:%M")])
    ws.append(["Hasta", (fin - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M")])
    ws.append([])
    ws.append(["Total unidades", "Total horas", "Unid/Hora", "Operaciones"])
    ws.append([total_unidades, total_horas, total_productividad, total_operaciones])
    ws.append([])
    _agregar_fila(ws, ["Operario", "Unidades", "Horas", "Unid/Hora", "Operaciones"], bold=True)

    for nombre, unidades, segundos, operaciones in resumen_operarios:
        horas, productividad = _horas_productividad(segundos, unidades)
        ws.append([nombre, int(unidades or 0), horas, productividad, int(operaciones or 0)])

    ws_por_dia = wb.create_sheet("Por dia")
    _agregar_fila(ws_por_dia, ["Fecha", "Operario", "Unidades", "Horas", "Unid/Hora", "Operaciones"], bold=True)
    for fecha, operario, unidades, segundos, operaciones in resumen_diario:
        horas, productividad = _horas_productividad(segundos, unidades)
        ws_por_dia.append([fecha, operario, int(unidades or 0), horas, productividad, int(operaciones or 0)])

    ws_actividad = wb.create_sheet("Por actividad")
    _agregar_fila(ws_actividad, ["Actividad", "Operario", "Unidades", "Horas", "Unid/Hora", "Operaciones"], bold=True)
    for actividad, operario, unidades, segundos, operaciones in resumen_actividad:
        horas, productividad = _horas_productividad(segundos, unidades)
        ws_actividad.append([actividad, operario, int(unidades or 0), horas, productividad, int(operaciones or 0)])

    ws_detalle = wb.create_sheet("Registros")
    _agregar_fila(
        ws_detalle,
        ["ID", "Fecha", "Pasada", "Operario", "Maquina", "Orden", "Proceso", "Actividad", "Cantidad", "Inicio", "Fin", "Horas", "Unid/Hora", "Eficiencia %", "Observacion"],
        bold=True,
    )
    for (registro_id, fecha, operario, maquina, orden_id, proceso, actividad, cantidad, inicio_reg, fin_reg,
         horas, horas_estandar, pasada, observacion) in registros:
        horas = float(horas or 0)
        cantidad = int(cantidad or 0)
        productividad = round(cantidad / horas, 2) if horas else 0
        # Sin tiempo estándar la eficiencia queda vacía (no es 0 %)
        eficiencia = round((float(horas_estandar) / horas) * 100, 1) if horas_estandar is not None and horas > 0 else ""
        ws_detalle.append([
            registro_id, fecha, pasada, operario, maquina, orden_id, proceso, actividad, cantidad,
            inicio_reg, fin_reg, horas, productividad, eficiencia, observacion,
        ])

    for sheet in wb.worksheets:
        _ajustar_columnas(sheet)

    header_fill = PatternFill("solid", fgColor="DDE7EE")
    for sheet in (ws_por_dia, ws_actividad, ws_detalle):
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")
        sheet.freeze_panes = "A2"

    for cell in ws[8]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    nombre_base = f"metricas_{filtro['periodo']}_{inicio.strftime('%Y%m%d')}_{(fin - timedelta(days=1)).strftime('%Y%m%d')}"
    nombre_archivo = f"{nombre_base}.xlsx"
    ruta = Path(gettempdir()) / f"{nombre_base}_{datetime.now().strftime('%H%M%S%f')}.xlsx"
    wb.save(ruta)

    return ruta, nombre_archivo


@router.get("/metricas_operarios", response_class=HTMLResponse)
@router.get("/metricas", response_class=HTMLResponse)
def metricas_operarios(request: Request, semana: str = None):
    if not request.session.get("role") == "admin":
        return RedirectResponse("/admin", 303)

    actual = lunes_de(hoy_local())
    try:
        lunes = lunes_de(datetime.strptime(semana[:10], "%Y-%m-%d").date()) if semana else actual
    except ValueError:
        lunes = actual

    conn = db()
    c = conn.cursor()
    datos = metricas_semana(c, lunes)
    semanas = semanas_con_datos(c)
    conn.close()

    if actual not in semanas:
        semanas.insert(0, actual)
    if lunes not in semanas:
        semanas.append(lunes)
        semanas.sort(reverse=True)
    ultima_con_datos = next((s for s in semanas if s != lunes and s <= actual), None)

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="Metricas.html",
        context={
            "request": request,
            "m": datos,
            "semanas": [{"valor": s.isoformat(), "etiqueta": f"{_fecha_corta(s)} – {_fecha_corta(s + timedelta(days=6))}"} for s in semanas],
            "semana_actual": actual.isoformat(),
            "anterior": (lunes - timedelta(days=7)).isoformat(),
            "siguiente": (lunes + timedelta(days=7)).isoformat() if lunes < actual else None,
            "ultima_con_datos": ultima_con_datos.isoformat() if ultima_con_datos else None,
            "hoy": hoy_local().isoformat(),
        },
    )


@router.get("/informes_gerenciales")
def informes_gerenciales(request: Request):
    # Los informes quedaron dentro de Métricas
    return RedirectResponse("/metricas_operarios", 303)


@router.get("/metricas/exportar_excel")
@router.get("/exportar_excel")
def exportar_metricas_excel(request: Request, periodo: str = "semanal", fecha: str = None):
    if not request.session.get("role") == "admin":
        return RedirectResponse("/admin", 303)

    filtro = _parsear_fecha_periodo(periodo, fecha)

    conn = db()
    c = conn.cursor()
    ruta, nombre_archivo = _crear_excel_metricas(c, filtro)
    conn.close()

    return FileResponse(
        path=str(ruta),
        filename=nombre_archivo,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/wip", response_class=HTMLResponse)
def wip_cuellos_botella(request: Request):
    if not request.session.get("role") == "admin":
        return RedirectResponse("/admin", 303)

    conn = db()
    c = conn.cursor()

    # Tendencia Produccion (Hoy vs Ayer)
    c.execute("""
        SELECT 
            COALESCE(SUM(CASE WHEN DATE(inicio) = CURRENT_DATE THEN cantidad ELSE 0 END), 0) as hoy,
            COALESCE(SUM(CASE WHEN DATE(inicio) = CURRENT_DATE - INTERVAL '1 day' THEN cantidad ELSE 0 END), 0) as ayer
        FROM registros_produccion
    """)
    row_tendencia = c.fetchone()
    prod_hoy = row_tendencia[0] if row_tendencia else 0
    prod_ayer = row_tendencia[1] if row_tendencia else 0
    
    tendencia_pct = 0
    if prod_ayer > 0:
        tendencia_pct = round(((prod_hoy - prod_ayer) / prod_ayer) * 100, 1)

    # 1. Cuellos de Botella (WIP por actividad) evitando doble conteo
    c.execute("""
        WITH OrderBottleneck AS (
            SELECT oa.orden_id,
                   MIN(oa.actividad_id) as cuello_actividad_id,
                   MAX(oa.cantidad_total - oa.cantidad_realizada) as pendiente
            FROM orden_actividades oa
            WHERE oa.cantidad_realizada < oa.cantidad_total
            GROUP BY oa.orden_id
        )
        SELECT a.nombre, SUM(ob.pendiente) as WIP
        FROM OrderBottleneck ob
        JOIN actividades a ON ob.cuello_actividad_id = a.id
        JOIN ordenes o ON ob.orden_id = o.id
        JOIN maquinas m ON m.id = o.maquina_id
        WHERE o.estado != 'CERRADA' 
          AND m.nombre NOT ILIKE '%admin%'
          AND m.nombre NOT ILIKE '%apoyo%'
        GROUP BY a.nombre
        ORDER BY WIP DESC
        LIMIT 8
    """)
    wip_actividades = [{"actividad": row[0], "wip": row[1]} for row in c.fetchall()]

    # Print de control para auditoría
    print(f"DEBUG WIP - Se han encontrado {len(wip_actividades)} cuellos de botella (Top 8).")
    if wip_actividades:
        print(f"DEBUG WIP - Peor cuello: {wip_actividades[0]['actividad']} con {wip_actividades[0]['wip']} unidades.")

    # 2. Órdenes Activas Críticas con Semáforo
    c.execute("""
        WITH LastAction AS (
            SELECT orden_id, MAX(inicio::timestamp) as ultimo_movimiento
            FROM registros_produccion
            GROUP BY orden_id
        ),
        OrderBottleneck AS (
            SELECT oa.orden_id, MIN(oa.actividad_id) as cuello_actividad_id
            FROM orden_actividades oa
            WHERE oa.cantidad_realizada < oa.cantidad_total
            GROUP BY oa.orden_id
        )
        SELECT o.id, m.nombre, o.cantidad, o.estado,
               COALESCE(SUM(oa.cantidad_realizada), 0) as total_hecho,
               COALESCE(SUM(oa.cantidad_total), 0) as total_planeado,
               la.ultimo_movimiento,
               a_cuello.nombre as actividad_atascada
        FROM ordenes o
        JOIN maquinas m ON m.id = o.maquina_id
        LEFT JOIN orden_actividades oa ON oa.orden_id = o.id
        LEFT JOIN LastAction la ON la.orden_id = o.id
        LEFT JOIN OrderBottleneck ob ON ob.orden_id = o.id
        LEFT JOIN actividades a_cuello ON a_cuello.id = ob.cuello_actividad_id
        WHERE o.estado != 'CERRADA'
          AND m.nombre NOT ILIKE '%admin%'
          AND m.nombre NOT ILIKE '%apoyo%'
        GROUP BY o.id, m.nombre, o.cantidad, o.estado, la.ultimo_movimiento, a_cuello.nombre
    """)
    
    ahora = datetime.now(TZ).replace(tzinfo=None)
    
    ordenes_activas = []
    wip_total_unidades = 0
    for row in c.fetchall():
        hecho = row[4]
        planeado = row[5]
        porcentaje = round((hecho / planeado * 100) if planeado > 0 else 0, 1)
        wip_total_unidades += (planeado - hecho) if planeado > hecho else 0
        
        ultimo_movimiento = row[6]
        dias_inactiva = (ahora - ultimo_movimiento).days if ultimo_movimiento else 999
        
        # Color Semáforo
        if dias_inactiva == 0:
            color = "green"
        elif dias_inactiva <= 2:
            color = "#f39c12" # yellow
        else:
            color = "red"
            
        ordenes_activas.append({
            "id": row[0],
            "producto": row[1],
            "cantidad": row[2],
            "estado": row[3],
            "avance_pct": porcentaje,
            "dias_inactiva": dias_inactiva,
            "semaforo": color,
            "actividad_atascada": row[7] or "Terminando"
        })
        
    # Ordenar por porcentaje de avance (las más atrasadas primero)
    ordenes_activas.sort(key=lambda x: x["avance_pct"])

    conn.close()

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="wip.html",
        context={
            "request": request,
            "wip_actividades": wip_actividades,
            "ordenes_activas": ordenes_activas,
            "wip_total": wip_total_unidades,
            "prod_hoy": prod_hoy,
            "prod_ayer": prod_ayer,
            "tendencia_pct": tendencia_pct
        },
    )
