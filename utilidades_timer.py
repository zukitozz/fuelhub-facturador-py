"""
Reloj de la BD: mide el desfase horario contra el servidor y convierte una fecha de
BD a hora local. No es dominio puro —detectar_desfase_bd() consulta la BD de la
app— por eso no vive en dominio/, pero tampoco es un puerto propio: es un cálculo
de infraestructura que usa aplicacion/ en varios lugares (ver fecha_local, más abajo).
"""
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from dominio.fechas import formatear_fecha_hora
from aplicacion.bd_app import _bd

logger = logging.getLogger(__name__)

# La aplicación guarda sus fechas con el reloj de su servidor de BD, que hoy corre
# en UTC; SUNAT en cambio espera la fecha de emisión en hora local del emisor. Sin
# corregir eso, toda venta hecha entre las 19:00 y la medianoche cae en el día
# siguiente y se le declararía a SUNAT una fecha futura, que rechaza.
#
# El desfase se MIDE contra la propia BD en vez de fijarlo, porque no es una
# decisión de este daemon: si alguien cambia la zona horaria del servidor a hora de
# Lima, un -5 fijo quedaría al revés del problema y correría las fechas para el otro
# lado sin que nadie se entere. Medirlo se autocorrige solo.
# Se puede forzar un valor con DESFASE_BD_HORAS (en horas) si hiciera falta.
DESFASE_BD_HORAS = os.getenv("DESFASE_BD_HORAS", "auto").strip().lower()
# Hasta la primera medición se asume lo que hay hoy; la medición ocurre al inicio de
# cada ciclo, antes de que se genere ningún comprobante.
_desfase_horas: float = -5.0
_desfase_medido = False
_lock_desfase = threading.Lock()

if DESFASE_BD_HORAS != "auto":
    try:
        _desfase_horas = float(DESFASE_BD_HORAS)
    except ValueError:
        # Un valor mal escrito no puede pasar por bueno en silencio: sería declarar
        # fechas corridas a SUNAT. Se avisa y se sigue midiendo.
        DESFASE_BD_HORAS = "auto"


def detectar_desfase_bd(conn) -> float:
    """
    Mide cuánto adelanta el reloj de la BD respecto de la hora local y lo recuerda.

    Es lo que hace que el daemon siga funcionando si alguien cambia la zona horaria
    del servidor: con la BD en UTC da -5, y si pasa a hora de Lima da 0, sin tocar
    nada acá. Se redondea a media hora porque ninguna zona horaria usa una
    granularidad menor, y así un par de segundos de latencia no ensucian el valor.
    """
    global _desfase_horas
    if DESFASE_BD_HORAS != "auto":
        return _desfase_horas
    try:
        filas = _bd().reloj(conn)
        # Se compara la lectura "de pared" del servidor contra el reloj local.
        pared = filas[0]["con_zona"].replace(tzinfo=None)
        crudo = (pared - datetime.now()).total_seconds() / 3600
        medido = round(crudo * 2) / 2
    except Exception:
        logger.exception("No se pudo medir el desfase horario de la BD; se conserva %+g h.",
                         _desfase_horas)
        return _desfase_horas

    global _desfase_medido
    correccion = -medido
    with _lock_desfase:
        if correccion != _desfase_horas or not _desfase_medido:
            logger.info(
                "El reloj de la BD adelanta %+g h respecto de la hora local; las fechas "
                "de emisión se corrigen en %+g h antes de declararlas a SUNAT.",
                medido, correccion,
            )
            _desfase_horas = correccion
            _desfase_medido = True
    return _desfase_horas


def fecha_local(fecha_raw) -> datetime:
    """
    Fecha de la BD llevada a la hora local del emisor.

    Es la única forma válida de leer una fecha de emisión: es la que va al
    comprobante y la que decide a qué día pertenece una boleta para el resumen
    diario. Ver detectar_desfase_bd().
    """
    fecha = formatear_fecha_hora(fecha_raw)
    if fecha.tzinfo is not None:
        # Si alguna vez llega con zona horaria explícita, se convierte de verdad en
        # vez de sumarle el desplazamiento a ciegas.
        return fecha.astimezone(timezone(timedelta(hours=_desfase_horas))).replace(tzinfo=None)
    return fecha + timedelta(hours=_desfase_horas)
