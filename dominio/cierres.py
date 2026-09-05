"""
Payloads de cierre de turno y cierre de día para FuelHub core: solo formato, sin
tocar la BD ni la red — igual que comprobante.py y resumen_diario.py.
"""
from dominio.texto import _texto

# (columna en Cierreturnos, medio que espera FuelHub core). Solo se declara un
# pago por cada medio con monto — un medio en 0 no se usó ese turno.
_MEDIOS_PAGO = (("efectivo", "EFECTIVO"), ("tarjeta", "TARJETA"), ("yape", "YAPE"))


def _num(valor):
    """float para JSON: los importes de detalle vienen como Decimal de SQL Server."""
    return float(valor) if valor is not None else None


def _fecha_negocio(fecha_inicio_iso: str, fecha_cierredia_iso: str) -> str:
    """
    Fecha (YYYY-MM-DD) del día de negocio al que pertenece un turno.

    Se toma la del Cierredia enlazado cuando existe —es el que decide a qué día
    de negocio pertenece el turno—, y si el turno todavía no quedó enlazado a
    ninguno se usa la fecha en que arrancó el turno como respaldo.
    """
    fuente = fecha_cierredia_iso or fecha_inicio_iso or ""
    return fuente[:10]


def payload_cierre_turno(cabecera: dict, detalle: list) -> dict:
    pagos = [
        {"medio": nombre, "monto": monto}
        for campo, nombre in _MEDIOS_PAGO
        if (monto := cabecera.get(campo)) not in (None, 0, 0.0)
    ]
    return {
        "codigoEstacion": cabecera.get("codigo_estacion"),
        "turno":          _texto(cabecera.get("turno")),
        "fechaNegocio":   _fecha_negocio(cabecera.get("fecha_inicio"), cabecera.get("cierredia_fecha")),
        "fechaInicio":    cabecera.get("fecha_inicio"),
        "fecha":          cabecera.get("fecha"),
        "total":          _num(cabecera.get("total")),
        "empleado": {
            "codigo": _texto(cabecera.get("empleado_codigo")),
            "nombre": _texto(cabecera.get("empleado_nombre")),
        },
        "pagos": pagos,
        "detalle": [
            {
                "productoId":          linea.get("producto_id"),
                "codigoLocal":         _texto(linea.get("codigo_local")),
                "producto":            _texto(linea.get("producto")),
                "medida":              _texto(linea.get("medida")),
                "totalCantidad":       _num(linea.get("total_cantidad")),
                "totalSoles":          _num(linea.get("total_soles")),
                "calibracionCantidad": _num(linea.get("calibracion_cantidad")),
                "calibracionSoles":    _num(linea.get("calibracion_soles")),
                "despachoCantidad":    _num(linea.get("despacho_cantidad")),
                "despachoSoles":       _num(linea.get("despacho_soles")),
            }
            for linea in detalle
        ],
    }


def payload_cierre_dia(cabecera: dict) -> dict:
    fecha = _texto(cabecera.get("fecha"))
    return {
        "codigoEstacion": cabecera.get("codigo_estacion"),
        "fechaNegocio":   fecha[:10],
        "fecha":          cabecera.get("fecha"),
        "total":          _num(cabecera.get("total")),
        "administrador": {
            "codigo": _texto(cabecera.get("admin_codigo")),
            "nombre": _texto(cabecera.get("admin_nombre")),
        },
    }
