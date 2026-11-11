from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from auth import require_admin
from database import db
import pandas as pd
import json
from datetime import datetime
router = APIRouter()


# ================= PANEL ADMINISTRATIVO =================

@router.get("/admin_tools", response_class=HTMLResponse)
def admin_tools_panel(request: Request):

    if request.session.get("role") != "admin":
        return RedirectResponse("/admin", 303)

    return request.app.state.templates.TemplateResponse(
        request=request, name="admin_tools_panel.html", context={"request": request}
    )


# ================= RESET METRICAS =================

@router.post("/admin_tools/reset_metricas")
def reset_metricas(request: Request):

    if request.session.get("role") != "admin":
        return RedirectResponse("/admin", 303)

    conn = db()
    c = conn.cursor()

    c.execute("DELETE FROM registros_produccion")
    c.execute("DELETE FROM bonos")
    c.execute("UPDATE ordenes SET porcentaje=0, estado='ABIERTA', cerrado_en=NULL")
    c.execute("UPDATE orden_actividades SET cantidad_realizada=0")

    conn.commit()
    conn.close()

    return RedirectResponse("/admin_tools", 303)


# ================= BORRAR REGISTROS =================

@router.post("/admin_tools/borrar_registros")
def borrar_registros(request: Request):

    if request.session.get("role") != "admin":
        return RedirectResponse("/admin", 303)

    conn = db()
    c = conn.cursor()

    c.execute("DELETE FROM registros_produccion")

    conn.commit()
    conn.close()

    return RedirectResponse("/admin_tools", 303)


# ================= CARGAR ESTANDARES =================

@router.get("/admin_tools/cargar_estandares_excel")
def cargar_estandares_excel(request: Request):

    if request.session.get("role") != "admin":
        return RedirectResponse("/admin", 303)

    conn = db()
    c = conn.cursor()

    ruta = "excel/estandares.xlsx"
    df = pd.read_excel(ruta)

    c.execute("DELETE FROM estandares_actividad")

    for _, row in df.iterrows():

        actividad_id = int(row["actividad_id"])
        unidades = float(row["unidades_por_hora"])
        costo = float(row["costo_mo_unidad"])

        c.execute("""
            INSERT INTO estandares_actividad
            (actividad_id, unidades_por_hora, costo_mo_unidad, costo_mo_hora)
            VALUES (%s, %s, %s, 0)
        """, (actividad_id, unidades, costo))

    conn.commit()
    conn.close()

    return RedirectResponse("/admin_tools", 303)

@router.post("/maquinas")
def crear_maquina(nombre: str):
    conn = db()
    c = conn.cursor()

    c.execute(
        "INSERT INTO maquinas (nombre) VALUES (%s)",
        (nombre,)
    )

    conn.commit()
    conn.close()

    return {"mensaje": "maquina creada"}

@router.put("/maquinas/{id}")
def editar_maquina(id: int, nombre: str):
    conn = db()
    c = conn.cursor()

    c.execute(
        "UPDATE maquinas SET nombre=%s WHERE id=%s",
        (nombre, id)
    )

    conn.commit()
    conn.close()

    return {"mensaje": "maquina actualizada"}

@router.delete("/maquinas/{id}")
def eliminar_maquina(id: int):
    conn = db()
    c = conn.cursor()

    c.execute(
        "DELETE FROM maquinas WHERE id=%s",
        (id,)
    )

    conn.commit()
    conn.close()

    return {"mensaje": "maquina eliminada"}


from notificaciones import notificar_ausencias_operarios

@router.get("/internal/notificar_ausencias")
def trigger_notificar_ausencias(request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    
    res = notificar_ausencias_operarios()
    return JSONResponse(res)

from backup_db import ejecutar_backup_completo

@router.post("/admin_tools/backup_manual")
def backup_manual_endpoint(request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    
    # Run synchronously to return result to frontend
    res = ejecutar_backup_completo(tipo="MANUAL")
    return JSONResponse(res)

# ================= EDITAR REGISTROS (VISTA) =================
@router.get("/admin_tools/editar_registros", response_class=HTMLResponse)
def editar_registros_vista(request: Request):
    if not require_admin(request):
        return RedirectResponse("/admin", 303)
        
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id, nombre FROM operarios ORDER BY nombre")
    operarios_tuples = c.fetchall()
    conn.close()
    
    operarios = [{"id": r[0], "nombre": r[1]} for r in operarios_tuples]
    
    return request.app.state.templates.TemplateResponse(
        request=request, 
        name="admin_editar_registros.html", 
        context={"request": request, "operarios": operarios}
    )

# ================= EDITAR CATALOGOS (VISTA) =================
@router.get("/admin_tools/editar_catalogos", response_class=HTMLResponse)
def editar_catalogos_vista(request: Request):
    if not require_admin(request):
        return RedirectResponse("/admin", 303)
        
    return request.app.state.templates.TemplateResponse(
        request=request, 
        name="admin_editar_catalogos.html", 
        context={"request": request}
    )

# ================= OBTENER REGISTROS (API) =================
@router.get("/admin_tools/api/registros")
def api_obtener_registros(request: Request, operario_id: int = None, fecha_inicio: str = None, fecha_fin: str = None):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
        
    conn = db()
    c = conn.cursor()
    
    query = """
        SELECT 
            rp.id,
            rp.cantidad,
            rp.inicio,
            rp.fin,
            rp.tiempo,
            o.nombre as operario_nombre,
            a.nombre as actividad_nombre,
            p.nombre as proceso_nombre,
            m.nombre as maquina_nombre,
            ord.id as orden_id,
            rp.observacion
        FROM registros_produccion rp
        JOIN operarios o ON rp.operario_id = o.id
        JOIN actividades a ON rp.actividad_id = a.id
        JOIN procesos p ON a.proceso_id = p.id
        JOIN maquinas m ON p.maquina_id = m.id
        JOIN ordenes ord ON rp.orden_id = ord.id
        WHERE 1=1
    """
    params = []
    
    if operario_id:
        query += " AND rp.operario_id = %s"
        params.append(operario_id)
        
    if fecha_inicio:
        query += " AND rp.inicio >= %s"
        params.append(fecha_inicio + "T00:00:00")
        
    if fecha_fin:
        query += " AND rp.inicio <= %s"
        params.append(fecha_fin + "T23:59:59")
        
    query += " ORDER BY rp.inicio DESC LIMIT 1000"
    
    c.execute(query, tuple(params))
    rows = c.fetchall()
    conn.close()
    
    cols = ["id", "cantidad", "inicio", "fin", "tiempo", "operario_nombre", "actividad_nombre", "proceso_nombre", "maquina_nombre", "orden_id", "observacion"]
    result = [dict(zip(cols, row)) for row in rows]
    return JSONResponse(result)

# ================= EDITAR REGISTRO (API) =================
@router.post("/admin_tools/api/registros/{registro_id}")
def api_editar_registro(request: Request, registro_id: int, data: dict):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
        
    nueva_cantidad = int(data.get("cantidad", 0))
    nuevo_inicio = data.get("inicio")
    nuevo_fin = data.get("fin")
    nuevo_tiempo = int(data.get("tiempo", 0))
    
    # Validar fechas y duración
    try:
        dt_inicio = datetime.fromisoformat(nuevo_inicio)
        dt_fin = datetime.fromisoformat(nuevo_fin)
        if dt_fin <= dt_inicio:
            return JSONResponse({"detail": "La fecha de fin debe ser mayor a la fecha de inicio"}, status_code=400)
        
        duracion_horas = (dt_fin - dt_inicio).total_seconds() / 3600
        if duracion_horas > 24:
            return JSONResponse({"detail": "La duración no puede exceder las 24 horas"}, status_code=400)
    except Exception as e:
        return JSONResponse({"detail": "Formato de fecha inválido"}, status_code=400)
    
    conn = db()
    c = conn.cursor()
    
    try:
        # 1. Obtener datos actuales
        c.execute("""
            SELECT cantidad, orden_id, actividad_id, inicio, fin, tiempo 
            FROM registros_produccion 
            WHERE id = %s
        """, (registro_id,))
        row = c.fetchone()
        
        if not row:
            conn.close()
            return JSONResponse({"detail": "Registro no encontrado"}, status_code=404)
            
        cantidad_actual, orden_id, actividad_id, inicio_actual, fin_actual, tiempo_actual = row
        delta = nueva_cantidad - cantidad_actual
        
        # 2. Actualizar registros_produccion
        c.execute("""
            UPDATE registros_produccion 
            SET cantidad = %s, inicio = %s, fin = %s, tiempo = %s
            WHERE id = %s
        """, (nueva_cantidad, nuevo_inicio, nuevo_fin, nuevo_tiempo, registro_id))
        
        # 3. Propagar delta a orden_actividades
        c.execute("""
            UPDATE orden_actividades
            SET cantidad_realizada = cantidad_realizada + %s
            WHERE orden_id = %s AND actividad_id = %s
        """, (delta, orden_id, actividad_id))
        
        # 4. Recalcular porcentaje de la orden usando la funcion centralizada
        from database import recalcular_porcentaje_orden
        nuevo_porcentaje = recalcular_porcentaje_orden(c, orden_id)
        
        # 5. Insertar auditoria
        admin_id = request.session.get("admin_id", request.session.get("user_id", 0))
        old_vals = json.dumps({"cantidad": cantidad_actual, "inicio": inicio_actual, "fin": fin_actual, "tiempo": tiempo_actual})
        new_vals = json.dumps({"cantidad": nueva_cantidad, "inicio": nuevo_inicio, "fin": nuevo_fin, "tiempo": nuevo_tiempo})
        
        c.execute("""
            INSERT INTO auditoria_registros_produccion 
            (registro_id, admin_id, valores_anteriores, valores_nuevos)
            VALUES (%s, %s, %s, %s)
        """, (registro_id, admin_id, old_vals, new_vals))
        
        conn.commit()
        return JSONResponse({"ok": True, "delta": delta, "nuevo_porcentaje": nuevo_porcentaje})
    except Exception as e:
        conn.rollback()
        return JSONResponse({"detail": f"Error interno: {str(e)}"}, status_code=500)
    finally:
        conn.close()

# ================= MIGRACION =================
@router.get("/admin_tools/run_migration_soft_delete")
def run_migration_soft_delete(request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    
    conn = db()
    c = conn.cursor()
    tablas = ['operarios', 'maquinas', 'procesos', 'actividades']
    resultados = []
    
    for tabla in tablas:
        try:
            c.execute(f"ALTER TABLE {tabla} ADD COLUMN activo BOOLEAN DEFAULT TRUE")
            c.execute(f"UPDATE {tabla} SET activo = TRUE WHERE activo IS NULL")
            conn.commit()
            resultados.append(f"{tabla}: Columna agregada y valores actualizados.")
        except Exception as e:
            conn.rollback()
            resultados.append(f"{tabla}: {str(e)}")
            
    conn.close()
    return JSONResponse({"ok": True, "resultados": resultados})
