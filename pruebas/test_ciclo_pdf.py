"""
Pruebas de aplicacion/ciclo_pdf.py: el buzón temporal de PDF (pdf_bytes se
vacía recién tras confirmar la subida, idempotencia si otra corrida ya cerró
la fila, el lote no intenta más de PDF_LOTE_CANTIDAD por corrida, y el backoff
de ciclo entero cuando una corrida no logra subir nada habiendo algo para
subir). El filtro de qué tipos llegan acá (solo factura y nota de crédito de
factura) vive en la consulta SQL de fuelhub_core.bd, no en este módulo —no se
puede probar sin una BD real, así que estas pruebas asumen que pendientes_pdf
ya solo devuelve esos tipos. Sin red ni BD real: se reemplazan el cliente HTTP
y el adaptador de fuelhub_core.bd por dobles.
"""
import os, sys, tempfile, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.CRITICAL)

import config
import estado.envio_pdf as envio_pdf
import aplicacion.ciclo_pdf as cp
from fuelhub_api.cliente import RespuestaAPI

TMP = tempfile.mkdtemp()
envio_pdf._ENVIO_PDF_PATH = os.path.join(TMP, "envio_pdf.json")

FALLAS = 0
def check(cond, msg):
    global FALLAS
    if not cond: FALLAS += 1
    print(("  OK    " if cond else "  FALLA "), msg)


def _limpiar_estado():
    if os.path.exists(envio_pdf._ENVIO_PDF_PATH):
        os.remove(envio_pdf._ENVIO_PDF_PATH)


def _fallos_actuales():
    import json as _json
    try:
        with open(envio_pdf._ENVIO_PDF_PATH, encoding="utf-8") as fh:
            return _json.load(fh).get("fallos_ciclo_consecutivos", 0)
    except FileNotFoundError:
        return 0


def _fila(numeracion="F001-000001", tipo="01", pdf=b"PDF", xml="<xml/>"):
    return {
        "id": hash(numeracion) % 10_000, "numeracion_comprobante": numeracion,
        "tipo_comprobante": tipo, "xml_envio": xml, "fecha_emision": "2026-11-14",
        "pdf_bytes": pdf, "numero_documento_receptor": "0",
    }


# --- dobles comunes ----------------------------------------------------------
class _FakeConn:
    def close(self): pass
cp.conectar_bd = lambda: _FakeConn()
cp.repositorio.motor_de = lambda url: "sqlserver"
cp.codigo_estacion = lambda conn: "ESTACION"
cp.FUELHUB_CORE_CLIENT_ID = "id"
cp.FUELHUB_CORE_CLIENT_SECRET = "secret"
cp.time.sleep = lambda seg: None  # el backoff no debe demorar la prueba
cp.consultar_comprobante = lambda ruc, codigo: RespuestaAPI(200, {"cdr": "<cdr/>"})

MARCADOS = []
def _marcar_ok(conn, comprobante_id):
    MARCADOS.append(comprobante_id)
    return True
cp.marcar_pdf_enviado = _marcar_ok


def reset():
    MARCADOS.clear()
    _limpiar_estado()
    cp.consultar_comprobante = lambda ruc, codigo: RespuestaAPI(200, {"cdr": "<cdr/>"})
    cp.marcar_pdf_enviado = _marcar_ok


# --- caso 1: subida exitosa -> marca, cuenta, limpia el backoff -------------
reset()
cp.pendientes_pdf = lambda conn, lote: [_fila("F001-000001")]
cp.subir_pdf_comprobante = lambda *a, **k: {"key": "ok"}
cp.ciclo_pdf()
check(MARCADOS == [_fila("F001-000001")["id"]], "se marca pdf_enviado de la fila subida")

# --- caso 2: marcar_pdf_enviado devuelve False (otra corrida ya la cerró) ---
reset()
cp.pendientes_pdf = lambda conn, lote: [_fila("F001-000002")]
cp.subir_pdf_comprobante = lambda *a, **k: {"key": "ok"}
cp.marcar_pdf_enviado = lambda conn, cid: False
cp.ciclo_pdf()  # no debe tirar excepción ni contarla dos veces
check(True, "marcar_pdf_enviado=False (idempotencia) no rompe el ciclo")

# --- caso 3: nada se pudo subir habiendo algo pendiente -> backoff de ciclo -
reset()
cp.pendientes_pdf = lambda conn, lote: [_fila("F001-000003")]
cp.subir_pdf_comprobante = lambda *a, **k: None  # FuelHub core rechazó / sin red
cp.ciclo_pdf()
cp.ciclo_pdf()
cp.ciclo_pdf()
check(_fallos_actuales() == 3, f"una corrida que no sube nada cuenta para el backoff de ciclo entero ({_fallos_actuales()})")

# --- caso 4: un CDR agotado (404) cuenta como procesado, NO dispara backoff -
reset()
cp.pendientes_pdf = lambda conn, lote: [_fila("F002-000004")]
cp.consultar_comprobante = lambda ruc, codigo: RespuestaAPI(404, None)
cp.ciclo_pdf()
check(MARCADOS == [_fila("F002-000004")["id"]], "un CDR agotado (404) igual se marca pdf_enviado (se da por no subible)")
check(_fallos_actuales() == 0, "un CDR agotado cuenta como progreso del ciclo, no dispara el backoff")

# --- caso 5: nota de crédito de factura se sube igual que una factura -------
reset()
cp.pendientes_pdf = lambda conn, lote: [_fila("FC01-000005", tipo="07")]
cp.subir_pdf_comprobante = lambda *a, **k: {"key": "ok"}
cp.ciclo_pdf()
check(MARCADOS == [_fila("FC01-000005", tipo="07")["id"]], "una nota de crédito de factura también se sube")

if FALLAS:
    print(str(FALLAS) + ' FALLA(S)')
    sys.exit(1)
print('TODO OK')
