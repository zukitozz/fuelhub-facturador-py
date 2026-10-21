"""
Caso de uso del hilo Generador: en cada ciclo, lee la BD de la aplicación y
manda cada comprobante pendiente a facturador-api (el servicio en la nube que
firma y envía a SUNAT a través de SFS) — reemplaza al SFS local que este
daemon manejaba antes directamente escribiendo archivos para un proceso Java.

Boletas: facturador-api las firma pero nunca las envía solas — quedan
"generado" (que acá se trata como cierre) hasta que la propia API arma y
declara el resumen diario sola, todos los días a las 00:10 hora de Lima (spec
§3). Este daemon YA NO dispara ese resumen ni lo sigue de a uno: solo concilia
una vez al día (_conciliar_resumenes_si_toca) para detectar un resumen
RECHAZADO por SUNAT y reencolar sus boletas — un resumen aceptado no requiere
ninguna acción, cada boleta ya quedó enviado=1 desde que se firmó.

Se pide `incluir=xml` en el POST para los dos tipos por igual (no solo
boleta): es la única oportunidad de conseguir el XML de una boleta, porque una
vez que el resumen diario la consume su propia consulta (GET) deja de dar
"generado" y pasa a mostrar el estado del resumen. Para factura/nota también
se homologa el mismo pedido, aunque ahí el GET posterior siga funcionando —
un solo camino de código en vez de dos. El CDR nunca se pide en el POST: para
factura/nota se consulta aparte cuando hace falta (ver
aplicacion/ciclo_pdf.py); una boleta nunca tiene CDR propio (es el del
resumen que la agrupa).

Manejo de cada estado de la respuesta (spec de facturador-api, HTTP siempre
201 salvo problemas de transporte — ver estado/envio_api.py para la
persistencia):
  - aceptado / aceptado_con_observaciones (factura/nota) → cierra: guarda
    hash/qr/xml/url, marca enviado=1.
  - generado (boleta) → se trata igual que un cierre: una boleta nunca pasa
    por "aceptado" individual, "generado" YA es su estado final.
  - generado (factura/nota) → SFS la firmó pero no la envió todavía: no se
    toca nada, sigue en enviado=0 y el próximo ciclo reintenta el POST solo.
  - enviado / validando / pendiente → SUNAT ya la tiene, falta el CDR: se
    guarda el xml si vino, no se marca enviado todavía, y se agrega a la
    cola de "esperando CDR" que se reconsulta por GET en cada ciclo.
  - rechazado → guarda el motivo (codigo_error + descripcion_error + des_obse),
    NO marca enviado, NO se reintenta más de MAX_REINTENTOS_RECHAZO veces.
  - anulado → venció el plazo de 7 días sin que SUNAT lo recibiera (spec §4):
    se trata igual que un rechazo (nunca va a poder reenviarse con el mismo
    número; hace falta uno nuevo), reutilizando el mismo mecanismo de bloqueo.
  - error → ambiguo por diseño (puede ser de red): se deja tal cual, sin
    backoff propio — la API ya reintenta sola (5 intentos cada 10 min, hasta
    3 días, spec §4) y verifica con SUNAT antes de reenviar, así que ni hace
    falta un GET de verificación acá ni tiene sentido duplicar ese reintento.
  - HTTP 404 (RUC sin alta) → error crítico, corta el ciclo entero.
  - HTTP 422 (payload inválido) → se trata como un rechazo: hay que corregir
    el dato, no tiene sentido reintentar igual.
  - HTTP 502 → la API ya verificó que no se envió nada: se reintenta en el
    próximo ciclo, sin gastar presupuesto de rechazos ni backoff propio.
  - Sin respuesta HTTP en absoluto (timeout real, corte de red) → ambiguo de
    verdad: se resuelve con un GET antes de seguir. Si ni el GET lo resuelve,
    queda "en vuelo" para el próximo ciclo (idempotencia ante un corte del
    propio daemon).

Ningún comprobante individual tiene backoff propio ante error/502/timeout: eso
ahora lo hace la API sola. El único backoff que queda es el del CICLO
ENTERO (_ESPERA_MAX_CICLO_MIN, ver config.py) — cuando el daemon directamente
no puede llegar a facturador-api (sin internet en la estación, servicio
caído), reintentar cada INTERVALO_GENERACION_SEG no tiene sentido y solo
llena el log; ese backoff se aplana en 2 horas por defecto.
"""
import logging
import time
from datetime import date, datetime, timedelta

import integraciones
from config import (
    FACTURADOR_API_URL, NOTIFICADOR_COMPROBANTES,
    MAX_REINTENTOS_RECHAZO, _MAX_ERRORS_SQL, _MAX_BLOQUEADOS_LOG, _ESPERA_MAX_CICLO_MIN,
)
from aplicacion.bd_app import conectar_bd, _bd, _escribir_bd
from aplicacion.lecturas import (
    obtener_emisor, obtener_receptor, obtener_items, obtener_comprobantes_pendientes,
)
from utilidades_timer import fecha_local
from fuelhub_api import cliente
from estado import envio_api

logger = logging.getLogger(__name__)

# Se elige acá (y no en config.py) porque el adaptador por defecto deja
# constancia en el log al instanciarse, y para entonces logging ya tiene sus
# handlers listos (los arma config.py, que este módulo importa antes de
# llegar a esta línea) — mismo motivo que antes en aplicacion/ciclo_cdr.py.
_notificador_comprobantes = integraciones.elegir(NOTIFICADOR_COMPROBANTES)

_ESTADOS_ACEPTADOS = ("aceptado", "aceptado_con_observaciones")
_ESTADOS_ESPERANDO_CDR = ("enviado", "validando", "pendiente")

# Cuántas fallas de infraestructura seguidas dentro de UN ciclo alcanzan para
# cortarlo y no seguir martillando un servicio que parece caído.
_UMBRAL_FALLOS_INFRA_CICLO = 3

# Último día (date) en que se corrió el intento de armar el resumen diario —
# en memoria, no en disco: perderlo en un reinicio de PM2 solo significa que
# se vuelve a intentar ese mismo día, no tiene costo.
_ultima_fecha_resumen = None


class _RucNoDadoDeAlta(Exception):
    """El RUC de esta estación no está registrado en facturador-api (404).
    Corta el ciclo entero — no tiene sentido seguir mandando el resto."""


class _FallaInfraestructura(Exception):
    """502, "error", o una respuesta sin HTTP claro — el comprobante se
    reintentará solo, pero cuenta para el backoff del ciclo si se repite
    seguido."""


def _notificar_comprobante_aceptado(ruc: str, tipo: str, numeracion: str):
    """
    Avisa a la integración configurada (ver integraciones/README.md) que este
    comprobante ya quedó cerrado en la BD de la app.

    Envuelto en su propio try/except a propósito: esta integración es
    best-effort y ajena al flujo de facturación, así que una falla acá no
    puede impedir que el ciclo siga.
    """
    try:
        _notificador_comprobantes.notificar(ruc, tipo, numeracion)
    except Exception:
        logger.exception(
            "Notificador de comprobantes (%s) falló para %s-%s; no afecta el cierre.",
            NOTIFICADOR_COMPROBANTES, tipo, numeracion,
        )


def _codigo_api(tipo: str, numeracion: str) -> str:
    return f"{tipo}-{numeracion}"


def _url_comprobante(ruc: str, codigo: str) -> str:
    return f"{FACTURADOR_API_URL}/empresas/{ruc}/comprobantes/{codigo}"


def _motivo_rechazo(body: dict) -> str:
    """codigo_error + descripcion_error, con des_obse como respaldo si no
    vino ninguno de los dos (ver spec §3)."""
    partes = [p for p in (body.get("codigo_error"), body.get("descripcion_error")) if p]
    return " - ".join(partes) or body.get("des_obse") or "sin detalle"


def _motivo_anulado(body: dict) -> str:
    """Un anulado no tiene codigo_error (nunca llegó a SUNAT): el motivo vive
    en des_obse (spec §4, vencimiento del plazo de 7 días)."""
    return f"ANULADO (plazo de 7 días vencido, requiere un número nuevo): {body.get('des_obse') or 'sin detalle'}"


def _armar_payload(conn, comp: dict) -> dict:
    receptor = obtener_receptor(conn, comp["factura_id"])
    items = obtener_items(conn, comp["factura_id"])
    payload = {
        "numeracion_comprobante": comp["numeracion_comprobante"],
        "tipo_comprobante": comp["tipo_comprobante"],
        "fecha_emision": fecha_local(comp["fecha_emision"]).isoformat(),
        "total": comp["total"],
        "gravadas": comp["gravadas"],
        "igv": comp["igv"],
        "tipo_moneda": comp["tipo_moneda"],
        "monto_letras": comp["monto_letras"],
        "items": items,
    }
    if receptor:
        payload["receptor"] = receptor
    if comp.get("referencia"):
        payload["referencia"] = comp["referencia"]
    return payload


def _cerrar_comprobante(conn, ruc: str, tipo: str, numeracion: str, body: dict):
    """
    Cierre exitoso: aceptado/aceptado_con_observaciones (factura/nota), o
    generado (boleta — ahí YA es el estado final, nunca pasa a "aceptado"
    individual). Guarda hash/qr/xml/url y marca enviado=1.
    """
    codigo = _codigo_api(tipo, numeracion)
    url = _url_comprobante(ruc, codigo)
    filas = _escribir_bd(
        _bd().guardar_respuesta, conn, numeracion,
        body.get("hash"), body.get("qr"), url, body.get("xml") or "",
    )
    if filas < 0:
        # guardar_respuesta falló (ej. el XML no entra en la columna): NO se
        # marca enviado sin haber guardado hash/qr/url/xml — para una boleta
        # eso sería irrecuperable, su GET individual da 404 apenas el resumen
        # la consume. Se deja "en vuelo": el próximo ciclo lo cierra con un
        # GET en vez de volver a mandar el mismo POST.
        logger.error(
            "No se pudo guardar hash/qr/url/xml de %s en la BD; no se marca "
            "enviado todavía.", codigo,
        )
        raise _FallaInfraestructura()
    _escribir_bd(_bd().marcar_enviado, conn, numeracion, True, True)
    envio_api.limpiar_rechazo(numeracion)
    envio_api.olvidar_en_vuelo(numeracion)
    envio_api.quitar_esperando_cdr(numeracion)
    _notificar_comprobante_aceptado(ruc, tipo, numeracion)
    logger.info("Comprobante %s cerrado (%s).", codigo, body.get("estado"))


def _cerrar_rechazado(numeracion: str, motivo: str):
    detalle = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {motivo}"[:_MAX_ERRORS_SQL]
    conn = None
    try:
        conn = conectar_bd()
        _escribir_bd(_bd().guardar_error, conn, numeracion, detalle)
    finally:
        if conn:
            conn.close()
    envio_api.olvidar_en_vuelo(numeracion)
    envio_api.quitar_esperando_cdr(numeracion)
    intentos = envio_api.contar_rechazo(numeracion, motivo)
    if intentos >= MAX_REINTENTOS_RECHAZO:
        logger.error(
            "Comprobante %s agotó los %d reenvíos y NO se reenviará hasta que se "
            "corrija el dato observado: %s", numeracion, MAX_REINTENTOS_RECHAZO, motivo,
        )
    else:
        logger.warning(
            "Comprobante %s rechazado (intento %d/%d): %s",
            numeracion, intentos, MAX_REINTENTOS_RECHAZO, motivo,
        )


def _resolver_ambiguo(ruc: str, tipo: str, numeracion: str) -> bool:
    """
    Pregunta a facturador-api qué pasó con un comprobante cuya llamada anterior
    quedó sin respuesta HTTP clara (timeout real, corte de red, o un corte
    justo a mitad del POST de una corrida anterior). Devuelve True si quedó
    resuelto, False si sigue ambiguo — ahí no se toca nada, se vuelve a
    preguntar en el próximo ciclo.
    """
    codigo = _codigo_api(tipo, numeracion)
    resp = cliente.consultar_comprobante(ruc, codigo)
    if resp.status == 404:
        logger.info("Comprobante %s no llegó a facturador-api (404); se reintentará.", codigo)
        envio_api.olvidar_en_vuelo(numeracion)
        return True
    if resp.status == 200 and resp.body:
        estado = resp.body.get("estado")
        if estado in _ESTADOS_ACEPTADOS or (tipo == "03" and estado == "generado"):
            conn = None
            try:
                conn = conectar_bd()
                _cerrar_comprobante(conn, ruc, tipo, numeracion, resp.body)
            finally:
                if conn:
                    conn.close()
            return True
        if estado == "rechazado":
            _cerrar_rechazado(numeracion, _motivo_rechazo(resp.body))
            return True
        if estado == "anulado":
            _cerrar_rechazado(numeracion, _motivo_anulado(resp.body))
            return True
        # generado (factura/nota, sin enviar aún) / enviado-validando-pendiente
        # / error: sigue en curso, no se toca.
    logger.info("Comprobante %s sigue ambiguo (HTTP %s); se reintenta la consulta.", codigo, resp.status)
    return False


def _resolver_pendientes_en_vuelo(ruc: str):
    """Antes de mandar nada nuevo, resuelve lo que quedó "en vuelo" de una
    corrida anterior — ver idempotencia en el docstring del módulo."""
    pendientes = envio_api.en_vuelo()
    if not pendientes:
        return
    logger.info("%d comprobante(s) en vuelo de una corrida anterior; resolviendo...", len(pendientes))
    for numeracion, tipo in pendientes.items():
        try:
            _resolver_ambiguo(ruc, tipo, numeracion)
        except _FallaInfraestructura:
            pass   # ya logueado en _cerrar_comprobante; se reintenta el próximo ciclo
        except Exception:
            logger.exception("Error resolviendo %s-%s en vuelo", tipo, numeracion)


def _resolver_esperando_cdr(ruc: str):
    """
    Reconsulta los comprobantes que SUNAT ya tiene pero todavía sin CDR
    (enviado/validando/pendiente) — no son "en vuelo" (esos son llamadas sin
    respuesta clara) ni rechazos, simplemente falta que aparezca el CDR.
    """
    pendientes = envio_api.esperando_cdr()
    for numeracion, tipo in pendientes.items():
        codigo = _codigo_api(tipo, numeracion)
        resp = cliente.consultar_comprobante(ruc, codigo)
        if resp.status == 404:
            # facturador-api ya no tiene este comprobante — no es "todavía no
            # hay CDR", es que dejó de existir ahí (p.ej. se reseteó su base).
            # Reintentarlo cada ciclo para siempre no lo iba a resolver nunca.
            logger.warning(
                "%s ya no existe en facturador-api (404 consultando su CDR); "
                "se deja de reintentar.", codigo,
            )
            envio_api.quitar_esperando_cdr(numeracion)
            continue
        if resp.status != 200 or not resp.body:
            continue
        estado = resp.body.get("estado")
        if estado in _ESTADOS_ACEPTADOS:
            conn = None
            try:
                conn = conectar_bd()
                _cerrar_comprobante(conn, ruc, tipo, numeracion, resp.body)
            except _FallaInfraestructura:
                pass   # ya logueado en _cerrar_comprobante; se reintenta el próximo ciclo
            finally:
                if conn:
                    conn.close()
        elif estado == "rechazado":
            _cerrar_rechazado(numeracion, _motivo_rechazo(resp.body))
        elif estado == "anulado":
            _cerrar_rechazado(numeracion, _motivo_anulado(resp.body))
        # sigue esperando: no se toca, se reconsulta en el próximo ciclo.


def _procesar_comprobante(conn, ruc: str, comp: dict):
    tipo = comp["tipo_comprobante"]
    numeracion = comp["numeracion_comprobante"]
    codigo = _codigo_api(tipo, numeracion)

    payload = _armar_payload(conn, comp)
    envio_api.marcar_en_vuelo(numeracion, tipo)
    resp = cliente.crear_comprobante(ruc, payload, numeracion)

    if resp.status == 201 and resp.body:
        estado = resp.body.get("estado")
        if estado in _ESTADOS_ACEPTADOS or (tipo == "03" and estado == "generado"):
            _cerrar_comprobante(conn, ruc, tipo, numeracion, resp.body)
            return
        if estado in _ESTADOS_ESPERANDO_CDR:
            # SUNAT ya la tiene, falta el CDR. Se guarda el xml si vino (para
            # boleta sería imposible más adelante; para factura/nota no hay
            # drama pero tampoco cuesta nada guardarlo ya), sin marcar enviado.
            xml = resp.body.get("xml")
            if xml:
                filas = _escribir_bd(
                    _bd().guardar_respuesta, conn, numeracion,
                    resp.body.get("hash"), resp.body.get("qr"),
                    _url_comprobante(ruc, codigo), xml,
                )
                if filas < 0:
                    # No se quedó sin marcar enviado (nunca se marca acá), pero
                    # el XML que vino en esta respuesta se perdió igual — no
                    # hay otra oportunidad de conseguirlo si después resulta
                    # ser una boleta que termina en "generado" sin volver a
                    # traer XML en el cuerpo.
                    logger.error(
                        "No se pudo guardar el XML de %s (estado %s) en la BD.",
                        codigo, estado,
                    )
            envio_api.agregar_esperando_cdr(numeracion, tipo)
            envio_api.olvidar_en_vuelo(numeracion)
            return
        if estado == "generado":
            # Factura/nota firmada pero todavía sin enviar: no se toca nada,
            # sigue en enviado=0 y el próximo ciclo la vuelve a intentar sola.
            envio_api.olvidar_en_vuelo(numeracion)
            return
        if estado == "rechazado":
            _cerrar_rechazado(numeracion, _motivo_rechazo(resp.body))
            return
        if estado == "anulado":
            _cerrar_rechazado(numeracion, _motivo_anulado(resp.body))
            return
        if estado == "error":
            # Ambiguo por diseño: la propia API reintenta sola (5 intentos
            # cada 10 min, hasta 3 días — spec §4), así que acá no hace falta
            # backoff propio ni un GET de verificación: se deja tal cual,
            # sigue en enviado=0 y el daemon la vuelve a mandar en el próximo
            # ciclo (reenviar el mismo número es seguro, la API consulta a
            # SUNAT antes de reintentar).
            envio_api.olvidar_en_vuelo(numeracion)
            logger.warning(
                "Comprobante %s en estado 'error' (%s); la API lo reintenta sola.",
                codigo, resp.body.get("des_obse", ""),
            )
            raise _FallaInfraestructura()
        logger.warning("Comprobante %s: estado desconocido %r; se revisa a mano.", codigo, estado)
        envio_api.olvidar_en_vuelo(numeracion)
        return

    if resp.status == 404:
        logger.error(
            "facturador-api respondió 404 para el RUC %s — no está dado de alta "
            "en el servicio. Se corta el ciclo: nada se puede emitir hasta "
            "resolverlo.", ruc,
        )
        envio_api.olvidar_en_vuelo(numeracion)
        raise _RucNoDadoDeAlta()

    if resp.status == 422:
        detalle = (resp.body or {}).get("detail") or (resp.body or {}).get("message") or "parámetros inválidos"
        _cerrar_rechazado(numeracion, str(detalle))
        return

    if resp.status == 502:
        envio_api.olvidar_en_vuelo(numeracion)
        logger.warning(
            "facturador-api verificó que %s no se envió (502); se reintenta en "
            "el próximo ciclo.", codigo,
        )
        raise _FallaInfraestructura()

    # Sin respuesta HTTP clara (timeout real, corte de red) o un 5xx no
    # documentado: se intenta resolver YA con un GET dentro de esta misma
    # pasada — si no alcanza, queda "en vuelo" para el próximo ciclo.
    _resolver_ambiguo(ruc, tipo, numeracion)
    raise _FallaInfraestructura()


def _conciliar_resumenes_si_toca(ruc: str):
    """Una vez por día alcanza: la API arma y resuelve el resumen sola, acá
    solo hace falta enterarse de qué quedó rechazado."""
    global _ultima_fecha_resumen
    hoy = date.today()
    if _ultima_fecha_resumen == hoy:
        return
    _ultima_fecha_resumen = hoy
    _conciliar_resumenes(ruc)


def _reencolar_boletas_de_resumen_rechazado(numeracion_rc: str, boletas: list):
    """
    El resumen se rechazó: sus boletas no quedaron declaradas ante SUNAT.
    Vuelven a enviado=0 para que el próximo ciclo las regenere (nuevo POST
    individual con hash/qr/xml propios) y entren a un resumen nuevo.
    """
    conn = None
    try:
        conn = conectar_bd()
        for numeracion in boletas:
            _escribir_bd(_bd().marcar_enviado, conn, numeracion, False, False)
            _escribir_bd(
                _bd().guardar_error, conn, numeracion,
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Resumen {numeracion_rc} rechazado; "
                f"se regenera para un resumen nuevo."[:_MAX_ERRORS_SQL],
            )
    finally:
        if conn:
            conn.close()
    logger.error(
        "Resumen %s RECHAZADO por SUNAT; sus %d boleta(s) vuelven a la cola "
        "para regenerarse: %s", numeracion_rc, len(boletas), ", ".join(boletas[:_MAX_BLOQUEADOS_LOG]),
    )


def _conciliar_resumenes(ruc: str):
    """
    GET /empresas/{ruc}/resumenes — la API arma y resuelve el resumen diario
    de boletas por su cuenta (spec §3), así que acá no hace falta dispararlo
    ni seguirlo de a uno: solo barrer el historial reciente para encontrar
    algún resumen RECHAZADO por SUNAT y reencolar sus boletas. Uno aceptado no
    requiere ninguna acción —cada boleta ya quedó enviado=1 al firmarse
    ("generado" ya es su cierre, ver _cerrar_comprobante)—.

    `desde` cubre algo más que el plazo de 7 días: un resumen puede tardar en
    resolverse y la lista trae también los ya resueltos, así que barrerla de
    más es inofensivo — lo que evita reprocesar un rechazo ya reencolado es
    el set persistido en envio_api.resumen_rechazo_procesado, no la ventana.
    """
    desde = (date.today() - timedelta(days=10)).isoformat()
    resp = cliente.listar_resumenes(ruc, desde)
    if resp.status != 200 or not resp.body:
        logger.info("No se pudo listar resúmenes para conciliar (HTTP %s).", resp.status)
        return
    for resumen in resp.body.get("resumenes", []):
        numeracion_rc = resumen.get("numeracion_rc")
        if not numeracion_rc or resumen.get("estado") != "rechazado":
            continue
        if envio_api.resumen_rechazo_procesado(numeracion_rc):
            continue
        _reencolar_boletas_de_resumen_rechazado(numeracion_rc, resumen.get("boletas", []))
        envio_api.marcar_resumen_rechazo_procesado(numeracion_rc)


def ciclo_generacion():
    logger.info("Consultando BD...")
    conn = None
    try:
        conn = conectar_bd()
        emisor = obtener_emisor(conn)
        if not emisor:
            logger.error("No se encontró información del Emisor en BD (falta EMISOR_RUC en el .env).")
            return
        ruc = emisor["ruc"]

        _resolver_pendientes_en_vuelo(ruc)
        _resolver_esperando_cdr(ruc)

        pendientes = obtener_comprobantes_pendientes(conn)
        logger.info("%d comprobante(s) pendiente(s).", len(pendientes))

        bloqueados_ahora = envio_api.bloqueados()
        fallos_infra = procesados = 0
        for comp in pendientes:
            numeracion = comp["numeracion_comprobante"]
            if numeracion in bloqueados_ahora:
                continue
            try:
                _procesar_comprobante(conn, ruc, comp)
                procesados += 1
            except _RucNoDadoDeAlta:
                return
            except _FallaInfraestructura:
                fallos_infra += 1
                if fallos_infra >= _UMBRAL_FALLOS_INFRA_CICLO:
                    logger.warning(
                        "%d falla(s) de infraestructura seguidas en este ciclo; se "
                        "corta acá y se reintenta en el próximo.", fallos_infra,
                    )
                    break
            except Exception:
                logger.exception("Error procesando %s", numeracion)

        # Solo se avisan los bloqueados que siguen apareciendo en pendientes():
        # uno que ya no vuelve a aparecer (ej. un tipo fuera de alcance que se
        # empezó a filtrar en el origen) no tiene nada más que alguien pueda
        # corregir, y quedaría avisándose para siempre sin ninguna acción posible.
        numeraciones_pendientes = {c["numeracion_comprobante"] for c in pendientes}
        bloqueados_vigentes = [n for n in bloqueados_ahora if n in numeraciones_pendientes]
        if bloqueados_vigentes:
            logger.warning(
                "%d comprobante(s) bloqueados (agotaron %d reenvíos) — requieren "
                "intervención manual: %s",
                len(bloqueados_vigentes), MAX_REINTENTOS_RECHAZO,
                ", ".join(bloqueados_vigentes[:_MAX_BLOQUEADOS_LOG]),
            )

        if procesados and fallos_infra == 0:
            envio_api.limpiar_fallos_ciclo()
        elif fallos_infra and procesados == 0 and pendientes:
            # El ciclo entero no pudo avanzar nada por fallas de infraestructura:
            # backoff creciente para no martillar un servicio que parece caído.
            fallos_ciclo = envio_api.contar_fallo_ciclo()
            espera_min = min(2 ** (fallos_ciclo - 1), _ESPERA_MAX_CICLO_MIN)
            logger.warning(
                "%d corrida(s) seguidas sin poder avanzar nada por fallas de "
                "infraestructura; se pausa %d min antes del próximo intento.",
                fallos_ciclo, espera_min,
            )
            time.sleep(espera_min * 60)

        _conciliar_resumenes_si_toca(ruc)

    except Exception:
        logger.exception("Error en ciclo_generacion")
    finally:
        if conn:
            conn.close()
