"""
Persistencia propia del ciclo de subida de PDF a FuelHub core (ver
aplicacion/ciclo_pdf.py): solo cuántas corridas seguidas no pudieron subir
nada, para el backoff de ciclo entero — en disco por el mismo motivo que
estado/envio_api.py: un reinicio de PM2 no puede perder de vista ese conteo.
"""
import json
import logging
import threading

from config import _ENVIO_PDF_PATH
from utilidades_files import escribir_archivo

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _leer() -> dict:
    try:
        with open(_ENVIO_PDF_PATH, encoding="utf-8") as fh:
            datos = json.load(fh)
        return datos if isinstance(datos, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.exception("No se pudo leer %s; se reinicia el estado de subida de PDF.", _ENVIO_PDF_PATH)
        return {}


def _guardar(datos: dict):
    try:
        escribir_archivo(_ENVIO_PDF_PATH, json.dumps(datos, ensure_ascii=False, indent=2))
    except OSError:
        logger.exception("No se pudo guardar %s; el estado de subida de PDF no persiste.", _ENVIO_PDF_PATH)


def contar_fallo_ciclo() -> int:
    with _lock:
        datos = _leer()
        fallos = int(datos.get("fallos_ciclo_consecutivos", 0)) + 1
        datos["fallos_ciclo_consecutivos"] = fallos
        _guardar(datos)
        return fallos


def limpiar_fallos_ciclo():
    with _lock:
        datos = _leer()
        if datos.get("fallos_ciclo_consecutivos"):
            datos["fallos_ciclo_consecutivos"] = 0
            _guardar(datos)
