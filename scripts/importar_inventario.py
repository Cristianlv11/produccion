"""
Carga inicial del inventario de Carlos desde sus Excel.

Uso:
    python scripts/importar_inventario.py ALMACEN.xlsm FUNDICION.xlsx [--db URL] [--reemplazar] [--probar]

- ALMACEN: «Inventario Retiro_nuevo» (Hoja1: referencia, cantidad total, entregadas, disponibles, precio, fecha).
- FUNDICION: «Inventario_Materiales_Fundicion_V2» (control por estado, hojas de lo que sale y llega de
  fundición y mecanizado, herramientas asignadas, ejes redondos, perillas y gomas).
- --db: URL de la base (por defecto DATABASE_URL del entorno).
- --reemplazar: borra lo que haya en las tablas de inventario antes de cargar (si no, se niega a cargar
  encima de un inventario que ya tiene datos).
- --probar: solo muestra el resumen, no escribe nada.
"""
import argparse
import os
import re
import sys
from datetime import date, datetime
from decimal import Decimal

import openpyxl
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routers.inventario import asegurar_schema_inventario, categoria_de  # noqa: E402


def limpio(texto) -> str:
    return re.sub(r"\s+", " ", str(texto or "")).strip()


def numero(valor):
    """Devuelve (número, nota). Acepta textos como «67-13» o «111-20 lleva Mecani..» y deja la nota."""
    if valor is None or valor == "":
        return Decimal(0), None
    if isinstance(valor, (int, float)):
        return Decimal(str(valor)), None
    texto = str(valor)
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*-\s*(\d+(?:[.,]\d+)?)", texto)
    if m:
        a, b = (Decimal(x.replace(",", ".")) for x in m.groups())
        return a - b, f"En el Excel decía «{limpio(texto)}»"
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)", texto)
    if m:
        return Decimal(m.group(1).replace(",", ".")), f"En el Excel decía «{limpio(texto)}»"
    return Decimal(0), f"En el Excel decía «{limpio(texto)}»"


def hoja(wb, empieza):
    for ws in wb.worksheets:
        if limpio(ws.title).lower().startswith(empieza.lower()):
            return ws
    return None


def fecha_de(valor):
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    return None


def leer_almacen(ruta):
    wb = openpyxl.load_workbook(ruta, data_only=True, read_only=True)
    ws = wb.worksheets[0]
    filas = list(ws.iter_rows(values_only=True))
    cab = [limpio(x).lower() for x in filas[0]]
    i_ref, i_total, i_entregadas, i_disp, i_precio = 0, 3, 4, 5, 6
    i_fecha = cab.index("fecha") if "fecha" in cab else 8
    materiales = {}
    for r in filas[1:]:
        if not r or not limpio(r[i_ref]) or limpio(r[i_ref]).lower() in ("total", "totales"):
            continue
        nombre = limpio(r[i_ref])
        total, _ = numero(r[i_total])
        entregadas, _ = numero(r[i_entregadas])
        disp = Decimal(str(r[i_disp])) if isinstance(r[i_disp], (int, float)) else total - entregadas
        precio = Decimal(str(r[i_precio])) if isinstance(r[i_precio], (int, float)) and r[i_precio] else None
        clave = nombre.lower()
        if clave in materiales:  # la misma referencia dos veces en el Excel: se suman
            m = materiales[clave]
            m["stock"] += disp
            m["precio"] = m["precio"] or precio
            m["notas"].append("Estaba dos veces en el Excel; se sumaron")
            continue
        notas = []
        if entregadas:
            notas.append(f"En el Excel: {total} compradas, {entregadas} entregadas")
        materiales[clave] = {"nombre": nombre, "stock": disp, "precio": precio, "fecha": fecha_de(r[i_fecha]),
                             "categoria": categoria_de(nombre), "notas": notas}
    return list(materiales.values())


def leer_fundicion(ruta):
    wb = openpyxl.load_workbook(ruta, data_only=True)
    piezas, movs, herramientas, extras = [], [], [], []

    ws = hoja(wb, "Control de Inventario")
    for r in ws.iter_rows(min_row=4, values_only=True):
        codigo, nombre = limpio(r[0]), limpio(r[1])
        if not codigo.startswith("MAT-") or not nombre:
            continue
        valores, notas = [], []
        for v in r[4:8]:
            n, nota = numero(v)
            valores.append(n)
            if nota:
                notas.append(nota)
        piezas.append({"codigo": codigo, "nombre": nombre, "medida": limpio(r[2]) or None,
                       "sin_procesar": valores[0], "en_mecanizado": valores[1], "arreglar": valores[2],
                       "devolver": valores[3], "nota": "; ".join(notas) or None})

    def historial(empieza, tipo, taller, col_cant=1, col_fecha=3, col_nota=2):
        ws = hoja(wb, empieza)
        if not ws:
            return
        for r in ws.iter_rows(min_row=2, values_only=True):
            pieza = limpio(r[0])
            if not pieza:
                continue
            cant, nota_num = numero(r[col_cant])
            nota = limpio(r[col_nota]) if col_nota is not None and len(r) > col_nota and isinstance(r[col_nota], str) else ""
            if not cant and not nota:
                continue
            movs.append({"pieza_texto": pieza, "tipo": tipo, "cantidad": cant, "taller": taller,
                         "fecha": fecha_de(r[col_fecha]) if len(r) > col_fecha else None,
                         "nota": "; ".join(x for x in (nota, nota_num) if x) or None})

    historial("Mecanizado salen Juan", "imp_sale_mecanizado", "Juan")
    historial("Mecanizado llegan Juan", "imp_llega_mecanizado", "Juan")
    historial("Mecanizado llegan Alberto", "imp_llega_mecanizado", "Alberto")
    historial("Salen para Mecaniz Alberto", "imp_sale_mecanizado", "Alberto", col_cant=1, col_fecha=2, col_nota=None)
    historial("LLegan fundicion", "imp_llega_fundicion", "Fundición")
    historial("Devolver fundicion", "imp_devuelta", "Fundición", col_cant=1, col_fecha=2, col_nota=3)

    ws = hoja(wb, "Herramientas Asignadas")
    if ws:
        for r in ws.iter_rows(min_row=2, values_only=True):
            persona, herr = limpio(r[2]), limpio(r[3]).strip(", ")
            if persona and herr and persona.lower() not in ("nombres", "nombre"):
                herramientas.append({"persona": persona, "herramientas": herr,
                                     "cantidad": int(r[4]) if isinstance(r[4], (int, float)) else None})

    for empieza, categoria in (("Ejes Redondos", "Aceros, tubos y ejes"), ("Perillas y Gomas", None)):
        ws = hoja(wb, empieza)
        if not ws:
            continue
        for r in ws.iter_rows(min_row=2, values_only=True):
            nombre = limpio(r[0])
            if not nombre:
                continue
            cant, _ = numero(r[1])
            desc, _ = numero(r[2])
            extras.append({"nombre": nombre, "stock": cant + desc, "precio": None, "fecha": fecha_de(r[4]) if len(r) > 4 else None,
                           "categoria": categoria or categoria_de(nombre),
                           "notas": [f"Del Excel de fundición ({limpio(ws.title)})"] + ([f"Se descontaron {abs(desc)}"] if desc else [])})
    return piezas, movs, herramientas, extras


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("almacen")
    ap.add_argument("fundicion")
    ap.add_argument("--db", default=os.getenv("DATABASE_URL"))
    ap.add_argument("--reemplazar", action="store_true")
    ap.add_argument("--probar", action="store_true")
    a = ap.parse_args()

    materiales = leer_almacen(a.almacen)
    piezas, movs, herramientas, extras = leer_fundicion(a.fundicion)
    materiales += extras
    por_cat = {}
    for m in materiales:
        por_cat[m["categoria"]] = por_cat.get(m["categoria"], 0) + 1
    print(f"Materiales: {len(materiales)}  |  piezas de fundición: {len(piezas)}  |  movimientos de historial: {len(movs)}"
          f"  |  herramientas: {len(herramientas)}")
    for cat, n in sorted(por_cat.items(), key=lambda x: -x[1]):
        print(f"   {n:4d}  {cat}")
    if a.probar:
        for m in materiales[:5]:
            print("   ", m)
        return

    conn = psycopg2.connect(a.db)
    c = conn.cursor()
    asegurar_schema_inventario(c)
    c.execute("SELECT (SELECT COUNT(*) FROM inv_materiales) + (SELECT COUNT(*) FROM inv_piezas)")
    if c.fetchone()[0] and not a.reemplazar:
        sys.exit("El inventario ya tiene datos. Use --reemplazar para borrarlos y cargar de nuevo.")
    if a.reemplazar:
        c.execute("TRUNCATE inv_movimientos, inv_materiales, inv_movs_piezas, inv_piezas, inv_herramientas RESTART IDENTITY")

    hoy = date.today()
    for i, m in enumerate(sorted(materiales, key=lambda x: (x["categoria"], x["nombre"].lower())), 1):
        unidad = "kg" if re.search(r"\bkg\b", m["nombre"].lower()) else "und"
        c.execute("""INSERT INTO inv_materiales (codigo, nombre, categoria, unidad, stock, precio, nota)
                     VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                  (f"INV-{i:04d}", m["nombre"][:200], m["categoria"], unidad, m["stock"], m["precio"],
                   "; ".join(m["notas"]) or None))
        mid = c.fetchone()[0]
        c.execute("""INSERT INTO inv_movimientos (material_id, tipo, cantidad, stock_despues, precio, nota, fecha, usuario)
                     VALUES (%s, 'ajuste', %s, %s, %s, 'Carga inicial desde el Excel de Carlos', %s, 'importación')""",
                  (mid, m["stock"], m["stock"], m["precio"], m["fecha"] or hoy))
    ids = {}
    for p in piezas:
        c.execute("""INSERT INTO inv_piezas (codigo, nombre, medida, sin_procesar, en_mecanizado, arreglar, devolver, nota)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                  (p["codigo"], p["nombre"], p["medida"], p["sin_procesar"], p["en_mecanizado"], p["arreglar"],
                   p["devolver"], p["nota"]))
        ids[p["codigo"]] = c.fetchone()[0]
    for m in movs:
        c.execute("""INSERT INTO inv_movs_piezas (pieza_texto, tipo, cantidad, taller, nota, fecha, importado, usuario)
                     VALUES (%s,%s,%s,%s,%s,%s,TRUE,'importación')""",
                  (m["pieza_texto"], m["tipo"], m["cantidad"], m["taller"], m["nota"], m["fecha"]))
    for h in herramientas:
        c.execute("INSERT INTO inv_herramientas (persona, herramientas, cantidad) VALUES (%s,%s,%s)",
                  (h["persona"], h["herramientas"], h["cantidad"]))
    conn.commit()
    conn.close()
    print("Cargado.")


if __name__ == "__main__":
    main()
