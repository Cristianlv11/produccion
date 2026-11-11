from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from psycopg2 import sql
from database import db
from auth import require_admin
from costos import costos_desde_unidades_hora

router = APIRouter()

TABLAS_PERMITIDAS = [
    "maquinas",
    "procesos",
    "actividades",
    "operarios",
    "estandares_actividad",
]

REFERENCIAS = {
    "maquinas": [("procesos", "maquina_id"), ("ordenes", "maquina_id")],
    "procesos": [("actividades", "proceso_id")],
    "actividades": [
        ("orden_actividades", "actividad_id"),
        ("registros_produccion", "actividad_id"),
        ("bonos", "actividad_id"),
        ("estandares_actividad", "actividad_id"),
    ],
    "operarios": [("registros_produccion", "operario_id"), ("bonos", "operario_id")],
    "estandares_actividad": [],
}

def sincronizar_secuencia(cursor, tabla: str):
    cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", (tabla,))
    row = cursor.fetchone()
    if not row or not row[0]: return
    seq = row[0]
    cursor.execute(sql.SQL("SELECT COALESCE(MAX(id), 0) FROM {}").format(sql.Identifier(tabla)))
    max_id = cursor.fetchone()[0] or 0
    if max_id > 0:
        cursor.execute("SELECT setval(%s, %s, true)", (seq, max_id))
    else:
        cursor.execute("SELECT setval(%s, 1, false)", (seq,))

def fila_en_uso(cursor, tabla: str, id: int):
    for tabla_ref, columna_ref in REFERENCIAS.get(tabla, []):
        cursor.execute(
            sql.SQL("SELECT COUNT(*) FROM {} WHERE {} = %s").format(
                sql.Identifier(tabla_ref), sql.Identifier(columna_ref)
            ),
            (id,),
        )
        total = cursor.fetchone()[0] or 0
        if total > 0:
            return tabla_ref, columna_ref, total
    return None

def eliminar_fila_segura(tabla: str, id: int):
    if tabla not in TABLAS_PERMITIDAS:
        raise HTTPException(status_code=400, detail="Tabla no permitida")
    
    conn = db()
    c = conn.cursor()
    try:
        uso = fila_en_uso(c, tabla, id)
        if uso:
            tabla_ref, columna_ref, total = uso
            
            # Intentar Soft Delete si la tabla lo soporta
            try:
                c.execute(sql.SQL("UPDATE {} SET activo = FALSE WHERE id = %s").format(sql.Identifier(tabla)), (id,))
                
                if tabla == 'operarios':
                    c.execute("UPDATE users SET activo = FALSE WHERE operario_id = %s", (id,))
                    
                conn.commit()
                return {"ok": True, "mensaje": f"El ítem estaba en uso por {total} registros en '{tabla_ref}'. Ha sido desactivado (Soft Delete) en lugar de eliminado."}
            except Exception as e:
                # Si falla (ej. la columna activo no existe aún), lanzar el 409 original
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=f"No se puede eliminar: esta fila esta siendo usada por {total} registro(s) en la tabla '{tabla_ref}'."
                )
        
        if tabla == 'operarios':
            c.execute("DELETE FROM users WHERE operario_id = %s", (id,))
            
        c.execute(sql.SQL("DELETE FROM {} WHERE id=%s").format(sql.Identifier(tabla)), (id,))
        sincronizar_secuencia(c, tabla)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
    return {"ok": True, "mensaje": "Fila eliminada permanentemente"}

# --- Validaciones de Asistente ---
def texto_obligatorio(data: dict, campo: str, etiqueta: str):
    valor = str(data.get(campo, "")).strip()
    if not valor:
        raise HTTPException(status_code=400, detail=f"{etiqueta} es obligatorio")
    return valor

def entero_obligatorio(data: dict, campo: str, etiqueta: str):
    try:
        valor = int(str(data.get(campo, "")).strip())
    except Exception:
        raise HTTPException(status_code=400, detail=f"{etiqueta} invalido")
    if valor <= 0:
        raise HTTPException(status_code=400, detail=f"{etiqueta} invalido")
    return valor

def float_obligatorio(data: dict, campo: str, etiqueta: str):
    try:
        valor = float(str(data.get(campo, "")).strip().replace(",", "."))
    except Exception:
        raise HTTPException(status_code=400, detail=f"{etiqueta} invalido")
    if valor <= 0:
        raise HTTPException(status_code=400, detail=f"{etiqueta} debe ser mayor a 0")
    return valor

def float_opcional(data: dict, campo: str):
    valor = data.get(campo, "")
    if valor is None or str(valor).strip() == "":
        return 0
    try:
        return float(str(valor).strip().replace(",", "."))
    except Exception:
        raise HTTPException(status_code=400, detail=f"{campo} invalido")

def unidades_por_hora_de(data: dict):
    """El estándar se pide en minutos por unidad (lo que entiende la supervisora);
    se acepta también unidades por hora por compatibilidad."""
    if str(data.get("minutos_unidad", "")).strip():
        return 60.0 / float_obligatorio(data, "minutos_unidad", "Minutos por unidad")
    return float_obligatorio(data, "unidades_por_hora", "Minutos por unidad")


def crear_estandar(cursor, actividad_id: int, data: dict):
    unidades = unidades_por_hora_de(data)
    costo_unidad, costo_hora = costos_desde_unidades_hora(cursor, unidades)
    sincronizar_secuencia(cursor, "estandares_actividad")
    cursor.execute(
        """INSERT INTO estandares_actividad
            (actividad_id, unidades_por_hora, costo_mo_unidad, costo_mo_hora)
        VALUES (%s, %s, %s, %s) RETURNING id""",
        (actividad_id, unidades, costo_unidad, costo_hora),
    )
    estandar_id = cursor.fetchone()[0]
    sincronizar_secuencia(cursor, "estandares_actividad")
    return estandar_id

def sincronizar_actividad_en_ordenes_abiertas(cursor, actividad_id: int):
    cursor.execute(
        """INSERT INTO orden_actividades
            (orden_id, actividad_id, cantidad_total, cantidad_realizada)
        SELECT o.id, a.id, o.cantidad, 0
        FROM actividades a
        JOIN procesos p ON p.id = a.proceso_id
        JOIN ordenes o ON o.maquina_id = p.maquina_id
        WHERE a.id = %s AND o.estado != 'CERRADA'
        AND NOT EXISTS (
            SELECT 1 FROM orden_actividades oa WHERE oa.orden_id = o.id AND oa.actividad_id = a.id
        )""",
        (actividad_id,),
    )
    return cursor.rowcount or 0

# --- Rutas del Asistente (Tarjetas) ---
@router.get("/config/catalogos")
def catalogos_configuracion(request: Request):
    if not require_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id, nombre FROM maquinas ORDER BY nombre, id")
    maquinas = [{"id": row[0], "nombre": row[1]} for row in c.fetchall()]
    c.execute("""
        SELECT p.id, p.nombre, p.maquina_id, m.nombre
        FROM procesos p JOIN maquinas m ON m.id = p.maquina_id
        ORDER BY m.nombre, p.nombre, p.id
    """)
    procesos = [{"id": row[0], "nombre": row[1], "maquina_id": row[2], "maquina": row[3]} for row in c.fetchall()]
    conn.close()
    return {"maquinas": maquinas, "procesos": procesos}

@router.post("/config/asistente/proceso")
def crear_proceso_asistente(data: dict, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    maquina_id = entero_obligatorio(data, "maquina_id", "Maquina")
    nombre = texto_obligatorio(data, "nombre", "Nombre del proceso")
    actividad_nombre = str(data.get("actividad_nombre", "")).strip()

    conn = db()
    c = conn.cursor()
    try:
        c.execute("SELECT id FROM maquinas WHERE id = %s", (maquina_id,))
        if not c.fetchone(): raise HTTPException(status_code=400, detail="La maquina seleccionada no existe")
        c.execute("SELECT id FROM procesos WHERE maquina_id = %s AND lower(trim(nombre)) = lower(trim(%s))", (maquina_id, nombre))
        if c.fetchone(): raise HTTPException(status_code=400, detail="Ese proceso ya existe para esta maquina")
        
        sincronizar_secuencia(c, "procesos")
        c.execute("INSERT INTO procesos (maquina_id, nombre) VALUES (%s, %s) RETURNING id", (maquina_id, nombre))
        proceso_id = c.fetchone()[0]
        sincronizar_secuencia(c, "procesos")
        
        actividad_id = None
        estandar_id = None
        ordenes_actualizadas = 0
        
        if actividad_nombre:
            actividad_payload = dict(data)
            actividad_payload["proceso_id"] = proceso_id
            actividad_payload["nombre"] = actividad_nombre
            sincronizar_secuencia(c, "actividades")
            c.execute("INSERT INTO actividades (proceso_id, nombre) VALUES (%s, %s) RETURNING id", (proceso_id, actividad_nombre))
            actividad_id = c.fetchone()[0]
            sincronizar_secuencia(c, "actividades")
            estandar_id = crear_estandar(c, actividad_id, actividad_payload)
            ordenes_actualizadas = sincronizar_actividad_en_ordenes_abiertas(c, actividad_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "proceso_id": proceso_id, "actividad_id": actividad_id, "estandar_id": estandar_id, "ordenes_actualizadas": ordenes_actualizadas}

@router.post("/config/asistente/actividad")
def crear_actividad_asistente(data: dict, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    proceso_id = entero_obligatorio(data, "proceso_id", "Proceso")
    nombre = texto_obligatorio(data, "nombre", "Nombre de la actividad")
    
    conn = db()
    c = conn.cursor()
    try:
        c.execute("SELECT id FROM procesos WHERE id = %s", (proceso_id,))
        if not c.fetchone(): raise HTTPException(status_code=400, detail="El proceso seleccionado no existe")
        c.execute("SELECT id FROM actividades WHERE proceso_id = %s AND lower(trim(nombre)) = lower(trim(%s))", (proceso_id, nombre))
        if c.fetchone(): raise HTTPException(status_code=400, detail="Esa actividad ya existe en este proceso")
        
        sincronizar_secuencia(c, "actividades")
        c.execute("INSERT INTO actividades (proceso_id, nombre) VALUES (%s, %s) RETURNING id", (proceso_id, nombre))
        actividad_id = c.fetchone()[0]
        sincronizar_secuencia(c, "actividades")
        estandar_id = crear_estandar(c, actividad_id, data)
        ordenes_actualizadas = sincronizar_actividad_en_ordenes_abiertas(c, actividad_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "actividad_id": actividad_id, "estandar_id": estandar_id, "ordenes_actualizadas": ordenes_actualizadas}

# --- CRUD MAQUINAS ---
@router.get("/config/maquinas")
def ver_maquinas(request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id, nombre FROM maquinas ORDER BY id")
    datos = [{"id": r[0], "nombre": r[1]} for r in c.fetchall()]
    conn.close()
    return {"ok": True, "data": datos}

@router.post("/config/maquinas")
def crear_maquina(data: dict, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    nombre = texto_obligatorio(data, "nombre", "Nombre")
    conn = db()
    c = conn.cursor()
    sincronizar_secuencia(c, "maquinas")
    c.execute("INSERT INTO maquinas (nombre) VALUES (%s)", (nombre,))
    sincronizar_secuencia(c, "maquinas")
    conn.commit()
    conn.close()
    return {"ok": True}

@router.put("/config/maquinas/{id}")
def editar_maquina(id: int, data: dict, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    nombre = texto_obligatorio(data, "nombre", "Nombre")
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE maquinas SET nombre=%s WHERE id=%s", (nombre, id))
    conn.commit()
    conn.close()
    return {"ok": True}

@router.delete("/config/maquinas/{id}")
def eliminar_maquina(id: int, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    return eliminar_fila_segura("maquinas", id)

# --- CRUD OPERARIOS ---
@router.get("/config/operarios")
def ver_operarios(request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    conn = db()
    c = conn.cursor()
    try:
        c.execute("SELECT id, nombre, activo FROM operarios ORDER BY id")
        datos = [{"id": r[0], "nombre": r[1], "activo": r[2]} for r in c.fetchall()]
    except Exception:
        conn.rollback()
        # Fallback si la columna no existe aún
        c.execute("SELECT id, nombre FROM operarios ORDER BY id")
        datos = [{"id": r[0], "nombre": r[1], "activo": True} for r in c.fetchall()]
    conn.close()
    return {"ok": True, "data": datos}

@router.post("/config/operarios")
def crear_operario(data: dict, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    nombre = texto_obligatorio(data, "nombre", "Nombre")
    conn = db()
    c = conn.cursor()
    sincronizar_secuencia(c, "operarios")
    c.execute("INSERT INTO operarios (nombre) VALUES (%s)", (nombre,))
    sincronizar_secuencia(c, "operarios")
    conn.commit()
    conn.close()
    return {"ok": True}

@router.put("/config/operarios/{id}")
def editar_operario(id: int, data: dict, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    nombre = texto_obligatorio(data, "nombre", "Nombre")
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE operarios SET nombre=%s WHERE id=%s", (nombre, id))
    conn.commit()
    conn.close()
    return {"ok": True}

@router.delete("/config/operarios/{id}")
def eliminar_operario(id: int, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    return eliminar_fila_segura("operarios", id)

# --- CRUD PROCESOS ---
@router.get("/config/procesos")
def ver_procesos(request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT p.id, p.nombre, m.nombre as maquina_nombre, p.maquina_id
        FROM procesos p JOIN maquinas m ON m.id = p.maquina_id ORDER BY p.id
    """)
    datos = [{"id": r[0], "nombre": r[1], "maquina_nombre": r[2], "maquina_id": r[3]} for r in c.fetchall()]
    conn.close()
    return {"ok": True, "data": datos}

@router.post("/config/procesos")
def crear_proceso(data: dict, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    maquina_id = entero_obligatorio(data, "maquina_id", "Maquina")
    nombre = texto_obligatorio(data, "nombre", "Nombre")
    conn = db()
    c = conn.cursor()
    sincronizar_secuencia(c, "procesos")
    c.execute("INSERT INTO procesos (maquina_id, nombre) VALUES (%s, %s)", (maquina_id, nombre))
    sincronizar_secuencia(c, "procesos")
    conn.commit()
    conn.close()
    return {"ok": True}

@router.put("/config/procesos/{id}")
def editar_proceso(id: int, data: dict, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    maquina_id = entero_obligatorio(data, "maquina_id", "Maquina")
    nombre = texto_obligatorio(data, "nombre", "Nombre")
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE procesos SET maquina_id=%s, nombre=%s WHERE id=%s", (maquina_id, nombre, id))
    conn.commit()
    conn.close()
    return {"ok": True}

@router.delete("/config/procesos/{id}")
def eliminar_proceso(id: int, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    return eliminar_fila_segura("procesos", id)

# --- CRUD ACTIVIDADES ---
@router.get("/config/actividades")
def ver_actividades(request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT a.id, a.nombre, p.nombre as proceso_nombre, a.proceso_id
        FROM actividades a JOIN procesos p ON p.id = a.proceso_id ORDER BY a.id
    """)
    datos = [{"id": r[0], "nombre": r[1], "proceso_nombre": r[2], "proceso_id": r[3]} for r in c.fetchall()]
    conn.close()
    return {"ok": True, "data": datos}

@router.post("/config/actividades")
def crear_actividad(data: dict, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    proceso_id = entero_obligatorio(data, "proceso_id", "Proceso")
    nombre = texto_obligatorio(data, "nombre", "Nombre")
    unidades_por_hora = unidades_por_hora_de(data)

    conn = db()
    c = conn.cursor()
    try:
        costo_mo_unidad, costo_mo_hora = costos_desde_unidades_hora(c, unidades_por_hora)
        sincronizar_secuencia(c, "actividades")
        c.execute("INSERT INTO actividades (proceso_id, nombre) VALUES (%s, %s) RETURNING id", (proceso_id, nombre))
        act_id = c.fetchone()[0]
        sincronizar_secuencia(c, "actividades")
        
        # Tambien crea el estandar
        sincronizar_secuencia(c, "estandares_actividad")
        c.execute("""
            INSERT INTO estandares_actividad (actividad_id, unidades_por_hora, costo_mo_unidad, costo_mo_hora)
            VALUES (%s, %s, %s, %s)
        """, (act_id, unidades_por_hora, costo_mo_unidad, costo_mo_hora))
        sincronizar_secuencia(c, "estandares_actividad")
        # Que aparezca de una vez en las órdenes abiertas de ese producto (y en la app)
        sincronizar_actividad_en_ordenes_abiertas(c, act_id)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True}

@router.put("/config/actividades/{id}")
def editar_actividad(id: int, data: dict, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    proceso_id = entero_obligatorio(data, "proceso_id", "Proceso")
    nombre = texto_obligatorio(data, "nombre", "Nombre")
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE actividades SET proceso_id=%s, nombre=%s WHERE id=%s", (proceso_id, nombre, id))
    conn.commit()
    conn.close()
    return {"ok": True}

@router.delete("/config/actividades/{id}")
def eliminar_actividad(id: int, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    return eliminar_fila_segura("actividades", id)

# --- CRUD ESTANDARES ---
@router.get("/config/estandares")
def ver_estandares(request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT e.id, a.nombre as actividad_nombre, e.unidades_por_hora, e.costo_mo_unidad, e.costo_mo_hora
        FROM estandares_actividad e JOIN actividades a ON a.id = e.actividad_id ORDER BY e.id
    """)
    datos = [{
        "id": r[0], "actividad_nombre": r[1], 
        "unidades_por_hora": float(r[2]) if r[2] is not None else 0.0,
        "minutos_unidad": round(60.0 / float(r[2]), 2) if r[2] else None,
        "costo_mo_unidad": float(r[3]) if r[3] is not None else 0.0, 
        "costo_mo_hora": float(r[4]) if r[4] is not None else 0.0
    } for r in c.fetchall()]
    conn.close()
    return {"ok": True, "data": datos}

@router.put("/config/estandares/{id}")
def editar_estandar(id: int, data: dict, request: Request):
    if not require_admin(request): return JSONResponse({"detail": "No autorizado"}, status_code=401)
    unidades = unidades_por_hora_de(data)
    conn = db()
    c = conn.cursor()
    c_unidad, c_hora = costos_desde_unidades_hora(c, unidades)
    c.execute("""
        UPDATE estandares_actividad 
        SET unidades_por_hora=%s, costo_mo_unidad=%s, costo_mo_hora=%s 
        WHERE id=%s
    """, (unidades, c_unidad, c_hora, id))
    conn.commit()
    conn.close()
    return {"ok": True}


# --- VISTA HTML (ASISTENTE) ---
@router.get("/configuracion", response_class=HTMLResponse)
def configuracion(request: Request):
    if "username" not in request.session:
        return RedirectResponse("/admin?next=configuracion", 303)
    if request.session.get("role") != "admin":
        return RedirectResponse("/", 303)
    return request.app.state.templates.TemplateResponse(
        request=request, name="configuracion_hub.html", context={"request": request}
    )


@router.get("/configuracion/asistente", response_class=HTMLResponse)
def configuracion_asistente(request: Request):
    if "username" not in request.session:
        return RedirectResponse("/admin?next=configuracion/asistente", 303)
    if request.session.get("role") != "admin":
        return RedirectResponse("/", 303)
    return request.app.state.templates.TemplateResponse(
        request=request, name="configuracion.html", context={"request": request}
    )
