"""
Puerto+adaptador del estado propio del daemon en disco: el correlativo y las
boletas de cada resumen diario (resumenes.json) y el conteo de reintentos de cada
comprobante rechazado (reintentos.json). Vive en disco y no en memoria porque PM2
reinicia el proceso solo, y ese estado tiene que sobrevivir al reinicio.
"""
