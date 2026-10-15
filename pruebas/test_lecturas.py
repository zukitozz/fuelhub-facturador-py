"""
Pruebas de aplicacion/lecturas.py:obtener_comprobantes_pendientes() — en
particular, que los tipos fuera del catálogo que acepta facturador-api
(01/03/07/08) se descarten ANTES de intentar nada, sin quemar reintentos.

Caso real que motivó esto: una base de un cliente traía tipo_comprobante='50'
y '51' en la misma columna (otros documentos que esa aplicación también
guarda ahí) — sin este filtro, facturador-api los rechazaba uno por uno hasta
bloquearlos, un dato que nunca iba a poder "corregirse".
"""
import os, sys, logging, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.CRITICAL)

import aplicacion.lecturas as lecturas

FALLAS = 0
def check(cond, msg):
    global FALLAS
    if not cond: FALLAS += 1
    print(("  OK    " if cond else "  FALLA "), msg)


def _fila(numeracion, tipo_comprobante):
    return {
        "id": 1, "tipo_comprobante": tipo_comprobante, "tipo_enum": None,
        "numeracion_comprobante": numeracion,
        "fecha_emision": datetime.datetime(2026, 10, 14, 10, 0),
        "tipo_moneda": "PEN", "gravadas": 100.0, "igv": 18.0, "total": 118.0,
        "monto_letras": "CIENTO DIECIOCHO CON 00/100 SOLES",
        "tipo_nota": None, "tipo_documento_afectado": None,
        "numeracion_documento_afectado": None, "motivo_documento_afectado": None,
    }


class FakeBD:
    @staticmethod
    def pendientes(conn):
        return [
            _fila("N001-000006", "50"),
            _fila("C001-000001", "51"),
            _fila("F002-000010", "01"),
        ]
lecturas._bd = lambda: FakeBD()

resultado = lecturas.obtener_comprobantes_pendientes(None)
numeraciones = [c["numeracion_comprobante"] for c in resultado]
check("N001-000006" not in numeraciones, "tipo '50' (fuera de alcance) se descarta")
check("C001-000001" not in numeraciones, "tipo '51' (fuera de alcance) se descarta")
check(numeraciones == ["F002-000010"], f"solo queda el tipo válido ({numeraciones})")

if FALLAS:
    print(str(FALLAS) + ' FALLA(S)')
    sys.exit(1)
print('TODO OK')
