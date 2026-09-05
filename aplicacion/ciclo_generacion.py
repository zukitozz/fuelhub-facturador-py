"""
Caso de uso del hilo Generador: en cada ciclo, lee la BD de la aplicación, genera
los archivos SFS de lo pendiente y los entrega al facturador local.
"""
import logging
import os
import time

from config import (
    SFS_BD_PATH, EMISOR_RUC_OVERRIDE, SOL_USUARIO, SOL_CLAVE,
    _ESTADOS_ERROR, _ESTADOS_BLOQUEADO, _NOMBRE_SITU, _MAX_BLOQUEADOS_LOG,
    MAX_REINTENTOS_RECHAZO, _TIPOS_SFS, ENVIADO_PENDIENTE,
)
from dominio.texto import _texto, _codigo, _marcas
from utilidades_timer import detectar_desfase_bd
from aplicacion.bd_app import _bd, conectar_bd
from aplicacion.lecturas import obtener_emisor, obtener_comprobantes_pendientes
from aplicacion.recuperacion_cdr import recuperar_cdr_pendientes, recuperar_cdr_resumenes
from sfs.api import sincronizar_bandeja_sfs, activar_procesamiento_sfs
from sfs.archivos import procesar_comprobante, generar_resumen_diario
from sfs.bd import _sfs_bd, _registrar_en_sfs_bd, _docs_en_vuelo, _limpiar_data_cerrados, _tiene_cdr, _eliminar_data_files
from sunat.consulta import estado_en_sunat, _guardar_cdr
from estado.reintentos import _reintentos_de, _contar_reintento, _espera_de, _anotar_espera_de_red, _es_falla_de_red

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
                # crédito: se le pregunta a SUNAT antes de tocar nada. Un
                # 'desconocido' NO habilita el reenvío (ver sunat.consulta.estado_en_sunat).
                estado, cdr, mensaje = estado_en_sunat(ruc_emisor, tip_docu, num_docu)
                if estado == "registrado":
                    _guardar_cdr(ruc_emisor, tip_docu, num_docu, cdr, mensaje)
                    continue
                if estado != "no_registrado":
                    cortes, minutos = _anotar_espera_de_red(num_docu, tip_docu, _texto(des_obse))
                    esperando.append((tip_docu, num_docu, minutos))
                    continue
                cortes, minutos = _anotar_espera_de_red(num_docu, tip_docu, _texto(des_obse))
                _bd().marcar_enviado(conn, num_docu, enviado=ENVIADO_PENDIENTE,
                                     limpiar_error=False)
                sfs.execute(
                    f"DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? "
                    f"AND IND_SITU IN ({marcas})",
                    (ruc_emisor, tip_docu, num_docu, *_ESTADOS_ERROR),
                )
                reintentados.append((tip_docu, num_docu, f"corte {cortes}"))
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
                sfs.execute(
                    "UPDATE DOCUMENTO SET IND_SITU='03', DES_OBSE='Aceptado (CDR procesado)' "
                    "WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? AND IND_SITU IN ('01','02')",
                    (ruc_emisor, tip, num),
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

        _reportar_clasificacion(fuera_alcance, omitidos, bloqueados)

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
