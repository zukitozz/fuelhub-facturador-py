"""
Los hilos del daemon: el Generador (ciclo_generacion en loop, habla con
facturador-api), Cierres, PDF y Reporte (los tres hablan con FuelHub core).
"""
import logging
import time

from config import (
    INTERVALO_GENERACION_SEG, INTERVALO_CIERRES_SEG,
    INTERVALO_PDF_SEG, INTERVALO_REPORTE_FACTURAS_SEG,
)
from aplicacion.ciclo_generacion import ciclo_generacion
from aplicacion.ciclo_cierres import ciclo_cierres
from aplicacion.ciclo_pdf import ciclo_pdf
from aplicacion.ciclo_reporte_facturas import ciclo_reporte_facturas

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


def hilo_pdf():
    logger.info("Hilo PDF iniciado (intervalo: %ds)", INTERVALO_PDF_SEG)
    while True:
        ciclo_pdf()
        time.sleep(INTERVALO_PDF_SEG)


def hilo_reporte_facturas():
    logger.info("Hilo REPORTE iniciado (intervalo: %ds)", INTERVALO_REPORTE_FACTURAS_SEG)
    while True:
        ciclo_reporte_facturas()
        time.sleep(INTERVALO_REPORTE_FACTURAS_SEG)
