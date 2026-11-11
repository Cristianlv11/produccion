
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from auth import login_user, hash_password
from database import db
from limiter import limiter

router = APIRouter()
templates = Jinja2Templates(directory="templates")

@router.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    if request.session.get("username"):
        role = request.session.get("role")
        if role == "admin": return RedirectResponse("/", status_code=303)
        elif role == "jefe_tickets": return RedirectResponse("/tickets/admin", status_code=303)
        elif role == "operario": return RedirectResponse("/inicio_operario", status_code=303)
        return RedirectResponse("/", status_code=303)
        
    return templates.TemplateResponse(
        request=request, name="login.html", context={"request": request})

@router.post("/admin")
@limiter.limit("5/minute")
def admin_post(request: Request, user: str = Form(...), password: str = Form(...)):
    if login_user(request, user, password):
        if request.session.get("role") == "operario" and request.session.get("debe_cambiar_password"):
            next_page = "/cambiar_password"
        else:
            next_page = request.query_params.get("next")
            
        if not next_page or next_page == "None":
            role = request.session.get("role")
            if role == "admin":
                next_page = "/"
            elif role == "jefe_tickets":
                next_page = "/tickets/admin"
            elif role == "operario":
                next_page = "/inicio_operario"
            elif role == "supervisor":
                next_page = "/produccion_en_proceso"
            else:
                next_page = "/"

        return RedirectResponse(next_page, status_code=303)

    return templates.TemplateResponse(
        request=request, name="login.html", context={"request": request, "error": "Usuario o contraseña incorrectos"}
    )

@router.get("/cambiar_password", response_class=HTMLResponse)
def cambiar_password_web(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="cambiar_password.html",
        context={"request": request, "error": ""},
    )

@router.post("/cambiar_password", response_class=HTMLResponse)
def cambiar_password_web_post(
    request: Request,
    nueva_password: str = Form(...),
    confirmar_password: str = Form(...)
):
    nueva_password = nueva_password.strip()
    confirmar_password = confirmar_password.strip()

    error = ""
    if not nueva_password:
        error = "Nuevo password requerido"
    elif len(nueva_password) < 4:
        error = "El password debe tener minimo 4 caracteres"
    elif nueva_password != confirmar_password:
        error = "Los passwords no coinciden"

    if error:
        return templates.TemplateResponse(
            request=request,
            name="cambiar_password.html",
            context={"request": request, "error": error},
            status_code=400,
        )

    conn = db()
    c = conn.cursor()
    c.execute(
        """
        UPDATE users
        SET password = %s,
            debe_cambiar_password = FALSE
        WHERE username = %s
          AND role = 'operario'
        """,
        (hash_password(nueva_password), request.session["username"]),
    )
    conn.commit()
    conn.close()

    request.session["debe_cambiar_password"] = False
    return RedirectResponse("/registro_web", status_code=303)

@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin", status_code=303)
