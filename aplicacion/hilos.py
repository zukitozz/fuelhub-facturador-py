"""
Los hilos del daemon: el Generador (ciclo_generacion en loop), el CDR (reacciona
al instante cuando llega un ZIP a RPTA, con un barrido periódico como red de
seguridad) y Cierres (envía a FuelHub core los cierres de turno/día pendientes).
"""
import logging
import os
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from config import (
    INTERVALO_GENERACION_SEG, INTERVALO_BARRIDO_RPTA_SEG, INTERVALO_CIERRES_SEG,
    SFS_RPTA_DIR, DIR_PROCESADOS, DIR_ERRORES,
)
from aplicacion.ciclo_generacion import ciclo_generacion
from aplicacion.ciclo_cdr import procesar_respuestas
from aplicacion.ciclo_cierres import ciclo_cierres

logger = logging.getLogger(__name__)


def hilo_generador():
    logger.info("Hilo GENERADOR iniciado (intervalo: %ds)", INTERVALO_GENERACION_SEG)
    while True:
        ciclo_generacion()
        time.sleep(INTERVALO_GENERACION_SEG)


def hilo_cierres():
    logger.info("Hilo CIERRES iniciado (intervalo: %ds)", INTERVALO_CIERRES_SEG)
    while True:
        ciclo_cierres()
        time.sleep(INTERVALO_CIERRES_SEG)


class CDRHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        if event.src_path.lower().endswith((".zip", ".xml")):
            logger.info("CDR detectado: %s", os.path.basename(event.src_path))
            procesar_respuestas()


def hilo_cdr():
    logger.info("Hilo CDR iniciado — monitoreando: %s", SFS_RPTA_DIR)
    os.makedirs(SFS_RPTA_DIR, exist_ok=True)
    os.makedirs(DIR_PROCESADOS, exist_ok=True)
    os.makedirs(DIR_ERRORES,    exist_ok=True)

    handler  = CDRHandler()
    observer = Observer()
    observer.schedule(handler, path=SFS_RPTA_DIR, recursive=False)
    observer.start()

    # Sin try/except KeyboardInterrupt: Python solo lo entrega al hilo principal.
    # El barrido periódico es la red de seguridad: recoge los CDR que llegaron a
    # medio escribir y los que watchdog no reportó (copias por red, reinicios).
    # Si no hay archivos nuevos, procesar_respuestas() sale de inmediato.
    while True:
        time.sleep(INTERVALO_BARRIDO_RPTA_SEG)
        procesar_respuestas()
