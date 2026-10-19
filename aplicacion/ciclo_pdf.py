"""
Caso de uso del hilo PDF: envía a FuelHub core el PDF de los comprobantes ya
aceptados (Comprobantes.pdf_bytes, enviado=1) y que todavía no se subieron
(pdf_enviado = 0 o NULL) — ver fuelhub-core: services/ingest-comprobante-pdf,
que lo sube a S3 para que la página web de consulta lo sirva. No participa del
flujo de facturación con SUNAT —es best-effort para esa consulta— así que una
falla acá nunca detiene el ciclo ni se propaga fuera de él.

Junto con el PDF, sube el XML y (para factura/nota) el CDR:
  - El XML ya está guardado en Comprobantes.xml_envio desde que
    aplicacion/ciclo_generacion.py lo recibió de facturador-api (columna
    `incluir=xml` del POST) — acá no se vuelve a pedir, para una boleta ya no
    se podría: una vez que el resumen diario la consume, su propia consulta
    (GET) empieza a devolver 404.
  - El CDR, en cambio, se pide fresco acá con un GET, solo para factura/nota:
    una boleta nunca tiene CDR propio (es el del resumen que la agrupa, y
    asociarlo por boleta no está resuelto del lado de facturador-api) — ver
    decisión explícita de dejarlas sin CDR en este envío.

_SQL_PENDIENTES_PDF ya exige enviado=1, así que para una boleta esta fila solo
aparece una vez que quedó firmada (su propio "generado" ya cuenta como cierre,
ver ciclo_generacion.py) — no hace falta esperar a ningún resumen para subir
su PDF+XML.

Mismo patrón que ciclo_cierres.py: mismas credenciales OAuth2
(FUELHUB_CORE_CLIENT_ID/SECRET), mismo chequeo de motor.
"""
import logging

import repositorio
from config import DATABASE_URL, EMISOR_RUC_OVERRIDE, FUELHUB_CORE_CLIENT_ID, FUELHUB_CORE_CLIENT_SECRET
from dominio.texto import _codigo
from aplicacion.bd_app import conectar_bd
from fuelhub_core.bd import codigo_estacion, pendientes_pdf, marcar_pdf_enviado
from fuelhub_core.api import subir_pdf_comprobante
from fuelhub_api.cliente import consultar_comprobante

logger = logging.getLogger(__name__)

_avisado_sin_credenciales = False

# Tipo de boleta: nunca tiene CDR propio, no tiene sentido pedírselo a facturador-api.
_TIPO_BOLETA = "03"


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
        subidos = 0
        for fila in pendientes_pdf(conn):
            comprobante_id = fila["id"]
            numeracion = fila["numeracion_comprobante"]
            try:
                tipo = _codigo(fila["tipo_comprobante"], "01")
                xml = fila["xml_envio"]
                xml_bytes = xml.encode("utf-8") if xml else None

                cdr_bytes = None
                if tipo != _TIPO_BOLETA:
                    cdr, agotado = _obtener_cdr(EMISOR_RUC_OVERRIDE, tipo, numeracion)
                    if agotado:
                        logger.warning(
                            "%s-%s no existe en facturador-api (404 pidiendo su "
                            "CDR); se da por no subible y se deja de reintentar.",
                            tipo, numeracion,
                        )
                        marcar_pdf_enviado(conn, comprobante_id)
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
