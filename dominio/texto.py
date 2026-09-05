"""Primitivas de texto: limpiar valores de BD y traducir el tipo de comprobante."""
import re

# La aplicación guarda el tipo por nombre, no con el código de SUNAT. NOTA_VENTA no
# es un comprobante electrónico —es un documento interno— y por eso no se mapea:
# queda fuera y quien la reciba la ignora.
_TIPOS_POR_NOMBRE = {
    "FACTURA":        "01",
    "BOLETA":         "03",
    "NOTA_CREDITO":   "07",
    "NOTA_DEBITO":    "08",
}


def _texto(valor, defecto: str = "") -> str:
    """
    Valor de BD como texto limpio, con respaldo si viene vacío o nulo. Evita el
    str(None) == "None" que se colaba a los archivos cuando la columna era NULL.
    """
    return str(valor if valor is not None else "").strip() or defecto


def _codigo(valor, defecto: str = "") -> str:
    """Código SUNAT de dos dígitos: '1' -> '01'. Devuelve el respaldo si no hay dato."""
    texto = _texto(valor)
    return texto.zfill(2) if texto else defecto


def _campo_pipe(valor, defecto: str = "") -> str:
    """Texto apto para un archivo delimitado por pipes."""
    return re.sub(r"[|\r\n\t]+", " ", _texto(valor)).strip() or defecto


def _marcas(cantidad: int) -> str:
    """Placeholders '?,?,?' para un IN de SQL."""
    return ",".join("?" * cantidad)


def _tipo_sunat(valor) -> str:
    """
    Código de comprobante de SUNAT a partir de lo que guarda la aplicación, que usa
    nombres ('BOLETA') en vez de códigos. Si ya viene un código, se deja pasar.
    """
    texto = _texto(valor).upper()
    if not texto:
        return ""
    return _TIPOS_POR_NOMBRE.get(texto, _codigo(texto))
