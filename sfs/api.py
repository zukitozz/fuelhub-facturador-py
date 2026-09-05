"""
Cliente REST del SFS local (facturadorApp). Todo lo que el daemon le pide por HTTP:
que relea DATA, que genere el XML de un documento y que lo entregue a SUNAT.
"""
import json
import logging
import time
import urllib.error
import urllib.request

from config import SFS_BASE_URL, _TIPOS_SFS, _ESPERA_XML_SEG, _ESPERA_REINTENTO_SEG, _ESPERA_ENTRE_DOCS_SEG, _COOLDOWN_REENVIO_SEG
from dominio.texto import _texto
from sfs.bd import _xml_generado

logger = logging.getLogger(__name__)

# Marca de tiempo (monotonic) del último intento por documento, para el cooldown
# de activar_procesamiento_sfs(). Vive en memoria: solo evita repetir un documento
# dentro del mismo proceso corriendo, no hace falta que sobreviva a un reinicio.
_ultimo_intento: dict = {}


def _sfs_post(path: str, payload: dict):
    url = f"{SFS_BASE_URL}/{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.URLError as e:
        logger.warning("SFS no disponible (%s): %s", url, e)
        return None
    except Exception:
        logger.exception("Error llamando SFS %s", path)
        return None


def _resumen_sfs(r) -> str:
    """
    Respuesta del SFS en una línea. Sin esto, un solo fallo vuelca al log toda la
    bandeja (listaBandejaFacturador), que son decenas de miles de caracteres.
    """
    if not isinstance(r, dict):
        return repr(r)
    partes = [f"{k}={r[k]!r}" for k in ("validacion", "mensaje") if r.get(k)]
    for clave, valor in r.items():
        if isinstance(valor, list):
            partes.append(f"{clave}=[{len(valor)} items]")
    return ", ".join(partes) or repr(r)


def sincronizar_bandeja_sfs() -> bool:
    """
    Fuerza al SFS a releer la carpeta DATA y registrar en su bandeja lo que haya
    nuevo. Devuelve True si respondió.

    Hace falta porque el SFS solo escanea DATA cuando la pantalla de su bandeja
    hace su refresco periódico (cargarArchivosContribuyente cuelga de
    ActualizarPantalla.htm, NO de CargarPantalla.htm —la carga inicial de la
    pantalla— pese a lo que sugiere el nombre) o desde un job programado que
    exige el temporizador prendido. Ni GenerarComprobante.htm ni enviarXML.htm
    lo hacen: operan sobre lo que ya está en la bandeja.

    Confirmado en la práctica: con CargarPantalla.htm el daemon llamaba a este
    endpoint cada ciclo sin ningún efecto —cero cargarArchivosContribuyente en
    el log del SFS durante 46 minutos seguidos— y el escaneo solo corría cuando
    alguien tenía la bandeja abierta en el navegador, porque es esa página la
    que dispara ActualizarPantalla.htm en su refresco automático. Sin este
    endpoint (el correcto) el daemon dependía de esa pestaña, y si quedaba en
    segundo plano el navegador le frenaba el temporizador y los documentos se
    quedaban sin procesar hasta que alguien volvía a tocar la PC.
    """
    r = _sfs_post("api/ActualizarPantalla.htm", {})
    if r is None:
        return False
    if r.get("validacion") != "EXITO":
        logger.warning("El SFS no pudo releer DATA: %s", _resumen_sfs(r))
        return False
    return True


def activar_procesamiento_sfs(documentos: list) -> list:
    """Envía los documentos al SFS local. Devuelve solo los que el SFS aceptó."""
    if not documentos:
        return []
    try:
        urllib.request.urlopen(f"{SFS_BASE_URL}/", timeout=3)
    except Exception:
        logger.warning("SFS no responde — envío automático desactivado.")
        return []

    # Que el SFS levante de DATA lo recién escrito antes de pedirle nada sobre ello:
    # los endpoints de generar y enviar solo ven lo que ya está en su bandeja.
    sincronizar_bandeja_sfs()

    enviados = []
    ahora    = time.monotonic()
    # El cooldown solo evita repetir un documento dentro del mismo ciclo, así que las
    # marcas vencidas no sirven de nada: sin purgarlas el diccionario crece un registro
    # por comprobante y nunca libera, en un proceso pensado para correr meses.
    for clave in [k for k, t in _ultimo_intento.items() if ahora - t >= _COOLDOWN_REENVIO_SEG]:
        del _ultimo_intento[clave]

    for doc in documentos:
        tip   = _texto(doc.get("tip_docu"))
        num   = _texto(doc.get("num_docu"))
        label = f"{tip}-{num}"
        if tip not in _TIPOS_SFS:
            logger.info("[SFS] Tipo %s fuera de alcance, omitido: %s", tip, label)
            continue

        previo = _ultimo_intento.get((tip, num))
        if previo is not None and ahora - previo < _COOLDOWN_REENVIO_SEG:
            continue
        _ultimo_intento[(tip, num)] = ahora

        payload = {k: doc[k] for k in ("num_ruc", "tip_docu", "num_docu")}

        r1 = _sfs_post("api/GenerarComprobante.htm", payload)
        if not (r1 and r1.get("validacion") == "EXITO"):
            logger.warning("[SFS] Error al generar XML para %s: %s", label, _resumen_sfs(r1))
            continue

        time.sleep(_ESPERA_XML_SEG)
        # El SFS trabaja en dos pasadas: la 1ra solo registra el archivo de DATA en
        # su bandeja (IND_SITU='01'); recién la 2da genera el XML ('02'). Sin este
        # segundo llamado, enviarXML responde "No existen datos que procesar".
        if not _xml_generado(_texto(doc.get("num_ruc")), tip, num):
            _sfs_post("api/GenerarComprobante.htm", payload)
            time.sleep(_ESPERA_XML_SEG)

        r2 = _sfs_post("api/enviarXML.htm", payload)
        if not (r2 and r2.get("validacion") == "EXITO"):
            time.sleep(_ESPERA_REINTENTO_SEG)
            r2 = _sfs_post("api/enviarXML.htm", payload)

        # OJO: "EXITO" solo dice que el SFS aceptó el pedido. NO garantiza que
        # SUNAT lo haya recibido — el SFS puede dejarlo en IND_SITU='06' (p.ej.
        # boletas de más de 5 días, que exigen resumen diario). Lo confirma el CDR.
        if r2 and r2.get("validacion") == "EXITO":
            logger.info("[SFS] Entregado al SFS: %s", label)
            enviados.append(doc)
        elif "no existen datos" in str((r2 or {}).get("mensaje", "")).lower():
            # Primera pasada: el SFS aún no generó el XML. Es el flujo normal,
            # no un error — el próximo ciclo lo retoma desde IND_SITU='01'.
            logger.info("[SFS] %s aún sin XML; se completa en el próximo ciclo.", label)
        else:
            logger.warning("[SFS] Error al entregar %s: %s", label, _resumen_sfs(r2))
        time.sleep(_ESPERA_ENTRE_DOCS_SEG)
    return enviados
