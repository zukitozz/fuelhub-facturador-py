"""
Caso de uso del hilo CDR: barre la carpeta RPTA, parsea cada CDR y cierra el
comprobante (o el resumen) que corresponda en la BD de la aplicación.
"""
import logging
import os
import threading
import zipfile
from datetime import datetime

import integraciones
from config import (
    SFS_RPTA_DIR, DIR_PROCESADOS, DIR_ERRORES, MAX_REINTENTOS_RECHAZO,
    _MAX_ERRORS_SQL, _CDR_ACEPTADOS, NOTIFICADOR_COMPROBANTES, EMISOR_RUC_OVERRIDE,
)
from dominio.cdr import _TIPO_RC, _reconciliar_numeracion, _datos_del_nombre_cdr, parsear_xml_cdr
from utilidades_files import _archivo_estable, _mover
from aplicacion.bd_app import conectar_bd, _bd, _escribir_bd
from estado.resumenes import _boletas_de_resumen
from estado.reintentos import _contar_reintento, _limpiar_reintento, _limpiar_reintentos
from sfs.bd import _cerrar_documento_en_sfs, _cerrar_resumen_en_sfs

logger = logging.getLogger(__name__)

# Serializa el barrido de RPTA: watchdog lanza una llamada por cada CDR que aparece
# y todas recorren el directorio completo (ver procesar_respuestas).
_lock_cdr = threading.Lock()

# Se elige acá (y no en config.py) porque el adaptador por defecto deja constancia
# en el log al instanciarse, y para entonces logging ya tiene sus handlers listos
# (los arma config.py, que este módulo importa antes de llegar a esta línea).
_notificador_comprobantes = integraciones.elegir(NOTIFICADOR_COMPROBANTES)


def _notificar_comprobante_aceptado(ruc: str, tipo: str, numeracion: str):
    """
    Avisa a la integración configurada (ver integraciones/README.md) que este
    comprobante —o este resumen diario, si numeracion empieza con 'RC-'— ya quedó
    aceptado por SUNAT y cerrado en la BD de la app.

    Envuelto en su propio try/except a propósito: esta integración es best-effort y
    ajena al flujo de facturación, así que una falla acá (ej. el bucket S3 no
    responde) no puede impedir que el CDR se archive ni que el ciclo siga.
    """
    try:
        _notificador_comprobantes.notificar(ruc, tipo, numeracion)
    except Exception:
        logger.exception(
            "Notificador de comprobantes (%s) falló para %s-%s; no afecta el "
            "cierre del CDR.", NOTIFICADOR_COMPROBANTES, tipo, numeracion,
        )


def _procesar_lineas_de_resumen(conn, numeracion_rc: str, boletas: list, parsed: dict) -> tuple:
    """
    Separa las boletas de un resumen ACEPTADO en limpias/excluidas según el código
    de su propia línea de respuesta. Devuelve (limpias, excluidas).

    Mismo criterio que ya usa parsear_xml_cdr() para el documento entero —"El
    ResponseCode manda: SUNAT solo devuelve 0 cuando acepta"—, aplicado ahora por
    línea: un código de solo ceros es la aceptación limpia; cualquier otro código,
    aunque el CDR lo etiquete como "observación", significa que SUNAT no registró
    esa boleta puntual, aunque sí haya aceptado el resumen que la contenía.
    Marcarla enviado=1 junto con las demás sería declararla aceptada cuando no lo
    está.

    Las excluidas no se pierden: quedan en enviado=0 con el motivo guardado, y
    vuelven a proponerse en un resumen futuro (aplicacion.lecturas.obtener_boletas_para_resumen)
    hasta agotar MAX_REINTENTOS_RECHAZO.
    """
    incluidas = set(boletas)
    codigos = {
        num: (cod, desc) for num, cod, desc in parsed.get("lineas", [])
        # La respuesta del resumen entero no es una observación de línea, y un
        # código de solo ceros es la aceptación limpia.
        if num in incluidas and (cod or "").strip("0") != ""
    }
    if not codigos:
        return boletas, []

    excluidas = [b for b in boletas if b in codigos]
    limpias = [b for b in boletas if b not in codigos]

    agotadas = []
    for num in excluidas:
        cod, desc = codigos[num]
        intentos = _contar_reintento(num, "03", desc or f"código {cod}")
        detalle = (
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Excluida del resumen {numeracion_rc} "
            f"(intento {intentos}/{MAX_REINTENTOS_RECHAZO})"
        )
        if cod:
            detalle += f" — código {cod}"
        if desc:
            detalle += f": {desc}"
        _escribir_bd(_bd().guardar_error, conn, num, detalle[:_MAX_ERRORS_SQL])
        if intentos >= MAX_REINTENTOS_RECHAZO:
            agotadas.append(num)

    logger.warning(
        "Resumen %s aceptado, pero SUNAT devolvió código en %d boleta(s); no se "
        "marcan enviado=1 y quedan pendientes para un resumen futuro: %s",
        numeracion_rc, len(excluidas), ", ".join(excluidas),
    )
    if agotadas:
        logger.error(
            "%d boleta(s) agotaron los %d reenvíos dentro de un resumen y NO se "
            "reincluirán hasta que se corrija el dato observado: %s",
            len(agotadas), MAX_REINTENTOS_RECHAZO, ", ".join(agotadas),
        )
    return limpias, excluidas


def _actualizar_sql_cdr(conn, numeracion: str, parsed: dict) -> bool:
    if not numeracion:
        logger.error(
            "CDR aceptado sin numeración reconocible (código %s): %s — "
            "el comprobante queda en enviado=0.",
            parsed.get("codigo"), parsed.get("descripcion"),
        )
        return False
    if parsed["status"] not in _CDR_ACEPTADOS:
        return False

    if numeracion.startswith(f"{_TIPO_RC}-"):
        # Un resumen no es una fila de Factura: agrupa muchas boletas, así que el
        # cierre es un fan-out a todas las que se guardaron en resumenes.json cuando
        # se generó, no un UPDATE de una sola fila.
        boletas = _boletas_de_resumen(numeracion)
        if not boletas:
            logger.error(
                "CDR aceptado del resumen %s pero no hay boletas registradas para él "
                "en resumenes.json; quedan en enviado=0.", numeracion,
            )
            return False

        # Separa las que SUNAT registró de verdad (limpias) de las que su propia
        # línea vino con código — esas NO se marcan enviado=1 aunque el resumen
        # entero se haya aceptado. Ver _procesar_lineas_de_resumen().
        limpias, excluidas = _procesar_lineas_de_resumen(conn, numeracion, boletas, parsed)
        filas = _escribir_bd(_bd().marcar_enviados, conn, limpias) if limpias else 0
        if limpias and filas == 0:
            logger.error(
                "CDR aceptado del resumen %s pero ninguna de sus boletas coincide en la "
                "BD; quedan en enviado=0.", numeracion,
            )
            return False

        _limpiar_reintento(numeracion)
        _limpiar_reintentos(limpias)
        # Recién ahora el resumen esta terminado de verdad: sus boletas limpias ya
        # quedaron cerradas. Marcarlo antes liberaba las boletas mientras todavia
        # figuraban pendientes, y se generaba otro resumen con ellas.
        _cerrar_resumen_en_sfs(EMISOR_RUC_OVERRIDE, numeracion)
        logger.info(
            "Resumen %s aceptado: %d boleta(s) marcadas enviado=1%s.",
            numeracion, filas,
            f"; {len(excluidas)} quedan pendientes por código de línea" if excluidas else "",
        )
        return True

    # errors se limpia junto con la aceptación: si el comprobante había sido rechazado
    # antes, el motivo viejo ya no aplica.
    filas = _escribir_bd(_bd().marcar_enviado, conn, numeracion)
    if filas > 0:
        _limpiar_reintento(numeracion)
        return True
    if filas == 0:
        # SUNAT aceptó algo que no está en Comprobante: numeración con otro formato,
        # comprobante borrado, o CDR de otro emisor. Silenciarlo dejaba el documento
        # en enviado=0 y el ZIP archivado como procesado, o sea perdido.
        logger.error(
            "CDR aceptado de %s pero ningún comprobante coincide en la BD; "
            "queda en enviado=0.", numeracion,
        )
    return False


def _registrar_error_cdr(conn, numeracion: str, parsed: dict) -> bool:
    """
    Deja el motivo del rechazo en Comprobante.errors. Sin esto el código de SUNAT
    solo vive en facturador.log, y quien mira la BD no tiene forma de saber por qué
    un comprobante sigue en enviado=0.
    """
    if not numeracion:
        return False
    detalle = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] CDR {parsed['status']}"
    if parsed.get("codigo"):
        detalle += f" — código {parsed['codigo']}"
    if parsed.get("descripcion"):
        detalle += f": {parsed['descripcion']}"

    if numeracion.startswith(f"{_TIPO_RC}-"):
        boletas = _boletas_de_resumen(numeracion)
        if not boletas:
            return False
        filas = _escribir_bd(_bd().guardar_error_varios, conn, boletas,
                             detalle[:_MAX_ERRORS_SQL])
        # Aunque haya sido rechazado, el ticket ya se consumio: dejarlo abierto
        # haria que se lo siguiera consultando en vano en cada ciclo. El motivo
        # del rechazo queda en Factura.errors, que es donde se consulta.
        _cerrar_resumen_en_sfs(EMISOR_RUC_OVERRIDE, numeracion)
        return filas > 0

    filas = _escribir_bd(_bd().guardar_error, conn, numeracion,
                         detalle[:_MAX_ERRORS_SQL])
    return filas > 0


def procesar_respuestas():
    """
    Barre RPTA. El lock serializa las llamadas: watchdog dispara una por cada CDR que
    llega y todas recorren el mismo directorio, así que sin esto dos hilos parsean y
    mueven el mismo archivo a la vez. Bloqueante a propósito —el que espera vuelve a
    listar el directorio al entrar— para que ningún CDR quede sin barrer.
    """
    with _lock_cdr:
        _barrer_rpta()


def _barrer_rpta():
    if not os.path.exists(SFS_RPTA_DIR):
        return

    archivos = [
        os.path.join(SFS_RPTA_DIR, f)
        for f in os.listdir(SFS_RPTA_DIR)
        if f.lower().endswith((".zip", ".xml"))
    ]
    if not archivos:
        return

    conn = None
    try:
        conn = conectar_bd()
        ok = err = 0
        for ruta in archivos:
            nombre = os.path.basename(ruta)
            try:
                if not _archivo_estable(ruta):
                    # Lo retoma el barrido periódico de hilo_cdr; no cuenta como error.
                    logger.info("CDR %s aún se está escribiendo; se retoma luego.", nombre)
                    continue
                if ruta.lower().endswith(".zip"):
                    with zipfile.ZipFile(ruta) as z:
                        xml_names = [n for n in z.namelist() if n.lower().endswith(".xml")]
                        if not xml_names:
                            _mover(ruta, DIR_ERRORES); err += 1; continue
                        parsed = parsear_xml_cdr(z.read(xml_names[0]))
                    # No alcanza con completar la numeración cuando falta: el CDR de
                    # una consulta trae una que existe pero no casa con la BD, así que
                    # hay que reconciliarla contra el nombre del archivo.
                    parsed["numeracion"] = _reconciliar_numeracion(parsed["numeracion"], nombre)
                else:
                    parsed = parsear_xml_cdr(ruta)

                num = parsed["numeracion"]
                logger.info("CDR %s | %s [%s]", nombre, num, parsed["status"])

                if parsed["status"] in _CDR_ACEPTADOS:
                    if _actualizar_sql_cdr(conn, num, parsed):
                        ok += 1
                        # Recién con el comprobante ya cerrado en la BD de la
                        # aplicación se limpia la fila del SFS. Al revés —limpiarla
                        # al depositar el CDR— se borraba el error visible aunque el
                        # cierre fallara después, y el comprobante quedaba en
                        # enviado=0 sin que nadie se enterara.
                        ruc_cdr, tipo_cdr = _datos_del_nombre_cdr(nombre)
                        _cerrar_documento_en_sfs(ruc_cdr, tipo_cdr, num)
                        _notificar_comprobante_aceptado(ruc_cdr, tipo_cdr, num)
                        _mover(ruta, DIR_PROCESADOS)
                    else:
                        # Aceptado por SUNAT pero no se pudo cerrar en la BD
                        # (sin numeración o sin fila que coincida). Archivarlo como
                        # procesado lo hacía desaparecer con el comprobante en
                        # enviado=0: va a errores/ para que quede a la vista.
                        _mover(ruta, DIR_ERRORES)
                        err += 1
                else:
                    # Un rechazo no se archiva como procesado: queda en errores/
                    # para revisión manual y el comprobante NO pasa a aceptado.
                    logger.error(
                        "CDR %s de %s (%s) — código %s: %s",
                        parsed["status"], num, nombre, parsed["codigo"], parsed["descripcion"],
                    )
                    if not _registrar_error_cdr(conn, num, parsed):
                        logger.warning(
                            "El motivo del rechazo de %s no se pudo guardar en la BD; "
                            "queda solo en este log.", num,
                        )
                    _mover(ruta, DIR_ERRORES)
                    err += 1

            except zipfile.BadZipFile:
                logger.warning("ZIP corrupto: %s", nombre)
                _mover(ruta, DIR_ERRORES); err += 1
            except Exception:
                logger.exception("Error procesando CDR %s", nombre)
                err += 1

        if ok or err:
            logger.info("CDRs procesados — OK: %d | Errores: %d", ok, err)
    finally:
        if conn:
            conn.close()
