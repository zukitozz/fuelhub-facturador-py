"""
Caso de uso del hilo PDF: envía a FuelHub core el PDF de factura y de nota de
crédito de factura ya aceptadas (Comprobantes.pdf_bytes, enviado=1) y que
todavía no se subieron (pdf_enviado = 0 o NULL) — ver fuelhub-core:
services/ingest-comprobante-pdf, que lo sube a S3 para que la página web de
consulta lo sirva. Boleta, nota de débito y nota de crédito de boleta quedan
afuera a propósito (decisión del equipo del facturador, ver el filtro en
fuelhub_core.bd._SQL_PENDIENTES_PDF) — este módulo nunca las ve. No participa
del flujo de facturación con SUNAT —es best-effort para esa consulta— así que
una falla acá nunca detiene el ciclo ni se propaga fuera de él.

pdf_bytes es un BUZÓN TEMPORAL, no un archivo permanente: cada base de
estación tiene un tope de 10 GB, así que Sofia lo llena y este ciclo lo sube y
lo vacía (ver fuelhub_core.bd.marcar_pdf_enviado) — nunca antes de confirmar
la subida. Se procesa en lotes chicos (PDF_LOTE_CANTIDAD, ver config.py) para
no intentar de una sola vez un atraso grande.

Junto con el PDF, sube el XML y el CDR:
  - El XML ya está guardado en Comprobantes.xml_envio desde que
    aplicacion/ciclo_generacion.py lo recibió de facturador-api (columna
    `incluir=xml` del POST) — acá no se vuelve a pedir.
  - El CDR se pide fresco acá con un GET: factura y nota de crédito siempre
    tienen CDR propio (a diferencia de la boleta, que no llega a este ciclo).

Si una corrida entera no logra subir nada (sin internet, FuelHub core/S3
caído), se aplica un backoff de CICLO ENTERO creciente
(PDF_ESPERA_MAX_CICLO_MIN) en vez de reintentar cada INTERVALO_PDF_SEG como si
nada pasara — mismo patrón que aplicacion/ciclo_generacion.py. Una fila que
falla sola (ej. un PDF puntual que FuelHub core rechaza) no bloquea al resto
del lote: se loguea y se sigue con la siguiente.

Mismo patrón que ciclo_cierres.py: mismas credenciales OAuth2
(FUELHUB_CORE_CLIENT_ID/SECRET), mismo chequeo de motor.
"""
import logging
import time

import repositorio
from config import (
    DATABASE_URL, EMISOR_RUC_OVERRIDE, FUELHUB_CORE_CLIENT_ID, FUELHUB_CORE_CLIENT_SECRET,
    PDF_LOTE_CANTIDAD, PDF_ESPERA_MAX_CICLO_MIN,
)
from dominio.texto import _codigo
from aplicacion.bd_app import conectar_bd
from fuelhub_core.bd import codigo_estacion, pendientes_pdf, marcar_pdf_enviado
from fuelhub_core.api import subir_pdf_comprobante
from fuelhub_api.cliente import consultar_comprobante
from estado import envio_pdf

logger = logging.getLogger(__name__)

_avisado_sin_credenciales = False


def _obtener_cdr(ruc: str, tipo: str, numeracion: str) -> tuple[str | None, bool]:
    """
    CDR fresco para factura/nota. Devuelve (cdr, agotado):
      - (cdr, False): listo para subir.
      - (None, False): todavía no está — se reintenta en el próximo ciclo.
      - (None, True): facturador-api ya no tiene este comprobante (404) — no es
        que falte el CDR, es que nunca va a existir (ej. un enviado=1 de antes
        de usar facturador-api). Insistir cada ciclo para siempre no lo iba a
        resolver nunca.
    """
    resp = consultar_comprobante(ruc, f"{tipo}-{numeracion}")
    if resp.status == 404:
        return None, True
    if resp.status != 200 or not resp.body:
        return None, False
    return resp.body.get("cdr"), False


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
        pendientes = pendientes_pdf(conn, PDF_LOTE_CANTIDAD)
        subidos = procesados = 0
        for fila in pendientes:
            comprobante_id = fila["id"]
            numeracion = fila["numeracion_comprobante"]
            try:
                tipo = _codigo(fila["tipo_comprobante"], "01")
                xml = fila["xml_envio"]
                xml_bytes = xml.encode("utf-8") if xml else None

                cdr, agotado = _obtener_cdr(EMISOR_RUC_OVERRIDE, tipo, numeracion)
                if agotado:
                    logger.warning(
                        "%s-%s no existe en facturador-api (404 pidiendo su "
                        "CDR); se da por no subible y se deja de reintentar.",
                        tipo, numeracion,
                    )
                    marcar_pdf_enviado(conn, comprobante_id)
                    procesados += 1
                    continue
                if not cdr:
                    # Todavía sin CDR: se reintenta en el próximo ciclo,
                    # no se sube el PDF a medias.
                    continue
                cdr_bytes = cdr.encode("utf-8")

                if subir_pdf_comprobante(
                    codigo, EMISOR_RUC_OVERRIDE, numeracion, bytes(fila["pdf_bytes"]),
                    fila["numero_documento_receptor"], fila["fecha_emision"],
                    xml_bytes, cdr_bytes,
                ) is not None:
                    # Si no afectó ninguna fila, otra corrida ya la había
                    # cerrado (AND pdf_enviado=0 de marcar_pdf_enviado): no es
                    # un error, solo no cuenta de nuevo.
                    if marcar_pdf_enviado(conn, comprobante_id):
                        subidos += 1
                    procesados += 1
            except Exception:
                logger.exception("Error subiendo a FuelHub core el PDF de %s", numeracion)

        if subidos:
            logger.info("%d PDF de comprobante(s) subido(s).", subidos)
        if procesados:
            envio_pdf.limpiar_fallos_ciclo()
        elif pendientes:
            # Había algo para subir y la corrida entera no pudo avanzar nada:
            # backoff creciente, en vez de reintentar cada INTERVALO_PDF_SEG
            # como si no hubiera pasado nada (sin internet, S3/FuelHub core
            # caído).
            fallos_ciclo = envio_pdf.contar_fallo_ciclo()
            espera_min = min(2 ** (fallos_ciclo - 1), PDF_ESPERA_MAX_CICLO_MIN)
            logger.warning(
                "%d corrida(s) seguidas sin poder subir ningún PDF; se pausa "
                "%d min antes del próximo intento.", fallos_ciclo, espera_min,
            )
            time.sleep(espera_min * 60)
    except Exception:
        logger.exception("Error en ciclo_pdf")
    finally:
        if conn:
            conn.close()
