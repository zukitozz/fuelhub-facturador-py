"""
Caso de uso: preguntarle a SUNAT por los documentos y resúmenes que el SFS dice
haber enviado pero cuyo CDR nunca volvió — típicamente por un corte de conexión.
Combina la BD del SFS (sfs/bd.py) con la consulta directa a SUNAT (sunat/).
"""
import logging
import time

from config import CONSULTA_SUNAT_TRAS_MIN, _COOLDOWN_CONSULTA_SEG, _TICKET_CON_CDR, _TICKET_EN_PROCESO
from dominio.cdr import _TIPO_RC
from sfs.bd import _sfs_bd, _docs_enviados_sin_cdr, _tiene_cdr, _eliminar_data_files, _resumenes_con_ticket
from sunat.consulta import estado_en_sunat, _guardar_cdr
from sunat.ticket import consultar_ticket_sunat

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
    CDR lo levanta y aplicacion.ciclo_cdr._actualizar_sql_cdr() lo reparte entre
    todas las boletas que el resumen agrupa.
    """
    ahora = time.monotonic()
    for numeracion, ticket in _resumenes_con_ticket(ruc_emisor):
        if _tiene_cdr(ruc_emisor, _TIPO_RC, numeracion):
            continue
        previo = _ultima_consulta.get((_TIPO_RC, numeracion))
        if previo is not None and ahora - previo < _COOLDOWN_CONSULTA_SEG:
            continue
        _ultima_consulta[(_TIPO_RC, numeracion)] = ahora

        codigo, mensaje, cdr = consultar_ticket_sunat(ruc_emisor, ticket)
        if codigo is None:
            logger.info("Ticket %s de %s: sin respuesta útil (%s); se reintenta.",
                        ticket, numeracion, mensaje)
            continue
        if codigo == _TICKET_EN_PROCESO:
            logger.info("SUNAT todavía procesa el resumen %s (ticket %s).", numeracion, ticket)
            continue
        if cdr and codigo in _TICKET_CON_CDR:
            # Vale tanto para el aceptado como para el rechazado: el parser del CDR
            # decide cuál es, igual que con cualquier otro comprobante.
            # El resumen NO se cierra acá: recién cuando el hilo CDR termine de
            # procesarlo. Cerrarlo al bajarlo dejaba un hueco de segundos en el que
            # el resumen ya figuraba cerrado —y por lo tanto sus boletas libres—
            # pero todavía no estaban en enviado=true, así que el ciclo siguiente
            # las tomaba y armaba otro resumen con las mismas.
            _guardar_cdr(ruc_emisor, _TIPO_RC, numeracion, cdr, f"ticket {ticket}: {mensaje}")
        else:
            logger.warning(
                "Ticket %s de %s devolvió el código %s sin CDR (%s); se reintenta.",
                ticket, numeracion, codigo, mensaje,
            )
