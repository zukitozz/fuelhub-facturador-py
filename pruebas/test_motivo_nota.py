"""
Pruebas de _completar_referencia_nota() (aplicacion/lecturas.py) — el fallback de
MOTIVO_NOTA_POR_DEFECTO cuando la BD no trae tipo_nota en una nota. Reescrito tras
el reemplazo de la emisión local por facturador-api: la lógica es la misma que
antes vivía en sfs/archivos.py:_referencia_nota(), solo movió de lugar.
"""
import os, sys, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.CRITICAL)

import config
import aplicacion.lecturas as lecturas

FALLAS = 0
def check(cond, msg):
    global FALLAS
    if not cond: FALLAS += 1
    print(("  OK    " if cond else "  FALLA "), msg)


def _fila(tipo_comp="07", **overrides):
    base = {
        "id": 1,
        "numeracion_comprobante": "FC03-000019",
        "tipo_comprobante": tipo_comp,
        "tipo_enum": None,
        "tipo_nota": None,
        "tipo_documento_afectado": "01",
        "numeracion_documento_afectado": "FC03-000018",
        "motivo_documento_afectado": None,
    }
    base.update(overrides)
    return base


# 1) comportamiento actual (sin configurar): NO se emite
config.MOTIVO_NOTA_POR_DEFECTO = lecturas.MOTIVO_NOTA_POR_DEFECTO = ""
r = lecturas._completar_referencia_nota(_fila())
check(r is None, "sin configurar, la nota sin motivo NO se emite (comportamiento previo intacto)")

# 2) con el defecto en 01: se emite con motivo 01 y su descripcion
lecturas.MOTIVO_NOTA_POR_DEFECTO = "01"
r = lecturas._completar_referencia_nota(_fila())
check(r is not None, "con MOTIVO_NOTA_POR_DEFECTO=01, la nota SI se emite")
if r:
    check(r["tipo_nota"] == "01", f"codigo de motivo = 01 ({r['tipo_nota']})")
    check(r["motivo_documento_afectado"] == "ANULACION DE LA OPERACION", f"descripcion correcta ({r['motivo_documento_afectado']})")
    check(r["numeracion_documento_afectado"] == "FC03-000018", f"conserva el documento afectado ({r['numeracion_documento_afectado']})")

# 3) si la nota YA trae motivo, el defecto no lo pisa
r = lecturas._completar_referencia_nota(_fila(tipo_nota="06", motivo_documento_afectado=None))
check(r and r["tipo_nota"] == "06", f"un motivo existente no se pisa ({r['tipo_nota'] if r else None})")
check(r and r["motivo_documento_afectado"] == "DEVOLUCION TOTAL", f"y usa su propia descripcion ({r['motivo_documento_afectado'] if r else None})")

# 4) faltando OTRO dato, el defecto no lo tapa
r = lecturas._completar_referencia_nota(_fila(numeracion_documento_afectado=None))
check(r is None, "si falta el documento afectado, sigue sin emitirse")

# 5) normalizacion: un "1" suelto se convierte en "01"
lecturas.MOTIVO_NOTA_POR_DEFECTO = "1"
r = lecturas._completar_referencia_nota(_fila())
check(r and r["tipo_nota"] == "01", f"'1' se normaliza a '01' ({r['tipo_nota'] if r else None})")

# 6) nota de debito (08) con su propio catalogo
lecturas.MOTIVO_NOTA_POR_DEFECTO = "01"
r = lecturas._completar_referencia_nota(_fila(tipo_comp="08", numeracion_documento_afectado="ND03-000001"))
check(r and r["motivo_documento_afectado"] == "INTERES POR MORA", f"catalogo 10 para ND ({r['motivo_documento_afectado'] if r else None})")

# 7) codigo que NO existe en el catalogo: no se emite
lecturas.MOTIVO_NOTA_POR_DEFECTO = "99"
r = lecturas._completar_referencia_nota(_fila())
check(r is None, f"motivo invalido '99' no se emite (dio {r})")

if FALLAS:
    print(str(FALLAS) + ' FALLA(S)')
    sys.exit(1)
print('TODO OK')
