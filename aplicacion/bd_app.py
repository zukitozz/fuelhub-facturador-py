"""
Acceso a la BD de la aplicación (PostgreSQL/SQL Server, ver repositorio/). El
daemon no sabe con qué motor está hablando: pide siempre lo mismo y repositorio.elegir()
devuelve el adaptador que corresponde a DATABASE_URL.
"""
import logging

import repositorio
from config import DATABASE_URL, DB_TIMEOUT_SEG

logger = logging.getLogger(__name__)


def _url_sin_clave(url: str) -> str:
    """La URL de conexión sin la contraseña, para poder mostrarla en el log."""
    return repositorio.url_sin_clave(url)


def _bd():
    """
    El adaptador del motor que indique DATABASE_URL.

    El daemon no sabe con qué base está hablando: pide siempre lo mismo y cada
    adaptador traduce a su esquema y su dialecto (ver repositorio/).
    """
    if not DATABASE_URL:
        raise RuntimeError("Falta DATABASE_URL en el .env")
    return repositorio.elegir(DATABASE_URL)


def conectar_bd():
    return _bd().conectar(DATABASE_URL, DB_TIMEOUT_SEG)


def _escribir_bd(operacion, *args) -> int:
    """
    Ejecuta una escritura del adaptador. Devuelve las filas afectadas, o -1 si falló.

    El try va acá y no en cada adaptador para que un motor nuevo no tenga que
    acordarse de replicar el manejo de errores.
    """
    try:
        return operacion(*args)
    except Exception:
        logger.exception("Error escribiendo en la BD (%s)", getattr(operacion, "__name__", "?"))
        return -1
