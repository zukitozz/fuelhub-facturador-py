"""
Parseo de CDR y de numeraciones SUNAT. Puro procesamiento de texto/XML — nada de
esto lee RPTA ni la BD; eso lo hace _barrer_rpta() en main.py, que le pasa bytes u
otra ruta ya abierta.
"""
import logging
import os
import re
import xml.etree.ElementTree as ET

from .texto import _texto

logger = logging.getLogger(__name__)

# Constantes.CONSTANTE_TIPO_DOCUMENTO_RBOLETAS: el SFS trata el resumen diario como
# un tipo de documento más, con los mismos dos endpoints REST que todo lo demás
# (GenerarComprobante.htm / enviarXML.htm) y el mismo patrón de dos pasadas. Puertas
# adentro, SUNAT usa un flujo con ticket (sendSummary + getStatus sobre el mismo
# billService) — confirmado contra el WSDL real de producción — pero eso lo resuelve
# el SFS solo: el daemon no necesita hablar SOAP para esto, a diferencia de la
# recuperación de CDR (que sí lo hace directo).
_TIPO_RC = "RC"


def _texto_de_nodo(xml: str, etiqueta: str) -> str:
    """Contenido de un nodo de la respuesta SOAP, sin importar su prefijo."""
    m = re.search(rf"<(?:\w+:)?{etiqueta}>(.*?)</(?:\w+:)?{etiqueta}>", xml, re.S)
    return m.group(1).strip() if m else ""


def _iter_elementos(elem, ancs=()):
    yield elem, ancs
    for hijo in elem:
        yield from _iter_elementos(hijo, ancs + (elem.tag.split("}")[-1],))


def _extraer_numeracion(texto) -> str | None:
    # La serie SUNAT son 4 caracteres alfanuméricos que arrancan con letra: F001,
    # B001, y también BC01/FC01/BC03 en notas de crédito. El patrón anterior exigía
    # 3 dígitos al final (\d{3}) y dejaba fuera esas series, con lo que el CDR de una
    # nota de crédito quedaba sin numeración y su comprobante nunca pasaba a enviado=1.
    # El resumen diario (RC-YYYYMMDD-NNN) tiene solo 2 letras antes del guión, así que
    # necesita su propia alternativa: nunca calzaría con las 4 exigidas por la otra.
    m = re.search(rf"{_TIPO_RC}-\d{{8}}-\d+|[A-Z][A-Z0-9]{{3}}-\d+", _texto(texto))
    return m.group(0) if m else None


def _respuestas_por_documento(root) -> list:
    """
    [(numeracion, codigo, descripcion)] de cada <cac:DocumentResponse> del CDR.

    El esquema los declara con maxOccurs="unbounded": un CDR de resumen puede
    traer uno por el resumen entero y otro por cada boleta que SUNAT observe. Sin
    recorrerlos todos, esas observaciones se pierden — el comprobante queda
    aceptado y nadie se entera de que una línea salió con reparos.
    """
    respuestas = []
    for elem in root.iter():
        if not isinstance(elem.tag, str) or elem.tag.split("}")[-1] != "DocumentResponse":
            continue
        datos = {}
        for hijo in elem.iter():
            if not isinstance(hijo.tag, str):
                continue
            tag = hijo.tag.split("}")[-1].lower()
            texto = _texto(hijo.text)
            if texto and tag in ("referenceid", "responsecode", "description") and tag not in datos:
                datos[tag] = texto
        if datos:
            respuestas.append((
                _extraer_numeracion(datos.get("referenceid", "")),
                datos.get("responsecode"),
                datos.get("description"),
            ))
    return respuestas


def _reconciliar_numeracion(del_xml: str | None, nombre_archivo: str) -> str | None:
    """
    Numeración canónica del comprobante, cuando el XML y el nombre del archivo no
    coinciden.

    SUNAT escribe el número distinto según por dónde llegue el CDR. El de sendBill
    —el envío normal, que entrega el SFS— trae 'F003-009595'. El de getStatusCdr
    —la consulta que hace estado_en_sunat()— trae '20605858601-01-F003-9571': con
    prefijo de RUC y tipo, y el correlativo SIN los ceros a la izquierda. Los dos
    son CDR legítimos y firmados; simplemente no usan el mismo formato.

    De ahí que el XML sirva para el estado y los códigos, pero no para la identidad:
    _extraer_numeracion() sacaba 'F003-9571' de ese segundo formato y no existe
    ninguna fila así en Comprobantes, que la guarda como 'F003-009571'. El nombre
    del archivo, en cambio, es canónico en los dos caminos: lo arma _guardar_cdr()
    desde el NUM_DOCU del SFS, y el SFS lo arma desde su propia tabla.

    No se rellena con ceros a un ancho fijo a propósito: los 6 dígitos son
    convención de esta aplicación, no de SUNAT —que admite hasta 8—, y fijarlos acá
    rompería con cualquier otro emisor. Se comparan los correlativos como enteros,
    que es la única equivalencia que vale sin importar el relleno.
    """
    del_nombre = _extraer_numeracion(nombre_archivo)
    if not del_nombre:
        return del_xml
    if not del_xml or del_xml == del_nombre:
        return del_nombre

    serie_xml, _, corr_xml = del_xml.partition("-")
    serie_arch, _, corr_arch = del_nombre.partition("-")
    # Los resúmenes (RC-YYYYMMDD-NNN) no entran acá: su correlativo no es un entero
    # suelto, así que isdigit() falla y se respeta lo que dijo el XML.
    if serie_xml == serie_arch and corr_xml.isdigit() and corr_arch.isdigit() \
            and int(corr_xml) == int(corr_arch):
        return del_nombre
    return del_xml


def _datos_del_nombre_cdr(nombre: str) -> tuple:
    """
    (ruc, tipo) a partir del nombre del CDR: 'R20605858601-01-F003-009571.zip'.

    Hacen falta para cerrar la fila en la bandeja del SFS, que se identifica por
    NUM_RUC + TIP_DOCU + NUM_DOCU. Devuelve (None, None) si el nombre no tiene esa
    forma, y quien llama simplemente no cierra nada.
    """
    m = re.match(r"R(\d{11})-([A-Z0-9]{2})-", _texto(nombre))
    return (m.group(1), m.group(2)) if m else (None, None)


def parsear_xml_cdr(fuente) -> dict:
    res = {"numeracion": None, "codigo": None, "descripcion": None,
           "status": "PENDIENTE", "lineas": []}
    try:
        root = ET.fromstring(fuente) if isinstance(fuente, bytes) else ET.parse(fuente).getroot()
        res["lineas"] = _respuestas_por_documento(root)
        # Las descripciones que cuelgan de un <Response> son las buenas; cualquier otra
        # queda de respaldo por si el CDR no trae ninguna en el lugar esperado.
        descripciones, respaldo = [], []
        for elem, ancs in _iter_elementos(root):
            if not isinstance(elem.tag, str):
                continue
            tag  = elem.tag.split("}")[-1].lower()
            text = _texto(elem.text)
            if not text:
                continue
            if tag == "responsecode" and not res["codigo"]:
                res["codigo"] = text
            elif tag in {"description", "responsedescription"}:
                if any("response" in a.lower() for a in ancs):
                    descripciones.append(text)
                elif tag == "description" and not respaldo:
                    respaldo.append(text)
            elif tag in {"referenceid", "id"} and not res["numeracion"]:
                res["numeracion"] = _extraer_numeracion(text)

        res["descripcion"] = " | ".join(dict.fromkeys(descripciones or respaldo)) or None

        if not res["numeracion"]:
            res["numeracion"] = _extraer_numeracion(res["descripcion"])
        if not res["numeracion"] and isinstance(fuente, str):
            res["numeracion"] = _extraer_numeracion(os.path.basename(fuente))

        codigo = _texto(res["codigo"])
        desc   = _texto(res["descripcion"]).lower()
        # El ResponseCode manda: SUNAT solo devuelve 0 cuando acepta. Cualquier
        # otro código es rechazo, aunque la descripción no diga "rechazado".
        if codigo:
            res["status"] = "ACEPTADO" if codigo.strip("0") == "" else "RECHAZADO"
        elif "acept" in desc:
            res["status"] = "ACEPTADO"
        elif "rechaz" in desc or "error" in desc or "no autorizado" in desc:
            res["status"] = "RECHAZADO"

        # Aceptada con observaciones sigue siendo aceptada (ver _CDR_ACEPTADOS)
        if res["status"] == "ACEPTADO" and "observ" in desc:
            res["status"] = "OBSERVADO"

    except Exception:
        logger.exception("Error parseando CDR %s", fuente if isinstance(fuente, str) else "<bytes>")
        res["status"] = "ERROR"
    return res
