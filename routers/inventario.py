"""
Inventario del almacén (perfil de Carlos, rol «inventario»; el admin también entra).

- Materiales: lo que hay en el almacén (láminas, tornillería, consumibles…), con stock y precio.
  Cada entrada, entrega o ajuste queda en inv_movimientos con el stock que quedó.
- Fundición y mecanizado: piezas que se mandan a terceros (pecheras, volantes, cilindros…),
  contadas por estado: llegó de fundición sin procesar, en el taller de mecanizado, para arreglar,
  para devolver a fundición, y lista en planta.
- Herramientas asignadas a cada colaborador.

La carga inicial sale de los Excel de Carlos (scripts/importar_inventario.py).
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from database import db

router = APIRouter()
TZ = ZoneInfo("America/Bogota")

ROLES_INVENTARIO = ("admin", "inventario")

CATEGORIAS = [
    "Láminas",
    "Aceros, tubos y ejes",
    "Tornillería",
    "Rodamientos y transmisión",
    "Eléctricos y motores",
    "Pintura y químicos",
    "Gases",
    "Herramientas y consumibles",
    "Caucho y plásticos",
    "Dotación y seguridad",
    "Otros",
]

# Palabras que deciden la categoría al importar (la primera que coincide gana; Carlos puede cambiarla luego)
PALABRAS_CATEGORIA = [
    ("Láminas", ("lamina", "lámina", "laminas")),
    ("Gases", ("g.l.p", "glp", " gas ", "cilindro mezcla", "oxigeno", "oxígeno", "nitrogeno", "nitrógeno", "argon", "argón", "acetileno", "co2")),
    ("Tornillería", ("tornillo", "tuerca", "arandela", "guasa", "remache", "prisionero", "esparrago", "espárrago", "perno",
                     "chazo", "puntilla", "grapa", "pasador", "chaveta", "nivelador", "perilla", "guaza", "mariposa", "tonillo")),
    ("Rodamientos y transmisión", ("chumacera", "chumasera", "chumcera", "retenedor", "brisagra", "rodamiento", "balinera", "inserto", "cadena", "piñon", "piñón", "polea",
                                   "correa", "camisa", "buje", "volante", "rodachin", "rodachín", "rueda", "bisagra")),
    ("Eléctricos y motores", ("motor", "terminal", "cable", "breaker", "presostato", "interruptor", "contactor", " rele ", "relé",
                              "enchufe", "clavija", "bombillo", " led ", "tablero", "variador", "ventilador", "estacion de mando", "relevo", "caja de inyeccion")),
    ("Pintura y químicos", ("aerosol", "pintura", "primer", "thinner", "disolvente", "esmalte", "gricoat", "laca", "anticorrosivo",
                            "masilla", "silicona", "pegante", "desengrasante", "aceite", "grasa", "wash", "poliuretano", "polibretano",
                            "poliester", "poker", "catalizador", "caralz", " ral ", "antiseize", "trabador")),
    ("Aceros, tubos y ejes", ("acero", "tubo", "eje ", "platina", "angulo", "ángulo", "varilla", "perfil", "barra", "canal ",
                              "hierro", "inoxidable", "tuberia", "tubería", "niple", "malla")),
    ("Caucho y plásticos", ("caucho", "goma", "empaque", "manguera", "amarre", "plastico", "plástico", "espuma", "polietileno")),
    ("Dotación y seguridad", ("camiseta", "careta", "lentes", "guante", "bota", "overol", "tapabocas", "casco", "protector",
                              "gafas", "gafa ", "delantal", "chaqueta", "polo", "jean ", "chaleco", "mascara", "máscara", "cartucho",
                              "filtro 3m", "filtros conicos", "encapsulado", "arnes", "arnés", "acetato", "bordado", "anclaje")),
    ("Herramientas y consumibles", ("broca", "machuelo", "fresa", "disco", "lija", "pincel", "cinta", "electrodo", "alambre",
                                    "boquilla", "tobera", "segueta", "lima", "brocha", "rodillo", "estopa", "frescasa", "tabla",
                                    "alicate", "avellanador", "adaptador", "calibrador", "flexometro", "flexómetro", "grata", "llave",
                                    "martillo", "prensa", "pulidora", "pulidra", "compresor", "brush", "soldadura", "punta de contacto",
                                    "pomada", "marciano", "machelo", "disc flap", "zuncho", "vinipel")),
]

TIPOS_MOV = {
    "entrada": "Entrada",
    "entrega": "Entrega",
    "ajuste": "Ajuste de conteo",
}

ESTADOS_PIEZA = [
    ("sin_procesar", "Llegó de fundición (sin procesar)"),
    ("en_mecanizado", "En el taller de mecanizado"),
    ("arreglar", "Mecanizado: arreglar"),
    ("devolver", "Para devolver a fundición"),
    ("en_planta", "Lista en planta"),
]
NOMBRE_ESTADO = dict(ESTADOS_PIEZA)
COLUMNAS_ESTADO = {k for k, _ in ESTADOS_PIEZA}

# Movimientos de las piezas de fundición/mecanizado: (texto, de, a). None = entra o sale de la planta.
MOVS_PIEZA = {
    "llega_fundicion": ("Llega de fundición", None, "sin_procesar"),
    "sale_mecanizado": ("Sale a mecanizado", "sin_procesar", "en_mecanizado"),
    "llega_mecanizado": ("Llega de mecanizado", "en_mecanizado", "en_planta"),
    "a_arreglar": ("Para arreglar", "en_planta", "arreglar"),
    "arreglada": ("Arreglada", "arreglar", "en_planta"),
    "a_devolver": ("Marcar para devolver", "sin_procesar", "devolver"),
    "devuelta": ("Devuelta a fundición", "devolver", None),
    "entrega": ("Entrega a producción", "en_planta", None),
}
# Textos de los movimientos que vienen de las hojas del Excel (solo historial)
TEXTOS_IMPORTADOS = {
    "imp_llega_fundicion": "Llegó de fundición",
    "imp_devuelta": "Devuelta a fundición",
    "imp_sale_mecanizado": "Salió a mecanizado",
    "imp_llega_mecanizado": "Llegó de mecanizado",
}


def categoria_de(nombre: str) -> str:
    n = f" {(nombre or '').lower()} "
    for categoria, palabras in PALABRAS_CATEGORIA:
        if any(p in n for p in palabras):
            return categoria
    return "Otros"


def asegurar_schema_inventario(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inv_materiales(
            id SERIAL PRIMARY KEY,
            codigo TEXT UNIQUE,
            nombre TEXT NOT NULL,
            categoria TEXT NOT NULL DEFAULT 'Otros',
            unidad TEXT NOT NULL DEFAULT 'und',
            stock NUMERIC NOT NULL DEFAULT 0,
            stock_minimo NUMERIC,
            precio NUMERIC,
            ubicacion TEXT,
            nota TEXT,
            activo BOOLEAN DEFAULT TRUE,
            creado TIMESTAMP DEFAULT NOW()
        )""")
    # Peso de una unidad: la tornillería se puede pesar en vez de contar
    cursor.execute("ALTER TABLE inv_materiales ADD COLUMN IF NOT EXISTS peso_gr NUMERIC")
    # Pesos de referencia (Excel de tornillería de febrero) para escoger el peso de cada material
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inv_pesos_ref(
            id SERIAL PRIMARY KEY,
            nombre TEXT NOT NULL,
            peso_gr NUMERIC NOT NULL
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inv_movimientos(
            id SERIAL PRIMARY KEY,
            material_id INTEGER NOT NULL REFERENCES inv_materiales(id) ON DELETE CASCADE,
            tipo TEXT NOT NULL,
            cantidad NUMERIC NOT NULL,
            stock_despues NUMERIC,
            precio NUMERIC,
            destino TEXT,
            nota TEXT,
            fecha DATE NOT NULL DEFAULT CURRENT_DATE,
            usuario TEXT,
            creado TIMESTAMP DEFAULT NOW()
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inv_piezas(
            id SERIAL PRIMARY KEY,
            codigo TEXT UNIQUE,
            nombre TEXT NOT NULL,
            medida TEXT,
            sin_procesar NUMERIC NOT NULL DEFAULT 0,
            en_mecanizado NUMERIC NOT NULL DEFAULT 0,
            arreglar NUMERIC NOT NULL DEFAULT 0,
            devolver NUMERIC NOT NULL DEFAULT 0,
            en_planta NUMERIC NOT NULL DEFAULT 0,
            nota TEXT,
            activo BOOLEAN DEFAULT TRUE
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inv_movs_piezas(
            id SERIAL PRIMARY KEY,
            pieza_id INTEGER REFERENCES inv_piezas(id) ON DELETE CASCADE,
            pieza_texto TEXT,
            tipo TEXT NOT NULL,
            cantidad NUMERIC NOT NULL,
            taller TEXT,
            nota TEXT,
            fecha DATE,
            importado BOOLEAN DEFAULT FALSE,
            usuario TEXT,
            creado TIMESTAMP DEFAULT NOW()
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inv_herramientas(
            id SERIAL PRIMARY KEY,
            persona TEXT NOT NULL,
            herramientas TEXT NOT NULL,
            cantidad INTEGER,
            actualizado TIMESTAMP DEFAULT NOW()
        )""")


def _permitido(request: Request):
    if not (request.session.get("username") and request.session.get("role") in ROLES_INVENTARIO):
        raise HTTPException(status_code=401, detail="Debe iniciar sesión con un usuario de inventario")


def _num(valor, campo="cantidad", positivo=True) -> Decimal:
    try:
        n = Decimal(str(valor).replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        raise HTTPException(status_code=400, detail=f"Escriba un número en {campo}")
    if not n.is_finite() or (positivo and n <= 0):
        raise HTTPException(status_code=400, detail=f"{campo.capitalize()} debe ser mayor que cero")
    return n


def _fecha(valor) -> date:
    if not valor:
        return datetime.now(TZ).date()
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail="Fecha no válida")


def fmt(n) -> str:
    """Cantidades a la colombiana y sin decimales de sobra: 2.602 · 86,1 · 114,85."""
    if n is None:
        return ""
    d = Decimal(n).quantize(Decimal("0.01")).normalize()
    signo = "-" if d < 0 else ""
    texto = f"{abs(d):f}"
    entero, _, dec = texto.partition(".")
    entero = f"{int(entero):,}".replace(",", ".")
    return signo + entero + ("," + dec if dec else "")


def pesos(n) -> str:
    if n is None:
        return ""
    return "$" + f"{round(Decimal(n)):,}".replace(",", ".")


def _plantilla(request: Request, nombre: str, contexto: dict):
    contexto = {"request": request, "fmt": fmt, "pesos": pesos, **contexto}
    return request.app.state.templates.TemplateResponse(request=request, name=nombre, context=contexto)


# ===================== Materiales =====================

@router.get("/inventario", response_class=HTMLResponse)
def ver_inventario(request: Request):
    _permitido(request)
    conn = db()
    c = conn.cursor()
    asegurar_schema_inventario(c)
    conn.commit()
    c.execute("""
        SELECT m.id, m.codigo, m.nombre, m.categoria, m.unidad, m.stock, m.stock_minimo, m.precio, m.ubicacion, m.nota,
               (SELECT MAX(fecha) FROM inv_movimientos v WHERE v.material_id = m.id), m.peso_gr
        FROM inv_materiales m WHERE COALESCE(m.activo, TRUE) = TRUE
        ORDER BY m.categoria, m.nombre
    """)
    materiales = [{
        "id": r[0], "codigo": r[1] or "", "nombre": r[2], "categoria": r[3], "unidad": r[4],
        "stock": r[5], "minimo": r[6], "precio": r[7], "ubicacion": r[8] or "", "nota": r[9] or "",
        "valor": (r[5] * r[7]) if (r[7] is not None and r[5] > 0) else None,
        "ultimo": r[10].strftime("%d/%m/%Y") if r[10] else "",
        "peso": r[11],
        "bajo": r[6] is not None and r[5] <= r[6],
        "negativo": r[5] < 0,
    } for r in c.fetchall()]
    c.execute("SELECT nombre, peso_gr FROM inv_pesos_ref ORDER BY nombre")
    pesos_ref = [{"nombre": r[0], "peso": r[1]} for r in c.fetchall()]
    conn.close()
    por_cat = {}
    for m in materiales:
        por_cat[m["categoria"]] = por_cat.get(m["categoria"], 0) + 1
    resumen = {
        "total": len(materiales),
        "valor": sum((m["valor"] for m in materiales if m["valor"]), Decimal(0)),
        "sin_precio": sum(1 for m in materiales if m["precio"] is None),
        "bajos": sum(1 for m in materiales if m["bajo"]),
        "negativos": sum(1 for m in materiales if m["negativo"]),
    }
    categorias = [(cat, por_cat.get(cat, 0)) for cat in CATEGORIAS]
    return _plantilla(request, "inventario.html", {
        "materiales": materiales, "categorias": categorias, "lista_categorias": CATEGORIAS,
        "resumen": resumen, "hoy": datetime.now(TZ).date().isoformat(), "pestana": "materiales", "pesos_ref": pesos_ref,
    })


@router.post("/inventario/material")
async def guardar_material(request: Request):
    """Crea un material nuevo o edita uno existente (nombre, categoría, unidad, precio, mínimo, ubicación)."""
    _permitido(request)
    datos = await request.json()
    nombre = (datos.get("nombre") or "").strip()[:200]
    if not nombre:
        raise HTTPException(status_code=400, detail="Escriba el nombre del material")
    categoria = datos.get("categoria") if datos.get("categoria") in CATEGORIAS else categoria_de(nombre)
    unidad = (datos.get("unidad") or "und").strip()[:20] or "und"
    precio = _num(datos["precio"], "precio", positivo=False) if str(datos.get("precio") or "").strip() else None
    minimo = _num(datos["minimo"], "mínimo", positivo=False) if str(datos.get("minimo") or "").strip() else None
    peso = _num(datos["peso"], "peso unitario") if str(datos.get("peso") or "").strip() else None
    ubicacion = (datos.get("ubicacion") or "").strip()[:80] or None
    nota = (datos.get("nota") or "").strip()[:200] or None
    conn = db()
    c = conn.cursor()
    asegurar_schema_inventario(c)
    material_id = datos.get("id")
    if material_id:
        c.execute("""UPDATE inv_materiales SET nombre=%s, categoria=%s, unidad=%s, precio=%s, stock_minimo=%s,
                     ubicacion=%s, nota=%s, peso_gr=%s WHERE id=%s""",
                  (nombre, categoria, unidad, precio, minimo, ubicacion, nota, peso, int(material_id)))
    else:
        stock = _num(datos.get("stock") or 0, "stock inicial", positivo=False)
        c.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM inv_materiales")
        codigo = f"INV-{c.fetchone()[0]:04d}"
        c.execute("""INSERT INTO inv_materiales (codigo, nombre, categoria, unidad, stock, precio, stock_minimo, ubicacion, nota, peso_gr)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                  (codigo, nombre, categoria, unidad, stock, precio, minimo, ubicacion, nota, peso))
        material_id = c.fetchone()[0]
        if stock:
            c.execute("""INSERT INTO inv_movimientos (material_id, tipo, cantidad, stock_despues, precio, nota, usuario)
                         VALUES (%s, 'entrada', %s, %s, %s, 'Stock inicial al crear el material', %s)""",
                      (material_id, stock, stock, precio, request.session.get("username")))
    conn.commit()
    conn.close()
    return {"ok": True, "id": material_id}


@router.post("/inventario/movimiento")
async def registrar_movimiento(request: Request):
    """Entrada (compra o llegada), entrega (sale del almacén) o ajuste (el conteo físico da otra cifra)."""
    _permitido(request)
    datos = await request.json()
    tipo = datos.get("tipo")
    if tipo not in TIPOS_MOV:
        raise HTTPException(status_code=400, detail="Tipo de movimiento no válido")
    material_id = int(datos.get("material_id") or 0)
    fecha = _fecha(datos.get("fecha"))
    destino = (datos.get("destino") or "").strip()[:120] or None
    nota = (datos.get("nota") or "").strip()[:200] or None
    if tipo == "entrega" and not destino:
        raise HTTPException(status_code=400, detail="Escriba a quién o para qué se entrega")
    conn = db()
    c = conn.cursor()
    c.execute("SELECT stock, nombre FROM inv_materiales WHERE id = %s FOR UPDATE", (material_id,))
    fila = c.fetchone()
    if not fila:
        conn.close()
        raise HTTPException(status_code=404, detail="Material no encontrado")
    stock, nombre = fila
    precio = None
    if tipo == "ajuste":
        nuevo = _num(datos.get("cantidad"), "conteo", positivo=False)
        if nuevo < 0:
            conn.close()
            raise HTTPException(status_code=400, detail="El conteo no puede ser negativo")
        cantidad = nuevo - stock
        if cantidad == 0:
            conn.close()
            return {"ok": True, "stock": fmt(stock), "sin_cambio": True}
    else:
        cantidad = _num(datos.get("cantidad"))
        if tipo == "entrega":
            if cantidad > stock and not datos.get("forzar"):
                conn.close()
                raise HTTPException(status_code=409, detail=f"Solo hay {fmt(stock)} de «{nombre}». ¿Entregar de todas formas?")
            cantidad = -cantidad
        if tipo == "entrada" and str(datos.get("precio") or "").strip():
            precio = _num(datos["precio"], "precio", positivo=False)
    nuevo_stock = stock + cantidad
    c.execute("""INSERT INTO inv_movimientos (material_id, tipo, cantidad, stock_despues, precio, destino, nota, fecha, usuario)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
              (material_id, tipo, cantidad, nuevo_stock, precio, destino, nota, fecha, request.session.get("username")))
    c.execute("UPDATE inv_materiales SET stock = %s, precio = COALESCE(%s, precio) WHERE id = %s",
              (nuevo_stock, precio, material_id))
    conn.commit()
    conn.close()
    return {"ok": True, "stock": fmt(nuevo_stock)}


@router.post("/inventario/material/{material_id}/archivar")
def archivar_material(material_id: int, request: Request):
    """Lo saca de la lista (no se borra: su historial queda)."""
    _permitido(request)
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE inv_materiales SET activo = FALSE WHERE id = %s", (material_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.get("/inventario/movimientos", response_class=HTMLResponse)
def ver_movimientos(request: Request, material: int = 0, tipo: str = "", desde: str = "", hasta: str = ""):
    _permitido(request)
    conn = db()
    c = conn.cursor()
    asegurar_schema_inventario(c)
    conn.commit()
    filtros, params = [], []
    if material:
        filtros.append("v.material_id = %s"); params.append(material)
    if tipo in TIPOS_MOV:
        filtros.append("v.tipo = %s"); params.append(tipo)
    if desde:
        filtros.append("v.fecha >= %s"); params.append(_fecha(desde))
    if hasta:
        filtros.append("v.fecha <= %s"); params.append(_fecha(hasta))
    donde = ("WHERE " + " AND ".join(filtros)) if filtros else ""
    c.execute(f"""
        SELECT v.fecha, m.nombre, m.unidad, v.tipo, v.cantidad, v.stock_despues, v.precio, v.destino, v.nota, v.usuario, m.id
        FROM inv_movimientos v JOIN inv_materiales m ON m.id = v.material_id
        {donde} ORDER BY v.fecha DESC, v.id DESC LIMIT 500
    """, params)
    movs = [{
        "fecha": r[0].strftime("%d/%m/%Y"), "material": r[1], "unidad": r[2], "tipo": TIPOS_MOV.get(r[3], r[3]),
        "clase": r[3], "cantidad": r[4], "stock": r[5], "precio": r[6], "destino": r[7] or "", "nota": r[8] or "",
        "usuario": r[9] or "", "material_id": r[10],
    } for r in c.fetchall()]
    nombre_material = ""
    if material:
        c.execute("SELECT nombre FROM inv_materiales WHERE id = %s", (material,))
        f = c.fetchone()
        nombre_material = f[0] if f else ""
    conn.close()
    return _plantilla(request, "inventario_movimientos.html", {
        "movs": movs, "material": material, "nombre_material": nombre_material, "tipo": tipo,
        "desde": desde, "hasta": hasta, "tipos": TIPOS_MOV, "pestana": "movimientos",
    })


# ===================== Fundición y mecanizado =====================

@router.get("/inventario/fundicion", response_class=HTMLResponse)
def ver_fundicion(request: Request):
    _permitido(request)
    conn = db()
    c = conn.cursor()
    asegurar_schema_inventario(c)
    conn.commit()
    c.execute("""SELECT id, codigo, nombre, medida, sin_procesar, en_mecanizado, arreglar, devolver, en_planta, nota
                 FROM inv_piezas WHERE COALESCE(activo, TRUE) = TRUE ORDER BY codigo""")
    piezas = [{
        "id": r[0], "codigo": r[1] or "", "nombre": r[2], "medida": r[3] or "",
        "sin_procesar": r[4], "en_mecanizado": r[5], "arreglar": r[6], "devolver": r[7], "en_planta": r[8],
        "nota": r[9] or "",
    } for r in c.fetchall()]
    c.execute("""SELECT v.fecha, COALESCE(p.nombre || COALESCE(' ' || p.medida, ''), v.pieza_texto), v.tipo, v.cantidad,
                        v.taller, v.nota, v.importado
                 FROM inv_movs_piezas v LEFT JOIN inv_piezas p ON p.id = v.pieza_id
                 ORDER BY v.fecha DESC NULLS LAST, v.id DESC LIMIT 300""")
    movs = [{
        "fecha": r[0].strftime("%d/%m/%Y") if r[0] else "sin fecha", "pieza": r[1] or "",
        "tipo": MOVS_PIEZA[r[2]][0] if r[2] in MOVS_PIEZA else TEXTOS_IMPORTADOS.get(r[2], r[2]),
        "cantidad": r[3], "taller": r[4] or "", "nota": r[5] or "", "importado": r[6],
    } for r in c.fetchall()]
    conn.close()
    return _plantilla(request, "inventario_fundicion.html", {
        "piezas": piezas, "movs": movs, "estados": ESTADOS_PIEZA,
        "movimientos": [(k, v[0], NOMBRE_ESTADO.get(v[1], "afuera"), NOMBRE_ESTADO.get(v[2], "afuera")) for k, v in MOVS_PIEZA.items()],
        "hoy": datetime.now(TZ).date().isoformat(), "pestana": "fundicion",
    })


@router.post("/inventario/fundicion/movimiento")
async def mover_pieza(request: Request):
    _permitido(request)
    datos = await request.json()
    tipo = datos.get("tipo")
    if tipo not in MOVS_PIEZA:
        raise HTTPException(status_code=400, detail="Movimiento no válido")
    _, de, a = MOVS_PIEZA[tipo]
    assert de in COLUMNAS_ESTADO | {None} and a in COLUMNAS_ESTADO | {None}
    cantidad = _num(datos.get("cantidad"))
    pieza_id = int(datos.get("pieza_id") or 0)
    conn = db()
    c = conn.cursor()
    c.execute(f"SELECT nombre, {de or 'sin_procesar'} FROM inv_piezas WHERE id = %s FOR UPDATE", (pieza_id,))
    fila = c.fetchone()
    if not fila:
        conn.close()
        raise HTTPException(status_code=404, detail="Pieza no encontrada")
    if de and cantidad > fila[1] and not datos.get("forzar"):
        conn.close()
        raise HTTPException(status_code=409, detail=f"En «{NOMBRE_ESTADO[de]}» solo hay {fmt(fila[1])}. ¿Moverlas de todas formas?")
    if de:
        c.execute(f"UPDATE inv_piezas SET {de} = {de} - %s WHERE id = %s", (cantidad, pieza_id))
    if a:
        c.execute(f"UPDATE inv_piezas SET {a} = {a} + %s WHERE id = %s", (cantidad, pieza_id))
    c.execute("""INSERT INTO inv_movs_piezas (pieza_id, tipo, cantidad, taller, nota, fecha, usuario)
                 VALUES (%s,%s,%s,%s,%s,%s,%s)""",
              (pieza_id, tipo, cantidad, (datos.get("taller") or "").strip()[:60] or None,
               (datos.get("nota") or "").strip()[:200] or None, _fecha(datos.get("fecha")), request.session.get("username")))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.post("/inventario/fundicion/pieza")
async def guardar_pieza(request: Request):
    _permitido(request)
    datos = await request.json()
    nombre = (datos.get("nombre") or "").strip()[:120]
    if not nombre:
        raise HTTPException(status_code=400, detail="Escriba el nombre de la pieza")
    medida = (datos.get("medida") or "").strip()[:40] or None
    nota = (datos.get("nota") or "").strip()[:200] or None
    conn = db()
    c = conn.cursor()
    asegurar_schema_inventario(c)
    if datos.get("id"):
        c.execute("UPDATE inv_piezas SET nombre=%s, medida=%s, nota=%s WHERE id=%s", (nombre, medida, nota, int(datos["id"])))
    else:
        c.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM inv_piezas")
        c.execute("INSERT INTO inv_piezas (codigo, nombre, medida, nota) VALUES (%s,%s,%s,%s)",
                  (f"MAT-{c.fetchone()[0]:03d}", nombre, medida, nota))
    conn.commit()
    conn.close()
    return {"ok": True}


# ===================== Herramientas =====================

@router.get("/inventario/herramientas", response_class=HTMLResponse)
def ver_herramientas(request: Request):
    _permitido(request)
    conn = db()
    c = conn.cursor()
    asegurar_schema_inventario(c)
    conn.commit()
    c.execute("SELECT id, persona, herramientas, cantidad FROM inv_herramientas ORDER BY persona")
    filas = [{"id": r[0], "persona": r[1], "herramientas": r[2], "cantidad": r[3]} for r in c.fetchall()]
    conn.close()
    return _plantilla(request, "inventario_herramientas.html", {"filas": filas, "pestana": "herramientas"})


@router.post("/inventario/herramientas")
async def guardar_herramientas(request: Request):
    _permitido(request)
    datos = await request.json()
    conn = db()
    c = conn.cursor()
    if datos.get("borrar") and datos.get("id"):
        c.execute("DELETE FROM inv_herramientas WHERE id = %s", (int(datos["id"]),))
    else:
        persona = (datos.get("persona") or "").strip()[:80]
        herramientas = (datos.get("herramientas") or "").strip()[:300]
        if not persona or not herramientas:
            conn.close()
            raise HTTPException(status_code=400, detail="Escriba la persona y las herramientas")
        cantidad = len([h for h in herramientas.split(",") if h.strip()])
        if datos.get("id"):
            c.execute("UPDATE inv_herramientas SET persona=%s, herramientas=%s, cantidad=%s, actualizado=NOW() WHERE id=%s",
                      (persona, herramientas, cantidad, int(datos["id"])))
        else:
            c.execute("INSERT INTO inv_herramientas (persona, herramientas, cantidad) VALUES (%s,%s,%s)",
                      (persona, herramientas, cantidad))
    conn.commit()
    conn.close()
    return {"ok": True}


# ===================== Etiquetas con código de barras =====================

def codigo_barras_svg(codigo: str) -> str:
    """Code 128 en SVG (lo lee cualquier pistola lectora). Se incrusta en la página, sin el encabezado XML."""
    import barcode
    from barcode.writer import SVGWriter
    svg = barcode.get("code128", codigo, writer=SVGWriter()).render(
        {"module_width": 0.33, "module_height": 11, "quiet_zone": 3, "write_text": False})
    svg = svg.decode() if isinstance(svg, bytes) else svg
    return svg[svg.index("<svg"):]


@router.get("/inventario/etiquetas", response_class=HTMLResponse)
def ver_etiquetas(request: Request, categoria: str = "", buscar: str = "", ids: str = ""):
    """Hoja para imprimir: una etiqueta por material con nombre, código de barras y código."""
    _permitido(request)
    conn = db()
    c = conn.cursor()
    asegurar_schema_inventario(c)
    conn.commit()
    filtros, params = ["COALESCE(activo, TRUE) = TRUE"], []
    lista_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if lista_ids:
        filtros.append("id = ANY(%s)"); params.append(lista_ids)
    if categoria in CATEGORIAS:
        filtros.append("categoria = %s"); params.append(categoria)
    if buscar.strip():
        for palabra in buscar.split():
            filtros.append("(nombre ILIKE %s OR codigo ILIKE %s)"); params += [f"%{palabra}%", f"%{palabra}%"]
    c.execute(f"""SELECT id, codigo, nombre, categoria, ubicacion FROM inv_materiales
                  WHERE {' AND '.join(filtros)} ORDER BY categoria, nombre LIMIT 600""", params)
    etiquetas = [{"codigo": r[1], "nombre": r[2], "categoria": r[3], "ubicacion": r[4] or "",
                  "barras": codigo_barras_svg(r[1])} for r in c.fetchall() if r[1]]
    conn.close()
    return _plantilla(request, "inventario_etiquetas.html", {
        "etiquetas": etiquetas, "categoria": categoria, "buscar": buscar, "ids": ids,
        "lista_categorias": CATEGORIAS, "pestana": "etiquetas",
    })
