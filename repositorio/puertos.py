"""
El contrato que debe cumplir cualquier adaptador de repositorio/ (ver README.md para
el detalle completo: las catorce claves de `pendientes()`, cuáles son obligatorias,
las cuatro diferencias entre motores). Este archivo no cambia nada en tiempo de
ejecución —repositorio.elegir() sigue devolviendo el módulo tal cual—; es la versión
en código de lo que el README ya describe en prosa, para que un adaptador nuevo tenga
dónde chequear la forma exacta de cada operación.

Cada adaptador (postgres.py, sqlserver.py) es un MÓDULO que expone estas diez
funciones a nivel de archivo, no una clase con instancias — por eso el puerto se
declara con @staticmethod en cada método: así el Protocol describe fielmente
"conectar(url, timeout)", sin un "self" que el módulo real nunca tiene.
"""
from typing import Protocol


class RepositorioComprobantes(Protocol):
    @staticmethod
    def conectar(url: str, timeout: int): ...

    @staticmethod
    def reloj(conn) -> list[dict]:
        """[{"con_zona": datetime, ...}]: la hora de pared del servidor de BD."""
        ...

    @staticmethod
    def emisor(conn) -> str:
        """Razón social del emisor. El RUC sale de EMISOR_RUC en el .env."""
        ...

    @staticmethod
    def receptor(conn, comprobante_id) -> dict:
        """{"tipo_documento", "numero_documento", "razon_social"}, o {} si no hay."""
        ...

    @staticmethod
    def items(conn, comprobante_id) -> list[dict]:
        ...

    @staticmethod
    def pendientes(conn) -> list[dict]:
        """
        Las catorce claves documentadas en README.md, sin distinguir tipo de
        comprobante ni filtrar filas incompletas — eso lo hace el daemon.
        """
        ...

    @staticmethod
    def marcar_enviados(conn, numeraciones: list, limpiar_error: bool = True) -> int:
        """Cierra en lote (un resumen diario). Devuelve las filas afectadas."""
        ...

    @staticmethod
    def marcar_enviado(conn, numeracion: str, enviado: bool = True, limpiar_error: bool = True) -> int:
        ...

    @staticmethod
    def guardar_error(conn, numeracion: str, detalle: str) -> int:
        ...

    @staticmethod
    def guardar_error_varios(conn, numeraciones: list, detalle: str) -> int:
        ...
