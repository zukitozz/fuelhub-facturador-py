"""Líneas del resumen diario de boletas (.RDI/.TRD). Ver README.md, 'Resumen diario de boletas'."""
from .texto import _campo_pipe
from .montos import formatear_decimal

# PipeResumenBoletaParser del SFS: el .RDI no es una cabecera única sino una línea
# por boleta con este mismo layout de 23 columnas; el .TRD es el desglose de
# tributos de cada línea, 6 columnas, vinculado por posición (idLineaRd = número de
# fila dentro del .RDI, 1-based). Confirmado decompilando el parser, mismo método
# que para notas y ND.
_COLS_RDI = 23
_COLS_TRD = 6


def _linea_rdi(fecha_emision: str, fecha_resumen: str, boleta: dict, receptor: dict) -> str:
    """
    Una línea del .RDI: PipeResumenBoletaParser no lee una cabecera única sino una
    línea por boleta con este mismo layout de 23 columnas.

    Dos campos que parecen intercambiables y no lo son (verificado en
    ConvertirRBoletasXML.ftl, que es lo que arma el XML final):
      - tipDocResumen -> <cbc:DocumentTypeCode>: el TIPO de comprobante, "03" para
        una boleta. Poner "1" acá lo rechaza SUNAT con el error 2241.
      - tipEstado     -> <cbc:ConditionCode>: el estado de la línea, "1" = nueva.

    Los bloques de documento modificado y de percepción son opcionales, y la
    plantilla los emite con `<#if serDocModifico != "">` / `<#if tipRegPercepcion
    != "">`: la condición es contra CADENA VACÍA, no contra "-". Un "-" ahí los
    daría por presentes y armaría un XML con esos nodos rellenos de basura, así que
    esos 8 campos van vacíos. Es lo contrario de lo que hace el resto de los
    archivos del daemon, donde "-" es el relleno habitual.
    """
    tipo_doc_rec = _campo_pipe(receptor.get("tipo_documento"), "0")
    num_doc_rec  = _campo_pipe(receptor.get("numero_documento"), "00000000")
    grav  = formatear_decimal(boleta["gravadas"])
    total = formatear_decimal(boleta["total"])
    campos = [
        fecha_emision, fecha_resumen, "03", boleta["numeracion_comprobante"],
        tipo_doc_rec, num_doc_rec, "PEN",
        f"{grav:.2f}", "0.00", "0.00", "0.00", "0.00", "0.00", f"{total:.2f}",
        "", "", "", "",
        "", "", "", "",
        "1",
    ]
    # Una columna de más o de menos hace que el SFS rechace el archivo entero con
    # un mensaje que no dice cuál falta; mejor que salte acá.
    if len(campos) != _COLS_RDI:
        raise ValueError(f".RDI: {len(campos)} columnas, se esperan {_COLS_RDI}")
    return "|".join(campos) + "|\n"


def _linea_trd(id_linea: int, boleta: dict) -> str:
    """
    Desglose de tributos de una línea del .RDI: 6 columnas, mismo patrón que el .tri
    de un comprobante individual. id_linea es la posición (1-based) de la boleta
    dentro del .RDI: es lo único que vincula ambos archivos, porque el parser no
    guarda un identificador propio por línea.
    """
    grav = formatear_decimal(boleta["gravadas"])
    igv  = formatear_decimal(boleta["igv"])
    campos = [str(id_linea), "1000", "IGV", "VAT", f"{grav:.2f}", f"{igv:.2f}"]
    if len(campos) != _COLS_TRD:
        raise ValueError(f".TRD: {len(campos)} columnas, se esperan {_COLS_TRD}")
    return "|".join(campos) + "|\n"
