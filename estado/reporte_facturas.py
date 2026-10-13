"""
Persistencia propia del daemon para el reporte semanal top-facturas
(reporte_facturas.json): qué semana fue la última mandada a FuelHub core, y la
Idempotency-Key en curso para la semana que se está intentando mandar.

La clave se genera una sola vez por semana real y se reutiliza en cada reintento
de esa misma semana —si se generara una nueva en cada intento, un timeout de red
cuya respuesta en realidad sí llegó a procesarse del lado de FuelHub core
terminaría duplicando la notificación de WhatsApp (ver POST
v1/reportes/top-facturas, header Idempotency-Key obligatorio)—. Por eso la clave
se persiste ANTES de llamar a la API, no después: si el proceso se cae a mitad de
la llamada, el próximo ciclo reintenta con la misma clave en vez de generar otra.
"""
import json
import logging
import threading
import uuid

from config import _REPORTE_FACTURAS_PATH
from utilidades_files import escribir_archivo

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _leer() -> dict:
    try:
        with open(_REPORTE_FACTURAS_PATH, encoding="utf-8") as fh:
            datos = json.load(fh)
        return datos if isinstance(datos, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # Un archivo corrupto no puede frenar el ciclo: se empieza de cero y se
        # avisa. El costo es, como mucho, una Idempotency-Key nueva para la
        # semana en curso si justo se corrompió a mitad de un reintento.
        logger.exception(
            "No se pudo leer %s; se reinicia el estado del reporte semanal.",
            _REPORTE_FACTURAS_PATH,
        )
        return {}


def _guardar(datos: dict):
    try:
        escribir_archivo(_REPORTE_FACTURAS_PATH, json.dumps(datos, ensure_ascii=False, indent=2))
    except OSError:
        logger.exception("No se pudo guardar %s; el estado del reporte semanal no persiste.", _REPORTE_FACTURAS_PATH)


def semana_ya_enviada(semana_inicio: str) -> bool:
    with _lock:
        estado = _leer()
    return estado.get("semana_inicio") == semana_inicio and bool(estado.get("enviado"))


def idempotency_key_para(semana_inicio: str) -> str:
    """
    Clave a usar para mandar (o reintentar) el reporte de esa semana. Si ya hay
    una clave en curso para esa misma semana la reutiliza; si es una semana
    nueva, genera una y la persiste antes de devolverla.
    """
    with _lock:
        estado = _leer()
        if estado.get("semana_inicio") == semana_inicio and estado.get("idempotency_key"):
            return estado["idempotency_key"]
        clave = str(uuid.uuid4())
        _guardar({"semana_inicio": semana_inicio, "idempotency_key": clave, "enviado": False})
        return clave


def marcar_semana_enviada(semana_inicio: str, idempotency_key: str):
    with _lock:
        _guardar({"semana_inicio": semana_inicio, "idempotency_key": idempotency_key, "enviado": True})
