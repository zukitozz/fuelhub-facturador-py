"""
Persistencia propia del daemon para los reintentos: cuántas veces se reenvió cada
comprobante rechazado, y el backoff de un corte de red (reintentos.json).
"""
import json
import logging
import threading
import time
from datetime import datetime

from config import _REINTENTOS_PATH, _ESPERA_MAX_RED_MIN
from dominio.texto import _texto
from utilidades_files import escribir_archivo

logger = logging.getLogger(__name__)

_lock_reintentos = threading.Lock()

# Frases que solo aparecen cuando el envío ni siquiera llegó a SUNAT. La lista es a
# propósito corta y literal: ante la menor duda conviene gastar un reintento y que
# el comprobante termine bloqueado —alguien lo mira— antes que reencolarlo para
# siempre por un error que en realidad era de datos.
#
# El "Could not send Message" es el confirmado en produccion (corte del 2026-08-29):
# el SFS no pudo ni abrir la conversación con SUNAT. Los demás son las variantes de
# red que devuelve la misma capa.
#
# OJO con lo que NO va acá: el "0111 - No tiene el perfil para enviar comprobantes
# electronicos" también aterriza en '06', pero es una respuesta de SUNAT, no una
# falla de red. Tiene que gastar reintentos y terminar bloqueado, porque no se
# arregla esperando.
_SENALES_DE_RED = (
    "could not send message",
    "connection timed out",
    "connect timed out",
    "read timed out",
    "connection refused",
    "connection reset",
    "unknownhostexception",
    "sockettimeoutexception",
    "socketexception",
    "no route to host",
    "network is unreachable",
)


def _leer_reintentos() -> dict:
    try:
        with open(_REINTENTOS_PATH, encoding="utf-8") as fh:
            datos = json.load(fh)
        return datos if isinstance(datos, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # Un archivo corrupto no puede frenar la emisión: se empieza de cero y se
        # avisa. El costo es volver a contar desde 1 para los rechazados vigentes.
        logger.exception("No se pudo leer %s; se reinicia el conteo de reintentos.", _REINTENTOS_PATH)
        return {}


def _guardar_reintentos(datos: dict):
    try:
        escribir_archivo(_REINTENTOS_PATH, json.dumps(datos, ensure_ascii=False, indent=2))
    except OSError:
        logger.exception("No se pudo guardar %s; el conteo de reintentos no persiste.", _REINTENTOS_PATH)


def _reintentos_de(numeracion: str) -> int:
    """Cuántos reenvíos lleva el comprobante, sin tocar el contador."""
    with _lock_reintentos:
        registro = _leer_reintentos().get(numeracion) or {}
    try:
        return int(registro.get("intentos", 0))
    except (TypeError, ValueError):
        return 0


def _contar_reintento(numeracion: str, tipo: str, motivo: str = "") -> int:
    """
    Suma un reenvío al comprobante y devuelve cuántos lleva. La clave es la
    numeración, igual que en aplicacion/ciclo_cdr.py:_actualizar_sql_cdr(), para
    que el contador se limpie solo cuando llegue el CDR de aceptación.
    """
    with _lock_reintentos:
        datos = _leer_reintentos()
        registro = datos.get(numeracion) or {}
        intentos = int(registro.get("intentos", 0)) + 1
        datos[numeracion] = {
            "tipo": tipo,
            "intentos": intentos,
            "ultimo": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "motivo": motivo or registro.get("motivo", ""),
        }
        _guardar_reintentos(datos)
        return intentos


def _limpiar_reintento(numeracion: str):
    """El comprobante salió aceptado: su historial de rechazos deja de importar."""
    with _lock_reintentos:
        datos = _leer_reintentos()
        if datos.pop(numeracion, None) is not None:
            _guardar_reintentos(datos)


def _limpiar_reintentos(numeraciones: list):
    """
    Igual que _limpiar_reintento() pero en lote: una sola lectura/escritura de
    reintentos.json para todo un resumen, en vez de una por boleta.
    """
    if not numeraciones:
        return
    with _lock_reintentos:
        datos = _leer_reintentos()
        tocado = False
        for num in numeraciones:
            if datos.pop(num, None) is not None:
                tocado = True
        if tocado:
            _guardar_reintentos(datos)


def _es_falla_de_red(motivo: str) -> bool:
    """
    True si el motivo del '06' es inequívocamente de comunicación.

    Separa las dos cosas que el SFS mete en el mismo estado: un dato mal armado
    —que reintentar no arregla— y un corte de red, donde el comprobante nunca salió
    y el mismo envío funciona apenas vuelve el servicio.
    """
    return any(s in _texto(motivo).lower() for s in _SENALES_DE_RED)


def _espera_de(numeracion: str) -> float:
    """Marca de tiempo (epoch) hasta la que este comprobante no se reintenta."""
    with _lock_reintentos:
        registro = _leer_reintentos().get(numeracion) or {}
    try:
        return float(registro.get("esperar_hasta", 0))
    except (TypeError, ValueError):
        return 0.0


def _anotar_espera_de_red(numeracion: str, tipo: str, motivo: str) -> tuple:
    """
    Registra un intento fallido por red y devuelve (cortes, minutos de espera).

    El contador va en 'cortes' y no en 'intentos' a propósito: 'intentos' es el
    presupuesto que agota un comprobante y lo bloquea, y una falla de red no debe
    gastarlo. Acá solo sirve para espaciar los reintentos.

    La espera se guarda en disco y no en memoria por el mismo motivo que el
    contador: PM2 reinicia el daemon solo, y un backoff en memoria volvería a cero
    en cada reinicio, martillando a SUNAT durante un corte largo.
    """
    with _lock_reintentos:
        datos = _leer_reintentos()
        registro = datos.get(numeracion) or {}
        cortes = int(registro.get("cortes", 0)) + 1
        # 1, 2, 4, 8, 15, 15... minutos. Arranca cerca del ciclo normal para que un
        # corte de segundos no demore el comprobante, y se aplana en 15 para no
        # dejarlo esperando media hora cuando el servicio ya volvió.
        minutos = min(2 ** (cortes - 1), _ESPERA_MAX_RED_MIN)
        registro.update({
            "tipo": tipo,
            "cortes": cortes,
            "ultimo": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "motivo": motivo or registro.get("motivo", ""),
            "esperar_hasta": time.time() + minutos * 60,
        })
        datos[numeracion] = registro
        _guardar_reintentos(datos)
        return cortes, minutos
