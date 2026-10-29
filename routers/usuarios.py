import re
import unicodedata
from urllib.parse import quote

import psycopg2

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from database import db
from auth import hash_password, require_admin

router = APIRouter()

templates = Jinja2Templates(directory="templates")

PASSWORD_TEMPORAL_DEFAULT = "123456"


def asegurar_schema_usuarios():
    conn = db()
    c = conn.cursor()
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS debe_cambiar_password BOOLEAN DEFAULT FALSE")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS telefono TEXT")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE")
    conn.commit()
    conn.close()


def slug_usuario(nombre: str) -> str:
    texto = unicodedata.normalize("NFKD", nombre or "")
    texto = texto.encode("ascii", "ignore").decode("ascii")
    texto = re.sub(r"[^a-zA-Z0-9]+", ".", texto).strip(".").lower()
    return texto or "operario"


def usuario_disponible(cursor, base: str) -> str:
    candidato = base
    sufijo = 2

    while True:
        cursor.execute("SELECT 1 FROM users WHERE username = %s", (candidato,))
        if not cursor.fetchone():
            return candidato

        candidato = f"{base}.{sufijo}"
        sufijo += 1


@router.get("/usuarios", response_class=HTMLResponse)
def ver_usuarios(request: Request, error: str = None):

    if not require_admin(request):
        return RedirectResponse("/admin", 303)

    asegurar_schema_usuarios()

    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT u.id, u.username, u.role, u.operario_id, COALESCE(o.nombre, ''),
               COALESCE(u.debe_cambiar_password, FALSE), COALESCE(u.telefono, ''),
               COALESCE(u.activo, TRUE)
        FROM users u
        LEFT JOIN operarios o ON o.id = u.operario_id
        ORDER BY COALESCE(u.activo, TRUE) DESC, u.id
    """)
    usuarios = c.fetchall()

    # Solo operarios activos que todavía no tienen usuario
    c.execute("""
        SELECT o.id, o.nombre FROM operarios o
        WHERE COALESCE(o.activo, TRUE) = TRUE
          AND NOT EXISTS (SELECT 1 FROM users u WHERE u.operario_id = o.id)
        ORDER BY o.nombre
    """)
    operarios = c.fetchall()

    conn.close()

    return templates.TemplateResponse(
        request=request, name="usuarios.html", context={
        "request": request,
        "usuarios": usuarios,
        "operarios": operarios,
        "error": error,
        "ok": request.query_params.get("ok"),
        "password_temporal_default": PASSWORD_TEMPORAL_DEFAULT
    })


ROLES_VALIDOS = ("admin", "operario", "jefe_tickets")


def volver(error: str = None, ok: str = None):
    if error:
        return RedirectResponse(f"/usuarios?error={quote(error)}", 303)
    if ok:
        return RedirectResponse(f"/usuarios?ok={quote(ok)}", 303)
    return RedirectResponse("/usuarios", 303)


def clave_usuario(texto: str) -> str:
    # Igual que el login: sin mayúsculas, espacios ni puntos
    return (texto or "").lower().replace(" ", "").replace(".", "")


@router.post("/usuarios/crear")
def crear_usuario(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    operario_id: str = Form(""),
    operario_nuevo: str = Form(""),
    debe_cambiar_password: str = Form(""),
    telefono: str = Form("")
):

    if not require_admin(request):
        return RedirectResponse("/admin", 303)

    username = username.strip()
    operario_nuevo = operario_nuevo.strip()
    telefono = telefono.strip()

    if len(username) < 3 or len(username) > 50:
        return volver("El usuario debe tener entre 3 y 50 caracteres")
    if len(password) < 4:
        return volver("El password debe tener mínimo 4 caracteres")
    if role not in ROLES_VALIDOS:
        return volver("Rol no válido")

    asegurar_schema_usuarios()

    conn = db()
    c = conn.cursor()

    try:
        c.execute("SELECT username FROM users")
        if any(clave_usuario(r[0]) == clave_usuario(username) for r in c.fetchall()):
            return volver(f"Ya existe un usuario «{username}» (o uno igual sin puntos ni espacios)")

        operario_id_valor = None
        if role == "operario":
            if operario_id == "nuevo":
                if len(operario_nuevo) < 3:
                    return volver("Escriba el nombre completo del operario nuevo")
                c.execute(
                    "SELECT id FROM operarios WHERE lower(trim(nombre)) = lower(trim(%s))",
                    (operario_nuevo,),
                )
                existente = c.fetchone()
                if existente:
                    operario_id_valor = existente[0]
                    c.execute("UPDATE operarios SET activo = TRUE WHERE id = %s", (operario_id_valor,))
                else:
                    c.execute(
                        "SELECT setval(pg_get_serial_sequence('operarios', 'id'), COALESCE((SELECT MAX(id) FROM operarios), 1))"
                    )
                    c.execute("INSERT INTO operarios (nombre) VALUES (%s) RETURNING id", (operario_nuevo,))
                    operario_id_valor = c.fetchone()[0]
            elif operario_id.isdigit():
                operario_id_valor = int(operario_id)
            else:
                return volver("Seleccione el operario o elija «Operario nuevo» y escriba su nombre")

            c.execute("SELECT id FROM users WHERE operario_id = %s", (operario_id_valor,))
            if c.fetchone():
                conn.rollback()
                return volver("Ese operario ya tiene un usuario. Edítelo o resetee su clave en la lista.")

        debe_cambiar = role == "operario" and debe_cambiar_password == "on"

        c.execute(
            """
            INSERT INTO users
                (username, password, role, operario_id, debe_cambiar_password, telefono)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (username, hash_password(password), role, operario_id_valor, debe_cambiar, telefono)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return volver(f"No se pudo crear el usuario: {e}")
    finally:
        conn.rollback()  # no-op tras commit; descarta lo pendiente si se salió antes
        conn.close()

    return volver(ok=f"Usuario «{username}» creado")


@router.post("/usuarios/crear_operarios")
def crear_usuarios_operarios(
    request: Request,
    password_temporal: str = Form(PASSWORD_TEMPORAL_DEFAULT)
):

    if not require_admin(request):
        return RedirectResponse("/admin", 303)

    asegurar_schema_usuarios()

    password_temporal = (password_temporal or PASSWORD_TEMPORAL_DEFAULT).strip()
    if len(password_temporal) < 4:
        return "El password temporal debe tener minimo 4 caracteres"

    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT o.id, o.nombre
        FROM operarios o
        LEFT JOIN users u ON u.operario_id = o.id
        WHERE u.id IS NULL
          AND COALESCE(o.activo, TRUE) = TRUE
        ORDER BY o.nombre
    """)
    operarios_sin_usuario = c.fetchall()

    creados = 0
    hashed = hash_password(password_temporal)

    for operario_id, nombre in operarios_sin_usuario:
        username = usuario_disponible(c, slug_usuario(nombre))
        c.execute(
            """
            INSERT INTO users
                (username, password, role, operario_id, debe_cambiar_password)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (username, hashed, "operario", operario_id, True),
        )
        creados += 1

    conn.commit()
    conn.close()

    return RedirectResponse(f"/usuarios?creados={creados}", 303)


@router.post("/usuarios/reset_password/{user_id}")
def reset_password_usuario(request: Request, user_id: int):

    if not require_admin(request):
        return RedirectResponse("/admin", 303)

    asegurar_schema_usuarios()

    conn = db()
    c = conn.cursor()
    c.execute(
        """
        UPDATE users
        SET password = %s,
            debe_cambiar_password = TRUE
        WHERE id = %s AND role = 'operario'
        """,
        (hash_password(PASSWORD_TEMPORAL_DEFAULT), user_id),
    )
    conn.commit()
    conn.close()

    return RedirectResponse("/usuarios?reset=1", 303)


@router.post("/usuarios/eliminar/{user_id}")
def eliminar_usuario(request: Request, user_id: int):

    if not require_admin(request):
        return RedirectResponse("/admin", 303)

    conn = db()
    c = conn.cursor()

    # Obtener info del usuario a borrar
    c.execute("SELECT username, role FROM users WHERE id = %s", (user_id,))
    target_user = c.fetchone()
    if not target_user:
        conn.close()
        return RedirectResponse("/usuarios", 303)
        
    target_username, target_role = target_user

    # Regla 1: No auto-borrarse
    if target_username == request.session.get("username"):
        conn.close()
        return RedirectResponse("/usuarios?error=No+puedes+eliminar+tu+propio+usuario", 303)

    # Regla 2: No borrar al último admin activo
    if target_role == "admin":
        c.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND COALESCE(activo, TRUE) = TRUE AND id <> %s", (user_id,))
        if c.fetchone()[0] == 0:
            conn.close()
            return volver("No puedes eliminar al último administrador")

    # Si el usuario tiene historial (tickets, notas, cierres de bonos), la base no deja borrarlo:
    # se desactiva para que no pueda entrar y se conserva el historial.
    try:
        c.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
        mensaje = f"Usuario «{target_username}» eliminado"
    except psycopg2.errors.ForeignKeyViolation:
        conn.rollback()
        c.execute("UPDATE users SET activo = FALSE WHERE id = %s", (user_id,))
        conn.commit()
        mensaje = (f"«{target_username}» tiene historial (tickets o registros), así que se desactivó en vez de borrarse: "
                   "ya no puede entrar. Puede reactivarlo cuando quiera.")
    finally:
        conn.close()

    return volver(ok=mensaje)


@router.post("/usuarios/reactivar/{user_id}")
def reactivar_usuario(request: Request, user_id: int):
    if not require_admin(request):
        return RedirectResponse("/admin", 303)

    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET activo = TRUE WHERE id = %s RETURNING username", (user_id,))
    fila = c.fetchone()
    conn.commit()
    conn.close()
    return volver(ok=f"Usuario «{fila[0]}» reactivado") if fila else volver()


@router.get("/usuarios/editar/{user_id}", response_class=HTMLResponse)
def editar_usuario_form(request: Request, user_id: int):
    if not require_admin(request):
        return RedirectResponse("/admin", 303)

    asegurar_schema_usuarios()
    
    conn = db()
    c = conn.cursor()
    
    c.execute("SELECT id, username, role, operario_id, telefono FROM users WHERE id = %s", (user_id,))
    usuario = c.fetchone()
    
    c.execute("SELECT id, nombre FROM operarios ORDER BY nombre")
    operarios = c.fetchall()
    conn.close()
    
    if not usuario:
        return RedirectResponse("/usuarios", 303)

    return templates.TemplateResponse(
        request=request, name="usuario_editar.html", context={
        "request": request,
        "usuario": usuario,
        "operarios": operarios
    })


@router.post("/usuarios/editar/{user_id}")
def editar_usuario_post(
    request: Request,
    user_id: int,
    role: str = Form(...),
    operario_id: str = Form(""),
    telefono: str = Form("")
):
    if not require_admin(request):
        return RedirectResponse("/admin", 303)

    asegurar_schema_usuarios()

    if role not in ROLES_VALIDOS:
        return volver("Rol no válido")
    operario_id_valor = int(operario_id) if operario_id.isdigit() else None
    if role != "operario":
        operario_id_valor = None
    elif not operario_id_valor:
        return volver("Un usuario operario debe estar vinculado a un operario")

    conn = db()
    c = conn.cursor()
    c.execute(
        """
        UPDATE users
        SET role = %s,
            operario_id = %s,
            telefono = %s
        WHERE id = %s
        """,
        (role, operario_id_valor, telefono.strip(), user_id)
    )
    conn.commit()
    conn.close()

    return volver(ok="Usuario actualizado")

    return RedirectResponse("/usuarios", 303)