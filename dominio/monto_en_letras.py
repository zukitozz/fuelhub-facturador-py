"""Importe en palabras, como lo exige SUNAT en la leyenda 1000 del comprobante."""
from .montos import formatear_decimal
from .texto import _texto

_UNIDADES = ("", "UNO", "DOS", "TRES", "CUATRO", "CINCO", "SEIS", "SIETE", "OCHO", "NUEVE",
             "DIEZ", "ONCE", "DOCE", "TRECE", "CATORCE", "QUINCE", "DIECISEIS", "DIECISIETE",
             "DIECIOCHO", "DIECINUEVE", "VEINTE")
_DECENAS  = ("", "", "VEINTI", "TREINTA", "CUARENTA", "CINCUENTA", "SESENTA", "SETENTA",
             "OCHENTA", "NOVENTA")
_CENTENAS = ("", "CIENTO", "DOSCIENTOS", "TRESCIENTOS", "CUATROCIENTOS", "QUINIENTOS",
             "SEISCIENTOS", "SETECIENTOS", "OCHOCIENTOS", "NOVECIENTOS")
_NOMBRE_MONEDA = {"PEN": "SOLES", "USD": "DOLARES AMERICANOS", "EUR": "EUROS"}


def _centenas_a_letras(n: int) -> str:
    if n == 100:
        return "CIEN"
    partes = []
    if n >= 100:
        partes.append(_CENTENAS[n // 100])
        n %= 100
    if n <= 20:
        if n:
            partes.append(_UNIDADES[n])
    elif n < 30:
        # 21..29 se escriben juntos: VEINTIUNO, VEINTIDOS, ...
        partes.append(_DECENAS[2] + _UNIDADES[n % 10])
    else:
        partes.append(_DECENAS[n // 10] + (f" Y {_UNIDADES[n % 10]}" if n % 10 else ""))
    return " ".join(p for p in partes if p)


def numero_a_letras(monto, moneda: str = "PEN") -> str:
    """
    Importe en palabras, como lo exige SUNAT en la leyenda 1000 del comprobante.

    La aplicación no guarda este texto, así que se arma acá. El formato es el usual
    en Perú: "CIENTO DIECIOCHO CON 00/100 SOLES".
    """
    valor = formatear_decimal(monto)
    entero = int(valor)
    centavos = int((valor - entero) * 100)

    if entero == 0:
        letras = "CERO"
    else:
        bloques = []
        millones, resto = divmod(entero, 1_000_000)
        miles, unidades = divmod(resto, 1000)
        if millones:
            bloques.append("UN MILLON" if millones == 1 else f"{_centenas_a_letras(millones)} MILLONES")
        if miles:
            bloques.append("MIL" if miles == 1 else f"{_centenas_a_letras(miles)} MIL")
        if unidades:
            bloques.append(_centenas_a_letras(unidades))
        letras = " ".join(bloques)

    return f"{letras} CON {centavos:02d}/100 {_NOMBRE_MONEDA.get(_texto(moneda, 'PEN').upper(), 'SOLES')}"
