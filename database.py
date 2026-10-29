import psycopg2
from psycopg2 import pool
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Inicializamos el pool de conexiones (1 a 20 conexiones)
try:
    db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=20,
        dsn=DATABASE_URL
    )
except Exception as e:
    print("Error iniciando Connection Pool:", e)
    db_pool = None

from contextvars import ContextVar

_active_connections = ContextVar("active_connections", default=None)

class PooledConnection:
    """Un wrapper que sobreescribe close() para devolver la conexión al pool en lugar de cerrarla"""
    def __init__(self, conn, pool_ref):
        self.conn = conn
        self.pool_ref = pool_ref
        self._closed = False
        
        # Registrar esta conexión si estamos dentro de un request
        active = _active_connections.get()
        if active is not None:
            active.append(self)
        
    def cursor(self, *args, **kwargs):
        return self.conn.cursor(*args, **kwargs)
        
    def commit(self):
        self.conn.commit()
        
    def rollback(self):
        self.conn.rollback()
        
    def close(self):
        if not self._closed:
            self.pool_ref.putconn(self.conn)
            self._closed = True

def db():
    # Si el pool falló al iniciar, usamos el método tradicional como fallback
    if db_pool is None:
        return psycopg2.connect(DATABASE_URL)
        
    conn = db_pool.getconn()
    return PooledConnection(conn, db_pool)

def sincronizar_actividades_ordenes_abiertas(cursor, orden_id=None):
    filtro_orden = "AND o.id = %s" if orden_id is not None else ""
    params = (orden_id,) if orden_id is not None else ()

    cursor.execute(f'''
        INSERT INTO orden_actividades
            (orden_id, actividad_id, cantidad_total, cantidad_realizada)
        SELECT
            o.id,
            a.id,
            o.cantidad,
            0
        FROM ordenes o
        JOIN procesos p ON p.maquina_id = o.maquina_id
        JOIN actividades a ON a.proceso_id = p.id
        WHERE o.estado != 'CERRADA'
        AND COALESCE(p.activo, TRUE) = TRUE
        AND COALESCE(a.activo, TRUE) = TRUE
        {filtro_orden}
        AND NOT EXISTS (
            SELECT 1
            FROM orden_actividades oa
            WHERE oa.orden_id = o.id
            AND oa.actividad_id = a.id
        )
    ''', params)
    return cursor.rowcount

def recalcular_porcentaje_orden(cursor, orden_id):
    """
    Recalcula el porcentaje de avance de una orden basándose en las actividades
    que contengan 'Empaque' en su nombre y actualiza la tabla ordenes.
    Si el porcentaje llega al 100%, marca la orden como CERRADA.
    Retorna el nuevo porcentaje calculado.
    """
    cursor.execute("""
        SELECT
            SUM(oa.cantidad_realizada),
            SUM(oa.cantidad_total)
        FROM orden_actividades oa
        JOIN actividades a ON a.id = oa.actividad_id
        WHERE oa.orden_id = %s
        AND a.nombre ILIKE '%%Empaque%%'
    """, (orden_id,))
    
    row = cursor.fetchone()
    
    if row and row[1] and row[1] > 0:
        porcentaje = round((row[0] / row[1]) * 100, 2)
    else:
        porcentaje = 0

    cursor.execute("""
        UPDATE ordenes
        SET porcentaje = %s,
            estado = CASE WHEN %s >= 100 THEN 'CERRADA' ELSE estado END
        WHERE id = %s
    """, (porcentaje, porcentaje, orden_id))
    
    return porcentaje
