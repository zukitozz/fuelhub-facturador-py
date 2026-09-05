"""
Generación de los archivos que el SFS local lee de su carpeta DATA: la cabecera,
el detalle, el IGV, el monto en letras y la forma de pago de un comprobante, y el
.RDI/.TRD de un resumen diario de boletas.
"""
import logging
import os
from datetime import datetime

from config import (
    SFS_DATA_DIR, _EXT_CABECERA, _EXT_CABECERA_POR_DEFECTO, _EXT_DATA,
    _TIPOS_NOTA, _TIPOS_SIN_FORMA_PAGO, _MOTIVOS_NOTA, MOTIVO_NOTA_POR_DEFECTO,
    MAX_BOLETAS_RESUMEN, _MAX_BLOQUEADOS_LOG,
)
from dominio.texto import _texto, _codigo, _campo_pipe
from dominio.montos import formatear_decimal
from dominio.comprobante import _nombre_base, _validar_campos_obligatorios, _linea_detalle
from dominio.resumen_diario import _linea_rdi, _linea_trd
from dominio.cdr import _TIPO_RC
from utilidades_files import escribir_archivo, _borrar_si_existe
from utilidades_timer import fecha_local
from aplicacion.lecturas import obtener_receptor, obtener_items, obtener_boletas_para_resumen, _avisar_incompleto
from estado.resumenes import _boletas_en_resumenes_activos, _registrar_resumen, _siguiente_numeracion_rc, _nombre_archivo_rc

logger = logging.getLogger(__name__)


def _referencia_nota(comp: dict, tipo_comp: str, num_comp: str):
    """
    (codMotivo, desMotivo, tipDocAfectado, numDocAfectado) para una nota, o None si
    falta algo. El código de motivo NO se deduce: es un dato tributario y una nota con
    el motivo equivocado es una declaración incorrecta ante SUNAT. Sin él la nota no se
    emite y queda reportada para que la completen.

    La única forma de rellenarlo es que alguien lo declare explícitamente en
    MOTIVO_NOTA_POR_DEFECTO, y eso solo tiene sentido donde la aplicacion de origen no
    puede generar más de un tipo de nota (ver el comentario de esa constante en config.py).
    """
    cod_motivo   = _codigo(comp.get("tipo_nota"))
    tip_afectado = _codigo(comp.get("tipo_documento_afectado"))
    num_afectado = _campo_pipe(comp.get("numeracion_documento_afectado"))

    if not cod_motivo and MOTIVO_NOTA_POR_DEFECTO:
        candidato = _codigo(MOTIVO_NOTA_POR_DEFECTO)
        catalogo = _MOTIVOS_NOTA.get(tipo_comp, {})
        if candidato in catalogo:
            cod_motivo = candidato
            # A nivel INFO y no WARNING: acá el motivo por defecto es la configuración
            # esperada, no una anomalía. Pero se registra en cada nota, porque queda
            # declarado ante SUNAT y tiene que poder rastrearse cuál salió así.
            logger.info(
                "Nota %s-%s sin tipo_nota; se usa el motivo por defecto %s (%s).",
                tipo_comp, num_comp, cod_motivo, catalogo[candidato],
            )
        else:
            # Un error de tipeo en el .env no puede convertirse en una declaración
            # con un motivo que no existe: se ignora y la nota queda sin emitir, que
            # es el comportamiento de siempre cuando falta el dato.
            logger.error(
                "MOTIVO_NOTA_POR_DEFECTO=%r no es un motivo válido para el tipo %s "
                "(catálogo: %s); se ignora y la nota no se emite.",
                MOTIVO_NOTA_POR_DEFECTO, tipo_comp, ", ".join(sorted(catalogo)) or "ninguno",
            )

    faltantes = [
        nombre for nombre, valor in (
            ("tipo_nota (código de motivo)",        cod_motivo),
            ("tipo_documento_afectado",             tip_afectado),
            ("numeracion_documento_afectado",       num_afectado),
        ) if not valor
    ]
    if faltantes:
        logger.warning(
            "Nota %s-%s sin datos de referencia (%s); no se emite hasta completarlos.",
            tipo_comp, num_comp, ", ".join(faltantes),
        )
        return None

    des_motivo = _campo_pipe(
        comp.get("motivo_documento_afectado"),
        _MOTIVOS_NOTA.get(tipo_comp, {}).get(cod_motivo, "OTROS CONCEPTOS"),
    )
    return cod_motivo, des_motivo, tip_afectado, num_afectado


def procesar_comprobante(conn, comp: dict, ruc_emisor: str) -> bool:
    num_comp = _texto(comp.get("numeracion_comprobante"))
    # zfill(2) para que el nombre de archivo coincida con el tip_docu que se manda al SFS
    tipo_comp = _codigo(comp.get("tipo_comprobante"), "01")

    # Validación previa: todo esto se chequea antes de escribir nada, para no dejar
    # archivos huérfanos en DATA por un comprobante que igual no se puede armar bien.
    faltantes = _validar_campos_obligatorios(comp)
    if faltantes:
        _avisar_incompleto(
            comp.get("factura_id") or num_comp,
            "Comprobante %s-%s sin datos obligatorios (%s); no se emite hasta completarlos.",
            tipo_comp, num_comp or "?", ", ".join(faltantes),
        )
        return False

    items = obtener_items(conn, comp.get("factura_id"))
    if not items:
        logger.warning(
            "Comprobante %s-%s sin ítems; no se emite hasta completarlos.",
            tipo_comp, num_comp,
        )
        return False

    # Las notas se validan antes de escribir nada: si les falta la referencia, el SFS
    # las rechazaría igual y quedarían archivos huérfanos en DATA.
    referencia = None
    if tipo_comp in _TIPOS_NOTA:
        referencia = _referencia_nota(comp, tipo_comp, num_comp)
        if referencia is None:
            return False

    base  = _nombre_base(ruc_emisor, tipo_comp, num_comp)
    os.makedirs(SFS_DATA_DIR, exist_ok=True)
    ext_cab = _EXT_CABECERA.get(tipo_comp, _EXT_CABECERA_POR_DEFECTO)
    rutas = {e: os.path.join(SFS_DATA_DIR, f"{base}.{e}") for e in _EXT_DATA}
    rutas["cabecera"] = rutas[ext_cab]

    receptor     = obtener_receptor(conn, comp.get("factura_id"))
    tipo_doc_rec = _campo_pipe(receptor.get("tipo_documento"),   "0")
    num_doc_rec  = _campo_pipe(receptor.get("numero_documento"), "00000000")
    razon_social = _campo_pipe(receptor.get("razon_social"),     "CLIENTE VARIOS")
    moneda       = _campo_pipe(comp.get("tipo_moneda"),          "PEN")
    monto_letras = _campo_pipe(comp.get("monto_letras"),         "SIN DESCRIPCION")

    # En hora local: es la fecha que se le declara a SUNAT (la BD guarda UTC).
    fecha_dt  = fecha_local(comp.get("fecha_emision"))
    fecha_str = fecha_dt.strftime("%Y-%m-%d")
    hora_str  = fecha_dt.strftime("%H:%M:%S")

    tot_grav  = formatear_decimal(comp.get("gravadas")  or comp.get("total_gravadas"))
    tot_igv   = formatear_decimal(comp.get("igv")       or comp.get("total_igv"))
    tot_venta = formatear_decimal(comp.get("total")     or comp.get("total_venta"))

    lineas_det = [_linea_detalle(item) for item in items]

    # Cola de la cabecera: totales y versiones, iguales en los dos layouts.
    totales = (
        f"{tot_igv:.2f}|{tot_grav:.2f}|{tot_venta:.2f}|"
        f"0.00|0.00|0.00|{tot_venta:.2f}|2.1|2.0|\n"
    )
    if referencia is not None:
        # Cabecera de nota (21 columnas, archivo .NOT): sin fecVencimiento y con la
        # referencia al documento afectado, que es lo que SUNAT exige en toda nota.
        cod_motivo, des_motivo, tip_afectado, num_afectado = referencia
        escribir_archivo(rutas["cabecera"],
            f"0101|{fecha_str}|{hora_str}|0000|{tipo_doc_rec}|{num_doc_rec}|"
            f"{razon_social}|{moneda}|{cod_motivo}|{des_motivo}|{tip_afectado}|{num_afectado}|"
            + totales
        )
        # El SFS reconoce el tipo por la extensión de la cabecera: un .cab sobrante
        # haría que tome la nota por una factura.
        _borrar_si_existe(rutas["cab"])
    else:
        # Cabecera de factura/boleta (18 columnas, archivo .cab).
        escribir_archivo(rutas["cabecera"],
            f"0101|{fecha_str}|{hora_str}|-|0000|{tipo_doc_rec}|{num_doc_rec}|"
            f"{razon_social}|{moneda}|" + totales
        )

    if tipo_comp in _TIPOS_SIN_FORMA_PAGO:
        _borrar_si_existe(rutas["PAG"])
    else:
        escribir_archivo(rutas["PAG"], f"Contado|{tot_venta:.2f}|{moneda}|\n")

    escribir_archivo(rutas["tri"], f"1000|IGV|VAT|{tot_grav:.2f}|{tot_igv:.2f}|\n")
    escribir_archivo(rutas["ley"], f"1000|{monto_letras}|\n")

    escribir_archivo(rutas["det"], "".join(lineas_det))

    # OJO: no se toca Comprobante.enviado acá. Generar los archivos no es haber
    # enviado nada; el estado lo mueve ciclo_generacion() recién cuando el SFS
    # confirma la recepción, y lo cierra el CDR de SUNAT.
    logger.info("Archivos SFS generados: %s", num_comp)
    return True


def generar_resumen_diario(conn, ruc_emisor: str):
    """
    Agrupa en un solo resumen las boletas pendientes de días anteriores y escribe
    sus .RDI/.TRD en DATA. Devuelve el doc {"num_ruc","tip_docu","num_docu"} listo
    para activar_procesamiento_sfs(), o None si no había boletas candidatas.
    """
    boletas = obtener_boletas_para_resumen(conn)
    if not boletas:
        return None
    excluidas = _boletas_en_resumenes_activos(ruc_emisor)
    boletas = [b for b in boletas if b["numeracion_comprobante"] not in excluidas]
    if not boletas:
        return None

    # Un resumen declara UNA sola fecha de referencia (<cbc:ReferenceDate> en
    # ConvertirRBoletasXML.ftl del SFS), asi que no puede mezclar dias: se toma el
    # mas antiguo pendiente y los demas esperan al proximo ciclo.
    boletas.sort(key=lambda b: (fecha_local(b["fecha_emision"]).date(),
                               b["numeracion_comprobante"]))
    dia = fecha_local(boletas[0]["fecha_emision"]).date()
    del_dia = [b for b in boletas if fecha_local(b["fecha_emision"]).date() == dia]
    boletas, restantes = del_dia[:MAX_BOLETAS_RESUMEN], len(del_dia) - MAX_BOLETAS_RESUMEN
    if restantes > 0:
        logger.info(
            "%s tiene %d boleta(s) pendientes; entran %d en este resumen y %d en el siguiente.",
            dia, len(del_dia), len(boletas), restantes,
        )

    hoy = datetime.now()
    fecha_resumen = hoy.strftime("%Y-%m-%d")
    numeracion_rc = _siguiente_numeracion_rc(hoy.strftime("%Y%m%d"))
    base = _nombre_archivo_rc(ruc_emisor, numeracion_rc)
    os.makedirs(SFS_DATA_DIR, exist_ok=True)

    lineas_rdi, lineas_trd = [], []
    for i, boleta in enumerate(boletas, start=1):
        receptor = obtener_receptor(conn, boleta.get("factura_id"))
        fecha_emision = fecha_local(boleta["fecha_emision"]).strftime("%Y-%m-%d")
        lineas_rdi.append(_linea_rdi(fecha_emision, fecha_resumen, boleta, receptor))
        lineas_trd.append(_linea_trd(i, boleta))

    escribir_archivo(os.path.join(SFS_DATA_DIR, f"{base}.RDI"), "".join(lineas_rdi))
    escribir_archivo(os.path.join(SFS_DATA_DIR, f"{base}.TRD"), "".join(lineas_trd))

    numeraciones = [b["numeracion_comprobante"] for b in boletas]
    _registrar_resumen(numeracion_rc, numeraciones)
    # Con un tope de 200 la lista entera hacia una linea de log de miles de
    # caracteres por resumen. El detalle completo vive en resumenes.json.
    muestra = ", ".join(numeraciones[:_MAX_BLOQUEADOS_LOG])
    if len(numeraciones) > _MAX_BLOQUEADOS_LOG:
        muestra += f" ... y {len(numeraciones) - _MAX_BLOQUEADOS_LOG} mas"
    logger.info(
        "Resumen diario %s generado con %d boleta(s): %s",
        numeracion_rc, len(numeraciones), muestra,
    )
    return {"num_ruc": ruc_emisor, "tip_docu": _TIPO_RC, "num_docu": numeracion_rc}
