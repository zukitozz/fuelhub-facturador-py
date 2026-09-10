"""
Persistencia propia del daemon para los reintentos: cuántas veces se reenvió cada
comprobante rechazado, y el backoff de un corte de red (reintentos.json).
"""
import json
import logging
import threading
import time
from datetime import datetime

from config import _REINTENTOS_PATH, _ESPERA_MAX_RED_MIN, MAX_CONSULTAS_FALLIDAS
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
    # SAAJ es la capa SOAP de Java del SFS. "Problem writing SAAJ model to stream:
    # e-factura.sunat.gob.pe" es que no pudo ni escribir la solicitud: el envio nunca
    # salio. Confirmado en produccion (corte del 2026-09-10), donde dejo 6 resumenes
    # sin salida reteniendo 1195 boletas.
    #
    # Dice "writing" a proposito y no "saaj model" a secas: SAAJ tambien lanza un
    # "Problem READING SAAJ model from stream", y ese es el caso opuesto --la solicitud
    # SI salio y lo que fallo fue leer la respuesta--. Ahi el envio pudo haber llegado a
    # SUNAT, y tratarlo como corte de red autorizaria a reenviarlo: exactamente el
    # duplicado que el resto de este archivo se esfuerza en evitar.
    "problem writing saaj model",
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


def _contar_consulta_fallida(tipo: str, numeracion: str, codigo: str, mensaje: str) -> int:
    """
    Suma una consulta sin respuesta útil y devuelve cuántas seguidas lleva.

    Va en reintentos.json, bajo su propia clave, por el mismo motivo que el resto del
    archivo: PM2 reinicia el daemon solo, y un contador en memoria volvería a cero en
    cada reinicio —justo cuando mas importa saber que esto lleva horas—. La clave
    incluye el tipo porque una consulta se hace por (tipo, numeracion), a diferencia
    del contador de reenvios, que se lleva solo por numeracion.
    """
    clave = f"consulta:{tipo}-{numeracion}"
    with _lock_reintentos:
        datos = _leer_reintentos()
        registro = datos.get(clave) or {}
        veces = int(registro.get("consultas", 0)) + 1
        datos[clave] = {
            "tipo": tipo,
            "consultas": veces,
            "ultimo": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "codigo": codigo,
            "motivo": mensaje or registro.get("motivo", ""),
        }
        _guardar_reintentos(datos)
        return veces


def _olvidar_consulta_fallida(tipo: str, numeracion: str):
    """SUNAT respondió algo concluyente: la racha de consultas fallidas ya no importa."""
    clave = f"consulta:{tipo}-{numeracion}"
    with _lock_reintentos:
        datos = _leer_reintentos()
        if datos.pop(clave, None) is not None:
            _guardar_reintentos(datos)


def _horas_en_proceso(numeracion: str) -> float:
    """
    Horas que lleva un ticket contestando "todavía lo estoy procesando".

    Se anota la primera vez y de ahí se mide. Va en reintentos.json y no en memoria
    por el mismo motivo que el resto del archivo: PM2 reinicia el daemon solo, y un
    contador en memoria arrancaría de cero en cada reinicio —justo cuando lo que hace
    falta saber es que esto lleva horas—.

    La cuenta arranca al primer "en proceso" y no cuando se genero el resumen: lo que
    interesa es hace cuanto que SUNAT viene diciendo lo mismo, no cuanto hace que
    existe el documento.
    """
    clave = f"proceso:{numeracion}"
    ahora = datetime.now()
    with _lock_reintentos:
        datos = _leer_reintentos()
        registro = datos.get(clave) or {}
        desde = registro.get("desde")
        if not desde:
            datos[clave] = {"desde": ahora.strftime("%Y-%m-%d %H:%M:%S")}
            _guardar_reintentos(datos)
            return 0.0
    try:
        return (ahora - datetime.strptime(desde, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
    except ValueError:
        return 0.0


def _olvidar_en_proceso(numeracion: str):
    """El ticket dejó de estar en proceso: la cuenta de horas ya no importa."""
    clave = f"proceso:{numeracion}"
    with _lock_reintentos:
        datos = _leer_reintentos()
        if datos.pop(clave, None) is not None:
            _guardar_reintentos(datos)
