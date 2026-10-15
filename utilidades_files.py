"""
Escritura de archivos de estado (envio_api.json, reporte_facturas.json): escritura
atómica genérica, sin saber nada de quién la usa.
"""
import os


def escribir_archivo(ruta: str, contenido: str):
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(contenido)
    os.replace(tmp, ruta)
