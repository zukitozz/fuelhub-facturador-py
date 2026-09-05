"""
Persistencia propia del daemon para los resúmenes diarios: qué correlativo va
siguiente y qué boletas quedaron agrupadas en cada uno (resumenes.json).
"""
import json
import logging
import os
import threading
from datetime import datetime

from config import (
    _RESUMENES_PATH, _GRACIA_REGISTRO_RC_SEG, EMISOR_RUC_OVERRIDE, _ESTADOS_CERRADOS,
    SFS_DATA_DIR,
)
from dominio.cdr import _TIPO_RC
from dominio.comprobante import _nombre_base
from utilidades_files import escribir_archivo
from sfs.bd import _tiene_cdr, _docs_en_vuelo

logger = logging.getLogger(__name__)

_lock_resumenes = threading.Lock()


def _leer_resumenes() -> dict:
    try:
        with open(_RESUMENES_PATH, encoding="utf-8") as fh:
            datos = json.load(fh)
        return datos if isinstance(datos, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.exception("No se pudo leer %s; se reinicia el registro de resúmenes.", _RESUMENES_PATH)
        return {}


def _guardar_resumenes(datos: dict):
    try:
        escribir_archivo(_RESUMENES_PATH, json.dumps(datos, ensure_ascii=False, indent=2))
    except OSError:
        logger.exception("No se pudo guardar %s; el registro de resúmenes no persiste.", _RESUMENES_PATH)


def _siguiente_numeracion_rc(fecha: str) -> str:
    """
    RC-{fecha}-{NNN} (fecha=YYYYMMDD), con NNN correlativo propio del daemon — la
    única numeración que el daemon asigna en vez de leer: para todo lo demás la
    aplicación ya la puso antes de que el comprobante llegue acá.

    Con el prefijo "RC-" incluido, porque es la forma canónica del id: es la que
    usa SUNAT en el XML, la que el SFS guarda en DOCUMENTO.NUM_DOCU y espera en su
    API REST, y la que vuelve en el CDR. La ÚNICA excepción es el nombre de archivo
    en DATA, que se arma sin el prefijo (ver _nombre_archivo_rc).

    El correlativo no sale solo del archivo: se saltean los números que ya tengan
    un CDR en disco. Si resumenes.json se pierde o se borra a mano, el contador
    vuelve a 001 — y un CDR anterior con esa misma numeración haría que el daemon
    diera por contestado un resumen que nunca envió, cerrándolo sin que llegue a
    SUNAT. Pasó de verdad al reiniciar el contador.
    """
    with _lock_resumenes:
        datos = _leer_resumenes()
        n = int(datos.get("ultimo_correlativo", 0))
        while True:
            n += 1
            numeracion = f"{_TIPO_RC}-{fecha}-{n:03d}"
            if not _tiene_cdr(EMISOR_RUC_OVERRIDE, _TIPO_RC, numeracion):
                break
            logger.warning(
                "Ya existe un CDR para %s; se saltea ese número. El contador de "
                "resúmenes venía atrasado respecto de lo ya emitido.", numeracion,
            )
        datos["ultimo_correlativo"] = n
        _guardar_resumenes(datos)
    return numeracion


def _nombre_archivo_rc(ruc_emisor: str, numeracion_rc: str) -> str:
    """
    Nombre base del archivo en DATA para un resumen, sin el prefijo "RC-" del id.

    validarNombreArchivo() del SFS exige exactamente 4 tramos separados por guión
    (RUC-TIPO-SERIE-NUMERO). Como _nombre_base() ya agrega el tipo, dejar el "RC-"
    del id daría 5 tramos y el SFS descarta el archivo en silencio: no genera el
    XML ni lo registra en su bandeja, sin ningún error que lo delate (confirmado
    decompilando esa validación).
    """
    return _nombre_base(ruc_emisor, _TIPO_RC, _sin_prefijo_rc(numeracion_rc))


def _sin_prefijo_rc(numeracion_rc: str) -> str:
    prefijo = f"{_TIPO_RC}-"
    return numeracion_rc[len(prefijo):] if numeracion_rc.startswith(prefijo) else numeracion_rc


def _registrar_resumen(numeracion_rc: str, boletas: list):
    with _lock_resumenes:
        datos = _leer_resumenes()
        datos.setdefault("resumenes", {})[numeracion_rc] = {
            "boletas": boletas,
            "generado": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _guardar_resumenes(datos)


def _boletas_de_resumen(numeracion_rc: str) -> list:
    entrada = _leer_resumenes().get("resumenes", {}).get(numeracion_rc) or {}
    return entrada.get("boletas", [])


def _boletas_en_resumenes_activos(ruc_emisor: str) -> set:
    """
    Boletas que ya entraron en algún resumen y por lo tanto NO pueden entrar en
    otro. Solo se liberan cuando ese resumen se cerró, porque ahí ya quedaron en
    enviado=true y las filtra aplicacion.lecturas.obtener_boletas_para_resumen()
    por su cuenta.

    En cualquier otro caso se retienen, incluso si el SFS no sabe nada del resumen:
    esa ausencia no distingue entre "nunca se entregó" y "se entregó y ya se
    limpió". Liberarlas ante la duda es lo que genera el peor error posible acá —
    las mismas boletas declaradas dos veces a SUNAT, que acepta ambos resúmenes sin
    notar que llevan los mismos comprobantes, y que solo se deshace con una
    comunicación de baja. Retenerlas de más, en cambio, se ve en el log y se
    resuelve sacando el resumen de resumenes.json.
    """
    en_vuelo = _docs_en_vuelo(ruc_emisor)
    activas = set()
    for numeracion_rc, entrada in _leer_resumenes().get("resumenes", {}).items():
        boletas = entrada.get("boletas", [])
        situ, _ = en_vuelo.get((_TIPO_RC, numeracion_rc), ("", ""))
        if situ and situ not in _ESTADOS_CERRADOS:
            activas.update(boletas)
            continue
        if situ:
            continue  # cerrado: ya se resolvió, no hace falta ningún resguardo

        # Sin rastro en la bandeja del SFS no se puede saber qué pasó: puede que
        # nunca se haya entregado, o que se haya entregado y ya se limpiara. Ante
        # esa duda las boletas NO se liberan.
        #
        # Antes se liberaban pasado un lapso, y eso genero dos resumenes con las
        # mismas boletas mientras el SFS reiniciaba en bucle: SUNAT acepto los
        # dos, porque no detecta que lleven los mismos comprobantes. Declarar dos
        # veces solo se deshace con una comunicacion de baja. Una boleta trabada,
        # en cambio, se ve en el log y se destraba sacando su resumen de
        # resumenes.json: molesto, pero reversible.
        activas.update(boletas)
        if not _rdi_presente(ruc_emisor, numeracion_rc):
            _avisar_resumen_sin_rastro(numeracion_rc, entrada, boletas)
    return activas


def _rdi_presente(ruc_emisor: str, numeracion_rc: str) -> bool:
    """
    True si el .RDI del resumen sigue en DATA, o sea que aun no se proceso: esos
    archivos solo se borran cuando el documento se cierra.
    """
    base = _nombre_archivo_rc(ruc_emisor, numeracion_rc)
    return os.path.exists(os.path.join(SFS_DATA_DIR, f"{base}.RDI"))


def _avisar_resumen_sin_rastro(numeracion_rc: str, entrada: dict, boletas: list):
    """Un resumen del que no queda ninguna señal necesita que alguien lo mire."""
    try:
        generado = datetime.strptime(entrada.get("generado", ""), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return
    if (datetime.now() - generado).total_seconds() < _GRACIA_REGISTRO_RC_SEG:
        return   # recien generado: es normal que todavia no haya rastro
    logger.warning(
        "El resumen %s no figura en el SFS ni tiene archivos en DATA. Sus %d "
        "boleta(s) quedan retenidas para no declararlas dos veces. Si se confirma "
        "que nunca llego a SUNAT, borrar su entrada de %s para que vuelvan a la cola.",
        numeracion_rc, len(boletas), os.path.basename(_RESUMENES_PATH),
    )
