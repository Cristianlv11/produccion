"""Seguimiento de eficiencia: revisión de estándares, ficha por operario y registros para revisar."""
from collections import OrderedDict
from datetime import datetime, time, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import require_admin
from database import db
from routers.metricas import NOMBRES_DIA, _fecha_corta, _pct, lunes_de, minutos_jornada
from routers.cortes import hoy as hoy_local

router = APIRouter()

# Rango en que un estándar se considera razonable (eficiencia histórica)
EFIC_MIN, EFIC_MAX = 50, 150
DIAS_REVISION = 180


def _solo_admin(request: Request):
    if "username" not in request.session:
        return RedirectResponse("/admin", 303)
    if request.session.get("role") != "admin":
        return RedirectResponse("/", 303)
    return None


# ================= REVISIÓN DE ESTÁNDARES =================

def datos_revision(cursor, dias=DIAS_REVISION):
    """Cada actividad activa con su estándar y lo que de verdad toma según los registros.

    El tiempo real es horas totales / unidades totales del periodo (incluye los registros
    de trabajo en curso con 0 unidades, que se compensan cuando se terminan)."""
    desde = datetime.combine(hoy_local() - timedelta(days=dias), time.min)
    cursor.execute("""
        SELECT a.id, m.nombre, p.nombre, a.nombre, COALESCE(a.indirecta, FALSE),
               e.unidades_por_hora,
               COALESCE(r.registros, 0), COALESCE(r.horas, 0), COALESCE(r.unidades, 0), r.ultimo
        FROM actividades a
        JOIN procesos p ON p.id = a.proceso_id
        JOIN maquinas m ON m.id = p.maquina_id
        LEFT JOIN LATERAL (
            SELECT unidades_por_hora FROM estandares_actividad
            WHERE actividad_id = a.id ORDER BY id LIMIT 1
        ) e ON TRUE
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS registros,
                   SUM(GREATEST(EXTRACT(EPOCH FROM (rp.fin::timestamp - rp.inicio::timestamp)), 0)) / 3600.0 AS horas,
                   SUM(rp.cantidad) AS unidades,
                   MAX(rp.inicio::timestamp)::date AS ultimo
            FROM registros_produccion rp
            WHERE rp.actividad_id = a.id AND rp.inicio::timestamp >= %s
              AND EXTRACT(EPOCH FROM (rp.fin::timestamp - rp.inicio::timestamp)) BETWEEN 0 AND 12 * 3600
        ) r ON TRUE
        WHERE COALESCE(a.activo, TRUE) = TRUE AND COALESCE(p.activo, TRUE) = TRUE
        ORDER BY m.nombre, p.id, a.id
    """, (desde,))
    filas = []
    for (aid, producto, proceso, actividad, indirecta, uph, registros, horas, unidades, ultimo) in cursor.fetchall():
        horas, unidades = float(horas or 0), int(unidades or 0)
        min_std = round(60.0 / float(uph), 2) if uph and float(uph) > 0 else None
        min_real = round(horas * 60 / unidades, 2) if unidades > 0 and horas > 0 else None
        efic = round(min_std / min_real * 100) if min_std and min_real else None
        if indirecta:
            estado = "indirecta"
        elif min_std is None:
            estado = "sin_estandar"
        elif efic is None:
            estado = "sin_datos"
        elif efic > EFIC_MAX:
            estado = "holgado"
        elif efic < EFIC_MIN:
            estado = "exigente"
        else:
            estado = "ok"
        filas.append({
            "id": aid, "producto": producto, "proceso": proceso, "actividad": actividad,
            "indirecta": indirecta, "min_std": min_std, "min_real": min_real, "eficiencia": efic,
            "registros": int(registros or 0), "horas": round(horas, 1), "unidades": unidades,
            "ultimo": _fecha_corta(ultimo) if ultimo else None, "estado": estado,
            # Solo se sugiere el tiempo real si hay suficiente información
            "confiable": int(registros or 0) >= 3 and unidades >= 3,
        })
    return filas


@router.get("/configuracion/estandares", response_class=HTMLResponse)
def vista_revision(request: Request):
    salida = _solo_admin(request)
    if salida:
        return salida
    conn = db()
    c = conn.cursor()
    try:
        filas = datos_revision(c)
    finally:
        conn.close()
    conteo = {}
    for f in filas:
        if f["registros"] or f["estado"] == "sin_estandar":
            conteo[f["estado"]] = conteo.get(f["estado"], 0) + 1
    return request.app.state.templates.TemplateResponse(
        request=request, name="revision_estandares.html",
        context={"request": request, "filas": filas, "conteo": conteo, "dias": DIAS_REVISION,
                 "efic_min": EFIC_MIN, "efic_max": EFIC_MAX},
    )


# ================= FICHA POR OPERARIO =================

def ficha_operario(cursor, operario_id, semanas=12):
    cursor.execute("SELECT id, nombre FROM operarios WHERE id = %s", (operario_id,))
    op = cursor.fetchone()
    if not op:
        raise HTTPException(status_code=404, detail="Operario no encontrado")
    hasta_lunes = lunes_de(hoy_local())
    desde_lunes = hasta_lunes - timedelta(weeks=semanas - 1)
    cursor.execute("""
        SELECT r.inicio::timestamp, r.fin::timestamp, r.cantidad, a.nombre, m.nombre,
               e.unidades_por_hora, COALESCE(a.indirecta, FALSE)
        FROM registros_produccion r
        JOIN actividades a ON a.id = r.actividad_id
        LEFT JOIN procesos p ON p.id = a.proceso_id
        LEFT JOIN maquinas m ON m.id = p.maquina_id
        LEFT JOIN LATERAL (
            SELECT unidades_por_hora FROM estandares_actividad
            WHERE actividad_id = a.id ORDER BY id LIMIT 1
        ) e ON TRUE
        WHERE r.operario_id = %s AND r.inicio::timestamp >= %s
        ORDER BY r.inicio::timestamp
    """, (operario_id, datetime.combine(desde_lunes, time.min)))
    filas = cursor.fetchall()
    jornada_h = minutos_jornada(cursor) / 60

    def nuevo():
        return {"horas": 0.0, "con_std": 0.0, "std": 0.0, "unidades": 0, "registros": 0, "indirectas": 0.0, "dias": set()}

    semanas_acc = OrderedDict((desde_lunes + timedelta(weeks=i), nuevo()) for i in range(semanas))
    actividades = {}
    for ini, fin, cantidad, actividad, producto, uph, indirecta in filas:
        horas = max((fin - ini).total_seconds() / 3600, 0) if ini and fin else 0
        cantidad = int(cantidad or 0)
        h_std = cantidad / float(uph) if uph and float(uph) > 0 and not indirecta else None
        for acc in (semanas_acc[lunes_de(ini.date())],
                    actividades.setdefault((actividad, producto), {"nombre": actividad, "producto": producto or "", "indirecta": indirecta, **nuevo()})):
            acc["horas"] += horas
            acc["unidades"] += cantidad
            acc["registros"] += 1
            acc["dias"].add(ini.date())
            if indirecta:
                acc["indirectas"] += horas
            elif h_std is not None:
                acc["con_std"] += horas
                acc["std"] += h_std

    serie = []
    for lunes, acc in semanas_acc.items():
        disp = len(acc["dias"]) * jornada_h
        serie.append({
            "etiqueta": f"{lunes:%d/%m}",
            "lunes": lunes.isoformat(),
            "horas": round(acc["horas"], 1),
            "unidades": acc["unidades"],
            "eficiencia": _pct(acc["std"], acc["con_std"]),
            "registrado": _pct(acc["horas"], disp),
            "indirecto": _pct(acc["indirectas"], acc["horas"]),
        })

    lista = []
    for a in actividades.values():
        if a["horas"] < 0.5:
            continue
        lista.append({"nombre": a["nombre"], "producto": a["producto"], "indirecta": a["indirecta"],
                      "horas": round(a["horas"], 1), "unidades": a["unidades"],
                      "eficiencia": _pct(a["std"], a["con_std"])})
    lista.sort(key=lambda a: -a["horas"])

    tot = nuevo()
    for acc in semanas_acc.values():
        for k in ("horas", "con_std", "std", "unidades", "registros", "indirectas"):
            tot[k] += acc[k]
        tot["dias"] |= acc["dias"]
    con_datos = [s for s in serie if s["eficiencia"] is not None]
    tendencia = None
    if len(con_datos) >= 4:
        mitad = len(con_datos) // 2
        antes = sum(s["eficiencia"] for s in con_datos[:mitad]) / mitad
        despues = sum(s["eficiencia"] for s in con_datos[mitad:]) / (len(con_datos) - mitad)
        tendencia = round(despues - antes)
    return {
        "id": op[0], "nombre": op[1], "semanas": semanas, "serie": serie, "actividades": lista,
        "desde": _fecha_corta(desde_lunes), "hasta": _fecha_corta(hasta_lunes + timedelta(days=6)),
        "total": {
            "horas": round(tot["horas"], 1), "unidades": tot["unidades"], "registros": tot["registros"],
            "dias": len(tot["dias"]), "eficiencia": _pct(tot["std"], tot["con_std"]),
            "registrado": _pct(tot["horas"], len(tot["dias"]) * jornada_h),
            "indirecto": _pct(tot["indirectas"], tot["horas"]),
        },
        "tendencia": tendencia,
    }


@router.get("/metricas/operario/{operario_id}", response_class=HTMLResponse)
def vista_ficha(request: Request, operario_id: int, semanas: int = 12):
    salida = _solo_admin(request)
    if salida:
        return salida
    semanas = min(max(semanas, 4), 52)
    conn = db()
    c = conn.cursor()
    try:
        f = ficha_operario(c, operario_id, semanas)
        c.execute("SELECT id, nombre FROM operarios WHERE COALESCE(activo, TRUE) = TRUE ORDER BY nombre")
        operarios = [{"id": r[0], "nombre": r[1]} for r in c.fetchall()]
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(
        request=request, name="ficha_operario.html",
        context={"request": request, "f": f, "operarios": operarios},
    )


# ================= REGISTROS PARA REVISAR =================

def registros_sospechosos(cursor, dias=120):
    desde = datetime.combine(hoy_local() - timedelta(days=dias), time.min)
    cursor.execute("""
        WITH r AS (
            SELECT rp.id, rp.operario_id, rp.inicio::timestamp AS ini, rp.fin::timestamp AS fin, rp.cantidad,
                   o.nombre AS operario, a.nombre AS actividad, m.nombre AS producto,
                   COALESCE(a.indirecta, FALSE) AS indirecta,
                   (SELECT unidades_por_hora FROM estandares_actividad e WHERE e.actividad_id = a.id ORDER BY id LIMIT 1) AS uph
            FROM registros_produccion rp
            JOIN operarios o ON o.id = rp.operario_id
            JOIN actividades a ON a.id = rp.actividad_id
            LEFT JOIN procesos p ON p.id = a.proceso_id
            LEFT JOIN maquinas m ON m.id = p.maquina_id
            WHERE rp.inicio::timestamp >= %s
        )
        SELECT r.*,
               EXISTS (SELECT 1 FROM r r2 WHERE r2.operario_id = r.operario_id AND r2.id <> r.id
                       AND r2.ini < r.fin AND r.ini < r2.fin) AS solapado
        FROM r ORDER BY r.ini DESC
    """, (desde,))
    cols = [d[0] for d in cursor.description]
    salida = []
    for fila in cursor.fetchall():
        r = dict(zip(cols, fila))
        horas = (r["fin"] - r["ini"]).total_seconds() / 3600 if r["ini"] and r["fin"] else 0
        motivos = []
        if horas <= 0:
            motivos.append("La hora de fin es igual o anterior a la de inicio")
        if horas > 10:
            motivos.append(f"Dura {horas:.1f} h (más de 10)")
        if r["ini"] and r["fin"] and r["ini"].date() != r["fin"].date():
            motivos.append("Empieza un día y termina otro")
        if r["solapado"]:
            motivos.append("Se cruza con otro registro del mismo operario")
        # (Las eficiencias raras se ven en Revisión de estándares: casi siempre es el estándar)
        if motivos:
            salida.append({
                "id": r["id"], "operario_id": r["operario_id"], "operario": r["operario"],
                "actividad": r["actividad"], "producto": r["producto"] or "", "cantidad": r["cantidad"],
                "inicio": r["ini"].strftime("%d/%m/%Y %H:%M") if r["ini"] else "",
                "fin": r["fin"].strftime("%d/%m/%Y %H:%M") if r["fin"] else "",
                "fecha": r["ini"].date().isoformat() if r["ini"] else "",
                "horas": round(horas, 2), "motivos": motivos,
            })
    return salida


@router.get("/admin_tools/registros_revisar", response_class=HTMLResponse)
def vista_registros_revisar(request: Request):
    salida = _solo_admin(request)
    if salida:
        return salida
    conn = db()
    c = conn.cursor()
    try:
        filas = registros_sospechosos(c)
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(
        request=request, name="registros_revisar.html", context={"request": request, "filas": filas},
    )
