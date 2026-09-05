"""Parseo de fechas de BD. La corrección de zona horaria vive en main.py
(fecha_local/detectar_desfase_bd): necesita leer el reloj de la BD, que es I/O."""
from datetime import datetime


def formatear_fecha_hora(fecha_raw) -> datetime:
    """
    La fecha tal como está guardada, sin mover la hora. Para lo que se le declara a
    SUNAT hay que pasarla antes por fecha_local(): lo que hay en la BD está en UTC.
    """
    if isinstance(fecha_raw, datetime):
        return fecha_raw
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(fecha_raw).strip(), fmt)
        except (ValueError, AttributeError):
            pass
    raise ValueError(f"Fecha inválida: {fecha_raw!r}")
