"""
Consulta del ticket de un resumen diario (SOAP getStatus). Un resumen no devuelve
su CDR en el acto como una factura: SUNAT responde un ticket y hay que volver a
preguntar por él. El SFS sabe hacerlo, pero solo desde un job programado
(ActualizarBajasJob) que exige tener el temporizador prendido, y prenderlo
levantaría también sus jobs de generar/enviar, que harían por su cuenta lo mismo
que este daemon hace por REST. Por eso la consulta la hace el daemon, con el mismo
patrón que usa sunat/consulta.py para recuperar CDR perdidos.
"""
import base64
import binascii
import re
import logging
import urllib.error
import urllib.request

from config import SOL_USUARIO, SOL_CLAVE, SFS_CONSTANTES_PATH
from dominio.cdr import _texto_de_nodo
from dominio.texto import _texto

logger = logging.getLogger(__name__)

_SOBRE_TICKET = """<?xml version="1.0" encoding="UTF-8"?>
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
    <ser:getStatus>
      <ticket>{ticket}</ticket>
    </ser:getStatus>
  </soapenv:Body>
</soapenv:Envelope>"""


def _url_bill_service() -> str:
    """
    Endpoint de envío del SFS (RUTA_SERV_CDP de constantes.properties), que es el
    mismo servicio donde se consulta el ticket.

    Se lee de ahí en vez de tener su propia variable para que la consulta salga
    SIEMPRE al ambiente al que el SFS está enviando: si alguien pasa el SFS de beta
    a producción, esto lo sigue solo. Preguntarle a producción por un ticket de
    beta —o al revés— devolvería "el ticket no existe".
    """
    try:
        # utf-8-sig y no utf-8: si alguien edita el archivo con el Bloc de notas le
        # queda un BOM al inicio, y con utf-8 ese caracter invisible se pega al
        # nombre de la primera propiedad.
        with open(SFS_CONSTANTES_PATH, encoding="utf-8-sig", errors="replace") as fh:
            for linea in fh:
                linea = linea.strip()
                # Las variantes que no se usan quedan comentadas con '#', y hay una
                # por cada tipo de servicio y ambiente: solo vale la activa.
                if linea.startswith("RUTA_SERV_CDP="):
                    return linea.split("=", 1)[1].strip()
    except OSError:
        logger.exception(
            "No se pudo leer %s para ubicar el servicio de SUNAT.", SFS_CONSTANTES_PATH
        )
    return ""


def consultar_ticket_sunat(ruc: str, ticket: str):
    """
    Pregunta a SUNAT por el resultado de un ticket de resumen.

    Devuelve (codigo, mensaje, cdr_zip). Un None en el código significa "todavía no
    sé" —falla de transporte, credenciales, servicio caído— y quien llama debe
    reintentar más tarde.

    Cuando SUNAT rechaza con un fault codificado, ese código SÍ vuelve: es la única
    forma de distinguir un ticket que ya se consumió (0127, definitivo) de un
    "Internal Error" pasajero. Aplastar los dos en None hacía que un resumen trabado
    se reintentara para siempre o no se reintentara nunca, según de qué lado se
    errara.
    """
    if not (SOL_USUARIO and SOL_CLAVE):
        return None, "faltan SOL_USUARIO y SOL_CLAVE en el .env", None
    url = _url_bill_service()
    if not url:
        return None, "no se pudo determinar el servicio de SUNAT (RUTA_SERV_CDP)", None

    sobre = _SOBRE_TICKET.format(usuario=f"{ruc}{SOL_USUARIO}", clave=SOL_CLAVE, ticket=ticket)
    peticion = urllib.request.Request(
        url,
        data=sobre.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "urn:getStatus"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=30) as r:
            respuesta = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")
        detalle = _texto_de_nodo(cuerpo, "faultstring") or f"HTTP {e.code}"
        codigo_fault = _codigo_de_fault(cuerpo)
        logger.warning("Consulta del ticket %s rechazada por SUNAT: %s", ticket, detalle)
        return codigo_fault or None, detalle, None
    except Exception as e:
        logger.warning("No se pudo consultar el ticket %s: %s", ticket, e)
        return None, str(e), None

    codigo  = _texto_de_nodo(respuesta, "statusCode")
    mensaje = _texto_de_nodo(respuesta, "statusMessage")
    b64     = _texto_de_nodo(respuesta, "content")
    cdr = None
    if b64:
        try:
            cdr = base64.b64decode(b64)
        except (ValueError, binascii.Error):
            logger.exception("SUNAT devolvió un CDR ilegible para el ticket %s", ticket)
    return codigo or None, mensaje, cdr


def _norm_codigo_ticket(codigo) -> str:
    """
    El código de getStatus sin los ceros de la izquierda, para poder compararlo.

    Existe porque SUNAT devuelve el mismo código en anchos distintos según la
    respuesta ('0' contra '0098'), y comparar el texto crudo hacía fallar la
    comparación justo en el caso más frecuente.

    El '0' se conserva como '0' y no se convierte en cadena vacía: vacío significa
    "SUNAT no dijo nada" —una falla de transporte— y cero significa "aceptado". Son
    dos cosas opuestas y aplastarlas daría por bueno un envío que nunca respondió.
    """
    texto = _texto(codigo)
    if not texto:
        return ""
    return texto.lstrip("0") or "0"


def _codigo_de_fault(cuerpo: str) -> str:
    """
    Código de SUNAT dentro del faultcode de un error SOAP.

    Viene pegado al espacio de nombres —"soap-env:Client.0127"— y es lo único que
    distingue un rechazo con veredicto de una falla pasajera. Sin extraerlo, los dos
    llegaban como None a quien llama y no habia forma de saber si convenia reintentar
    o si SUNAT ya habia dicho la ultima palabra.
    """
    m = re.search(r"<(?:\w+:)?faultcode>(.*?)</(?:\w+:)?faultcode>", cuerpo, re.S)
    if not m:
        return ""
    n = re.search(r"(\d{3,4})\s*$", m.group(1).strip())
    return n.group(1) if n else ""
