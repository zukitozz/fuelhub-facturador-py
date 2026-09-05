"""
Adaptador por defecto de NotificadorComprobanteAceptado: no hace nada.

Es lo que corre mientras no haya un destino real configurado (NOTIFICADOR_COMPROBANTES
sin definir o en "noop"). Deja constancia una sola vez, al elegirse, y no en cada
comprobante: por comprobante ensuciaría facturador.log sin aportar nada, ya que "no
hace nada" es siempre el mismo resultado.
"""
import logging

logger = logging.getLogger(__name__)


class NotificadorNoOp:
    def __init__(self):
        logger.info("Notificador de comprobantes: ninguno configurado (NOOP); no se notifica nada.")

    def notificar(self, ruc: str, tipo: str, numeracion: str) -> None:
        pass
