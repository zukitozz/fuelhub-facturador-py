"""
main.py — Punto de entrada del daemon.
Gestionar con PM2: ver aplicacion/hilos.py para qué hace cada hilo.

Arranca los hilos del daemon. Qué hace cada uno vive en aplicacion/hilos.py; acá
solo se decide cuáles corren y en qué orden. Es el lugar para sumar un hilo nuevo.
"""
import logging
import threading
import time

from config import DATABASE_URL, FACTURADOR_API_URL
from aplicacion.bd_app import _url_sin_clave
from aplicacion.hilos import hilo_generador, hilo_cierres, hilo_pdf, hilo_reporte_facturas

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  FACTURADOR")
    logger.info("  FACTURADOR-API: %s", FACTURADOR_API_URL)
    logger.info("  BASE DE DATOS: %s", _url_sin_clave(DATABASE_URL))
    logger.info("=" * 60)

    hilos = [
        threading.Thread(target=hilo_generador,        name="Generador", daemon=True),
        threading.Thread(target=hilo_cierres,           name="Cierres",   daemon=True),
        threading.Thread(target=hilo_pdf,               name="PDF",       daemon=True),
        threading.Thread(target=hilo_reporte_facturas,  name="Reporte",   daemon=True),
    ]
    for t in hilos:
        t.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Deteniendo...")
