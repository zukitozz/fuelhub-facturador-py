"""
BD SQLite propia del SFS local (BDFacturador.db, tabla DOCUMENTO) y los archivos
que deja en la carpeta DATA. El daemon consulta y corrige acá el estado que el SFS
lleva de cada documento, sin tocar el motor de la aplicación (ver repositorio/).
"""
import logging
import os
import sqlite3
import xml.etree.ElementTree as ET
import time
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime

from config import (
    SFS_BD_PATH, SFS_DATA_DIR, SFS_RPTA_DIR, DIR_PROCESADOS,
    _EXT_DATA, _EXT_DATA_SFS, _ESTADOS_CERRADOS, _ESTADOS_ERROR,
    _ESPERA_XML_SEG, _TIPOS_SFS,
    _ESTADOS_RESUMEN_ABIERTO, _MAX_DES_OBSE,
)
from dominio.texto import _texto, _marcas
from dominio.cdr import _TIPO_RC, _veredicto_cdr, parsear_xml_cdr
from utilidades_files import _borrar_si_existe

logger = logging.getLogger(__name__)


@contextmanager
def _sfs_bd(escritura: bool = False):
    """
    Conexión a la BD SQLite del SFS. Hace falta closing() además del `with` porque el
    context manager de sqlite3 hace commit/rollback pero NO cierra la conexión; con
    escritura=True la transacción se confirma al salir.
    """
    conexion = sqlite3.connect(SFS_BD_PATH)
    with closing(conexion):
        if escritura:
            with conexion:
                yield conexion
        else:
            yield conexion


def _xml_generado(ruc: str, tip: str, num: str) -> bool:
    """True si el SFS ya generó el XML del documento (FEC_GENE con valor)."""
    if not os.path.exists(SFS_BD_PATH):
        return True  # sin BD del SFS no se puede comprobar; no bloquear el envío
    try:
        with _sfs_bd() as sfs:
            fila = sfs.execute(
                "SELECT FEC_GENE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=?",
                (ruc, tip, num),
            ).fetchone()
        return bool(fila and fila[0])
    except sqlite3.Error:
        return True


def _resumenes_con_ticket(ruc_emisor: str) -> list:
    """
    [(num_docu, ticket)] de los resúmenes que todavía pueden resolverse por su ticket.

    Se pide que el resumen NO esté cerrado y que conserve ticket, en vez de exigir
    los estados '08'/'09' como antes. El motivo: si la consulta del ticket falla
    —SUNAT devolviendo "Internal Error", por ejemplo— el SFS deja el resumen en '05',
    y con el filtro viejo eso lo sacaba de esta lista para siempre. El ticket seguía
    guardado y seguía siendo válido, pero nadie volvía a usarlo.

    Eso paso en produccion el 2026-09-06: siete resumenes quedaron en '05' por una
    falla pasajera de SUNAT y retuvieron 1239 boletas durante 12 horas, cuando los
    siete tickets respondian "aceptado" al consultarlos a mano.

    Un ticket ya consumido tambien entra acá, y esta bien: SUNAT contesta 0127 y de
    eso se encarga recuperar_cdr_resumenes(), que lo distingue de una consulta que
    fallo y merece otro intento.
    """
    if not os.path.exists(SFS_BD_PATH):
        return []
    marcas = _marcas(len(_ESTADOS_RESUMEN_ABIERTO))
    try:
        with _sfs_bd() as sfs:
            return [
                (_texto(num), _texto(tk))
                for num, tk in sfs.execute(
                    f"SELECT NUM_DOCU, NUM_TICKET FROM DOCUMENTO "
                    f"WHERE NUM_RUC=? AND TIP_DOCU=? AND IND_SITU IN ({marcas}) "
                    f"AND NUM_TICKET IS NOT NULL AND NUM_TICKET <> ''",
                    (ruc_emisor, _TIPO_RC, *_ESTADOS_RESUMEN_ABIERTO),
                )
            ]
    except sqlite3.Error:
        logger.exception("No se pudo leer la BD del SFS para buscar tickets de resumen.")
        return []


def _cerrar_resumen_en_sfs(ruc_emisor: str, numeracion: str, veredicto: str = ""):
    """
    Da por cerrado el resumen en la bandeja del SFS una vez que su CDR está en RPTA.

    Un ticket de SUNAT se consume al consultarlo: si el SFS lo vuelve a consultar
    después de que el daemon ya lo usó, recibe "El ticket no existe" y deja el
    resumen en IND_SITU='05'. Ese estado cuenta como bloqueado, así que el resumen
    se reportaría como trabado en cada ciclo y sus archivos nunca saldrían de DATA
    —pese a estar perfectamente emitido y con las boletas ya cerradas—.

    Se marca '03' con el mismo criterio que usa _activar_pendientes_sfs_bd() cuando
    encuentra un CDR ya descargado: en la bandeja del SFS ese estado significa "ya
    no me ocupo de esto". El veredicto real de SUNAT no vive acá sino en el CDR, que
    es quien decide si las boletas quedan en enviado=true o con su motivo de rechazo.

    El WHERE sale de _ESTADOS_RESUMEN_ABIERTO, la misma constante que decide a cuáles
    consultarles el ticket. Antes exigía '08'/'09' escrito a mano y quedó atrás
    cuando la consulta se amplió para rescatar los resúmenes en '05': el rescate
    funcionaba, pero el cierre no encontraba la fila, el UPDATE afectaba cero filas y
    el resumen se quedaba en '05' para siempre —reconsultándose y reportándose como
    trabado aunque sus boletas ya estuvieran cerradas.
    """
    if not os.path.exists(SFS_BD_PATH):
        return
    # El '03' es el mismo para un aceptado y para un rechazado —significa "ya no me
    # ocupo de esto"—, así que el único lugar donde se puede leer qué contestó SUNAT
    # es este texto. Quien llama pasa el veredicto que ya tiene; si no lo tiene, se lo
    # lee del CDR archivado en vez de suponerlo.
    obse = veredicto or _veredicto_archivado(ruc_emisor, _TIPO_RC, numeracion)
    try:
        with _sfs_bd(escritura=True) as sfs:
            sfs.execute(
                f"UPDATE DOCUMENTO SET IND_SITU='03', DES_OBSE=? "
                f"WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? "
                f"AND IND_SITU IN ({_marcas(len(_ESTADOS_RESUMEN_ABIERTO))})",
                (obse[:_MAX_DES_OBSE], ruc_emisor, _TIPO_RC, numeracion,
                 *_ESTADOS_RESUMEN_ABIERTO),
            )
    except sqlite3.Error:
        logger.exception("No se pudo cerrar el resumen %s en la bandeja del SFS.", numeracion)


def _registrar_en_sfs_bd(ruc_emisor: str, docs: list):
    if not os.path.exists(SFS_BD_PATH):
        return
    time.sleep(_ESPERA_XML_SEG)
    with _sfs_bd(escritura=True) as sfs:
        for doc in docs:
            tip = _texto(doc.get("tip_docu"))
            if tip not in _TIPOS_SFS:
                continue
            num  = _texto(doc.get("num_docu"))
            arch = f"{ruc_emisor}-{tip}-{num}"
            existe = sfs.execute(
                "SELECT 1 FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=?",
                (ruc_emisor, tip, num),
            ).fetchone()
            if not existe:
                # Estado pendiente, no '03'/aceptado: SUNAT todavía no respondió.
                sfs.execute(
                    "INSERT INTO DOCUMENTO (NUM_RUC, TIP_DOCU, NUM_DOCU, NOM_ARCH, IND_SITU, DES_OBSE) "
                    "VALUES (?,?,?,?,?,?)",
                    (ruc_emisor, tip, num, arch, "01", "Enviado al SFS, esperando CDR"),
                )


def _tiene_cdr(ruc: str, tip: str, num: str) -> bool:
    """
    True si el CDR de este comprobante ya está en disco.

    Se exige que el archivo tenga contenido, no solo que exista: un ZIP que quedó en
    0 bytes hacía que recuperar_cdr_pendientes() diera el CDR por recuperado y no
    volviera a consultarle a SUNAT, mientras el barrido tampoco podía procesarlo. El
    comprobante quedaba en enviado=0 sin ninguna via de salida (ver
    _archivo_abandonado).
    """
    nombre = f"R{ruc}-{tip}-{num}.zip"
    for d in (SFS_RPTA_DIR, DIR_PROCESADOS):
        ruta = os.path.join(d, nombre)
        try:
            if os.path.getsize(ruta) > 0:
                return True
        except OSError:
            continue
    return False


def _eliminar_data_files(nom_arch: str):
    for ext in _EXT_DATA + _EXT_DATA_SFS:
        _borrar_si_existe(os.path.join(SFS_DATA_DIR, f"{nom_arch}.{ext}"))


def _limpiar_data_cerrados(ruc_emisor: str, en_vuelo: dict) -> int:
    """
    Borra de DATA los archivos de los comprobantes que el SFS ya cerró con SUNAT.

    Se recorre la carpeta y no la tabla DOCUMENTO a propósito: DATA solo tiene lo
    pendiente más lo recién cerrado, mientras que DOCUMENTO es el histórico y crece
    sin límite. Así el trabajo por ciclo es proporcional a lo que queda por limpiar.
    """
    if not os.path.isdir(SFS_DATA_DIR):
        return 0
    prefijo = f"{ruc_emisor}-"
    bases = {
        os.path.splitext(nombre)[0]
        for nombre in os.listdir(SFS_DATA_DIR)
        if nombre.startswith(prefijo)
    }
    borrados = 0
    for base in bases:
        # base = <ruc>-<tipo>-<serie>-<correlativo>
        partes = base.split("-")
        if len(partes) < 4:
            continue
        tip, num = partes[1], "-".join(partes[2:])
        if tip == _TIPO_RC:
            # El nombre de archivo de un resumen va sin el "RC-" del id (lo exige
            # validarNombreArchivo del SFS), pero en la bandeja el número sí lo
            # lleva. Sin reponerlo acá, sus archivos nunca calzaban y quedaban en
            # DATA para siempre. Ver estado/resumenes.py: _nombre_archivo_rc().
            num = f"{_TIPO_RC}-{num}"
        situ, _ = en_vuelo.get((tip, num), ("", ""))
        if situ in _ESTADOS_CERRADOS:
            _eliminar_data_files(base)
            borrados += 1
    return borrados


def _docs_enviados_sin_cdr(ruc_emisor: str) -> list:
    """
    Documentos que el SFS dice haber enviado y que siguen sin cerrarse, con cuántos
    minutos llevan así. Son los únicos candidatos a consultarle a SUNAT: si no tienen
    fecha de envío es que nunca salieron, y preguntar por ellos no tiene sentido.

    Los resúmenes (RC) quedan afuera: su respuesta vive detrás de un ticket y se
    consulta con getStatus, no con getStatusCdr (ver recuperar_cdr_resumenes). Si
    entraran acá, se les preguntaría con una serie-número que no existe como tal, y
    una respuesta de "no registrado" borraría el resumen de la bandeja junto con su
    ticket — perdiendo el único modo de recuperar su CDR.
    """
    if not os.path.exists(SFS_BD_PATH):
        return []
    marcas = _marcas(len(_ESTADOS_CERRADOS))
    ahora = datetime.now()
    pendientes = []
    try:
        with _sfs_bd() as sfs:
            filas = sfs.execute(
                "SELECT TIP_DOCU, NUM_DOCU, FEC_ENVI FROM DOCUMENTO "
                f"WHERE NUM_RUC=? AND TIP_DOCU<>? AND IND_SITU NOT IN ({marcas}) "
                "AND FEC_ENVI IS NOT NULL AND FEC_ENVI <> ''",
                (ruc_emisor, _TIPO_RC, *_ESTADOS_CERRADOS),
            ).fetchall()
    except sqlite3.Error:
        logger.exception("No se pudo leer la BD del SFS para buscar documentos sin CDR.")
        return []

    for tip, num, fec_envi in filas:
        try:
            enviado_el = datetime.strptime(_texto(fec_envi), "%d/%m/%Y %H:%M:%S")
        except ValueError:
            continue
        pendientes.append((_texto(tip), _texto(num), (ahora - enviado_el).total_seconds() / 60))
    return pendientes


def _docs_en_vuelo(ruc_emisor: str) -> dict:
    """
    {(tip_docu, num_docu): (IND_SITU, DES_OBSE)} de lo que el SFS ya tiene
    registrado y por lo tanto está entregado, en proceso o bloqueado. Se usa para
    no reenviar a SUNAT un comprobante que sigue en enviado=0 solo porque su CDR
    todavía no llegó — y para separar esos de los que están trabados (_ESTADOS_BLOQUEADO).
    """
    if not os.path.exists(SFS_BD_PATH):
        return {}
    try:
        with _sfs_bd() as sfs:
            return {
                (_texto(t), _texto(n)): (_texto(situ), _texto(obse))
                for t, n, situ, obse in sfs.execute(
                    "SELECT TIP_DOCU, NUM_DOCU, IND_SITU, DES_OBSE FROM DOCUMENTO WHERE NUM_RUC=?",
                    (ruc_emisor,),
                )
            }
    except sqlite3.Error:
        logger.exception("No se pudo leer la BD del SFS; se omite el filtro de duplicados.")
        return {}


def _cerrar_documento_en_sfs(ruc: str, tipo: str, numeracion: str):
    """
    Da por enviado y aceptado un documento en la bandeja del SFS.

    Solo hace falta cuando el CDR no llegó por el camino del SFS sino que lo bajó
    el daemon de SUNAT (ver _guardar_cdr): en ese caso la fila queda como la dejó
    el error de red, en IND_SITU='06' y con FEC_ENVI vacía. Sin esto pasaban dos
    cosas, las dos vistas en producción el 2026-09-03: resetear_rechazados() volvía
    a levantar la fila en el ciclo siguiente —consultaba a SUNAT otra vez, bajaba
    el mismo CDR, y así cada 60 segundos durante casi cuatro horas—, y la bandeja
    mostraba el comprobante como "Con Errores" pese a estar aceptado en SUNAT.

    El WHERE filtra por los estados de error a propósito: si la fila ya está
    cerrada, el UPDATE no afecta ninguna y el segundo pase del mismo CDR —watchdog
    y barrido periódico pueden verlo dos veces— no revierte nada.
    """
    if not (ruc and tipo and numeracion) or not os.path.exists(SFS_BD_PATH):
        return
    marcas = _marcas(len(_ESTADOS_ERROR))
    try:
        with _sfs_bd(escritura=True) as sfs:
            sfs.execute(
                f"UPDATE DOCUMENTO SET IND_SITU='03', FEC_ENVI=?, DES_OBSE='-' "
                f"WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? AND IND_SITU IN ({marcas})",
                (datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                 ruc, tipo, numeracion, *_ESTADOS_ERROR),
            )
    except sqlite3.Error:
        logger.exception(
            "No se pudo cerrar %s-%s en la bandeja del SFS; el comprobante quedó "
            "bien cerrado en la BD igual.", tipo, numeracion,
        )


def _ticket_de_resumen(ruc_emisor: str, numeracion: str) -> str:
    """
    Ticket guardado de un resumen, o "" si no tiene.

    Sirve como evidencia de si SUNAT llegó a recibirlo: el ticket lo escribe el SFS
    con lo que devuelve sendSummary, así que sin ticket el envío no llegó. Es lo que
    permite decidir si un resumen trabado se puede volver a armar sin arriesgar
    declarar las mismas boletas dos veces.
    """
    if not os.path.exists(SFS_BD_PATH):
        return ""
    try:
        with _sfs_bd() as sfs:
            fila = sfs.execute(
                "SELECT NUM_TICKET FROM DOCUMENTO "
                "WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=?",
                (ruc_emisor, _TIPO_RC, numeracion),
            ).fetchone()
    except sqlite3.Error:
        # Ante la duda se responde "tiene ticket": eso frena el reenvío, que es el
        # lado seguro. Decir que no tiene habilitaría a declarar de nuevo algo que
        # quizá SUNAT ya recibió.
        logger.exception("No se pudo leer el ticket del resumen %s.", numeracion)
        return "desconocido"
    return _texto(fila[0]) if fila else ""


def _cdr_ya_procesado(ruc: str, tip: str, num: str) -> bool:
    """
    True si el CDR ya paso por el hilo CDR y quedo archivado.

    La distincion con _tiene_cdr() importa: que el archivo este en RPTA solo dice
    que se bajo, no que se haya repartido entre las boletas. Recien cuando el
    barrido lo procesa sin errores lo mueve a procesados/, y esa mudanza es la
    unica evidencia de que el CDR ya hizo su trabajo.
    """
    ruta = os.path.join(DIR_PROCESADOS, f"R{ruc}-{tip}-{num}.zip")
    try:
        return os.path.getsize(ruta) > 0
    except OSError:
        return False


def _veredicto_archivado(ruc: str, tip: str, num: str) -> str:
    """
    Veredicto leído del CDR que ya está en disco, para cerrar sin tener que afirmarlo.

    Hace falta donde se cierra un documento por el solo hecho de que su CDR existe
    —ver _activar_pendientes_sfs_bd() y el rescate de recuperar_cdr_resumenes()—: ahí
    no hay un `parsed` a mano, y suponer "aceptado" es exactamente lo que hacía mentir
    a la bandeja. Si el archivo no se puede leer, el texto lo dice en vez de inventar
    un veredicto.
    """
    for carpeta in (DIR_PROCESADOS, SFS_RPTA_DIR):
        ruta = os.path.join(carpeta, f"R{ruc}-{tip}-{num}.zip")
        try:
            with zipfile.ZipFile(ruta) as z:
                xmls = [n for n in z.namelist() if n.lower().endswith(".xml")]
                if xmls:
                    return _veredicto_cdr(parsear_xml_cdr(z.read(xmls[0])))
        except (OSError, zipfile.BadZipFile, ET.ParseError):
            continue
    return "CDR procesado; ver el CDR archivado"
