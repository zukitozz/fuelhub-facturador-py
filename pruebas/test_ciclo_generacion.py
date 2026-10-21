"""
Pruebas de aplicacion/ciclo_generacion.py: el manejo de cada respuesta posible de
facturador-api (aceptado, rechazado con bloqueo tras N intentos, 404 crítico, 502
con backoff, ambiguo resuelto por GET). Sin red ni BD real: se reemplazan el
cliente HTTP y el adaptador de BD por dobles.
"""
import os, sys, tempfile, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.CRITICAL)

import config
import estado.envio_api as envio_api
import aplicacion.ciclo_generacion as cg
from fuelhub_api.cliente import RespuestaAPI

TMP = tempfile.mkdtemp()
envio_api._ENVIO_API_PATH = os.path.join(TMP, "envio_api.json")
config.MAX_REINTENTOS_RECHAZO = cg.MAX_REINTENTOS_RECHAZO = 3

FALLAS = 0
def check(cond, msg):
    global FALLAS
    if not cond: FALLAS += 1
    print(("  OK    " if cond else "  FALLA "), msg)


def _limpiar_estado():
    if os.path.exists(envio_api._ENVIO_API_PATH):
        os.remove(envio_api._ENVIO_API_PATH)


def _comp(numeracion="F002-000002", tipo="01"):
    return {
        "id": 1, "factura_id": 1, "tipo_comprobante": tipo,
        "numeracion_comprobante": numeracion,
        "fecha_emision": __import__("datetime").datetime(2026, 10, 10, 10, 0),
        "tipo_moneda": "PEN", "gravadas": 100.0, "igv": 18.0, "total": 118.0,
        "monto_letras": "CIENTO DIECIOCHO CON 00/100 SOLES", "referencia": None,
    }


# --- dobles comunes a todas las pruebas -------------------------------------
class _FakeConn:
    def close(self): pass
cg.conectar_bd = lambda: _FakeConn()
cg.time.sleep = lambda seg: None   # el backoff del ciclo no debe demorar la prueba
cg.obtener_emisor = lambda conn: {"ruc": "20609785269", "razon_social": "PRUEBA"}
cg.obtener_receptor = lambda conn, fid: {}
cg.obtener_items = lambda conn, fid: [{
    "descripcion": "ITEM", "codigo_producto": None, "medida": "NIU",
    "cantidad": 1, "valor": 100.0, "valor_venta": 100.0, "igv_venta": 18.0, "precio": 118.0,
}]
# por defecto, sin resúmenes que conciliar: evita que la conciliación diaria
# (dispara una vez por calendario, no por test) llegue a llamar de verdad.
cg.cliente.listar_resumenes = lambda ruc, desde: RespuestaAPI(200, {"resumenes": []})

CAP = {"guardado_respuesta": [], "marcado_enviado": [], "guardado_error": []}
class FakeBD:
    @staticmethod
    def guardar_respuesta(conn, numeracion, hash_, qr, url, xml_envio):
        CAP["guardado_respuesta"].append((numeracion, hash_, qr))
        return 1
    @staticmethod
    def marcar_enviado(conn, numeracion, enviado=True, limpiar_error=True):
        CAP["marcado_enviado"].append(numeracion)
        return 1
    @staticmethod
    def guardar_error(conn, numeracion, detalle):
        CAP["guardado_error"].append((numeracion, detalle))
        return 1
cg._bd = lambda: FakeBD()
cg._escribir_bd = lambda fn, conn, *a: fn(conn, *a)


def reset():
    CAP["guardado_respuesta"].clear(); CAP["marcado_enviado"].clear(); CAP["guardado_error"].clear()
    _limpiar_estado()


# --- caso 1: comprobante aceptado -------------------------------------------
reset()
cg.obtener_comprobantes_pendientes = lambda conn: [_comp("F002-000002")]
cg.cliente.crear_comprobante = lambda ruc, payload, numeracion="": RespuestaAPI(
    201, {"codigo": "01-F002-000002", "estado": "aceptado", "des_obse": "-",
          "hash": "HASH==", "qr": "20609785269|01|...|HASH=="},
)
cg.ciclo_generacion()
check(CAP["marcado_enviado"] == ["F002-000002"], f"se marca enviado=1 ({CAP['marcado_enviado']})")
check(CAP["guardado_respuesta"] and CAP["guardado_respuesta"][0][1] == "HASH==", "se guarda el hash")
check(not CAP["guardado_error"], "no se guarda ningún error")
check(not envio_api.en_vuelo(), "no queda nada 'en vuelo' tras un cierre exitoso")

# --- caso 2: boleta "generado" se trata igual que aceptado ------------------
reset()
cg.obtener_comprobantes_pendientes = lambda conn: [_comp("B001-000050", tipo="03")]
cg.cliente.crear_comprobante = lambda ruc, payload, numeracion="": RespuestaAPI(
    201, {"codigo": "03-B001-000050", "estado": "generado", "des_obse": "-", "hash": "H2==", "qr": "qr2"},
)
cg.ciclo_generacion()
check(CAP["marcado_enviado"] == ["B001-000050"], "boleta 'generado' se marca enviado=1 igual")

# --- caso 3: rechazado, se bloquea tras MAX_REINTENTOS_RECHAZO --------------
reset()
cg.obtener_comprobantes_pendientes = lambda conn: [_comp("F002-000003")]
cg.cliente.crear_comprobante = lambda ruc, payload, numeracion="": RespuestaAPI(
    201, {"codigo": "01-F002-000003", "estado": "rechazado", "des_obse": "2800 - dato inválido"},
)
for _ in range(3):
    cg.ciclo_generacion()
check(not CAP["marcado_enviado"], "nunca se marca enviado=1")
check(len(CAP["guardado_error"]) == 3, f"se reintenta hasta el tope ({len(CAP['guardado_error'])} intentos)")
check(envio_api.bloqueado("F002-000003"), "queda bloqueado tras agotar los reintentos")
antes = len(CAP["guardado_error"])
cg.ciclo_generacion()
check(len(CAP["guardado_error"]) == antes, "una vez bloqueado, no se vuelve a intentar")

# --- caso 3b: anulado (venció el plazo de 7 días) se trata como un rechazo --
reset()
cg.obtener_comprobantes_pendientes = lambda conn: [_comp("F002-000003b")]
cg.cliente.crear_comprobante = lambda ruc, payload, numeracion="": RespuestaAPI(
    201, {"codigo": "01-F002-000003b", "estado": "anulado", "des_obse": "venció el plazo de 7 días"},
)
cg.ciclo_generacion()
check(not CAP["marcado_enviado"], "un anulado nunca se marca enviado=1")
check(CAP["guardado_error"] and "ANULADO" in CAP["guardado_error"][0][1], "se guarda el motivo de la anulación")

# --- caso 4: HTTP 404 (RUC sin alta) corta el ciclo entero ------------------
reset()
cg.obtener_comprobantes_pendientes = lambda conn: [_comp("F002-000004"), _comp("F002-000005")]
llamadas = {"n": 0}
def fake_404(ruc, payload, numeracion=""):
    llamadas["n"] += 1
    return RespuestaAPI(404, {"detail": "RUC no dado de alta"})
cg.cliente.crear_comprobante = fake_404
cg.ciclo_generacion()
check(llamadas["n"] == 1, f"se corta tras el primer 404, no sigue con el resto ({llamadas['n']} llamada(s))")

# --- caso 5: HTTP 502 no cuenta contra el presupuesto de rechazos -----------
reset()
cg.obtener_comprobantes_pendientes = lambda conn: [_comp("F002-000006")]
cg.cliente.crear_comprobante = lambda ruc, payload, numeracion="": RespuestaAPI(502, {"message": "Internal server error"})
cg.ciclo_generacion()
check(not CAP["guardado_error"], "un 502 no se guarda como error de datos")
check(not envio_api.bloqueado("F002-000006"), "un 502 no bloquea el comprobante")
check("F002-000006" not in envio_api.en_vuelo(), "no queda 'en vuelo': se reintenta solo en el próximo ciclo")

# --- caso 6: respuesta ambigua (timeout) se resuelve con un GET -------------
reset()
cg.obtener_comprobantes_pendientes = lambda conn: [_comp("F002-000007")]
cg.cliente.crear_comprobante = lambda ruc, payload, numeracion="": RespuestaAPI(None, None, "timed out")
cg.cliente.consultar_comprobante = lambda ruc, codigo: RespuestaAPI(
    200, {"codigo": codigo, "estado": "aceptado", "des_obse": "-", "hash": "H7==", "qr": "qr7"},
)
cg.ciclo_generacion()
check(CAP["marcado_enviado"] == ["F002-000007"], "el GET posterior cierra el comprobante igual")
check(not envio_api.en_vuelo(), "ya no queda 'en vuelo' una vez resuelto")

# --- caso 7: 'generado' en FACTURA (no boleta) no es ni éxito ni error ------
reset()
cg.obtener_comprobantes_pendientes = lambda conn: [_comp("F002-000008")]
cg.cliente.crear_comprobante = lambda ruc, payload, numeracion="": RespuestaAPI(
    201, {"codigo": "01-F002-000008", "estado": "generado", "des_obse": "-"},
)
cg.ciclo_generacion()
check(not CAP["marcado_enviado"], "no se marca enviado=1 todavía")
check(not CAP["guardado_error"], "no se guarda ningún error (no es un rechazo)")
check(not envio_api.bloqueado("F002-000008"), "no queda bloqueado (se reintenta solo)")
check(not envio_api.en_vuelo(), "no queda 'en vuelo': simplemente sigue pendiente")

# --- caso 8: 'enviado' sin CDR -> se guarda el xml, se espera el CDR -------
reset()
cg.obtener_comprobantes_pendientes = lambda conn: [_comp("F002-000009")]
cg.cliente.crear_comprobante = lambda ruc, payload, numeracion="": RespuestaAPI(
    201, {"codigo": "01-F002-000009", "estado": "enviado", "des_obse": "-",
          "hash": "H9==", "qr": "qr9", "xml": "<Invoice/>"},
)
cg.ciclo_generacion()
check(not CAP["marcado_enviado"], "todavía no se marca enviado=1 (falta el CDR)")
check(CAP["guardado_respuesta"] and CAP["guardado_respuesta"][0][1] == "H9==", "se guarda el hash/xml ya mismo")
check("F002-000009" in envio_api.esperando_cdr(), "queda en la cola de 'esperando CDR'")

# el próximo ciclo, ya con CDR disponible, lo resuelve por GET:
reset_llamados = {"n": 0}
def fake_get_con_cdr(ruc, codigo):
    reset_llamados["n"] += 1
    return RespuestaAPI(200, {"codigo": codigo, "estado": "aceptado", "des_obse": "-",
                              "hash": "H9==", "qr": "qr9", "cdr": "<ApplicationResponse/>"})
cg.obtener_comprobantes_pendientes = lambda conn: []   # nada nuevo que mandar este ciclo
cg.cliente.consultar_comprobante = fake_get_con_cdr
cg.ciclo_generacion()
check(CAP["marcado_enviado"] == ["F002-000009"], "el ciclo siguiente lo cierra solo, vía GET")
check("F002-000009" not in envio_api.esperando_cdr(), "sale de la cola de espera")

# --- caso 8b: 404 consultando el CDR -> deja de reintentar para siempre ----
reset()
cg.obtener_comprobantes_pendientes = lambda conn: [_comp("F002-000011")]
cg.cliente.crear_comprobante = lambda ruc, payload, numeracion="": RespuestaAPI(
    201, {"codigo": "01-F002-000011", "estado": "enviado", "des_obse": "-"},
)
cg.ciclo_generacion()
check("F002-000011" in envio_api.esperando_cdr(), "queda en la cola de 'esperando CDR'")
cg.obtener_comprobantes_pendientes = lambda conn: []
cg.cliente.consultar_comprobante = lambda ruc, codigo: RespuestaAPI(404, None)
cg.ciclo_generacion()
check("F002-000011" not in envio_api.esperando_cdr(), "un 404 lo saca de la cola en vez de reintentar para siempre")

# --- caso 9: estado 'error' -> backoff, SIN contar como rechazo ------------
reset()
cg.obtener_comprobantes_pendientes = lambda conn: [_comp("F002-000010")]
cg.cliente.crear_comprobante = lambda ruc, payload, numeracion="": RespuestaAPI(
    201, {"codigo": "01-F002-000010", "estado": "error", "des_obse": "fallo transitorio de SFS"},
)
cg.ciclo_generacion()
check(not CAP["guardado_error"], "un estado 'error' no se guarda como rechazo de datos")
check(not envio_api.bloqueado("F002-000010"), "no cuenta contra el presupuesto de rechazos")
check("F002-000010" not in envio_api.en_vuelo(), "no queda 'en vuelo': la API lo reintenta sola")

# --- caso 10: conciliación detecta un resumen rechazado -> reencola boletas -
reset()
marcados_false = []
def fake_marcar_enviado_false(conn, numeracion, enviado=True, limpiar_error=True):
    if enviado is False:
        marcados_false.append(numeracion)
    else:
        CAP["marcado_enviado"].append(numeracion)
    return 1
cg._bd = lambda: type("FakeBD2", (), {
    "marcar_enviado": staticmethod(fake_marcar_enviado_false),
    "guardar_error": staticmethod(FakeBD.guardar_error),
    "guardar_respuesta": staticmethod(FakeBD.guardar_respuesta),
})()
cg.obtener_comprobantes_pendientes = lambda conn: []
cg.cliente.listar_resumenes = lambda ruc, desde: RespuestaAPI(200, {"ruc": "20609785269", "total": 1, "resumenes": [
    {"numeracion_rc": "RC-20261010-001", "codigo": "RC-RC-20261010-001", "estado": "rechazado",
     "des_obse": "2282 - ya informado", "boletas": ["B001-000001", "B001-000002"]},
]})
cg._ultima_fecha_resumen = None   # fuerza a conciliar hoy
cg.ciclo_generacion()
check(set(marcados_false) == {"B001-000001", "B001-000002"}, f"las boletas del resumen rechazado vuelven a enviado=0 ({marcados_false})")
check(envio_api.resumen_rechazo_procesado("RC-20261010-001"), "el resumen rechazado queda marcado como ya procesado")

# aunque /resumenes lo siga listando (trae el historial completo), no se reencola dos veces:
marcados_false.clear()
cg._ultima_fecha_resumen = None
cg.ciclo_generacion()
check(not marcados_false, "un resumen ya procesado no se reencola de nuevo aunque siga apareciendo")
cg._bd = lambda: FakeBD()   # se restaura para no afectar corridas futuras de este archivo

# --- caso 11: falla guardar_respuesta -> no marca enviado, cierra por GET --
reset()
intentos_post = {"n": 0}
def fake_post_cuenta(ruc, payload, numeracion=""):
    intentos_post["n"] += 1
    return RespuestaAPI(201, {"codigo": "01-F002-000012", "estado": "aceptado", "des_obse": "-",
                              "hash": "H12==", "qr": "qr12"})
cg.obtener_comprobantes_pendientes = lambda conn: [_comp("F002-000012")]
cg.cliente.crear_comprobante = fake_post_cuenta
cg._bd = lambda: type("FakeBDFalla", (), {
    "guardar_respuesta": staticmethod(lambda conn, *a: -1),
    "marcar_enviado":    staticmethod(FakeBD.marcar_enviado),
    "guardar_error":     staticmethod(FakeBD.guardar_error),
})()
cg.ciclo_generacion()
check(not CAP["marcado_enviado"], "no se marca enviado si falló el guardado de hash/qr/xml")
check("F002-000012" in envio_api.en_vuelo(), "queda 'en vuelo' para resolverse por GET")
check(intentos_post["n"] == 1, "un solo POST, no se reintenta en el mismo ciclo")

# el próximo ciclo, con la BD ya funcionando, se resuelve con un GET —sin un segundo POST—:
cg._bd = lambda: FakeBD()
cg.obtener_comprobantes_pendientes = lambda conn: []
cg.cliente.consultar_comprobante = lambda ruc, codigo: RespuestaAPI(
    200, {"codigo": codigo, "estado": "aceptado", "des_obse": "-", "hash": "H12==", "qr": "qr12"},
)
cg.ciclo_generacion()
check(CAP["marcado_enviado"] == ["F002-000012"], "el ciclo siguiente lo cierra por GET")
check(intentos_post["n"] == 1, "nunca hubo un segundo POST")
cg._bd = lambda: FakeBD()

if FALLAS:
    print(str(FALLAS) + ' FALLA(S)')
    sys.exit(1)
print('TODO OK')
