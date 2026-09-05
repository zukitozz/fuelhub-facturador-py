"""
Consultas a la BD de la aplicación (AUXILIAR, SQL Server) para los cierres de
turno y de día que se envían a FuelHub core.

Específico de ese esquema —no pasa por repositorio/— porque Cierreturnos y
Cierredias solo existen en instalaciones de grifo con SQL Server; el resto de
la aplicación (Comprobantes) sí es multi-motor. Ver aplicacion/ciclo_cierres.py,
que primero comprueba el motor antes de llamar a este módulo.
"""
import logging
from contextlib import closing

logger = logging.getLogger(__name__)

# El turno trae la fecha del Cierredia enlazado (decide la fechaNegocio, ver
# dominio/cierres.py) y el empleado que lo cerró. codigoEstacion y el
# administrador del día NO se traen acá —viven en una sola fila cada uno
# (Emisores, Usuarios con rol ADMIN_ROLE)— así que se consultan aparte y se
# combinan en Python: un JOIN los duplicaría por cada Cierreturno/Cierredia si
# alguna vez dejan de ser una fila única.
_SQL_PENDIENTES_TURNO = """
SELECT ct.id,
       ct.turno,
       CONVERT(varchar, ct.fecha_inicio, 127) AS fecha_inicio,
       CONVERT(varchar, ct.fecha, 127)        AS fecha,
       ct.total, ct.efectivo, ct.tarjeta, ct.yape,
       CONVERT(varchar, cd.fecha, 127)        AS cierredia_fecha,
       u.usuario  AS empleado_codigo,
       u.nombre   AS empleado_nombre
  FROM Cierreturnos ct
  LEFT JOIN Cierredias cd ON cd.id = ct.CierrediaId
  LEFT JOIN Usuarios   u  ON u.id  = ct.UsuarioId
 WHERE (ct.enviado = 0 OR ct.enviado IS NULL)
 ORDER BY ct.id
"""

_SQL_DETALLE_TURNO = """
SELECT ctd.codigo               AS codigo_local,
       ctd.producto,
       ctd.medida,
       ctd.total_cantidad,
       ctd.total_soles,
       ctd.calibracion_cantidad,
       ctd.calibracion_soles,
       ctd.despacho_cantidad,
       ctd.despacho_soles,
       p.uuid                   AS producto_id
  FROM Cierreturnosdetalle ctd
  LEFT JOIN Productos p ON p.codigo = ctd.codigo
 WHERE ctd.CierreturnoId = ?
 ORDER BY ctd.id
"""

_SQL_PENDIENTES_DIA = """
SELECT cd.id,
       CONVERT(varchar, cd.fecha, 127) AS fecha,
       cd.total
  FROM Cierredias cd
 WHERE (cd.enviado = 0 OR cd.enviado IS NULL)
 ORDER BY cd.id
"""


def _filas(conn, sql: str, params: tuple = ()) -> list:
    with closing(conn.cursor()) as cur:
        cur.execute(sql, params)
        columnas = [d[0] for d in cur.description]
        return [dict(zip(columnas, fila)) for fila in cur.fetchall()]


def _escribir(conn, sql: str, params: tuple):
    with closing(conn.cursor()) as cur:
        cur.execute(sql, params)
    conn.commit()


def pendientes_cierreturnos(conn) -> list:
    return _filas(conn, _SQL_PENDIENTES_TURNO)


def detalle_cierreturno(conn, cierreturno_id) -> list:
    return _filas(conn, _SQL_DETALLE_TURNO, (cierreturno_id,))


def pendientes_cierredias(conn) -> list:
    return _filas(conn, _SQL_PENDIENTES_DIA)


def codigo_estacion(conn):
    """El código de esta estación (Emisores.codigo). Hay una única fila."""
    filas = _filas(conn, "SELECT TOP 1 codigo FROM Emisores")
    if not filas:
        logger.error("No hay ninguna fila en Emisores; los cierres van sin codigoEstacion.")
        return None
    return filas[0]["codigo"]


def admin_operador(conn) -> dict:
    """
    {"codigo","nombre"} del usuario administrador, para el cierre de día — esa
    tabla no tiene un enlace propio a Usuarios (a diferencia de Cierreturnos con
    UsuarioId), así que se toma el rol ADMIN_ROLE. Si hubiera más de uno se avisa
    y se usa el primero: no hay forma de saber cuál cerró el día puntual.
    """
    filas = _filas(conn, "SELECT usuario, nombre FROM Usuarios WHERE rol='ADMIN_ROLE' ORDER BY id")
    if not filas:
        logger.warning("No hay ningún usuario con rol ADMIN_ROLE; el cierre de día va sin administrador.")
        return {}
    if len(filas) > 1:
        logger.warning(
            "Hay %d usuarios con rol ADMIN_ROLE; se usa el primero (%s) para el cierre de día.",
            len(filas), filas[0]["usuario"],
        )
    return {"codigo": filas[0]["usuario"], "nombre": filas[0]["nombre"]}


def marcar_enviado_cierreturno(conn, cierreturno_id):
    _escribir(conn, "UPDATE Cierreturnos SET enviado=1 WHERE id=?", (cierreturno_id,))


def marcar_enviado_cierredia(conn, cierredia_id):
    _escribir(conn, "UPDATE Cierredias SET enviado=1 WHERE id=?", (cierredia_id,))
