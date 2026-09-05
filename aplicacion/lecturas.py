"""
Lee la BD de la aplicación y la traduce al vocabulario que el resto del daemon
espera: el emisor, el receptor y los ítems de un comprobante, los comprobantes por
emitir y las boletas candidatas al resumen diario.
"""
import logging
from datetime import datetime

from config import EMISOR_RUC_OVERRIDE, MAX_REINTENTOS_RECHAZO
from dominio.texto import _texto, _tipo_sunat
from dominio.montos import _base_e_igv, _desglosar_igv
from dominio.monto_en_letras import numero_a_letras
from dominio.comprobante import _validar_campos_obligatorios
from utilidades_timer import fecha_local
from aplicacion.bd_app import _bd
from estado.reintentos import _reintentos_de

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
    se respetan las suyas.
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
            "dec_cantidad":    cantidad,
            "valor":           f["valor"]     if f["valor"]     is not None else valor_unit,
            "valor_venta":     valor_venta,
            "igv_venta":       f["igv_venta"] if f["igv_venta"] is not None else igv_venta,
            "precio":          precio_unit,
        })
    return items


def obtener_comprobantes_pendientes(conn):
    """
    Comprobantes por emitir, con los nombres de campo que espera el resto del daemon.

    La aplicación guarda el tipo como texto ('BOLETA', 'FACTURA') y no el código de
    SUNAT, y deja en NULL el desglose de importes: ambas cosas se resuelven acá para
    que sfs.archivos.procesar_comprobante() reciba siempre lo mismo, venga de donde
    venga.

    Las boletas (03) quedan afuera a propósito: van por el resumen diario
    (ver obtener_boletas_para_resumen/sfs.archivos.generar_resumen_diario), nunca
    individualmente.
    """
    # Los datos del comprobante viven en Factura: la tabla Comprobante se fusionó
    # dentro de ella, así que "id" y "factura_id" son la misma fila (se repite el
    # nombre solo porque obtener_receptor() y obtener_items() esperan esa clave).
    # Las filas sin numeración NO se filtran acá: una venta cobrada a la que la
    # aplicación nunca le asignó número igual no se puede emitir, pero descartarla en
    # el SQL la hacía desaparecer sin una sola línea en el log. Pasa a la validación,
    # que la reporta identificándola por su id.
    filas = _bd().pendientes(conn)
    pendientes = []
    for f in filas:
        tipo_comp = _tipo_sunat(f["tipo_comprobante"] or f["tipo_enum"])
        if tipo_comp == "03":
            continue
        gravadas, igv = f["gravadas"], f["igv"]
        if gravadas is None or igv is None:
            gravadas, igv = _base_e_igv(f["total"])
        pendientes.append({
            "id":                             f["id"],
            "factura_id":                     f["id"],
            "tipo_comprobante":               tipo_comp,
            "numeracion_comprobante":         f["numeracion_comprobante"],
            "fecha_emision":                  f["fecha_emision"],
            "tipo_moneda":                    f["tipo_moneda"],
            "tipo_nota":                      f["tipo_nota"],
            "tipo_documento_afectado":        _tipo_sunat(f["tipo_documento_afectado"]),
            "numeracion_documento_afectado":  f["numeracion_documento_afectado"],
            "motivo_documento_afectado":      f["motivo_documento_afectado"],
            "gravadas":                       gravadas,
            "igv":                            igv,
            "total":                          f["total"],
            "monto_letras": _texto(f["monto_letras"]) or numero_a_letras(f["total"], f["tipo_moneda"]),
        })
    return pendientes


def obtener_boletas_para_resumen(conn) -> list:
    """
    Boletas sin enviar, emitidas antes de hoy: el pool de candidatas para el próximo
    resumen diario. Las de hoy se dejan para el resumen de un día siguiente — recién
    "cerraron" su día una vez que termina, y mandar un resumen a medio día se presta
    a que lleguen más boletas después y queden fuera.
    """
    filas = _bd().pendientes(conn)
    hoy = datetime.now().date()
    candidatas = []
    for f in filas:
        if _tipo_sunat(f["tipo_comprobante"] or f["tipo_enum"]) != "03":
            continue
        faltantes = _validar_campos_obligatorios({
            "numeracion_comprobante": f["numeracion_comprobante"],
            "fecha_emision":          f["fecha_emision"],
            "total":                  f["total"],
        })
        if faltantes:
            _avisar_incompleto(
                f["id"],
                "Boleta %s sin datos obligatorios (%s); no entra al resumen hasta completarlos.",
                f["numeracion_comprobante"] or f["id"], ", ".join(faltantes),
            )
            continue
        # Una boleta que un resumen ya excluyó MAX_REINTENTOS_RECHAZO veces por venir
        # con código de línea no se vuelve a proponer sola: seguiría chocando con el
        # mismo dato observado. Ver aplicacion/ciclo_cdr.py:_procesar_lineas_de_resumen().
        if _reintentos_de(f["numeracion_comprobante"]) >= MAX_REINTENTOS_RECHAZO:
            _avisar_incompleto(
                f["id"],
                "Boleta %s agotó los reenvíos dentro de un resumen; no se reincluye "
                "hasta que se corrija el dato observado.",
                f["numeracion_comprobante"] or f["id"],
            )
            continue
        # En hora local, que es la que define a qué día pertenece la boleta: en UTC
        # una boleta de las 20:00 figuraría como del día siguiente y nunca entraría.
        if fecha_local(f["fecha_emision"]).date() >= hoy:
            continue
        gravadas, igv = f["gravadas"], f["igv"]
        if gravadas is None or igv is None:
            gravadas, igv = _base_e_igv(f["total"])
        candidatas.append({
            "id":                     f["id"],
            "factura_id":             f["id"],
            "numeracion_comprobante": f["numeracion_comprobante"],
            "fecha_emision":          f["fecha_emision"],
            "gravadas":               gravadas,
            "igv":                    igv,
            "total":                  f["total"],
            "monto_letras": _texto(f["monto_letras"]) or numero_a_letras(f["total"], "PEN"),
        })
    return candidatas
