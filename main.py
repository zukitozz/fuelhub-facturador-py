"""
main.py — Punto de entrada del daemon de Facturación Electrónica SUNAT (SFS v2.1)
Gestionar con PM2: pm2 start sfs.config.js --only facturador

Arranca los hilos del daemon. Qué hace cada uno vive en aplicacion/hilos.py; acá
solo se decide cuáles corren y en qué orden. Es el lugar para sumar un hilo nuevo.
"""
import logging
import threading
import time

from config import SFS_DATA_DIR, SFS_RPTA_DIR, DATABASE_URL
from aplicacion.bd_app import _url_sin_clave
from aplicacion.ciclo_cdr import procesar_respuestas
from aplicacion.hilos import hilo_generador, hilo_cdr, hilo_cierres, hilo_pdf

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  FACTURADOR SUNAT - SFS v2.1")
    logger.info("  SFS DATA : %s", SFS_DATA_DIR)
    logger.info("  SFS RPTA : %s", SFS_RPTA_DIR)
    logger.info("  BASE DE DATOS: %s", _url_sin_clave(DATABASE_URL))
    logger.info("=" * 60)

    # Barrido inicial: recoge lo que llegó a RPTA mientras el daemon estaba caído.
    procesar_respuestas()

    hilos = [
        threading.Thread(target=hilo_generador, name="Generador", daemon=True),
        threading.Thread(target=hilo_cdr,       name="CDR",       daemon=True),
        threading.Thread(target=hilo_cierres,   name="Cierres",   daemon=True),
        threading.Thread(target=hilo_pdf,       name="PDF",       daemon=True),
    ]
    for t in hilos:
        t.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Deteniendo...")
