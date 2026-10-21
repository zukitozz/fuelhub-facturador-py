"""
Persistencia propia de la emisión vía facturador-api (envio_api.json), en disco
por la misma razón que el resto de estado/: un reinicio de PM2 no puede perder
de vista nada de esto.

Tres cosas separadas que comparten un solo archivo:

1. "En vuelo": qué comprobante se anotó justo ANTES de mandar el POST y todavía
   no se confirmó. Si el daemon se cae entre el POST y marcar_enviado(), al
   reiniciar lo primero que hace es resolver estas marcas con un GET en vez de
   reenviar a ciegas — ver aplicacion/ciclo_generacion.py.
2. Rechazos: cuántas veces volvió rechazado/con error un comprobante, hasta
   bloquearlo (MAX_REINTENTOS_RECHAZO) — mismo criterio que el daemon ya usaba
   con el SFS local, adaptado acá sin ninguna tabla DOCUMENTO que lo respalde.
3. Backoff de infraestructura: cuánto esperar antes de reintentar un
   comprobante que falló por una causa ajena al dato (502, timeout) — no gasta
   presupuesto de rechazos, porque reintentar es gratis y probablemente exitoso
   en cuanto vuelva el servicio.
"""
import json
import logging
import threading
from datetime import datetime

from config import _ENVIO_API_PATH, MAX_REINTENTOS_RECHAZO
from utilidades_files import escribir_archivo

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _leer() -> dict:
    try:
        with open(_ENVIO_API_PATH, encoding="utf-8") as fh:
            datos = json.load(fh)
        return datos if isinstance(datos, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.exception("No se pudo leer %s; se reinicia el estado de envío.", _ENVIO_API_PATH)
        return {}


def _guardar(datos: dict):
    try:
        escribir_archivo(_ENVIO_API_PATH, json.dumps(datos, ensure_ascii=False, indent=2))
    except OSError:
        logger.exception("No se pudo guardar %s; el estado de envío no persiste.", _ENVIO_API_PATH)


# --- "en vuelo": idempotencia ante un corte justo durante el POST -----------

def marcar_en_vuelo(numeracion: str, tipo: str):
    with _lock:
        datos = _leer()
        datos.setdefault("en_vuelo", {})[numeracion] = {
            "tipo": tipo, "desde": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _guardar(datos)


def olvidar_en_vuelo(numeracion: str):
    with _lock:
        datos = _leer()
        if datos.get("en_vuelo", {}).pop(numeracion, None) is not None:
            _guardar(datos)


def en_vuelo() -> dict:
    """{numeracion: tipo} de todo lo que quedó a mitad de un POST en la corrida
    anterior — se resuelven con un GET antes de seguir con el ciclo normal."""
    return {n: e.get("tipo", "") for n, e in _leer().get("en_vuelo", {}).items()}


# --- rechazos: bloqueo tras agotar los reintentos ---------------------------

def contar_rechazo(numeracion: str, motivo: str) -> int:
    with _lock:
        datos = _leer()
        registro = datos.get("rechazos", {}).get(numeracion) or {}
        intentos = int(registro.get("intentos", 0)) + 1
        datos.setdefault("rechazos", {})[numeracion] = {
            "intentos": intentos,
            "ultimo": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "motivo": motivo,
        }
        _guardar(datos)
        return intentos


def bloqueado(numeracion: str) -> bool:
    registro = _leer().get("rechazos", {}).get(numeracion) or {}
    return int(registro.get("intentos", 0)) >= MAX_REINTENTOS_RECHAZO


def limpiar_rechazo(numeracion: str):
    """El comprobante salió aceptado (o alguien corrigió el dato y se decidió
    reintentar a mano): su historial de rechazos deja de importar."""
    with _lock:
        datos = _leer()
        if datos.get("rechazos", {}).pop(numeracion, None) is not None:
            _guardar(datos)


def bloqueados() -> dict:
    """{numeracion: motivo} de los que agotaron MAX_REINTENTOS_RECHAZO — para
    reportarlos cada ciclo sin volver a intentarlos."""
    return {
        n: e.get("motivo", "") for n, e in _leer().get("rechazos", {}).items()
        if int(e.get("intentos", 0)) >= MAX_REINTENTOS_RECHAZO
    }


# --- backoff del ciclo entero: varias corridas seguidas sin poder avanzar ---
# --- Un comprobante puntual en "error"/502/timeout ya NO tiene backoff -------
# --- propio: eso lo reintenta la API sola. Esto es para cuando el daemon no -
# --- puede ni llegar a facturador-api (sin internet, servicio caído). -------

def contar_fallo_ciclo() -> int:
    """Suma una corrida que terminó sin poder procesar nada por fallas de
    infraestructura repetidas, y devuelve cuántas lleva seguidas — ver
    aplicacion/ciclo_generacion.py, que usa esto para pausar el hilo con una
    espera creciente en vez de reintentar cada INTERVALO_GENERACION_SEG como si
    nada pasara."""
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


# --- conciliación de resúmenes diarios: ahora los arma y resuelve la propia -
# --- API sola (spec §3); acá solo se recuerda qué resumen RECHAZADO ya se ---
# --- reencoló, para no repetirlo cada vez que /resumenes lo vuelva a listar -
# --- (trae el historial completo, no solo lo pendiente). --------------------

def resumen_rechazo_procesado(numeracion_rc: str) -> bool:
    return numeracion_rc in _leer().get("resumenes_rechazo_procesados", [])


def marcar_resumen_rechazo_procesado(numeracion_rc: str):
    with _lock:
        datos = _leer()
        procesados = set(datos.get("resumenes_rechazo_procesados", []))
        procesados.add(numeracion_rc)
        datos["resumenes_rechazo_procesados"] = sorted(procesados)
        _guardar(datos)


# --- comprobantes aceptados por SUNAT pero sin CDR todavía (estado enviado/ -
# --- validando/pendiente): se reconsultan con GET en cada ciclo hasta que --
# --- el CDR aparezca. No son "en vuelo" (esos son llamadas sin respuesta --
# --- clara) ni "rechazos": acá SUNAT ya confirmó que lo tiene. -------------

def agregar_esperando_cdr(numeracion: str, tipo: str):
    with _lock:
        datos = _leer()
        datos.setdefault("esperando_cdr", {})[numeracion] = {"tipo": tipo}
        _guardar(datos)


def esperando_cdr() -> dict:
    """{numeracion: tipo} de los comprobantes aceptados por SUNAT a la espera
    de que aparezca su CDR."""
    return {n: e.get("tipo", "") for n, e in _leer().get("esperando_cdr", {}).items()}


def quitar_esperando_cdr(numeracion: str):
    with _lock:
        datos = _leer()
        if datos.get("esperando_cdr", {}).pop(numeracion, None) is not None:
            _guardar(datos)
