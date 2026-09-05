"""Redondeo e IGV: separar un importe con impuesto incluido en base + impuesto."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Todo se factura gravado al 18%: es lo que corresponde a los servicios de estética.
_FACTOR_IGV = Decimal("1.18")


def formatear_decimal(valor, decimales: int = 2) -> Decimal:
    """
    Importe redondeado, con 2 decimales salvo que se pidan otros.

    El valor unitario es el único campo que necesita más: se declara con 6 porque
    SUNAT verifica que cantidad × valor unitario cuadre con el valor de venta, y
    con 2 decimales la cuenta no cierra. Un servicio de S/10 en 3 unidades da
    2.823333 por unidad; redondeado a 2.82, tres unidades suman 8.46 contra los
    8.47 declarados como valor de venta.
    """
    if valor is None:
        return Decimal(0).quantize(Decimal(1).scaleb(-decimales))
    try:
        return Decimal(str(valor)).quantize(Decimal(1).scaleb(-decimales),
                                            rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0).quantize(Decimal(1).scaleb(-decimales))


def _base_e_igv(total):
    """
    Separa un importe con IGV incluido en base imponible e impuesto.

    La aplicación solo guarda el total cobrado. Se asume todo gravado al 18%, que es
    lo que corresponde a los servicios de estética; un ítem exonerado o gratuito
    necesitaría el tipo de afectación, que la base no tiene (ver README).
    """
    bruto = formatear_decimal(total)
    base = (bruto / _FACTOR_IGV).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(base), float(bruto - base)


def _desglosar_igv(precio_unitario, cantidad, total_linea):
    """(valor unitario sin IGV, valor de venta de la línea, IGV de la línea)."""
    # Los dos factores pasan por formatear_decimal, como ya hacia la linea de abajo:
    # en SQL Server 'cantidad' es nvarchar, y multiplicar texto por un float reventaba
    # el ciclo entero con TypeError cuando la linea no traia total.
    if total_linea is not None:
        total = total_linea
    else:
        total = float(formatear_decimal(precio_unitario, 6)
                      * (formatear_decimal(cantidad, 6) or Decimal("1")))
    valor_venta, igv = _base_e_igv(total)
    cant = formatear_decimal(cantidad) or Decimal("1")
    unitario = (Decimal(str(valor_venta)) / cant) if cant else Decimal("0")
    return float(unitario), valor_venta, igv
