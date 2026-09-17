"""
Consultas a la BD de la aplicación (AUXILIAR, SQL Server) para lo que se envía
a FuelHub core: los cierres de turno y de día, y el PDF de los comprobantes
(pdf_bytes/pdf_enviado).

Específico de ese esquema —no pasa por repositorio/— porque Cierreturnos y
Cierredias, y las columnas pdf_bytes/pdf_enviado de Comprobantes, solo existen
en instalaciones de grifo con SQL Server; el resto de la aplicación
(Comprobantes en su forma multi-motor) sí pasa por repositorio/. Ver
aplicacion/ciclo_cierres.py y aplicacion/ciclo_pdf.py, que primero comprueban
el motor antes de llamar a este módulo.
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

# Solo los turnos ya aceptados por FuelHub core tienen uuid; uno todavía sin
# enviar (o rechazado) no aporta un id porque ahí no existe.
_SQL_TURNOS_UUID_DE_DIA = """
SELECT uuid
  FROM Cierreturnos
 WHERE CierrediaId = ?
   AND uuid IS NOT NULL
 ORDER BY id
"""

# Cuántos turnos de ese día todavía no fueron confirmados por FuelHub core
# (pendientes de enviar o rechazados): mientras haya alguno, el cierre de día
# no se manda —iría con cierresTurnoIds incompleto.
_SQL_TURNOS_SIN_CONFIRMAR_DE_DIA = """
SELECT COUNT(*) AS pendientes
  FROM Cierreturnos
 WHERE CierrediaId = ?
   AND uuid IS NULL
"""

# pdf_bytes IS NOT NULL: la aplicación todavía no generó el PDF de muchos
# comprobantes en cualquier momento dado, y eso no es un error que haya que
# reportar acá —simplemente no hay nada que subir todavía.
# numero_documento = '0' es el receptor "Clientes Varios" (boleta sin DNI/RUC
# capturado, ver repositorio/sqlserver.py:receptor); esos PDF no se suben. Se
# deja la puerta abierta (LEFT JOIN) por si algún comprobante quedó sin
# ReceptorId: ahí no se sabe que es "varios", así que no se lo excluye.
_SQL_PENDIENTES_PDF = """
SELECT c.id, c.numeracion_comprobante, c.pdf_bytes
  FROM Comprobantes c
  LEFT JOIN Receptores r ON r.id = c.ReceptorId
 WHERE c.pdf_bytes IS NOT NULL
   AND (c.pdf_enviado = 0 OR c.pdf_enviado IS NULL)
   AND (r.numero_documento IS NULL OR r.numero_documento <> '0')
 ORDER BY c.id
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


def uuids_cierreturno_de_dia(conn, cierredia_id) -> list:
    return [fila["uuid"] for fila in _filas(conn, _SQL_TURNOS_UUID_DE_DIA, (cierredia_id,))]


def turnos_sin_confirmar_de_dia(conn, cierredia_id) -> int:
    return _filas(conn, _SQL_TURNOS_SIN_CONFIRMAR_DE_DIA, (cierredia_id,))[0]["pendientes"]


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


def marcar_enviado_cierreturno(conn, cierreturno_id, uuid_fuelhub: str):
    """uuid_fuelhub es el "id" con que FuelHub core registró el cierre."""
    _escribir(conn, "UPDATE Cierreturnos SET enviado=1, uuid=? WHERE id=?", (uuid_fuelhub, cierreturno_id))


def marcar_enviado_cierredia(conn, cierredia_id):
    _escribir(conn, "UPDATE Cierredias SET enviado=1 WHERE id=?", (cierredia_id,))


def pendientes_pdf(conn) -> list:
    return _filas(conn, _SQL_PENDIENTES_PDF)


def marcar_pdf_enviado(conn, comprobante_id):
    _escribir(conn, "UPDATE Comprobantes SET pdf_enviado=1 WHERE id=?", (comprobante_id,))
