"""Costo de mano de obra calculado a partir del salario.

Se asume que los operarios ganan el salario mínimo: el costo de una hora es
salario mensual / horas trabajadas al mes, y el de una unidad es ese costo
por los minutos estándar de la actividad. Ambos valores se editan en
Configuración › Actividades y tiempos.
"""

SALARIO_DEFECTO = 1750905  # Salario mínimo Colombia 2026 (Decreto 1469 de 2025)
HORAS_MES_DEFECTO = 210    # Jornada de 42 h semanales (7 h x 30 días)


def asegurar_parametros(cursor):
    cursor.execute("CREATE TABLE IF NOT EXISTS parametros (clave TEXT PRIMARY KEY, valor TEXT)")
    cursor.execute(
        "INSERT INTO parametros (clave, valor) VALUES ('salario_mensual', %s), ('horas_mes', %s) ON CONFLICT (clave) DO NOTHING",
        (str(SALARIO_DEFECTO), str(HORAS_MES_DEFECTO)),
    )


def leer_parametros(cursor):
    asegurar_parametros(cursor)
    cursor.execute("SELECT clave, valor FROM parametros WHERE clave IN ('salario_mensual', 'horas_mes')")
    datos = dict(cursor.fetchall())
    salario = float(datos.get("salario_mensual") or SALARIO_DEFECTO)
    horas = float(datos.get("horas_mes") or HORAS_MES_DEFECTO) or HORAS_MES_DEFECTO
    return {"salario_mensual": salario, "horas_mes": horas, "costo_hora": round(salario / horas, 2)}


def costos_desde_unidades_hora(cursor, unidades_por_hora):
    """(costo por unidad, costo por hora) para una actividad con ese estándar."""
    costo_hora = leer_parametros(cursor)["costo_hora"]
    if not unidades_por_hora or float(unidades_por_hora) <= 0:
        return None, costo_hora
    return round(costo_hora / float(unidades_por_hora), 2), costo_hora


def guardar_parametros(cursor, salario, horas):
    asegurar_parametros(cursor)
    for clave, valor in (("salario_mensual", salario), ("horas_mes", horas)):
        cursor.execute("UPDATE parametros SET valor = %s WHERE clave = %s", (str(valor), clave))


def recalcular_todos(cursor):
    """Recalcula el costo de todas las actividades con el salario actual."""
    costo_hora = leer_parametros(cursor)["costo_hora"]
    cursor.execute(
        """UPDATE estandares_actividad
           SET costo_mo_hora = %s,
               costo_mo_unidad = CASE WHEN unidades_por_hora > 0 THEN ROUND((%s / unidades_por_hora)::numeric, 2) END""",
        (costo_hora, costo_hora),
    )
    return cursor.rowcount
