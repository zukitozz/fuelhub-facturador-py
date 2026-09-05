"""
Lectura y escritura de archivos, genérica: no sabe qué es DATA, RPTA ni un CDR —
recibe siempre la ruta como parámetro. Lo que sí conoce esas carpetas (por qué se
borra, cuándo se mueve a procesados/errores) se queda en main.py; acá solo el
mecanismo de bajo nivel.
"""
import logging
import os
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
