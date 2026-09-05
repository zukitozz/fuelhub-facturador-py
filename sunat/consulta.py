"""
Consulta directa a SUNAT (SOAP billConsultService, getStatusCdr): ¿el comprobante
ya está registrado? Sirve para el caso de la conexión cortada, cuando el SFS mandó
el documento pero nunca volvió su CDR y nadie sabe si llegó.
"""
import base64
import binascii
import logging
import os
import urllib.error
import urllib.request

from config import SUNAT_CONSULTA_URL, SOL_USUARIO, SOL_CLAVE, _CODIGOS_NO_REGISTRADO, SFS_RPTA_DIR
from dominio.cdr import _texto_de_nodo

logger = logging.getLogger(__name__)

# Plantilla del sobre SOAP. La autenticación va como UsernameToken de WS-Security y
# el usuario es el RUC pegado al usuario SOL secundario, sin separador.
_SOBRE_CONSULTA = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:ser="http://service.sunat.gob.pe">
  <soapenv:Header>
    <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>{usuario}</wsse:Username>
        <wsse:Password>{clave}</wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>
  </soapenv:Header>
  <soapenv:Body>
    <ser:getStatusCdr>
      <rucComprobante>{ruc}</rucComprobante>
      <tipoComprobante>{tipo}</tipoComprobante>
      <serieComprobante>{serie}</serieComprobante>
      <numeroComprobante>{numero}</numeroComprobante>
    </ser:getStatusCdr>
  </soapenv:Body>
</soapenv:Envelope>"""


def consultar_estado_sunat(ruc: str, tipo: str, numeracion: str):
    """
    Pregunta a SUNAT si un comprobante está registrado.

    Devuelve (codigo, mensaje, cdr_zip) donde cdr_zip son los bytes del CDR cuando
    SUNAT lo entrega, o None. Ante cualquier fallo devuelve (None, motivo, None):
    quien llama debe tratar esa respuesta como "no sé", nunca como "no existe".
    Reenviar un comprobante que en realidad sí llegó lo duplica ante SUNAT.
    """
    if not (SOL_USUARIO and SOL_CLAVE):
        return None, "faltan SOL_USUARIO y SOL_CLAVE en el .env", None
    if "-" not in numeracion:
        return None, f"numeración sin serie: {numeracion!r}", None

    serie, correlativo = numeracion.split("-", 1)
    sobre = _SOBRE_CONSULTA.format(
        usuario=f"{ruc}{SOL_USUARIO}",
        clave=SOL_CLAVE,
        ruc=ruc,
        tipo=tipo,
        serie=serie,
        # SUNAT espera el correlativo como número, sin los ceros de la izquierda.
        numero=correlativo.lstrip("0") or "0",
    )
    peticion = urllib.request.Request(
        SUNAT_CONSULTA_URL,
        data=sobre.encode("utf-8"),
        # El SOAPAction no es opcional: sin él SUNAT despacha a getStatus —la consulta
        # de tickets— y responde "El ticket no existe" para cualquier comprobante.
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "urn:getStatusCdr"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=30) as r:
            respuesta = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")
        detalle = _texto_de_nodo(cuerpo, "faultstring") or f"HTTP {e.code}"
        logger.warning("Consulta a SUNAT rechazada para %s-%s: %s", tipo, numeracion, detalle)
        return None, detalle, None
    except Exception as e:
        logger.warning("No se pudo consultar a SUNAT por %s-%s: %s", tipo, numeracion, e)
        return None, str(e), None

    codigo  = _texto_de_nodo(respuesta, "statusCode")
    mensaje = _texto_de_nodo(respuesta, "statusMessage")
    b64     = _texto_de_nodo(respuesta, "content")
    cdr = None
    if b64:
        try:
            cdr = base64.b64decode(b64)
        except (ValueError, binascii.Error):
            logger.exception("SUNAT devolvió un CDR ilegible para %s-%s", tipo, numeracion)
    return codigo or None, mensaje, cdr


def estado_en_sunat(ruc: str, tipo: str, numeracion: str) -> str:
    """
    'registrado' | 'no_registrado' | 'desconocido', más el CDR si SUNAT lo entrega.

    La distinción entre 'no_registrado' y 'desconocido' es lo importante: solo el
    primero autoriza a reenviar. Cualquier código que no esté en la lista blanca se
    trata como desconocido, porque reenviar algo que en realidad sí llegó lo duplica
    ante SUNAT, y eso no se deshace sin una nota de crédito.
    """
    codigo, mensaje, cdr = consultar_estado_sunat(ruc, tipo, numeracion)
    if codigo is None:
        return "desconocido", None, mensaje
    if cdr:
        return "registrado", cdr, mensaje
    if codigo in _CODIGOS_NO_REGISTRADO:
        return "no_registrado", None, mensaje
    logger.warning(
        "SUNAT respondió por %s-%s un código que no sabemos interpretar (%s: %s); "
        "no se reenvía por las dudas.", tipo, numeracion, codigo, mensaje,
    )
    return "desconocido", None, mensaje


def _guardar_cdr(ruc: str, tipo: str, numeracion: str, cdr: bytes, mensaje: str):
    """
    Deja el CDR recuperado en RPTA, con el mismo nombre que le pondría el SFS.

    De ahí lo levanta el hilo CDR y lo procesa como cualquier otro. El nombre
    importa más de lo que parece: el XML de un CDR de consulta trae la numeración en
    otro formato que la de un envío normal, así que es el nombre —armado desde el
    NUM_DOCU canónico— el que permite reconciliarla (ver dominio/cdr.py:
    _reconciliar_numeracion). Se escribe con nombre temporal y se renombra para que
    watchdog no lo levante a medio escribir.

    Lo que este camino NO deja resuelto, a diferencia del normal, es la fila en la
    bandeja del SFS: sigue con el error de red que la trajo hasta acá. La cierra
    sfs.bd._cerrar_documento_en_sfs() una vez que el comprobante quedó cerrado en la
    BD de la aplicación, no antes.
    """
    os.makedirs(SFS_RPTA_DIR, exist_ok=True)
    destino = os.path.join(SFS_RPTA_DIR, f"R{ruc}-{tipo}-{numeracion}.zip")
    with open(destino + ".tmp", "wb") as fh:
        fh.write(cdr)
    os.replace(destino + ".tmp", destino)
    logger.info(
        "CDR de %s-%s recuperado desde SUNAT (%s); queda en RPTA para procesar.",
        tipo, numeracion, mensaje,
    )
