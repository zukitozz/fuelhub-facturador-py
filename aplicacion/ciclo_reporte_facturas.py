"""
Caso de uso del hilo Reporte: arma y envía a FuelHub core el reporte semanal
antifraude top-facturas (POST v1/reportes/top-facturas) — el top-20 de clientes
que más facturaron en la última semana ya cerrada (lunes a domingo) y qué
despachador generó cada una de sus facturas.

Se manda los lunes (a pedido explícito, en vez de apenas la semana anterior
queda cerrada): el ciclo corre cada hora pero no hace nada si hoy no es lunes,
y usa el estado persistido en estado/reporte_facturas.py para no reenviar la
misma semana dos veces ni perder el intento si el primer envío del lunes falla
—se reintenta más tarde ese mismo lunes, con la misma Idempotency-Key.
"""
import logging
from datetime import date, timedelta

import repositorio
from config import DATABASE_URL, FUELHUB_CORE_CLIENT_ID, FUELHUB_CORE_CLIENT_SECRET, TOP_FACTURAS_CANTIDAD
from aplicacion.bd_app import conectar_bd
from fuelhub_core.bd import codigo_estacion, facturas_semana
from fuelhub_core.api import enviar_reporte_top_facturas
from estado.reporte_facturas import semana_ya_enviada, idempotency_key_para, marcar_semana_enviada

logger = logging.getLogger(__name__)

_avisado_sin_credenciales = False


def _semana_completa_mas_reciente(hoy: date) -> date:
    """Lunes de la última semana (lunes a domingo) que ya terminó del todo a la
    fecha 'hoy' — nunca la semana en curso, todavía incompleta."""
    lunes_de_hoy = hoy - timedelta(days=hoy.weekday())
    return lunes_de_hoy - timedelta(days=7)


def _armar_top(filas: list, cantidad: int) -> list:
    """
    Agrupa las facturas (una fila por factura) por cliente, suma totalSoles y
    devuelve los 'cantidad' clientes que más facturaron, de mayor a menor —
    fuelhub-local decide el criterio de orden, FuelHub core solo valida
    estructura (ver spec: "no reordena ni valida que efectivamente sean los
    top 20").
    """
    por_cliente = {}
    for fila in filas:
        doc = fila["numero_documento_cliente"]
        cliente = por_cliente.setdefault(doc, {
            "numeroDocumentoCliente": doc,
            "nombreCliente": fila["nombre_cliente"],
            "totalSoles": 0.0,
            "facturas": [],
        })
        cliente["totalSoles"] += float(fila["total"])
        cliente["facturas"].append({
            "numeracion": fila["numeracion"],
            "fecha": fila["fecha"],
            "monto": float(fila["total"]),
            "despachadorNombre": fila["despachador_nombre"],
            "despachadorDocumento": fila["despachador_documento"],
        })
    ordenados = sorted(por_cliente.values(), key=lambda c: c["totalSoles"], reverse=True)
    return ordenados[:cantidad]


def ciclo_reporte_facturas():
    global _avisado_sin_credenciales
    if not (FUELHUB_CORE_CLIENT_ID and FUELHUB_CORE_CLIENT_SECRET):
        # Best-effort, igual que cierres/pdf: sin credenciales no hay nada para
        # hacer, y no hay que repetir el mismo aviso en cada ciclo.
        if not _avisado_sin_credenciales:
            logger.warning(
                "Faltan FUELHUB_CORE_CLIENT_ID/FUELHUB_CORE_CLIENT_SECRET en el "
                ".env; el reporte semanal top-facturas no se envía."
            )
            _avisado_sin_credenciales = True
        return
    if repositorio.motor_de(DATABASE_URL) != "sqlserver":
        # El reporte necesita Comprobantes.UsuarioId/Cierreturnos (esquema de
        # grifo); en cualquier otro motor no hay despachador que reportar.
        return
    if date.today().weekday() != 0:
        # Solo lunes (0=lunes): el resto de la semana este ciclo no hace nada.
        # El intervalo del hilo sigue siendo horario para poder reintentar el
        # mismo lunes si el primer envío falla (ver semana_ya_enviada), no para
        # mandar el reporte cualquier día.
        return

    semana_inicio = _semana_completa_mas_reciente(date.today())
    semana_inicio_str = semana_inicio.isoformat()
    if semana_ya_enviada(semana_inicio_str):
        return

    conn = None
    try:
        conn = conectar_bd()
        codigo = codigo_estacion(conn)
        semana_fin_str = (semana_inicio + timedelta(days=7)).isoformat()
        filas = facturas_semana(conn, semana_inicio_str, semana_fin_str)
    except Exception:
        logger.exception("Error calculando el reporte top-facturas de la semana %s", semana_inicio_str)
        return
    finally:
        if conn:
            conn.close()

    top = _armar_top(filas, TOP_FACTURAS_CANTIDAD)
    clave = idempotency_key_para(semana_inicio_str)
    respuesta = enviar_reporte_top_facturas(codigo, semana_inicio_str, top, clave)
    if respuesta is not None:
        marcar_semana_enviada(semana_inicio_str, clave)
        logger.info(
            "Reporte top-facturas de la semana %s enviado a FuelHub core (%d cliente(s)).",
            semana_inicio_str, len(top),
        )
    else:
        logger.warning(
            "No se pudo enviar el reporte top-facturas de la semana %s; se reintenta en el próximo ciclo.",
            semana_inicio_str,
        )
