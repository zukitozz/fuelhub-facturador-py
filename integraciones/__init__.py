"""
Integraciones salientes del daemon: lo que se dispara cuando un comprobante queda
aceptado por SUNAT, más allá del propio flujo de facturación (ej. subir el PDF a S3,
avisar a otro sistema). Mismo patrón que repositorio/: un puerto por operación
(puertos.py) y un adaptador por destino posible, elegido por configuración en vez de
por una segunda variable que pueda quedar en desacuerdo.

Para agregar un destino nuevo: un archivo acá con una clase que implemente el puerto
que corresponda, y una entrada en _ADAPTADORES. Ver README.md para el contrato.
"""
from . import noop

_ADAPTADORES = {
    "noop": noop.NotificadorNoOp,
    # "s3": ...   se suma cuando exista el adaptador de subida a S3.
}


def elegir(nombre: str):
    """
    El adaptador de NotificadorComprobanteAceptado que indique NOTIFICADOR_COMPROBANTES.
    Un nombre no reconocido cae a "noop" en vez de reventar el daemon: esta integración
    es best-effort y un typo en el .env no puede tumbar la emisión de comprobantes.
    """
    import logging
    clase = _ADAPTADORES.get((nombre or "").strip().lower())
    if clase is None:
        logging.getLogger(__name__).warning(
            "NOTIFICADOR_COMPROBANTES=%r no reconocido (opciones: %s); se usa 'noop'.",
            nombre, ", ".join(sorted(_ADAPTADORES)),
        )
        clase = noop.NotificadorNoOp
    return clase()
