"""
Carga los pesos de tornillería (Excel «INVENTARIO_TORNILLERIA_FEBRERO» de la app de inventario anterior).

Uso:
    python scripts/importar_pesos.py TORNILLERIA.xlsm [--db URL]

- Llena la tabla de referencia inv_pesos_ref (la reemplaza entera): Carlos la usa para escoger el peso
  de cada material al editarlo.
- Pone el peso solo a los materiales cuyo nombre es igual (sin tildes, mayúsculas ni espacios de más)
  y que todavía no tienen peso. Los demás no se adivinan: los nombres de los dos Excel no coinciden
  y un peso equivocado daría conteos equivocados.
"""
import argparse
import os
import re
import sys
import unicodedata

import openpyxl
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routers.inventario import asegurar_schema_inventario  # noqa: E402


def normal(texto) -> str:
    t = unicodedata.normalize("NFD", str(texto or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/.,x ]+", " ", t)).strip(" .")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("excel")
    ap.add_argument("--db", default=os.getenv("DATABASE_URL"))
    a = ap.parse_args()

    wb = openpyxl.load_workbook(a.excel, data_only=True, read_only=True)
    pesos = []
    for r in wb.worksheets[0].iter_rows(min_row=2, values_only=True):
        if r and r[0] and isinstance(r[1], (int, float)) and r[1] > 0:
            pesos.append((re.sub(r"\s+", " ", str(r[0])).strip(), r[1]))

    conn = psycopg2.connect(a.db)
    c = conn.cursor()
    asegurar_schema_inventario(c)
    c.execute("TRUNCATE inv_pesos_ref RESTART IDENTITY")
    for nombre, peso in pesos:
        c.execute("INSERT INTO inv_pesos_ref (nombre, peso_gr) VALUES (%s, %s)", (nombre, peso))
    c.execute("SELECT id, nombre FROM inv_materiales WHERE peso_gr IS NULL")
    por_nombre = {}
    for mid, nombre in c.fetchall():
        por_nombre.setdefault(normal(nombre), []).append(mid)
    puestos = 0
    for nombre, peso in pesos:
        for mid in por_nombre.get(normal(nombre), []):
            c.execute("UPDATE inv_materiales SET peso_gr = %s WHERE id = %s AND peso_gr IS NULL", (peso, mid))
            puestos += c.rowcount
    conn.commit()
    conn.close()
    print(f"Tabla de referencia: {len(pesos)} pesos. Materiales con peso puesto por nombre igual: {puestos}.")


if __name__ == "__main__":
    main()
