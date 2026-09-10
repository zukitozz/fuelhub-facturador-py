"""
Lectura y escritura de archivos, genérica: no sabe qué es DATA, RPTA ni un CDR —
recibe siempre la ruta como parámetro. Lo que sí conoce esas carpetas (por qué se
borra, cuándo se mueve a procesados/errores) se queda en main.py; acá solo el
mecanismo de bajo nivel.
"""
import logging
import os

from config import MINUTOS_CDR_VACIO
import time

logger = logging.getLogger(__name__)


def escribir_archivo(ruta: str, contenido: str):
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(contenido)
    os.replace(tmp, ruta)


def _borrar_si_existe(ruta: str):
    """
    Borra un archivo que puede no estar. Se usa al regenerar un comprobante: el SFS
    levanta todo lo que encuentre en DATA, así que un archivo sobrante de una
    emisión anterior se colaría en la nueva.
    """
    try:
        os.remove(ruta)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("No se pudo borrar %s", ruta)


def _mover(ruta: str, carpeta: str):
    os.makedirs(carpeta, exist_ok=True)
    try:
        os.replace(ruta, os.path.join(carpeta, os.path.basename(ruta)))
    except Exception:
        logger.exception("No se pudo mover %s a %s", ruta, carpeta)


def _archivo_estable(ruta: str, intentos: int = 5, espera: float = 0.5) -> bool:
    """
    True cuando el tamaño del archivo dejó de cambiar. SUNAT/SFS deja el ZIP en RPTA
    mientras todavía lo escribe y watchdog avisa apenas se crea: abrirlo de inmediato
    daba BadZipFile —y lo mandaba a errores/— sobre un archivo que estaba sano.

    Un archivo que se queda en 0 bytes nunca se da por estable, y eso es correcto: no
    hay nada que abrir. Distinguir ese caso de uno que todavía crece es tarea de quien
    llama (ver _archivo_abandonado), porque acá no se puede saber cuánto lleva así.
    """
    ultimo = -1
    for _ in range(intentos):
        try:
            actual = os.path.getsize(ruta)
        except OSError:
            return False
        if actual > 0 and actual == ultimo:
            return True
        ultimo = actual
        time.sleep(espera)
    return False


def _archivo_abandonado(ruta: str) -> bool:
    """
    True si el archivo lleva demasiado tiempo vacío como para seguir esperándolo.

    Un ZIP que se corta a medio escribir —un corte del lado del SFS, disco lleno—
    queda en 0 bytes para siempre. _archivo_estable() nunca lo da por bueno, asi que
    el barrido lo saltaba en cada ciclo con el mismo INFO de "aún se está escribiendo"
    sin que nadie lo resolviera. Visto en produccion el 2026-09-05: horas repitiendo
    esa linea.

    Y no era solo ruido en el log: _tiene_cdr() solo mira que el archivo exista, asi
    que ese ZIP vacio hacia que recuperar_cdr_pendientes() diera por recuperado el CDR
    y no volviera a consultarle a SUNAT. El comprobante quedaba en enviado=0 aunque
    SUNAT lo hubiera aceptado, sin ninguna via de salida.

    El umbral de tiempo es lo que separa un archivo abandonado de uno que recien
    empieza: los dos miden 0 bytes, y la unica diferencia es hace cuanto.
    """
    try:
        if os.path.getsize(ruta) > 0:
            return False
        edad = time.time() - os.path.getmtime(ruta)
    except OSError:
        return False
    return edad > MINUTOS_CDR_VACIO * 60
