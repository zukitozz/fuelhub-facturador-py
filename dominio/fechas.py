"""Parseo de fechas de BD."""
from datetime import datetime


def formatear_fecha_hora(fecha_raw) -> datetime:
    """
    La fecha tal como está guardada, sin mover la hora ni la fecha: es la que
    declaró quien emitió (fecha de negocio), no un instante que haya que
    convertir de huso horario. Hubo una versión anterior que le restaba horas
    asumiendo que la BD guardaba en UTC — eso corría la fecha un día para atrás
    cuando la columna de origen era solo fecha (sin hora real), y además
    rompía las facturas retroactivas (fecha de negocio anterior a hoy): se
    sacó por completo, ver historial de aplicacion/ciclo_generacion.py.
    """
    if isinstance(fecha_raw, datetime):
        return fecha_raw
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(fecha_raw).strip(), fmt)
        except (ValueError, AttributeError):
            pass
    raise ValueError(f"Fecha inválida: {fecha_raw!r}")
