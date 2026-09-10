"""
Caso de uso del hilo Generador: en cada ciclo, lee la BD de la aplicación, genera
los archivos SFS de lo pendiente y los entrega al facturador local.
"""
import logging
import os
import time
from datetime import datetime

from config import (
    SFS_BD_PATH, EMISOR_RUC_OVERRIDE, SOL_USUARIO, SOL_CLAVE,
    _ESTADOS_ERROR, _ESTADOS_BLOQUEADO, _NOMBRE_SITU, _MAX_BLOQUEADOS_LOG,
    MAX_REINTENTOS_RECHAZO, _TIPOS_SFS, ENVIADO_PENDIENTE,
    _RESUMENES_PATH, _ESTADOS_CERRADOS, DIAS_RETENCION_RESUMENES, _GRACIA_REGISTRO_RC_SEG, HORAS_TICKET_EN_PROCESO, _MAX_DES_OBSE,
)
from dominio.texto import _texto, _codigo, _marcas
from utilidades_timer import detectar_desfase_bd
from aplicacion.bd_app import _bd, conectar_bd
from aplicacion.lecturas import obtener_emisor, obtener_comprobantes_pendientes
from aplicacion.recuperacion_cdr import recuperar_cdr_pendientes, recuperar_cdr_resumenes
from sfs.api import sincronizar_bandeja_sfs, activar_procesamiento_sfs
from sfs.archivos import procesar_comprobante, generar_resumen_diario
from sfs.bd import (
    _sfs_bd, _registrar_en_sfs_bd, _docs_en_vuelo, _limpiar_data_cerrados, _tiene_cdr,
    _eliminar_data_files, _ticket_de_resumen, _veredicto_archivado,
)
from sunat.consulta import estado_en_sunat, _guardar_cdr
from estado.reintentos import _reintentos_de, _contar_reintento, _espera_de, _anotar_espera_de_red, _es_falla_de_red
from estado.resumenes import (
    _leer_resumenes, _guardar_resumenes, _lock_resumenes, _olvidar_resumen,
    _resumen_vencido, _descartar_archivos_de_resumen, _boletas_de_resumen,
)
from dominio.cdr import _TIPO_RC

logger = logging.getLogger(__name__)


def resetear_rechazados(conn, ruc_emisor: str):
    if not os.path.exists(SFS_BD_PATH):
        return
    # Placeholders dinámicos: _ESTADOS_ERROR puede tener uno o varios estados.
    marcas = _marcas(len(_ESTADOS_ERROR))
    with _sfs_bd(escritura=True) as sfs:
        rows = sfs.execute(
            f"SELECT NUM_DOCU, TIP_DOCU, DES_OBSE FROM DOCUMENTO "
            f"WHERE NUM_RUC=? AND IND_SITU IN ({marcas})",
            (ruc_emisor, *_ESTADOS_ERROR),
        ).fetchall()
        if not rows:
            return
        reintentados, agotados, esperando = [], [], []
        for num_docu, tip_docu, des_obse in rows:
            # Un '06' de red es otra cosa que un rechazo: el comprobante nunca llegó a
            # SUNAT, los datos están bien, y el mismo envío funciona apenas vuelve el
            # servicio. Gastarle presupuesto de reintentos lo bloqueaba en menos de 5
            # minutos frente a un corte de horas (produccion, 2026-08-29: 7 facturas
            # bloqueadas por un corte de 2 h, todas aceptadas despues sin tocarles un
            # dato). Se reencola sin tope, espaciado, y con una salvaguarda antes de
            # reenviar.
            if _es_falla_de_red(des_obse):
                falta = _espera_de(num_docu) - time.time()
                if falta > 0:
                    esperando.append((tip_docu, num_docu, falta / 60))
                    continue
                # Un "no se pudo enviar" no distingue entre "nunca salió" y "salió y
                # la respuesta se perdió". Reenviar el segundo caso duplica el
                # comprobante ante SUNAT, y eso solo se deshace con una nota de
                # crédito, así que hace falta una evidencia antes de tocar nada.
                #
                # Para un resumen esa evidencia NO es estado_en_sunat(): esa consulta
                # va contra billConsultService, que solo acepta comprobantes
                # individuales, y con tip_docu='RC' SUNAT contesta "0009: EL tipo de
                # comprobante debe de ser (01, 07, 08, ...)". RC no está en esa lista
                # y nunca va a estarlo, así que la respuesta no dice nada del
                # documento y el resumen quedaba reintentando una consulta imposible
                # para siempre —RC-20260906-016 acumuló 54 consultas así, con sus
                # boletas sin declarar—.
                #
                # La evidencia para un resumen es el ticket: lo escribe el SFS cuando
                # SUNAT responde a sendSummary, así que su ausencia significa que el
                # envío no llegó. Es el equivalente del 0127 que autoriza a reenviar
                # un comprobante suelto. Al revés, un resumen CON ticket ya fue
                # recibido y no se reenvía por acá: lo resuelve
                # recuperar_cdr_resumenes() consultando ese ticket.
                if tip_docu == _TIPO_RC:
                    if _ticket_de_resumen(ruc_emisor, num_docu):
                        cortes, minutos = _anotar_espera_de_red(
                            num_docu, tip_docu, _texto(des_obse))
                        esperando.append((tip_docu, num_docu, minutos))
                        continue
                    # Sin ticket: SUNAT no lo recibió y se puede volver a armar.
                else:
                    estado, cdr, mensaje = estado_en_sunat(ruc_emisor, tip_docu, num_docu)
                    if estado == "registrado":
                        _guardar_cdr(ruc_emisor, tip_docu, num_docu, cdr, mensaje)
                        continue
                    if estado != "no_registrado":
                        cortes, minutos = _anotar_espera_de_red(
                            num_docu, tip_docu, _texto(des_obse))
                        esperando.append((tip_docu, num_docu, minutos))
                        continue
                cortes, minutos = _anotar_espera_de_red(num_docu, tip_docu, _texto(des_obse))
                if tip_docu == _TIPO_RC:
                    # Un resumen no es una fila de Comprobantes: marcar_enviado() con
                    # su numeración no matchea nada. Lo que hay que devolver a la cola
                    # son sus boletas, y para eso alcanza con olvidar el resumen —el
                    # ciclo siguiente las reagrupa en uno nuevo—. Sin esto, borrar la
                    # fila del SFS dejaba al resumen sin rastro y sus boletas seguían
                    # retenidas por algo que ya no existía.
                    boletas_libres = _olvidar_resumen(num_docu)
                    _descartar_archivos_de_resumen(ruc_emisor, num_docu)
                    logger.warning(
                        "El resumen %s no llegó a obtener ticket, así que SUNAT no lo "
                        "recibió: se descarta y sus %d boleta(s) vuelven a la cola "
                        "para armar uno nuevo.", num_docu, len(boletas_libres),
                    )
                else:
                    _bd().marcar_enviado(conn, num_docu, enviado=ENVIADO_PENDIENTE,
                                         limpiar_error=False)
                sfs.execute(
                    f"DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? "
                    f"AND IND_SITU IN ({marcas})",
                    (ruc_emisor, tip_docu, num_docu, *_ESTADOS_ERROR),
                )
                reintentados.append((tip_docu, num_docu, f"corte {cortes}"))
                continue

            # De acá para abajo el motivo NO es de red: un rechazo real, o —lo que
            # importa— uno que todavía no sabemos leer. La lista de _SENALES_DE_RED es
            # corta a propósito, así que esta rama es donde cae lo desconocido, y para
            # un resumen era un pozo: marcar_enviado() con una numeración RC no matchea
            # ninguna fila de Comprobantes, el DELETE lo sacaba de la bandeja, y como
            # nadie llamaba a _olvidar_resumen() su entrada quedaba sin marcar. El
            # resumen desaparecía de todos lados menos de resumenes.json, donde seguía
            # reteniendo sus boletas para siempre. Producción, 2026-09-10: 6 resúmenes
            # así, 1195 boletas del día anterior retenidas por algo que ya no existía.
            #
            # Se decide con el ticket, el mismo criterio que la rama de red de arriba.
            if tip_docu == _TIPO_RC:
                if _ticket_de_resumen(ruc_emisor, num_docu):
                    # SUNAT ya lo recibió: su CDR llega detrás de ese ticket y lo
                    # resuelve recuperar_cdr_resumenes(). El ticket se lee de esta misma
                    # fila, así que borrarla —lo que hacía antes— dejaba al resumen sin
                    # nada que consultar y sin forma de cerrarse nunca.
                    esperando.append((tip_docu, num_docu, 0))
                    continue
                boletas_libres = _olvidar_resumen(num_docu)
                _descartar_archivos_de_resumen(ruc_emisor, num_docu)
                logger.warning(
                    "El resumen %s quedó en error sin llegar a obtener ticket (%s). "
                    "SUNAT no lo recibió: se descarta y sus %d boleta(s) vuelven a la "
                    "cola para armar uno nuevo.",
                    num_docu, _texto(des_obse)[:120], len(boletas_libres),
                )
                sfs.execute(
                    f"DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? "
                    f"AND IND_SITU IN ({marcas})",
                    (ruc_emisor, tip_docu, num_docu, *_ESTADOS_ERROR),
                )
                reintentados.append((tip_docu, num_docu, "resumen descartado"))
                continue

            # Se consulta antes de sumar: al agotarse, el documento se queda en '10'
            # y esta rama corre en cada ciclo. Sumando siempre, el contador crecería
            # sin sentido y reescribiría el archivo cada 60 segundos para siempre.
            if _reintentos_de(num_docu) >= MAX_REINTENTOS_RECHAZO:
                # Se deja la fila en DOCUMENTO: así el comprobante sigue contando como
                # "en vuelo" —no se regenera— y el reporte de bloqueados lo levanta.
                agotados.append((tip_docu, num_docu))
                continue
            intentos = f"{_contar_reintento(num_docu, tip_docu, _texto(des_obse))}/{MAX_REINTENTOS_RECHAZO}"
            # Vuelve a la cola sin tocar errors: el motivo del rechazo tiene que
            # seguir a la vista mientras se reintenta.
            _bd().marcar_enviado(conn, num_docu, enviado=ENVIADO_PENDIENTE,
                                 limpiar_error=False)
            sfs.execute(
                f"DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? "
                f"AND IND_SITU IN ({marcas})",
                (ruc_emisor, tip_docu, num_docu, *_ESTADOS_ERROR),
            )
            reintentados.append((tip_docu, num_docu, intentos))

    if reintentados:
        logger.info(
            "%d comprobante(s) vuelven a la cola: %s",
            len(reintentados),
            ", ".join(f"{t}-{n} ({i})" for t, n, i in reintentados),
        )
    if esperando:
        # A nivel INFO y con su propio texto: estos NO requieren que nadie haga nada,
        # solo que vuelva el servicio. Mezclarlos con los bloqueados mandaba a buscar
        # una corrección manual que no existía.
        logger.info(
            "%d comprobante(s) esperando que vuelva SUNAT; reintentan solos: %s",
            len(esperando),
            ", ".join(f"{t}-{n} (en {m:.0f} min)" for t, n, m in esperando),
        )
    if agotados:
        # Reenviar de nuevo daría el mismo rechazo: los datos son idénticos. Solo
        # llegan acá los que NO son falla de red — esos no gastan presupuesto.
        logger.error(
            "%d comprobante(s) agotaron los %d reenvíos permitidos y NO se reenviarán "
            "hasta que se corrija el dato observado: %s",
            len(agotados), MAX_REINTENTOS_RECHAZO,
            ", ".join(f"{t}-{n}" for t, n in agotados),
        )


def _activar_pendientes_sfs_bd(ruc_emisor: str, ya_procesados: list):
    if not os.path.exists(SFS_BD_PATH):
        return
    ya_keys = {(d["tip_docu"], d["num_docu"]) for d in ya_procesados}
    with _sfs_bd(escritura=True) as sfs:
        tipos = sorted(_TIPOS_SFS)
        rows = sfs.execute(
            "SELECT TIP_DOCU, NUM_DOCU, NOM_ARCH FROM DOCUMENTO "
            f"WHERE NUM_RUC=? AND TIP_DOCU IN ({_marcas(len(tipos))}) "
            "AND IND_SITU IN ('01','02')",
            (ruc_emisor, *tipos),
        ).fetchall()
        docs_extra = []
        for tip, num, nom_arch in rows:
            if (tip, num) in ya_keys:
                continue
            if _tiene_cdr(ruc_emisor, tip, num):
                # Se cierra porque el CDR existe, no porque diga que fue aceptado: hay
                # que leerlo para no rotular "Aceptado" algo que SUNAT rechazó.
                sfs.execute(
                    "UPDATE DOCUMENTO SET IND_SITU='03', DES_OBSE=? "
                    "WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? AND IND_SITU IN ('01','02')",
                    (_veredicto_archivado(ruc_emisor, tip, num)[:_MAX_DES_OBSE],
                     ruc_emisor, tip, num),
                )
                _eliminar_data_files(nom_arch or f"{ruc_emisor}-{tip}-{num}")
                continue
            docs_extra.append({"num_ruc": ruc_emisor, "tip_docu": tip, "num_docu": num})
    if docs_extra:
        logger.info("%d doc(s) en SFS BD pendientes de activar.", len(docs_extra))
        activar_procesamiento_sfs(docs_extra)


def _reportar_clasificacion(fuera_alcance: int, omitidos: int, bloqueados: list):
    """Resume en el log qué pasó con los pendientes que no se generaron este ciclo."""
    if fuera_alcance:
        logger.info(
            "%d comprobante(s) de tipo fuera de alcance (solo se emiten %s).",
            fuera_alcance, ", ".join(sorted(_TIPOS_SFS)),
        )
    if omitidos:
        logger.info("%d comprobante(s) ya entregados al SFS, esperando CDR.", omitidos)
    if not bloqueados:
        return
    # No son "esperando CDR": el SFS nunca los mandó y no lo va a hacer solo.
    logger.warning(
        "%d comprobante(s) BLOQUEADOS en el SFS — requieren intervención manual:",
        len(bloqueados),
    )
    for tip, num, situ, obse in bloqueados[:_MAX_BLOQUEADOS_LOG]:
        logger.warning("    %s-%s [%s]: %s", tip, num, _NOMBRE_SITU.get(situ, situ), obse or "sin detalle")
    if len(bloqueados) > _MAX_BLOQUEADOS_LOG:
        logger.warning("    ... y %d más.", len(bloqueados) - _MAX_BLOQUEADOS_LOG)


def ciclo_generacion():
    logger.info("Consultando BD...")
    conn = None
    try:
        conn = conectar_bd()
        emisor = obtener_emisor(conn)
        if not emisor:
            logger.error("No se encontró información del Emisor en BD.")
            return

        ruc_emisor = EMISOR_RUC_OVERRIDE or _texto(emisor.get("ruc"), "00000000000")

        # Antes de leer una sola fecha: medir con qué reloj las guarda la aplicación.
        # De esto depende qué día se le declara a SUNAT (ver utilidades_timer.detectar_desfase_bd).
        detectar_desfase_bd(conn)

        # Primero que el SFS relea DATA: todo lo que sigue —qué está en vuelo, qué
        # falta activar, qué se puede limpiar— se decide mirando su bandeja, y sin
        # esto no refleja lo que quedó escrito en ciclos anteriores.
        sincronizar_bandeja_sfs()

        resetear_rechazados(conn, ruc_emisor)

        # Antes de decidir qué generar: si algo se envió y su CDR nunca volvió
        # —típicamente por un corte de conexión—, preguntarle a SUNAT si lo tiene.
        # Y los resúmenes ya enviados esperan su CDR detrás de un ticket, que hay
        # que consultar aparte: SUNAT no lo devuelve en el momento del envío.
        if SOL_USUARIO and SOL_CLAVE:
            recuperar_cdr_pendientes(ruc_emisor)
            recuperar_cdr_resumenes(ruc_emisor)

        comprobantes = obtener_comprobantes_pendientes(conn)
        logger.info("%d comprobante(s) pendiente(s).", len(comprobantes))

        en_vuelo = _docs_en_vuelo(ruc_emisor)
        docs_generados = []
        bloqueados = []
        omitidos = fuera_alcance = 0
        for comp in comprobantes:
            try:
                tip = _codigo(comp.get("tipo_comprobante"), "01")
                num = _texto(comp.get("numeracion_comprobante"))
                # Fuera de alcance: se descarta acá para no regenerar sus archivos
                # en cada ciclo, ya que nunca van a entrar al SFS.
                if tip not in _TIPOS_SFS:
                    fuera_alcance += 1
                    continue
                # Sigue en enviado=0 pero el SFS ya lo tiene: no reenviar. Puede estar
                # esperando CDR o trabado en un estado que el daemon no resuelve solo.
                if (tip, num) in en_vuelo:
                    situ, obse = en_vuelo[(tip, num)]
                    # Un '06' de red no está trabado: lo reintenta resetear_rechazados()
                    # solo, apenas vuelva el servicio. Reportarlo como BLOQUEADO mandaba
                    # a buscar una corrección manual que no hacía falta.
                    if situ in _ESTADOS_BLOQUEADO and not _es_falla_de_red(obse):
                        bloqueados.append((tip, num, situ, obse))
                    else:
                        omitidos += 1
                    continue
                if procesar_comprobante(conn, comp, ruc_emisor):
                    docs_generados.append({
                        "num_ruc":  ruc_emisor,
                        "tip_docu": tip,
                        "num_docu": num,
                    })
            except Exception:
                logger.exception("Error procesando %r", comp.get("numeracion_comprobante"))

        # Las boletas no entran al loop de arriba (ver aplicacion/lecturas.py):
        # se agrupan acá en un resumen diario, que de ahí en más sigue el mismo
        # camino que cualquier otro documento (activar_procesamiento_sfs, etc.).
        try:
            resumen_doc = generar_resumen_diario(conn, ruc_emisor)
            if resumen_doc:
                docs_generados.append(resumen_doc)
        except Exception:
            logger.exception("Error generando el resumen diario de boletas")

        # Mantenimiento de resumenes.json, no parte del flujo: se hace después de
        # generar para que una falla acá nunca impida emitir un resumen, y por su
        # cuenta corre una sola vez al día (ver _podar_resumenes).
        try:
            _podar_resumenes(conn)
        except Exception:
            logger.exception("Error podando %s", os.path.basename(_RESUMENES_PATH))

        _reportar_clasificacion(fuera_alcance, omitidos, bloqueados)
        # Va aparte porque los resúmenes no son filas de Comprobantes y por eso nunca
        # entran en la lista de bloqueados que arma el bucle de arriba.
        _reportar_resumenes_trabados(ruc_emisor)

        if docs_generados:
            logger.info("%d comprobante(s) generados, entregando al SFS...", len(docs_generados))
            # Solo se registra lo que el SFS confirmó; lo demás sigue en enviado=0
            # y se reintenta en el próximo ciclo.
            enviados = activar_procesamiento_sfs(docs_generados)
            _registrar_en_sfs_bd(ruc_emisor, enviados)

            no_enviados = len(docs_generados) - len(enviados)
            if no_enviados:
                logger.warning(
                    "%d comprobante(s) no llegaron al SFS; se reintentan en el próximo ciclo.",
                    no_enviados,
                )

        _activar_pendientes_sfs_bd(ruc_emisor, docs_generados)

        # Se relee el estado en vez de reusar el de arriba: lo entregado en este mismo
        # ciclo pudo cerrarse ya, y así sus archivos no esperan al ciclo siguiente.
        cerrados = _limpiar_data_cerrados(ruc_emisor, _docs_en_vuelo(ruc_emisor))
        if cerrados:
            logger.info("%d comprobante(s) cerrados; archivos de DATA eliminados.", cerrados)

    except Exception:
        logger.exception("Error en ciclo_generacion")
    finally:
        if conn:
            conn.close()


def _numeraciones_pendientes(conn, numeraciones) -> set:
    """Cuáles de estas numeraciones siguen en enviado=0."""
    buscadas = set(numeraciones)
    return {n for f in _bd().pendientes(conn)
            if (n := _texto(f.get("numeracion_comprobante"))) in buscadas}


def _podar_resumenes(conn):
    """
    Saca de resumenes.json las entradas ya resueltas que pasaron el margen.

    Hasta el 2026-09-09 nada podaba ese archivo: la única eliminación era el pop() de
    _olvidar_resumen(), que es justamente lo que este arreglo dejó de hacer. Sin una
    política de retención el archivo solo crece —199 KB y 8.432 referencias a boletas
    al momento del incidente, con entradas de 11 días atrás que ya no cumplían ninguna
    función—.

    Corre una vez por día porque es mantenimiento, no parte del flujo: así la consulta
    de pendientes que necesita no se repite en cada ciclo.

    Dos entradas nunca se podan, por viejas que sean: una cuyo resumen siga sin
    resolverse (ver _resumen_vencido) y una cuyas boletas sigan en enviado=0 aunque el
    resumen figure cerrado —pasa cuando SUNAT observó alguna línea y esa boleta quedó
    afuera, y su mapeo es lo que permite entender por qué—.
    """
    ahora = datetime.now()
    datos = _leer_resumenes()
    try:
        ultima = datetime.strptime(_texto(datos.get("ultima_poda")), "%Y-%m-%d %H:%M:%S")
        if (ahora - ultima).total_seconds() < 24 * 3600:
            return
    except ValueError:
        pass   # nunca se podó, o la marca está ilegible: se poda ahora

    vencidas = {n: e for n, e in (datos.get("resumenes") or {}).items()
                if _resumen_vencido(n, e, ahora)}
    pendientes = _numeraciones_pendientes(
        conn, {b for e in vencidas.values() for b in e.get("boletas", [])},
    ) if vencidas else set()

    podadas, retenidas = [], []
    with _lock_resumenes:
        datos = _leer_resumenes()      # releer bajo el lock: el ciclo pudo tocarlo
        resumenes = datos.setdefault("resumenes", {})
        for numeracion_rc in vencidas:
            entrada = resumenes.get(numeracion_rc)
            if entrada is None:
                continue
            if any(b in pendientes for b in entrada.get("boletas", [])):
                retenidas.append(numeracion_rc)
                continue
            resumenes.pop(numeracion_rc, None)
            podadas.append(numeracion_rc)
        datos["ultima_poda"] = ahora.strftime("%Y-%m-%d %H:%M:%S")
        _guardar_resumenes(datos)

    if podadas:
        logger.info(
            "Poda de %s: se sacaron %d resumen(es) resueltos de mas de %d dias; "
            "quedan %d.", os.path.basename(_RESUMENES_PATH), len(podadas),
            DIAS_RETENCION_RESUMENES, len(resumenes),
        )
    if retenidas:
        logger.warning(
            "%d resumen(es) viejos se conservan porque todavia tienen boletas en "
            "enviado=0: %s", len(retenidas), ", ".join(retenidas[:_MAX_BLOQUEADOS_LOG]),
        )


def _nombre_situ_rc(situ: str) -> str:
    """
    Nombre del estado tal como se lee en un resumen, no en un comprobante.

    _NOMBRE_SITU traduce el '05' como "anulado", que es lo que significa para una
    factura. En un resumen ese estado lo deja el SFS cuando la consulta del ticket
    no le sirvio, y nada se anulo: mostrar "anulado" manda a buscar algo que no
    paso, justo en el aviso que existe para orientar a quien lo lee.
    """
    if situ == "05":
        return "05, consulta del ticket sin resolver"
    return _NOMBRE_SITU.get(situ, situ)


def _reportar_resumenes_trabados(ruc_emisor: str):
    """
    Avisa por cada resumen que retiene boletas y no termina de resolverse.

    _avisar_resumen_sin_rastro() ya cubre el resumen que desaparecio de la bandeja
    del SFS, pero no el que sigue ahi en un estado que no avanza. Ese caso no
    generaba una sola linea: los resumenes no son filas de Comprobantes, asi que
    nunca llegan al bloque de BLOQUEADOS que arma el ciclo, y el unico rastro era el
    conteo de pendientes sin nada que lo explicara.

    Es exactamente lo que dejo pasar el incidente del 2026-09-06: siete resumenes
    trabados retuvieron 1239 boletas durante 12 horas sin un solo WARNING.

    Se avisa desde _GRACIA_REGISTRO_RC_SEG para no gritar por un resumen que acaba de
    generarse y todavia esta en curso normal.
    """
    en_vuelo = _docs_en_vuelo(ruc_emisor)
    trabados = []
    for numeracion_rc, entrada in _leer_resumenes().get("resumenes", {}).items():
        situ, obse = en_vuelo.get((_TIPO_RC, numeracion_rc), ("", ""))
        if not situ or situ in _ESTADOS_CERRADOS:
            continue          # sin rastro lo cubre _avisar_resumen_sin_rastro; cerrado no molesta
        try:
            generado = datetime.strptime(entrada.get("generado", ""), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        horas = (datetime.now() - generado).total_seconds() / 3600
        if horas * 3600 < _GRACIA_REGISTRO_RC_SEG:
            continue
        trabados.append((numeracion_rc, situ, len(entrada.get("boletas", [])), horas, obse))

    if not trabados:
        return
    retenidas = sum(t[2] for t in trabados)
    logger.warning(
        "%d resumen(es) sin resolverse retienen %d boleta(s), que no se pueden "
        "reagrupar hasta que se cierren:", len(trabados), retenidas,
    )
    for numeracion_rc, situ, cuantas, horas, obse in sorted(trabados, key=lambda t: -t[3]):
        logger.warning(
            "    %s [%s] — %d boleta(s), %.1f h sin cerrar: %s",
            numeracion_rc, _nombre_situ_rc(situ), cuantas, horas, obse or "sin detalle",
        )
