"""
Caso de uso: preguntarle a SUNAT por los documentos y resúmenes que el SFS dice
haber enviado pero cuyo CDR nunca volvió — típicamente por un corte de conexión.
Combina la BD del SFS (sfs/bd.py) con la consulta directa a SUNAT (sunat/).
"""
import logging
import time
from datetime import datetime

from config import CONSULTA_SUNAT_TRAS_MIN, _COOLDOWN_CONSULTA_SEG, _TICKET_CON_CDR, _TICKET_EN_PROCESO, _TICKET_NO_EXISTE, HORAS_TICKET_EN_PROCESO, _ESTADOS_RESUMEN_ABIERTO, MAX_CONSULTAS_FALLIDAS
from dominio.cdr import _TIPO_RC
from sfs.bd import (
    _sfs_bd, _docs_enviados_sin_cdr, _tiene_cdr, _eliminar_data_files,
    _resumenes_con_ticket, _cdr_ya_procesado, _cerrar_resumen_en_sfs,
)
from sunat.consulta import estado_en_sunat, _guardar_cdr
from sunat.ticket import consultar_ticket_sunat
from sunat.ticket import _norm_codigo_ticket
from estado.reintentos import (
    _contar_consulta_fallida, _olvidar_consulta_fallida, _horas_en_proceso,
    _olvidar_en_proceso,
)
from estado.resumenes import (
    _olvidar_resumen, _descartar_archivos_de_resumen, _boletas_de_resumen,
)
from dominio.texto import _texto

logger = logging.getLogger(__name__)

# Cada cuánto se puede volver a consultar el mismo documento, para no golpear el
# servicio de SUNAT en cada ciclo por algo que sigue igual.
_ultima_consulta: dict = {}


def recuperar_cdr_pendientes(ruc_emisor: str):
    """
    Para cada comprobante enviado que lleva rato sin CDR, le pregunta a SUNAT.

    Es la salida al caso de la conexión cortada: el SFS mandó el documento pero la
    respuesta nunca volvió, así que nadie sabe si llegó. Si SUNAT lo tiene, su CDR
    queda en RPTA y el hilo CDR lo cierra solo. Si confirma que no lo tiene, se
    borra de la bandeja del SFS para que el próximo ciclo lo regenere y reenvíe.
    """
    ahora = time.monotonic()
    for tip, num, minutos in _docs_enviados_sin_cdr(ruc_emisor):
        if minutos < CONSULTA_SUNAT_TRAS_MIN:
            continue
        if _tiene_cdr(ruc_emisor, tip, num):
            continue  # el CDR ya está en disco, lo levanta el hilo CDR
        previo = _ultima_consulta.get((tip, num))
        if previo is not None and ahora - previo < _COOLDOWN_CONSULTA_SEG:
            continue
        _ultima_consulta[(tip, num)] = ahora

        logger.info(
            "%s-%s lleva %.0f min enviado sin CDR; consultando a SUNAT...", tip, num, minutos
        )
        estado, cdr, mensaje = estado_en_sunat(ruc_emisor, tip, num)
        if estado == "registrado":
            _guardar_cdr(ruc_emisor, tip, num, cdr, mensaje)
        elif estado == "no_registrado":
            # No llegó: se saca de la bandeja para que vuelva a generarse y salir.
            logger.warning(
                "SUNAT no tiene %s-%s: el envío no llegó. Vuelve a la cola.", tip, num
            )
            with _sfs_bd(escritura=True) as sfs:
                sfs.execute(
                    "DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=?",
                    (ruc_emisor, tip, num),
                )
            _eliminar_data_files(f"{ruc_emisor}-{tip}-{num}")
        else:
            logger.warning(
                "No se pudo determinar si SUNAT tiene %s-%s (%s); no se reenvía.",
                tip, num, mensaje,
            )


def recuperar_cdr_resumenes(ruc_emisor: str):
    """
    Consulta el ticket de cada resumen enviado y baja su CDR cuando ya está listo.

    El CDR queda en RPTA y de ahí en adelante el flujo es el de siempre: el hilo
    CDR lo levanta y _actualizar_sql_cdr() lo reparte entre todas las boletas que
    el resumen agrupa.
    """
    ahora = time.monotonic()
    for numeracion, ticket in _resumenes_con_ticket(ruc_emisor):
        if _tiene_cdr(ruc_emisor, _TIPO_RC, numeracion):
            # Con el CDR ya archivado, el resumen deberia estar cerrado en la bandeja
            # —lo cierra _actualizar_sql_cdr() al procesarlo—, pero si por lo que sea
            # no lo esta, nada volveria a moverlo: este continue corta antes de
            # reconsultar, el CDR ya no vuelve a RPTA y el hilo CDR no lo reprocesa,
            # asi que _cerrar_resumen_en_sfs() no llega a correr nunca. El resumen se
            # quedaba en su estado abierto de forma permanente, reconsultandose no
            # —eso lo frena este mismo corte— pero si reportandose como trabado en
            # cada ciclo, diciendo que retiene boletas que ya estan cerradas.
            #
            # Solo con el CDR en procesados/, no en RPTA: que el archivo exista no
            # significa que el hilo CDR ya lo haya repartido entre las boletas, y
            # cerrar antes daria el resumen por bueno con sus boletas todavia en
            # enviado=0.
            if _cdr_ya_procesado(ruc_emisor, _TIPO_RC, numeracion):
                _cerrar_resumen_en_sfs(ruc_emisor, numeracion)
            continue
        previo = _ultima_consulta.get((_TIPO_RC, numeracion))
        if previo is not None and ahora - previo < _COOLDOWN_CONSULTA_SEG:
            continue
        _ultima_consulta[(_TIPO_RC, numeracion)] = ahora

        codigo, mensaje, cdr = consultar_ticket_sunat(ruc_emisor, ticket)
        codigo = _norm_codigo_ticket(codigo)
        if codigo == _TICKET_NO_EXISTE:
            # Definitivo: el ticket se consumió y ya no hay nada que preguntarle a
            # SUNAT. No se reintenta —daría siempre lo mismo— y se reporta, porque
            # sus boletas siguen retenidas y solo una persona puede decidir qué
            # hacer con ellas (ver _reportar_resumenes_trabados).
            logger.error(
                "El ticket %s del resumen %s ya no existe en SUNAT (%s). Sus boletas "
                "siguen retenidas: hay que verificar en el portal si el resumen fue "
                "aceptado antes de tocar nada.",
                ticket, numeracion, mensaje,
            )
            continue
        # La racha se olvida solo ante una respuesta concluyente: el CDR recuperado o
        # el "todavía lo estoy procesando". Antes se olvidaba apenas el código no
        # fuera None, con lo que un fault con código —el 0100, por ejemplo, que es un
        # transitorio documentado de SUNAT— reseteaba la cuenta en cada intento y
        # jamás llegaba al tope: el resumen se reconsultaba para siempre sin que nadie
        # se enterara, que es justo lo que MAX_CONSULTAS_FALLIDAS venía a evitar.
        if codigo == _TICKET_EN_PROCESO:
            # "Todavía lo estoy procesando" no es una falla, así que no gasta el
            # presupuesto de consultas fallidas. Pero tampoco puede repetirse en un
            # INFO tranquilo para siempre: un ticket que dice esto durante 24 horas
            # está muerto del lado de SUNAT, no encolado —verificado el 2026-09-06,
            # cuando otro resumen enviado ese mismo día se proceso en minutos—.
            # Por eso se lleva desde cuándo, y pasado el umbral el aviso escala.
            _olvidar_consulta_fallida(_TIPO_RC, numeracion)
            horas = _horas_en_proceso(numeracion)
            if horas >= HORAS_TICKET_EN_PROCESO:
                logger.error(
                    "El ticket %s del resumen %s lleva %.1f h en 'en proceso' y "
                    "retiene %d boleta(s). REQUIERE REVISIÓN MANUAL: verificar en el "
                    "portal de SUNAT si el resumen se declaró. NO se reenvía solo: ya "
                    "tiene ticket, así que SUNAT lo recibió y reenviarlo declararía "
                    "las mismas boletas dos veces.",
                    ticket, numeracion, horas, len(_boletas_de_resumen(numeracion)),
                )
            else:
                logger.info("SUNAT todavía procesa el resumen %s (ticket %s, %.1f h).",
                            numeracion, ticket, horas)
            continue
        if not (cdr and codigo in _TICKET_CON_CDR):
            # Sin CDR no hay veredicto, venga o no con código: cuenta contra el tope.
            veces = _contar_consulta_fallida(
                _TIPO_RC, numeracion, _texto(codigo) or "sin codigo", mensaje)
            if veces >= MAX_CONSULTAS_FALLIDAS:
                logger.error(
                    "El ticket %s del resumen %s lleva %d consultas sin respuesta útil "
                    "(%s: %s). REQUIERE REVISIÓN MANUAL: sus boletas siguen retenidas.",
                    ticket, numeracion, veces, _texto(codigo) or "sin código", mensaje,
                )
            else:
                logger.info(
                    "Ticket %s de %s: sin respuesta útil (%s: %s); se reintenta (%d/%d).",
                    ticket, numeracion, _texto(codigo) or "sin código", mensaje,
                    veces, MAX_CONSULTAS_FALLIDAS,
                )
            continue
        _olvidar_consulta_fallida(_TIPO_RC, numeracion)
        # Llegó el CDR: si venía de una racha de "en proceso", esa cuenta ya no importa.
        _olvidar_en_proceso(numeracion)
        # Vale tanto para el aceptado como para el rechazado: el parser del CDR
        # decide cuál es, igual que con cualquier otro comprobante.
        # El resumen NO se cierra acá: recién cuando el hilo CDR termine de
        # procesarlo. Cerrarlo al bajarlo dejaba un hueco de segundos en el que
        # el resumen ya figuraba cerrado —y por lo tanto sus boletas libres—
        # pero todavía no estaban en enviado=true, así que el ciclo siguiente
        # las tomaba y armaba otro resumen con las mismas.
        _guardar_cdr(ruc_emisor, _TIPO_RC, numeracion, cdr, f"ticket {ticket}: {mensaje}")
