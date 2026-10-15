"""Validación de un comprobante individual (factura/boleta/nota) antes de
mandarlo a facturador-api."""
from .texto import _texto
from .fechas import formatear_fecha_hora


def _validar_campos_obligatorios(comp: dict) -> list:
    """
    Campos sin los que no se puede armar el payload de facturador-api ni una
    línea del resumen diario. Solo devuelve qué falta —no decide qué hacer con
    eso—, para que sirva tanto a la generación individual como a cualquier otro
    camino que necesite esta misma validación.

    No repite lo que ya filtra la consulta SQL (numeracionComprobante IS NOT
    NULL); igual se valida acá porque es la única garantía si algún día una
    fila llega por otro camino, y porque una fecha ilegible pasaba antes como
    una excepción genérica sin motivo claro en el log.
    """
    faltantes = []
    if not _texto(comp.get("numeracion_comprobante")):
        faltantes.append("numeracion_comprobante")
    try:
        formatear_fecha_hora(comp.get("fecha_emision"))
    except (ValueError, TypeError):
        faltantes.append("fecha_emision")
    if comp.get("total") is None:
        faltantes.append("total")
    return faltantes
