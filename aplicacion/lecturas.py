"""
Lee la BD de la aplicación y la traduce al vocabulario que el resto del daemon
espera: el emisor, el receptor y los ítems de un comprobante, y los comprobantes
por emitir — incluidas las boletas, que ahora van por el mismo camino que
cualquier otro tipo (ver aplicacion/ciclo_generacion.py: facturador-api las
firma pero no las envía solas, las agrupa en el resumen diario por su cuenta).
"""
import logging

from config import EMISOR_RUC_OVERRIDE, MOTIVO_NOTA_POR_DEFECTO, _TIPOS_NOTA, _MOTIVOS_NOTA
from dominio.texto import _texto, _codigo, _tipo_sunat
from dominio.montos import _base_e_igv, _desglosar_igv
from dominio.monto_en_letras import numero_a_letras
from dominio.comprobante import _validar_campos_obligatorios
from aplicacion.bd_app import _bd

logger = logging.getLogger(__name__)

# Comprobantes ya reportados como incompletos. Una venta a la que le falta un dato
# se queda así hasta que alguien la corrija, y repetir el aviso en cada ciclo llenaría
# el log de la misma línea cada 60 segundos. Se avisa una vez por corrida: si sigue
# sin resolverse, vuelve a aparecer en el próximo arranque.
_avisados_incompletos: set = set()


def _avisar_incompleto(clave, mensaje: str, *args):
    if clave in _avisados_incompletos:
        return
    _avisados_incompletos.add(clave)
    logger.warning(mensaje, *args)


def obtener_emisor(conn):
    """
    Datos del emisor. La aplicación no tiene tabla de emisores —solo guarda el nombre
    en Configuracion—, así que el RUC sale de EMISOR_RUC en el .env.
    """
    razon = _texto(_bd().emisor(conn))
    if not EMISOR_RUC_OVERRIDE:
        return None
    return {"ruc": EMISOR_RUC_OVERRIDE, "razon_social": razon}


def obtener_receptor(conn, factura_id):
    """
    Receptor del comprobante. Cada esquema lo guarda distinto —uno separa tipo y
    número de documento, otro los deduce del RUC o el DNI—, así que la traducción vive
    en el adaptador y acá llega ya normalizado.
    """
    if not factura_id:
        return {}
    return _bd().receptor(conn, factura_id) or {}


def obtener_items(conn, factura_id):
    """
    Ítems del comprobante, con el desglose de IGV que la aplicación no guarda.

    FacturaItem tiene columnas para el desglose (valor, valorVenta, igvVenta, precio)
    pero la aplicación solo llena nombre, cantidad, precioUnit y total. Cuando faltan
    se calculan desde el precio con IGV incluido; si algún día empieza a llenarlas,
    se respetan las suyas. valor_venta SIEMPRE queda en el dict devuelto, calculado si
    hiciera falta: facturador-api lo exige en el payload (items[].valor_venta).
    """
    filas = _bd().items(conn, factura_id)
    items = []
    for f in filas:
        cantidad = f["dec_cantidad"] or f["cantidad"] or 1
        precio_unit = f["precio"] if f["precio"] is not None else f["precio_unit"]
        valor_unit, valor_venta, igv_venta = _desglosar_igv(precio_unit, cantidad, f["total"])
        items.append({
            "descripcion":     f["descripcion"],
            "codigo_producto": f["codigo_producto"],
            # ZZ = "servicio" en el catálogo 03 de SUNAT. Si el esquema del cliente
            # trae su propia unidad de medida (un grifo factura galones), se respeta.
            "medida":          f.get("medida") or "ZZ",
            "cantidad":        cantidad,
            "valor":           f["valor"]     if f["valor"]     is not None else valor_unit,
            "valor_venta":     valor_venta,
            "igv_venta":       f["igv_venta"] if f["igv_venta"] is not None else igv_venta,
            "precio":          precio_unit,
        })
    return items


def _completar_referencia_nota(f: dict) -> dict | None:
    """
    Objeto 'referencia' del payload de facturador-api, solo para notas (07/08).
    None si falta algo —la nota no se emite hasta que se complete, igual que
    siempre—. El código de motivo NO se deduce: es un dato tributario y una nota
    con el motivo equivocado es una declaración incorrecta ante SUNAT.

    La única forma de rellenarlo es que alguien lo declare explícitamente en
    MOTIVO_NOTA_POR_DEFECTO, y eso solo tiene sentido donde la aplicación de
    origen no puede generar más de un tipo de nota (ver el comentario de esa
    constante en config.py).
    """
    tipo_comp = _tipo_sunat(f["tipo_comprobante"] or f.get("tipo_enum"))
    cod_motivo = _codigo(f.get("tipo_nota"))
    tip_afectado = _codigo(f.get("tipo_documento_afectado"))
    num_afectado = _texto(f.get("numeracion_documento_afectado"))

    if not cod_motivo and MOTIVO_NOTA_POR_DEFECTO:
        candidato = _codigo(MOTIVO_NOTA_POR_DEFECTO)
        catalogo = _MOTIVOS_NOTA.get(tipo_comp, {})
        if candidato in catalogo:
            cod_motivo = candidato
            logger.info(
                "Nota %s sin tipo_nota en la BD; se usa MOTIVO_NOTA_POR_DEFECTO=%s (%s).",
                f["numeracion_comprobante"], candidato, catalogo[candidato],
            )
        else:
            logger.error(
                "MOTIVO_NOTA_POR_DEFECTO=%r no es un motivo válido para el tipo %s "
                "(catálogo: %s); se ignora y la nota no se emite.",
                MOTIVO_NOTA_POR_DEFECTO, tipo_comp, ", ".join(sorted(catalogo)) or "ninguno",
            )

    faltantes = [
        nombre for nombre, valor in (
            ("tipo_nota (código de motivo)",        cod_motivo),
            ("tipo_documento_afectado",              tip_afectado),
            ("numeracion_documento_afectado",        num_afectado),
        ) if not valor
    ]
    if faltantes:
        _avisar_incompleto(
            f["id"],
            "Nota %s sin datos obligatorios (%s); no se emite hasta completarlos.",
            f["numeracion_comprobante"] or f["id"], ", ".join(faltantes),
        )
        return None

    catalogo = _MOTIVOS_NOTA.get(tipo_comp, {})
    return {
        "tipo_nota": cod_motivo,
        "tipo_documento_afectado": tip_afectado,
        "numeracion_documento_afectado": num_afectado,
        "motivo_documento_afectado": _texto(f.get("motivo_documento_afectado")) or catalogo.get(cod_motivo, ""),
    }


def obtener_comprobantes_pendientes(conn):
    """
    Comprobantes por emitir, con los nombres de campo que espera el resto del
    daemon — incluidas las boletas (03): a diferencia del SFS local, acá cada
    comprobante se manda individualmente a facturador-api sin importar el tipo;
    es la propia API la que, para una boleta, firma pero no envía sola (queda a
    la espera del resumen diario — ver aplicacion/ciclo_generacion.py).
    """
    filas = _bd().pendientes(conn)
    pendientes = []
    for f in filas:
        tipo_comp = _tipo_sunat(f["tipo_comprobante"] or f["tipo_enum"])
        if not tipo_comp:
            _avisar_incompleto(
                f["id"], "Comprobante id=%s sin tipo reconocible; no se emite.", f["id"],
            )
            continue
        faltantes = _validar_campos_obligatorios(f)
        if faltantes:
            _avisar_incompleto(
                f["id"],
                "Comprobante %s sin datos obligatorios (%s); no se emite hasta completarlos.",
                f["numeracion_comprobante"] or f["id"], ", ".join(faltantes),
            )
            continue
        gravadas, igv = f["gravadas"], f["igv"]
        if gravadas is None or igv is None:
            gravadas, igv = _base_e_igv(f["total"])
        comp = {
            "id":                             f["id"],
            "factura_id":                     f["id"],
            "tipo_comprobante":               tipo_comp,
            "numeracion_comprobante":         f["numeracion_comprobante"],
            "fecha_emision":                  f["fecha_emision"],
            "tipo_moneda":                    f["tipo_moneda"] or "PEN",
            "gravadas":                       gravadas,
            "igv":                            igv,
            "total":                          f["total"],
            "monto_letras": _texto(f["monto_letras"]) or numero_a_letras(f["total"], f["tipo_moneda"] or "PEN"),
            "referencia": None,
        }
        if tipo_comp in _TIPOS_NOTA:
            referencia = _completar_referencia_nota(f)
            if referencia is None:
                continue   # ya se avisó en _completar_referencia_nota
            comp["referencia"] = referencia
        pendientes.append(comp)
    return pendientes
