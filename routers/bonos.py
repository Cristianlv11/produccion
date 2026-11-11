import json
from fastapi import APIRouter, Request, Response, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from database import db
from costos import leer_parametros
from datetime import datetime

router = APIRouter()

# --- FUNCIONES DE CÁLCULO ---

def get_session_user_id(request: Request, c):
    username = request.session.get("username")
    if not username: return None
    c.execute("SELECT id FROM users WHERE username = %s", (username,))
    row = c.fetchone()
    return row[0] if row else None


def calcular_detalles_operario(c, operario_id: int, operario_nombre: str, mes: int, anio: int):
    c.execute("""
        SELECT 
            a.id as actividad_id,
            a.nombre as actividad,
            COALESCE(m.nombre, '') as maquina,
            SUM(r.cantidad) as unidades,
            SUM((EXTRACT(EPOCH FROM r.fin::timestamp) - EXTRACT(EPOCH FROM r.inicio::timestamp)) / 3600.0) as horas,
            COUNT(r.id) as cantidad_registros,
            e.unidades_por_hora as unidades_estandar,
            COALESCE(a.indirecta, FALSE) as indirecta
        FROM registros_produccion r
        JOIN actividades a ON a.id = r.actividad_id
        LEFT JOIN LATERAL (
            SELECT unidades_por_hora FROM estandares_actividad
            WHERE actividad_id = a.id ORDER BY id LIMIT 1
        ) e ON TRUE
        LEFT JOIN procesos p ON p.id = a.proceso_id
        LEFT JOIN maquinas m ON m.id = p.maquina_id
        WHERE r.operario_id = %s
        AND TO_CHAR(r.inicio::timestamp, 'MM') = %s
        AND TO_CHAR(r.inicio::timestamp, 'YYYY') = %s
        GROUP BY a.id, a.nombre, m.nombre, e.unidades_por_hora, a.indirecta
    """, (operario_id, f"{mes:02d}", str(anio)))
    rows = c.fetchall() or []

    # Un solo costo por hora para todos (salario configurado en Actividades y tiempos):
    # el valor de una unidad es ese costo por el tiempo estándar de la actividad.
    # (Se lee después de traer las filas: usa el mismo cursor.)
    costo_hora = leer_parametros(c)["costo_hora"]
    detalle = []
    
    total_unidades = 0
    total_horas = 0
    total_bono = 0
    total_registros = 0
    alertas = []

    for row in rows:
        actividad_id = row[0]
        actividad = row[1]
        maquina = row[2]
        unidades = float(row[3] or 0)
        horas = float(row[4] or 0)
        cantidad_registros = int(row[5] or 0)

        total_unidades += unidades
        total_horas += horas
        total_registros += cantidad_registros

        if horas <= 0:
            if horas < 0:
                alertas.append(f"Registros con horas negativas en '{actividad}'.")
            rendimiento_real = 0
        else:
            rendimiento_real = unidades / horas

        unidades_estandar = float(row[6]) if row[6] else 0
        indirecta = bool(row[7])
        costo_base = costo_hora / unidades_estandar if unidades_estandar else 0

        eficiencia = rendimiento_real / unidades_estandar if unidades_estandar and not indirecta else 0
        estandar_por_revisar = eficiencia > 1.5 and horas > 0

        if indirecta or estandar_por_revisar:
            # Indirecta: no produce unidades. Más de 150 %: el estándar está mal puesto y
            # pagaría bono de más; no paga hasta que se revise en Revisión de estándares.
            porcentaje = 0
        elif eficiencia >= 1.10:
            porcentaje = 0.04
        elif eficiencia >= 1.00:
            porcentaje = 0.03
        elif eficiencia >= 0.90:
            porcentaje = 0.02
        else:
            porcentaje = 0

        bono = unidades * costo_base * porcentaje
        total_bono += bono

        if estandar_por_revisar:
            alertas.append(f"'{actividad}' va al {eficiencia*100:.0f} %: no paga bono hasta revisar su estándar.")

        detalle.append({
            "actividad_id": actividad_id,
            "maquina": maquina,
            "actividad": actividad,
            "unidades": unidades,
            "horas": round(horas,2),
            "rendimiento": round(rendimiento_real,2),
            "estandar": unidades_estandar,
            "eficiencia": round(eficiencia*100,2),
            "porcentaje": porcentaje*100,
            "bono": round(bono,2),
            "cantidad_registros": cantidad_registros,
            "indirecta": indirecta,
            "estandar_por_revisar": estandar_por_revisar,
        })

    c.execute("""
        SELECT COUNT(DISTINCT date(r.inicio)) as dias_trabajados
        FROM registros_produccion r
        WHERE r.operario_id = %s
        AND TO_CHAR(r.inicio::timestamp, 'MM') = %s
        AND TO_CHAR(r.inicio::timestamp, 'YYYY') = %s
    """, (operario_id, f"{mes:02d}", str(anio)))
    dias_trabajados = float(c.fetchone()[0] or 0)
    
    # Misma jornada que el horario de pasadas (sin descansos)
    from routers.metricas import minutos_jornada
    horas_disponibles = (dias_trabajados * minutos_jornada(c)) / 60
    eficiencia_ocupacion = total_horas / horas_disponibles if horas_disponibles > 0 else 0

    if eficiencia_ocupacion < 0.90:
        total_bono = 0
        for d in detalle:
            d["bono"] = 0

    if total_registros == 1:
        alertas.append("Solo tiene 1 registro en el mes.")
        
    if eficiencia_ocupacion > 0 and eficiencia_ocupacion < 0.3 and total_bono > 0 and any(d["eficiencia"] > 100 for d in detalle):
        alertas.append("Ocupación <30% pero gana bono. Posible error en hora de fin.")

    # Solo actividades directas con estándar razonable
    medibles = [d for d in detalle if d["estandar"] and not d["indirecta"] and not d["estandar_por_revisar"]]
    horas_medibles = sum(d["horas"] for d in medibles)
    eficiencia_productiva_global = sum(d["eficiencia"] * d["horas"] for d in medibles) / horas_medibles if horas_medibles > 0 else 0

    return {
        "operario_id": operario_id,
        "nombre": operario_nombre,
        "unidades": round(total_unidades,2),
        "horas": round(total_horas,2),
        "dias_trabajados": int(dias_trabajados),
        "eficiencia_ocupacion": round(eficiencia_ocupacion * 100, 2),
        "eficiencia_productiva": round(eficiencia_productiva_global, 2),
        "bono_total": round(total_bono, 2),
        "alertas": alertas,
        "detalle": detalle
    }


def obtener_bonos_mes(c, mes: int, anio: int):
    # Verificar si está cerrado
    c.execute("SELECT datos_json FROM cierre_bonos WHERE mes = %s AND anio = %s", (mes, anio))
    cierre = c.fetchone()
    if cierre:
        return json.loads(cierre[0]), True

    # Si no, calcular en vivo
    c.execute("""
        SELECT DISTINCT o.id, o.nombre
        FROM registros_produccion r
        JOIN operarios o ON o.id = r.operario_id
        WHERE TO_CHAR(r.inicio::timestamp, 'MM') = %s
        AND TO_CHAR(r.inicio::timestamp, 'YYYY') = %s
    """, (f"{mes:02d}", str(anio)))
    operarios = c.fetchall()
    
    resultado = []
    for op_id, op_nombre in operarios:
        calc = calcular_detalles_operario(c, op_id, op_nombre, mes, anio)
        if calc["horas"] > 0 or len(calc["detalle"]) > 0:
            resultado.append(calc)
            
    return resultado, False


# ================= PÁGINA BONOS =================

@router.get("/bonos", response_class=HTMLResponse)
def bonos(request: Request):
    if not request.session.get("role") == "admin":
        return RedirectResponse("/admin", 303)

    hoy = datetime.now()
    mes = int(request.query_params.get("mes", hoy.month))
    anio = int(request.query_params.get("anio", hoy.year))

    conn = db()
    c = conn.cursor()
    datos, cerrado = obtener_bonos_mes(c, mes, anio)
    conn.close()

    return request.app.state.templates.TemplateResponse(
        request=request, name="bonos.html", context={
            "request": request,
            "datos": datos,
            "mes": mes,
            "anio": anio,
            "cerrado": cerrado
        }
    )


# ================= DETALLE BONO =================

@router.get("/bonos/detalle", response_class=HTMLResponse)
def detalle_bono(request: Request):
    if not request.session.get("role") == "admin":
        return RedirectResponse("/admin", 303)

    operario_id_str = request.query_params.get("operario_id")
    if not operario_id_str:
        return RedirectResponse("/bonos", 303)
        
    operario_id = int(operario_id_str)
    mes = int(request.query_params.get("mes"))
    anio = int(request.query_params.get("anio"))

    conn = db()
    c = conn.cursor()
    datos, cerrado = obtener_bonos_mes(c, mes, anio)
    conn.close()

    # Buscar el operario en los datos
    op_data = next((d for d in datos if d["operario_id"] == operario_id), None)
    if not op_data:
        return RedirectResponse("/bonos", 303)

    return request.app.state.templates.TemplateResponse(
        request=request, name="bono_detalle.html", context={
            "request": request,
            "nombre": op_data["nombre"],
            "operario_id": operario_id,
            "mes": mes,
            "anio": anio,
            "detalle": op_data["detalle"],
            "total_bono": op_data["bono_total"],
            "alertas": op_data["alertas"],
            "cerrado": cerrado
        }
    )


# ================= CERRAR/REABRIR MES =================

@router.post("/bonos/cerrar")
def cerrar_mes(request: Request, mes: int = Query(...), anio: int = Query(...)):
    if not request.session.get("role") == "admin":
        return RedirectResponse("/admin", 303)
        
    conn = db()
    c = conn.cursor()
    
    # Calcular en vivo
    c.execute("""
        SELECT DISTINCT o.id, o.nombre
        FROM registros_produccion r
        JOIN operarios o ON o.id = r.operario_id
        WHERE TO_CHAR(r.inicio::timestamp, 'MM') = %s
        AND TO_CHAR(r.inicio::timestamp, 'YYYY') = %s
    """, (f"{mes:02d}", str(anio)))
    operarios = c.fetchall()
    
    resultado = []
    for op_id, op_nombre in operarios:
        calc = calcular_detalles_operario(c, op_id, op_nombre, mes, anio)
        if calc["horas"] > 0 or len(calc["detalle"]) > 0:
            resultado.append(calc)
            
    json_data = json.dumps(resultado)
    user_id = get_session_user_id(request, c)
    
    c.execute("""
        INSERT INTO cierre_bonos (mes, anio, datos_json, cerrado_por)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (mes, anio) DO UPDATE SET datos_json = EXCLUDED.datos_json, fecha_cierre = CURRENT_TIMESTAMP
    """, (mes, anio, json_data, user_id))
    
    conn.commit()
    conn.close()
    return RedirectResponse(f"/bonos?mes={mes}&anio={anio}", 303)


@router.post("/bonos/reabrir")
def reabrir_mes(request: Request, mes: int = Query(...), anio: int = Query(...)):
    if not request.session.get("role") == "admin":
        return RedirectResponse("/admin", 303)
        
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM cierre_bonos WHERE mes = %s AND anio = %s", (mes, anio))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/bonos?mes={mes}&anio={anio}", 303)


# ================= ACTUALIZAR ESTÁNDAR INLINE =================

from fastapi import Form

@router.post("/bonos/actualizar_estandar")
def actualizar_estandar(
    request: Request, 
    actividad_id: int = Form(...), 
    nuevo_estandar: float = Form(...),
    operario_id: int = Form(...),
    mes: int = Form(...),
    anio: int = Form(...)
):
    if not request.session.get("role") == "admin":
        return RedirectResponse("/admin", 303)
        
    conn = db()
    c = conn.cursor()
    
    # nuevo_estandar llega en minutos por unidad (igual que en el resto del sistema)
    if nuevo_estandar > 0:
        from routers.tiempos import guardar_estandar
        guardar_estandar(c, actividad_id, nuevo_estandar)
        conn.commit()
    conn.close()
    
    return RedirectResponse(f"/bonos/detalle?operario_id={operario_id}&mes={mes}&anio={anio}", 303)


# ================= VER REGISTROS CRUDOS =================

@router.get("/bonos/registros", response_class=HTMLResponse)
def registros_crudos(request: Request, operario_id: int, actividad_id: int, mes: int, anio: int):
    if not request.session.get("role") == "admin":
        return RedirectResponse("/admin", 303)
        
    conn = db()
    c = conn.cursor()
    
    c.execute("""
        SELECT r.id, r.inicio, r.fin, r.cantidad
        FROM registros_produccion r
        WHERE r.operario_id = %s AND r.actividad_id = %s
        AND TO_CHAR(r.inicio::timestamp, 'MM') = %s
        AND TO_CHAR(r.inicio::timestamp, 'YYYY') = %s
        ORDER BY r.inicio DESC
    """, (operario_id, actividad_id, f"{mes:02d}", str(anio)))
    
    registros = c.fetchall()
    
    c.execute("SELECT nombre FROM operarios WHERE id = %s", (operario_id,))
    op_nombre = c.fetchone()[0]
    
    c.execute("SELECT nombre FROM actividades WHERE id = %s", (actividad_id,))
    act_nombre = c.fetchone()[0]
    
    # Obtener todas las actividades para el dropdown de edición
    c.execute("""
        SELECT a.id, p.nombre, a.nombre 
        FROM actividades a 
        JOIN procesos p ON p.id = a.proceso_id 
        ORDER BY p.nombre, a.nombre
    """)
    todas_actividades = c.fetchall()
    
    # Comprobar si el mes está cerrado
    c.execute("SELECT id FROM cierre_bonos WHERE mes = %s AND anio = %s", (mes, anio))
    cerrado = c.fetchone() is not None
    
    conn.close()
    
    return request.app.state.templates.TemplateResponse(
        request=request, name="bono_registros.html", context={
            "request": request,
            "registros": registros,
            "operario": op_nombre,
            "actividad": act_nombre,
            "mes": mes,
            "anio": anio,
            "operario_id": operario_id,
            "actividad_id": actividad_id,
            "todas_actividades": todas_actividades,
            "cerrado": cerrado
        }
    )

@router.post("/bonos/registros/editar")
def editar_registro_crudo(
    request: Request,
    registro_id: int = Form(...),
    nueva_actividad_id: int = Form(...),
    nueva_cantidad: int = Form(...),
    operario_id: int = Form(...),
    actividad_id_actual: int = Form(...),
    mes: int = Form(...),
    anio: int = Form(...)
):
    if not request.session.get("role") == "admin":
        return RedirectResponse("/admin", 303)

    conn = db()
    c = conn.cursor()

    # Comprobar si el mes está cerrado (si lo está, no deberíamos dejar editar, pero por seguridad doble check)
    c.execute("SELECT id FROM cierre_bonos WHERE mes = %s AND anio = %s", (mes, anio))
    if c.fetchone():
        conn.close()
        return RedirectResponse(f"/bonos/registros?operario_id={operario_id}&actividad_id={actividad_id_actual}&mes={mes}&anio={anio}", 303)

    c.execute("""
        UPDATE registros_produccion
        SET actividad_id = %s, cantidad = %s
        WHERE id = %s
    """, (nueva_actividad_id, nueva_cantidad, registro_id))
    
    conn.commit()
    conn.close()

    return RedirectResponse(f"/bonos/registros?operario_id={operario_id}&actividad_id={actividad_id_actual}&mes={mes}&anio={anio}", 303)



# ================= EXPORTAR EXCEL =================

@router.get("/bonos/exportar")
def exportar_bonos(request: Request, mes: int, anio: int):
    import pandas as pd
    import io
    from fastapi.responses import StreamingResponse
    
    if not request.session.get("role") == "admin":
        return RedirectResponse("/admin", 303)
        
    conn = db()
    c = conn.cursor()
    datos, _ = obtener_bonos_mes(c, mes, anio)
    conn.close()
    
    flat_data = []
    for op in datos:
        flat_data.append({
            "Operario": op["nombre"],
            "Unidades Totales": op["unidades"],
            "Horas Totales": op["horas"],
            "Dias Trabajados": op["dias_trabajados"],
            "Eficiencia Ocupacion %": op["eficiencia_ocupacion"],
            "Bono Total $": op["bono_total"],
            "Alertas": " | ".join(op["alertas"])
        })
        
    df = pd.DataFrame(flat_data)
    stream = io.BytesIO()
    with pd.ExcelWriter(stream, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name=f"Bonos_{mes}_{anio}")
        
    stream.seek(0)
    
    headers = {
        'Content-Disposition': f'attachment; filename="Bonos_{mes:02d}_{anio}.xlsx"'
    }
    return StreamingResponse(stream, headers=headers, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
