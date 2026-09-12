"""
Cliente HTTP de FuelHub core: token OAuth2 (client_credentials, Cognito), el
envío de los cierres de turno y de día, y la subida del PDF de comprobantes.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from base64 import b64encode

from config import (
    FUELHUB_CORE_BASE_URL, FUELHUB_CORE_TOKEN_URL,
    FUELHUB_CORE_CLIENT_ID, FUELHUB_CORE_CLIENT_SECRET,
)

logger = logging.getLogger(__name__)

# Token en memoria: no hace falta que sobreviva a un reinicio del proceso —pedir
# uno de más al arrancar no cuesta nada—, a diferencia de reintentos.json.
_lock_token = threading.Lock()
_token: dict = {"valor": None, "vence": 0.0}

# Margen antes del vencimiento real (expires_in), para no arrancar una llamada
# con un token que expira a mitad de camino.
_MARGEN_TOKEN_SEG = 60


def _pedir_token() -> tuple:
    credenciales = b64encode(f"{FUELHUB_CORE_CLIENT_ID}:{FUELHUB_CORE_CLIENT_SECRET}".encode()).decode()
    peticion = urllib.request.Request(
        FUELHUB_CORE_TOKEN_URL,
        data=b"grant_type=client_credentials",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credenciales}",
        },
        method="POST",
    )
    with urllib.request.urlopen(peticion, timeout=30) as r:
        datos = json.loads(r.read().decode())
    return datos["access_token"], int(datos.get("expires_in", 3600))


def _token_vigente() -> str:
    """Token cacheado en memoria; se renueva solo cuando está por vencer."""
    if not (FUELHUB_CORE_CLIENT_ID and FUELHUB_CORE_CLIENT_SECRET):
        raise RuntimeError("Faltan FUELHUB_CORE_CLIENT_ID/FUELHUB_CORE_CLIENT_SECRET en el .env")
    with _lock_token:
        if _token["valor"] and time.monotonic() < _token["vence"]:
            return _token["valor"]
        valor, expira_en = _pedir_token()
        _token["valor"] = valor
        _token["vence"] = time.monotonic() + max(expira_en - _MARGEN_TOKEN_SEG, 0)
        return valor


def _enviar(method: str, path: str, payload: dict, headers_extra: dict = None) -> bool:
    """True si FuelHub core aceptó el envío (2xx)."""
    try:
        token = _token_vigente()
    except Exception:
        logger.exception("No se pudo obtener el token de FuelHub core; se reintenta en el próximo ciclo.")
        return False

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if headers_extra:
        headers.update(headers_extra)

    peticion = urllib.request.Request(
        f"{FUELHUB_CORE_BASE_URL}/{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(peticion, timeout=30) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")
        if e.code == 401:
            # Token vencido o revocado: se descarta el cacheado para que la
            # próxima llamada pida uno nuevo en vez de repetir el mismo rechazo.
            with _lock_token:
                _token["valor"] = None
        logger.warning(
            "FuelHub core rechazó %s %s (HTTP %s): %s | payload enviado: %s",
            method, path, e.code, cuerpo[:500], json.dumps(payload, ensure_ascii=False)[:500],
        )
        return False
    except Exception:
        logger.exception("Error llamando a FuelHub core (%s %s)", method, path)
        return False


def _post(path: str, payload: dict, idempotency_key: str) -> bool:
    return _enviar("POST", path, payload, {"Idempotency-Key": idempotency_key})


def enviar_cierre_turno(cierreturno_id, payload: dict) -> bool:
    # Clave estable por fila: un reintento del mismo cierre —tras un timeout de
    # red, por ejemplo— reusa la misma clave, para que FuelHub core lo trate
    # como el mismo evento y no lo cuente dos veces.
    clave = str(uuid.uuid5(uuid.NAMESPACE_URL, f"cierreturno:{cierreturno_id}"))
    return _post("v1/cierres-turno", payload, clave)


def enviar_cierre_dia(cierredia_id, payload: dict) -> bool:
    clave = str(uuid.uuid5(uuid.NAMESPACE_URL, f"cierredia:{cierredia_id}"))
    return _post("v1/cierres-dia", payload, clave)


def subir_pdf_comprobante(codigo_estacion: str, ruc: str, numeracion: str, pdf_bytes: bytes) -> bool:
    """
    PUT v1/comprobantes/{numeracion}/pdf — sube el PDF ya generado del
    comprobante para que la página de consulta lo sirva desde S3 (ver
    fuelhub-core: services/ingest-comprobante-pdf). Sin Idempotency-Key: a
    diferencia de los cierres (un INSERT), acá el Lambda hace un PutObject a
    una key fija (ruc/numeracion.pdf) — subir el mismo PDF dos veces pisa el
    mismo objeto sin ningún efecto secundario.
    """
    ruta = f"v1/comprobantes/{urllib.parse.quote(numeracion, safe='')}/pdf"
    payload = {
        "codigoEstacion": codigo_estacion,
        "ruc": ruc,
        "contentBase64": b64encode(pdf_bytes).decode("ascii"),
    }
    return _enviar("PUT", ruta, payload)
