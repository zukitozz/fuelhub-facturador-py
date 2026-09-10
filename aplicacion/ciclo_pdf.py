"""
Caso de uso del hilo PDF: envía a FuelHub core el PDF de los comprobantes que
la aplicación ya generó (Comprobantes.pdf_bytes) y que todavía no se subieron
(pdf_enviado = 0 o NULL) — ver fuelhub-core: services/ingest-comprobante-pdf,
que lo sube a S3 para que la página web de consulta lo sirva. No participa del
flujo de facturación con SUNAT —es best-effort para esa consulta— así que una
falla acá nunca detiene el ciclo ni se propaga fuera de él.

Mismo patrón que ciclo_cierres.py: mismas credenciales OAuth2
(FUELHUB_CORE_CLIENT_ID/SECRET), mismo chequeo de motor.
"""
import logging

import repositorio
from config import DATABASE_URL, EMISOR_RUC_OVERRIDE, FUELHUB_CORE_CLIENT_ID, FUELHUB_CORE_CLIENT_SECRET
from aplicacion.bd_app import conectar_bd
from fuelhub_core.bd import codigo_estacion, pendientes_pdf, marcar_pdf_enviado
from fuelhub_core.api import subir_pdf_comprobante

logger = logging.getLogger(__name__)

_avisado_sin_credenciales = False


def ciclo_pdf():
    global _avisado_sin_credenciales
    if not (FUELHUB_CORE_CLIENT_ID and FUELHUB_CORE_CLIENT_SECRET):
        # Best-effort, igual que ciclo_cierres: sin credenciales configuradas
        # este hilo no tiene nada para hacer, y no debe ensuciar el log en
        # cada ciclo repitiendo el mismo aviso.
        if not _avisado_sin_credenciales:
            logger.warning(
                "Faltan FUELHUB_CORE_CLIENT_ID/FUELHUB_CORE_CLIENT_SECRET en el "
                ".env; los PDF de comprobantes no se suben."
            )
            _avisado_sin_credenciales = True
        return
    if repositorio.motor_de(DATABASE_URL) != "sqlserver":
        # pdf_bytes/pdf_enviado son columnas del esquema de grifo (SQL Server);
        # en cualquier otro motor no existen y no hay nada que hacer.
        return

    conn = None
    try:
        conn = conectar_bd()
        codigo = codigo_estacion(conn)
        subidos = 0
        for fila in pendientes_pdf(conn):
            comprobante_id = fila["id"]
            numeracion = fila["numeracion_comprobante"]
            try:
                if subir_pdf_comprobante(codigo, EMISOR_RUC_OVERRIDE, numeracion, bytes(fila["pdf_bytes"])):
                    marcar_pdf_enviado(conn, comprobante_id)
                    subidos += 1
            except Exception:
                logger.exception("Error subiendo a FuelHub core el PDF de %s", numeracion)
        if subidos:
            logger.info("%d PDF de comprobante(s) subido(s).", subidos)
    except Exception:
        logger.exception("Error en ciclo_pdf")
    finally:
        if conn:
            conn.close()
