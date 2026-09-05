"""
Caso de uso del hilo Cierres: envía a FuelHub core los cierres de turno y de día
que la aplicación todavía no mandó (Cierreturnos/Cierredias.enviado = 0 o NULL).
"""
import logging

import repositorio
from config import DATABASE_URL, FUELHUB_CORE_CLIENT_ID, FUELHUB_CORE_CLIENT_SECRET
from dominio.cierres import payload_cierre_turno, payload_cierre_dia
from aplicacion.bd_app import conectar_bd
from fuelhub_core.bd import (
    pendientes_cierreturnos, detalle_cierreturno, pendientes_cierredias,
    codigo_estacion, admin_operador, marcar_enviado_cierreturno, marcar_enviado_cierredia,
)
from fuelhub_core.api import enviar_cierre_turno, enviar_cierre_dia

logger = logging.getLogger(__name__)

_avisado_sin_credenciales = False


def _enviar_turnos(conn, codigo: str) -> int:
    enviados = 0
    for turno in pendientes_cierreturnos(conn):
        try:
            turno["codigo_estacion"] = codigo
            detalle = detalle_cierreturno(conn, turno["id"])
            payload = payload_cierre_turno(turno, detalle)
            if enviar_cierre_turno(turno["id"], payload):
                marcar_enviado_cierreturno(conn, turno["id"])
                enviados += 1
        except Exception:
            logger.exception("Error enviando cierre de turno %s", turno.get("id"))
    return enviados


def _enviar_dias(conn, codigo: str, admin: dict) -> int:
    enviados = 0
    for dia in pendientes_cierredias(conn):
        try:
            dia["codigo_estacion"] = codigo
            dia["admin_codigo"] = admin.get("codigo")
            dia["admin_nombre"] = admin.get("nombre")
            payload = payload_cierre_dia(dia)
            if enviar_cierre_dia(dia["id"], payload):
                marcar_enviado_cierredia(conn, dia["id"])
                enviados += 1
        except Exception:
            logger.exception("Error enviando cierre de día %s", dia.get("id"))
    return enviados


def ciclo_cierres():
    global _avisado_sin_credenciales
    if not (FUELHUB_CORE_CLIENT_ID and FUELHUB_CORE_CLIENT_SECRET):
        # Best-effort, igual que el notificador de comprobantes: sin credenciales
        # configuradas este hilo no tiene nada para hacer, y no debe ensuciar el
        # log en cada ciclo repitiendo el mismo aviso.
        if not _avisado_sin_credenciales:
            logger.warning(
                "Faltan FUELHUB_CORE_CLIENT_ID/FUELHUB_CORE_CLIENT_SECRET en el "
                ".env; los cierres de turno/día no se envían."
            )
            _avisado_sin_credenciales = True
        return
    if repositorio.motor_de(DATABASE_URL) != "sqlserver":
        # Cierreturnos/Cierredias son del esquema de grifo (SQL Server); en
        # cualquier otro motor no existen y no hay nada que hacer.
        return

    conn = None
    try:
        conn = conectar_bd()
        codigo = codigo_estacion(conn)
        admin = admin_operador(conn)

        enviados_turno = _enviar_turnos(conn, codigo)
        if enviados_turno:
            logger.info("%d cierre(s) de turno enviados a FuelHub core.", enviados_turno)

        enviados_dia = _enviar_dias(conn, codigo, admin)
        if enviados_dia:
            logger.info("%d cierre(s) de día enviados a FuelHub core.", enviados_dia)
    except Exception:
        logger.exception("Error en ciclo_cierres")
    finally:
        if conn:
            conn.close()
