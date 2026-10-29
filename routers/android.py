import os
from datetime import datetime
from zoneinfo import ZoneInfo
from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Depends, Request
from limiter import limiter
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from auth import hash_password, verify_password
from database import db, sincronizar_actividades_ordenes_abiertas

router = APIRouter()

ANDROID_TOKEN_SALT = "android-operario-token"
ANDROID_TOKEN_MAX_AGE = int(os.getenv("ANDROID_TOKEN_MAX_AGE", "2592000"))


def asegurar_schema_android():
    conn = db()
    c = conn.cursor()
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS debe_cambiar_password BOOLEAN DEFAULT FALSE")
    conn.commit()
    conn.close()


def token_serializer():
    secret_key = os.getenv("SECRET_KEY")
    if not secret_key:
        raise HTTPException(status_code=500, detail="SECRET_KEY no configurado")
    return URLSafeTimedSerializer(secret_key)


def generar_token_android(user_id, username, role, operario_id):
    return token_serializer().dumps(
        {
            "user_id": user_id,
            "username": username,
            "role": role,
            "operario_id": operario_id,
        },
        salt=ANDROID_TOKEN_SALT,
    )


def leer_token_android(authorization: str = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Token Android requerido")

    token = authorization.split(" ", 1)[1].strip()

    try:
        return token_serializer().loads(
            token,
            salt=ANDROID_TOKEN_SALT,
            max_age=ANDROID_TOKEN_MAX_AGE,
        )
    except SignatureExpired as exc:
        raise HTTPException(status_code=401, detail="Sesion expirada") from exc
    except BadSignature as exc:
        raise HTTPException(status_code=401, detail="Token invalido") from exc


def usuario_android_actual(payload: dict = Depends(leer_token_android)):
    asegurar_schema_android()

    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT u.id, u.username, u.role, u.operario_id, o.nombre,
               COALESCE(u.debe_cambiar_password, FALSE)
        FROM users u
        LEFT JOIN operarios o ON o.id = u.operario_id
        WHERE u.id = %s
    """, (payload.get("user_id"),))
    row = c.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")

    user_id, username, role, operario_id, operario_nombre, debe_cambiar = row

    if role != "operario" or not operario_id:
        raise HTTPException(status_code=403, detail="Usuario Android sin operario asignado")

    return {
        "user_id": user_id,
        "username": username,
        "role": role,
        "operario_id": operario_id,
        "operario_nombre": operario_nombre,
        "debe_cambiar_password": bool(debe_cambiar),
    }


def usuario_android_habilitado(usuario=Depends(usuario_android_actual)):
    if usuario.get("debe_cambiar_password"):
        raise HTTPException(
            status_code=403,
            detail="Debe crear su password personal antes de registrar produccion",
        )
    return usuario


def campo_entero(data: dict, *nombres: str) -> int:
    for nombre in nombres:
        valor = data.get(nombre)
        if valor is not None and valor != "":
            return int(valor)

    raise HTTPException(
        status_code=400,
        detail=f"Campo requerido faltante: {'/'.join(nombres)}"
    )


def fecha_android(valor):
    if not valor:
        return None

    texto = str(valor).replace("Z", "+00:00")

    try:
        return datetime.fromisoformat(texto)
    except ValueError:
        return datetime.strptime(str(valor), "%Y-%m-%d %H:%M:%S")


@router.post("/android/login")
@limiter.limit("5/minute")
def login_android(request: Request, data: dict):
    asegurar_schema_android()

    username = str(data.get("username") or data.get("usuario") or "").strip()
    password = str(data.get("password") or data.get("clave") or "")

    if not username or not password:
        raise HTTPException(status_code=400, detail="Usuario y password son requeridos")

    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT u.id, u.username, u.password, u.role, u.operario_id, o.nombre,
               COALESCE(u.debe_cambiar_password, FALSE)
        FROM users u
        LEFT JOIN operarios o ON o.id = u.operario_id
        WHERE (REPLACE(REPLACE(LOWER(u.username), ' ', ''), '.', '') = REPLACE(REPLACE(LOWER(%s), ' ', ''), '.', '') 
            OR REPLACE(REPLACE(LOWER(o.nombre), ' ', ''), '.', '') = REPLACE(REPLACE(LOWER(%s), ' ', ''), '.', ''))
          AND COALESCE(u.activo, TRUE) = TRUE
        ORDER BY CASE WHEN REPLACE(REPLACE(LOWER(u.username), ' ', ''), '.', '') = REPLACE(REPLACE(LOWER(%s), ' ', ''), '.', '') THEN 0 ELSE 1 END
        LIMIT 1
    """, (username, username, username))
    row = c.fetchone()
    conn.close()

    if not row or not verify_password(password, row[2]):
        raise HTTPException(status_code=401, detail="Usuario o password incorrecto")

    user_id, username, _, role, operario_id, operario_nombre, debe_cambiar = row

    if role != "operario" or not operario_id:
        raise HTTPException(status_code=403, detail="Este usuario no tiene operario asignado")

    token = generar_token_android(user_id, username, role, operario_id)

    return {
        "token": token,
        "token_type": "bearer",
        "expires_in": ANDROID_TOKEN_MAX_AGE,
        "usuario": {
            "id": user_id,
            "username": username,
            "role": role,
        },
        "operario": {
            "id": operario_id,
            "nombre": operario_nombre,
        },
        "debe_cambiar_password": bool(debe_cambiar),
    }


@router.post("/android/cambiar_password")
def cambiar_password_android(data: dict, usuario=Depends(usuario_android_actual)):
    nueva_password = str(
        data.get("nueva_password") or data.get("new_password") or data.get("password") or ""
    ).strip()
    confirmar_password = str(
        data.get("confirmar_password") or data.get("confirm_password") or nueva_password
    ).strip()

    if not nueva_password:
        raise HTTPException(status_code=400, detail="Nuevo password requerido")

    if len(nueva_password) < 4:
        raise HTTPException(status_code=400, detail="El password debe tener minimo 4 caracteres")

    if nueva_password != confirmar_password:
        raise HTTPException(status_code=400, detail="Los passwords no coinciden")

    conn = db()
    c = conn.cursor()
    c.execute(
        """
        UPDATE users
        SET password = %s,
            debe_cambiar_password = FALSE
        WHERE id = %s
        """,
        (hash_password(nueva_password), usuario["user_id"]),
    )
    conn.commit()
    conn.close()

    return {"status": "ok", "debe_cambiar_password": False}


@router.get("/android/me")
def android_me(usuario=Depends(usuario_android_actual)):
    return {
        "usuario": {
            "id": usuario["user_id"],
            "username": usuario["username"],
            "role": usuario["role"],
        },
        "operario": {
            "id": usuario["operario_id"],
            "nombre": usuario["operario_nombre"],
        },
        "debe_cambiar_password": usuario["debe_cambiar_password"],
    }


# ================= REGISTRO ANDROID =================

def guardar_registro_android(data: dict, operario_id: int):

    try:
        orden_id = campo_entero(data, "orden_id", "orden")
        # Ya no requerimos estrictamente actividad_id > 0
        actividad_id = int(data.get("actividad_id") or data.get("actividad") or 0)
        cantidad = campo_entero(data, "cantidad")
        tiempo = int(data.get("tiempo") or 0)
    except ValueError:
        raise HTTPException(status_code=400, detail="Campos numericos invalidos")

    actividad_nombre = data.get("actividad_nombre")

    if actividad_id <= 0 and not actividad_nombre:
        raise HTTPException(status_code=400, detail="Debe proporcionar un id de actividad o un nombre de actividad")

    conn = db()
    c = conn.cursor()

    # --- LÓGICA DE ACTIVIDAD NUEVA O RECUPERADA ---
    if actividad_id <= 0 and actividad_nombre:
        # Intentar resolver el ID si la actividad ya existe en esta orden (caso de reanudación en Android)
        c.execute("""
            SELECT a.id 
            FROM orden_actividades oa
            JOIN actividades a ON a.id = oa.actividad_id
            WHERE oa.orden_id = %s AND LOWER(TRIM(a.nombre)) = LOWER(TRIM(%s))
            LIMIT 1
        """, (orden_id, actividad_nombre))
        row = c.fetchone()
        
        if row:
            actividad_id = row[0]
        else:
            # Buscar si la actividad existe globalmente en otra orden o proceso
            c.execute("SELECT id FROM actividades WHERE LOWER(TRIM(nombre)) = LOWER(TRIM(%s)) LIMIT 1", (actividad_nombre,))
            row_global = c.fetchone()
            
            if row_global:
                actividad_id = row_global[0]
            else:
                # Si no existe en absoluto, y como viene de una app antigua (que enviaba el nombre), la creamos
                # para no hacerle perder al operario las horas trabajadas.
                # Se asume un proceso_id genérico o NULL si la BD lo permite. 
                # Buscaremos un proceso de la orden para asociarla, o el primero que exista.
                c.execute("SELECT id FROM procesos WHERE maquina_id = (SELECT maquina_id FROM ordenes WHERE id = %s) LIMIT 1", (orden_id,))
                proc_row = c.fetchone()
                proceso_id = proc_row[0] if proc_row else None
                
                c.execute("INSERT INTO actividades (nombre, proceso_id) VALUES (%s, %s) RETURNING id", (actividad_nombre, proceso_id))
                actividad_id = c.fetchone()[0]

            # Ahora insertamos la actividad en orden_actividades con cantidad_total = 0 si no estaba
            c.execute("""
                INSERT INTO orden_actividades (orden_id, actividad_id, cantidad_total, cantidad_realizada)
                VALUES (%s, %s, 0, 0)
                ON CONFLICT DO NOTHING
            """, (orden_id, actividad_id))

    inicio = fecha_android(data.get("inicio"))
    fin = fecha_android(data.get("fin"))

    if inicio and fin:
        tiempo = max(0, int((fin - inicio).total_seconds()))
    else:
        fin = datetime.now(ZoneInfo("America/Bogota"))
        inicio = fin - timedelta(seconds=tiempo)

    c.execute("""
    INSERT INTO registros_produccion
    (operario_id, orden_id, actividad_id, cantidad, inicio, fin, tiempo)
    VALUES (%s,%s,%s,%s,%s,%s,%s)
    """,(
        operario_id,
        orden_id,
        actividad_id,
        cantidad,
        inicio.strftime("%Y-%m-%d %H:%M:%S"),
        fin.strftime("%Y-%m-%d %H:%M:%S"),
        tiempo
    ))

    # actualizar actividad
    c.execute("""
    UPDATE orden_actividades
    SET cantidad_realizada = cantidad_realizada + %s
    WHERE orden_id=%s AND actividad_id=%s
    """,(
        cantidad,
        orden_id,
        actividad_id
    ))

    # recalcular porcentaje general
    c.execute("""
        SELECT SUM(cantidad_realizada),
               SUM(cantidad_total)
        FROM orden_actividades
        WHERE orden_id=%s
    """,(orden_id,))

    row = c.fetchone()

    if row and row[1] and row[1] > 0:
        porcentaje = round((row[0] / row[1]) * 100, 2)
    else:
        porcentaje = 0

    c.execute("""
        UPDATE ordenes
        SET porcentaje=%s,
            estado=CASE WHEN %s >= 100 THEN 'CERRADA' ELSE estado END
        WHERE id=%s
    """,(porcentaje, porcentaje, orden_id))

    conn.commit()
    conn.close()

    return {"status":"ok"}


@router.post("/registro_android")
def registro_android(data: dict, usuario=Depends(usuario_android_habilitado)):
    return guardar_registro_android(data, usuario["operario_id"])


# ================= LISTAS PARA ANDROID =================

@router.get("/operarios")
def operarios():
    conn = db()
    c = conn.cursor()

    try:
        c.execute("SELECT id, nombre FROM operarios WHERE activo = TRUE")
        rows = c.fetchall() or []
    except Exception:
        conn.rollback()
        c.execute("SELECT id, nombre FROM operarios")
        rows = c.fetchall() or []

    conn.close()

    return [{"id": r[0], "nombre": r[1]} for r in rows]


@router.get("/maquinas")
def maquinas():
    conn = db()
    c = conn.cursor()

    c.execute("SELECT id,nombre FROM maquinas")
    rows = c.fetchall() or []

    conn.close()

    return [{"id": r[0], "nombre": r[1]} for r in rows]


@router.get("/ordenes")
def ordenes_android():
    conn = db()
    c = conn.cursor()

    c.execute("""
    SELECT 
        o.id,
        o.maquina_id,
        m.nombre AS producto,
        COALESCE(o.cantidad, 0) AS cantidad,
        o.estado,
        o.porcentaje,
        o.cerrado_en
    FROM ordenes o
    JOIN maquinas m ON m.id = o.maquina_id
    WHERE o.estado != 'CERRADA'
    ORDER BY o.id DESC
    """)

    rows = c.fetchall() or []

    conn.close()

    return [
        {
            "id": r[0],
            "maquina_id": r[1],
            "producto": r[2],
            "cantidad": r[3],
            "estado": r[4],
            "porcentaje": r[5],
            "cerrado_en": r[6]
        }
        for r in rows
    ]


@router.get("/procesos/{orden_id}")
def procesos_android(orden_id:int):
    conn = db()
    c = conn.cursor()

    # Sincronizar automáticamente cualquier actividad nueva añadida a la máquina
    sincronizar_actividades_ordenes_abiertas(c, orden_id)
    conn.commit()

    c.execute("""
    SELECT DISTINCT p.id, p.nombre
    FROM procesos p
    LEFT JOIN actividades a ON a.proceso_id = p.id
    LEFT JOIN orden_actividades oa ON oa.actividad_id = a.id AND oa.orden_id = %s
    WHERE COALESCE(p.activo, TRUE) = TRUE
      AND ((oa.orden_id = %s AND COALESCE(a.activo, TRUE) = TRUE)
       OR (UPPER(p.nombre) = 'OTROS' AND p.maquina_id = (SELECT maquina_id FROM ordenes WHERE id = %s)))
    """, (orden_id, orden_id, orden_id))
    rows = c.fetchall() or []

    conn.close()

    return [{"id": r[0], "nombre": r[1]} for r in rows]


@router.get("/actividades/{orden}/{proceso}")
def actividades_android(orden:int, proceso:int):
    conn = db()
    c = conn.cursor()

    c.execute("""
    SELECT a.id,a.nombre
    FROM orden_actividades oa
    JOIN actividades a ON a.id=oa.actividad_id
    JOIN procesos p ON p.id=a.proceso_id
    WHERE oa.orden_id=%s AND a.proceso_id=%s
      AND COALESCE(a.activo, TRUE) = TRUE
      AND COALESCE(p.activo, TRUE) = TRUE
    """,(orden,proceso))
    rows = c.fetchall() or []

    conn.close()

    return [{"id": r[0], "nombre": r[1]} for r in rows]