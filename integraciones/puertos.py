"""
Puertos de las integraciones salientes: lo que el daemon pide, sin saber quién lo
implementa. Ver integraciones/README.md para el contrato completo.
"""
from typing import Protocol


class NotificadorComprobanteAceptado(Protocol):
    """
    Se invoca una vez que un comprobante (o un resumen diario de boletas) ya quedó
    aceptado por SUNAT y cerrado en la BD de la app — el mismo punto donde hoy el
    daemon archiva el CDR en RPTA/procesados/. Sirve para lo que necesite reaccionar
    a esa aceptación sin conocer nada del SFS ni de SUNAT (ej. subir el PDF a S3).

    Debe ser best-effort: una excepción acá no debe impedir que el daemon archive el
    CDR ni que el ciclo siga. Quien llama a este puerto ya se encarga de eso.
    """

    def notificar(self, ruc: str, tipo: str, numeracion: str) -> None: ...
