"""Validación y armado de líneas de un comprobante individual (factura/boleta/nota)."""
import re

from .texto import _texto, _campo_pipe
from .montos import formatear_decimal
from .fechas import formatear_fecha_hora

# Todos los parsers del SFS exigen 36 columnas en el detalle. Ojo con la nota de
# débito: su mensaje de error dice "(30 columnas)", pero su bytecode compara contra
# 36 igual que el resto. Guiarse por ese texto hace que el SFS rechace el archivo
# con un mensaje que apunta justo al número equivocado.
_COLS_DET = 36


def _nombre_base(ruc: str, tipo: str, num: str) -> str:
    serie, corr = num.split("-", 1) if "-" in num else ("0000", num or "00000000")
    nombre = f"{ruc}-{tipo}-{serie}-{corr}"
    return re.sub(r'[<>:"/\\|?*\n\r\t]+', "_", nombre.strip())[:250]


def _validar_campos_obligatorios(comp: dict) -> list:
    """
    Campos sin los que no se puede armar un comprobante ni una línea del resumen
    diario. Solo devuelve qué falta —no decide qué hacer con eso—, para que sirva
    tanto a procesar_comprobante() como a obtener_boletas_para_resumen(): cada
    camino de emisión define si bloquea del todo o solo excluye esa fila.

    No repite lo que ya filtra la consulta SQL (numeracionComprobante IS NOT NULL);
    igual se valida acá porque es la única garantía si algún día una fila llega por
    otro camino, y porque una fecha ilegible pasaba hoy como una excepción genérica
    sin motivo claro en el log.
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


def _linea_detalle(item: dict) -> str:
    """Una línea del archivo .det: las 36 columnas en el orden que lee el SFS."""
    cant   = formatear_decimal(item.get("dec_cantidad") or item.get("cantidad_venta") or item.get("cantidad", 1))
    # Con 6 decimales, no 2: es lo que hace cuadrar cantidad × valor unitario
    # contra el valor de venta, que es lo que SUNAT verifica.
    v_unit = formatear_decimal(item.get("valor"), 6)
    v_vta  = formatear_decimal(item.get("valor_venta"))
    igv_it = formatear_decimal(item.get("igv_venta"))
    p_unit = formatear_decimal(item.get("precio"))

    campos = [
        _campo_pipe(item.get("medida"), "NIU"),
        f"{cant:.2f}",
        _campo_pipe(item.get("codigo_producto"), "-"),
        "-",
        _campo_pipe(item.get("descripcion"), "ITEM"),
        f"{v_unit:.6f}",
        f"{igv_it:.2f}", "1000", f"{igv_it:.2f}", f"{v_vta:.2f}", "IGV", "VAT", "10", "18.00",
    ] + ["-"] * 19 + [
        f"{p_unit:.2f}", f"{v_vta:.2f}", "0.00",
    ]
    return "|".join(campos[:_COLS_DET]) + "|\n"
