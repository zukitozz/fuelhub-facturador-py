"""
Cliente HTTP de facturador-api (https://facturador.peru-hub.com): el servicio en
la nube que firma y envía los comprobantes a SUNAT a través de SFS.

Reemplaza al SFS local: antes este daemon escribía archivos para un proceso Java
en la misma PC y vigilaba una carpeta a la espera del CDR; ahora llama por HTTP
a un servicio que vive en otro lado y que responde el veredicto en la misma
llamada (ver facturador-api/app/main.py del otro repo). Acá solo el transporte
—qué hacer con cada respuesta (reintentos, backoff, ambigüedad) vive en
aplicacion/ciclo_generacion.py—.

Sin biblioteca externa (requests): este proyecto no la trae como dependencia, y
urllib alcanza para lo que hace falta, igual que fuelhub_core/api.py.
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from config import FACTURADOR_API_URL, FACTURADOR_API_TIMEOUT_SEG, FACTURADOR_API_KEY

logger = logging.getLogger(__name__)


class RespuestaAPI:
    """
    Resultado de una llamada a facturador-api.

    status=None significa que ni siquiera hubo respuesta HTTP (timeout, conexión
    rechazada, DNS, lo que sea) — la llamada es AMBIGUA: no se sabe si el
    servidor llegó a procesar el pedido antes de que se cortara la comunicación.
    Un 5xx que no sea 502 se trata igual de ambiguo, porque "Internal Server
    Error" tampoco dice si el comprobante se guardó o no del otro lado. El único
    5xx no ambiguo es el 502: ahí facturador-api ya verificó que SFS no tiene el
    comprobante, así que reintentar es seguro.
    """
    __slots__ = ("status", "body", "error")

    def __init__(self, status: int | None, body: dict | None, error: str = ""):
        self.status = status
        self.body = body
        self.error = error

    @property
    def ambigua(self) -> bool:
        return self.status is None or (self.status >= 500 and self.status != 502)

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


def _llamar(metodo: str, ruta: str, payload: dict = None, etiqueta: str = "") -> RespuestaAPI:
    url = f"{FACTURADOR_API_URL}/{ruta.lstrip('/')}"
    headers = {"Content-Type": "application/json"}
    if FACTURADOR_API_KEY:
        headers["Authorization"] = f"Bearer {FACTURADOR_API_KEY}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    peticion = urllib.request.Request(url, data=data, headers=headers, method=metodo)

    inicio = time.monotonic()
    try:
        with urllib.request.urlopen(peticion, timeout=FACTURADOR_API_TIMEOUT_SEG) as r:
            cuerpo, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        cuerpo, status = e.read(), e.code
    except Exception as e:
        # Nunca se loguean credenciales acá: ni FACTURADOR_API_KEY ni nada del
        # payload, solo el verbo, la ruta y la etiqueta (numeración/código) que
        # ya es pública en los logs de siempre.
        duracion = time.monotonic() - inicio
        logger.warning(
            "%s %s | %s | sin respuesta tras %.1fs: %s", metodo, ruta, etiqueta, duracion, e,
        )
        return RespuestaAPI(None, None, str(e))

    duracion = time.monotonic() - inicio
    try:
        body = json.loads(cuerpo) if cuerpo else {}
    except json.JSONDecodeError:
        body = None
    logger.info("%s %s | %s | HTTP %s (%.1fs)", metodo, ruta, etiqueta, status, duracion)
    return RespuestaAPI(status, body)


def crear_comprobante(ruc: str, payload: dict, numeracion: str = "", incluir: str = "xml") -> RespuestaAPI:
    """
    POST /empresas/{ruc}/comprobantes?incluir={incluir} — firma y envía un
    comprobante. La respuesta llega en la misma llamada (puede tardar, ver
    FACTURADOR_API_TIMEOUT_SEG): no hay un segundo paso de "consultar más
    tarde" salvo que la llamada sea ambigua (ver RespuestaAPI.ambigua).

    incluir="xml" por defecto, para los dos tipos por igual (factura/nota y
    boleta): el XML se pide siempre en esta primera llamada porque, para una
    boleta, es la ÚNICA oportunidad — una vez que el resumen diario la
    consume, su propia consulta (GET) empieza a devolver 404 y ya no hay
    forma de recuperarlo. El CDR no se pide acá: para factura/nota se
    consulta aparte cuando hace falta (consultar_comprobante); una boleta
    nunca tiene CDR propio.
    """
    ruta = f"empresas/{urllib.parse.quote(ruc)}/comprobantes"
    if incluir:
        ruta += f"?incluir={urllib.parse.quote(incluir)}"
    return _llamar("POST", ruta, payload, numeracion)


def consultar_comprobante(ruc: str, codigo: str) -> RespuestaAPI:
    """GET /empresas/{ruc}/comprobantes/{codigo} — siempre trae xml/cdr cuando
    existen, sin necesidad de pedirlos. Sirve tanto para resolver una llamada
    ambigua del POST como para consultar el estado/CDR más adelante."""
    ruta = f"empresas/{urllib.parse.quote(ruc)}/comprobantes/{urllib.parse.quote(codigo, safe='')}"
    return _llamar("GET", ruta, None, codigo)


def listar_resumenes(ruc: str, desde: str, limite: int = 100) -> RespuestaAPI:
    """
    GET /empresas/{ruc}/resumenes?desde=AAAA-MM-DD&limite=N — resúmenes de
    boletas que la API armó y envió por su cuenta (una boleta nunca se manda
    sola; queda "generado" hasta que un resumen diario la declara — ver spec
    §3). Ya no hay que disparar ese resumen desde acá (POST
    /resumenes-diarios quedó deprecado del lado del daemon): esto es solo
    para conciliar qué quedó rechazado y reencolar esas boletas.
    """
    ruta = (
        f"empresas/{urllib.parse.quote(ruc)}/resumenes"
        f"?desde={urllib.parse.quote(desde)}&limite={limite}"
    )
    return _llamar("GET", ruta, None, "resumenes")
