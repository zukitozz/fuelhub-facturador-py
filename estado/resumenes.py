"""
Persistencia propia del daemon para los resúmenes diarios: qué correlativo va
siguiente y qué boletas quedaron agrupadas en cada uno (resumenes.json).
"""
import json
import logging
import os
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime

from config import (
    _RESUMENES_PATH, _GRACIA_REGISTRO_RC_SEG, EMISOR_RUC_OVERRIDE, _ESTADOS_CERRADOS,
    SFS_DATA_DIR, _SFS_FIRMA_DIR, MAX_DECLARACIONES_BOLETA, MAX_RESUMENES_DIA,
    DIAS_RETENCION_RESUMENES, _MAX_BLOQUEADOS_LOG,
)
from dominio.cdr import _TIPO_RC
from dominio.texto import _texto
from dominio.comprobante import _nombre_base
from utilidades_files import escribir_archivo
from sfs.bd import _tiene_cdr, _docs_en_vuelo, _eliminar_data_files

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
        # Descartado: sus boletas ya volvieron a la cola a propósito, retenerlas acá
        # las dejaría sin poder entrar a ningún resumen nuevo. La entrada sigue en el
        # archivo solo para poder honrar un CDR que llegue tarde (ver
        # _olvidar_resumen), no para bloquear nada.
        #
        # Cerrado: sus boletas quedaron en enviado=1 y obtener_boletas_para_resumen()
        # ya las filtra por su cuenta. Hace falta mirarlo acá igual porque la fila del
        # SFS se limpia con el tiempo, y sin esta marca la entrada caía en la rama de
        # "sin rastro" de abajo y avisaba en cada ciclo de un resumen que terminó bien.
        if entrada.get("descartado") or entrada.get("cerrado"):
            continue
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


def _olvidar_resumen(numeracion_rc: str) -> list:
    """
    Libera las boletas de un resumen que no llegó a SUNAT y devuelve cuáles eran,
    CONSERVANDO el registro de qué llevaba.

    Corresponde cuando el envío falló sin devolver ticket, porque libera sus boletas
    para que se reagrupen en un resumen nuevo. Sin esto, reencolar un resumen no
    alcanzaba: al borrar su fila de la bandeja del SFS quedaba sin rastro, y
    _boletas_en_resumenes_activos() retiene ante la falta de rastro —correctamente,
    porque ahí no sabe qué paso—. Las boletas quedaban retenidas por un resumen que ya
    no existía: un bloqueo cambiado por otro.

    Hasta el 2026-09-09 esto hacía un pop() de la entrada, y eso son dos cosas
    distintas pegadas en una: LIBERAR las boletas —correcto— y OLVIDAR cuáles eran
    —nunca correcto—. La inferencia "sin ticket ⇒ SUNAT no lo recibió" no siempre
    vale: durante un bloqueo de ~19 horas varios envíos sí habían llegado y quedaron
    encolados del lado de SUNAT, que los aceptó al recuperarse. Ese CDR tardío llegaba
    a _actualizar_sql_cdr() y se encontraba sin mapeo, así que no podía cerrar nada;
    las boletas seguían en enviado=0, el ciclo las reagrupaba, y arrancaba el bucle de
    redeclaración que dejó 143 boletas declaradas 40 veces.

    Por eso la entrada se marca como descartada en vez de borrarse: es el único dato
    que permite honrar un CDR que llegue después. La poda de _podar_resumenes() se
    encarga de que el archivo no crezca sin fin.
    """
    with _lock_resumenes:
        datos = _leer_resumenes()
        entrada = (datos.get("resumenes") or {}).get(numeracion_rc)
        if entrada is None:
            return []
        entrada["descartado"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _guardar_resumenes(datos)
    return entrada.get("boletas", [])


def _resumen_descartado(numeracion_rc: str) -> str:
    """Cuándo se descartó este resumen, o "" si sigue vigente."""
    entrada = _leer_resumenes().get("resumenes", {}).get(numeracion_rc) or {}
    return _texto(entrada.get("descartado"))


def _marcar_resumen_cerrado(numeracion_rc: str, boletas: list = None):
    """
    Deja constancia de que este resumen ya cerró sus boletas.

    Es la única evidencia positiva de que se resolvió: la fila del SFS se limpia con
    el tiempo, y sin esto no habría forma de distinguir un resumen terminado de uno
    que quedó a medias. La poda lo necesita para no borrar entradas que todavía
    pueden hacer falta, y _boletas_en_resumenes_activos() para no avisar de un
    resumen "sin rastro" que en realidad terminó bien.
    """
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock_resumenes:
        datos = _leer_resumenes()
        resumenes = datos.setdefault("resumenes", {})
        # Puede no existir si el mapeo se reconstruyó desde FIRMA/: se crea igual, para
        # que quede el registro de qué boletas se cerraron y con qué resumen.
        entrada = resumenes.setdefault(numeracion_rc, {"boletas": list(boletas or []),
                                                       "generado": ahora})
        entrada["cerrado"] = ahora
        entrada.pop("descartado", None)   # llegó su CDR: ya no está descartado
        _guardar_resumenes(datos)


def _resumenes_que_repiten(numeracion_rc: str, boletas: list) -> list:
    """
    Otros resúmenes vigentes que declaran alguna de estas mismas boletas.

    Sirve para reconocer un duplicado ante SUNAT sin consultarle nada: si el CDR de un
    resumen descartado llega aceptado y sus boletas ya viajaron en otro resumen, esas
    boletas están declaradas dos veces y alguien va a tener que dar una de baja.
    """
    propias = set(boletas)
    repiten = []
    for otro, entrada in _leer_resumenes().get("resumenes", {}).items():
        if otro == numeracion_rc or entrada.get("descartado"):
            continue
        if propias.intersection(entrada.get("boletas", [])):
            repiten.append(otro)
    return sorted(repiten)


def _boletas_desde_firma(ruc_emisor: str, numeracion_rc: str) -> list:
    """
    Reconstruye qué boletas llevaba un resumen leyendo su XML firmado en FIRMA/.

    Último recurso para cuando la entrada de resumenes.json ya no está: el XML es lo
    que se le declaró a SUNAT, así que su lista de <cbc:ID> es la fuente autoritativa.
    Así se recuperaron a mano las 143 boletas del incidente del 2026-09-09, y así se
    pueden cerrar los CDR tardíos de los resúmenes que se descartaron ANTES de este
    arreglo —esas entradas ya se borraron y no hay forma de recuperarlas de otro lado—.

    El <cbc:ID> del propio resumen (RC-YYYYMMDD-NNN) no entra: el patrón exige los 4
    caracteres de una serie SUNAT, y el del resumen tiene solo 2 letras antes del guión.
    """
    ruta = os.path.join(_SFS_FIRMA_DIR, f"{_nombre_archivo_rc(ruc_emisor, numeracion_rc)}.xml")
    try:
        root = ET.parse(ruta).getroot()
    except (OSError, ET.ParseError):
        return []
    boletas = []
    for elem in root.iter():
        if not isinstance(elem.tag, str) or elem.tag.split("}")[-1].lower() != "id":
            continue
        texto = _texto(elem.text)
        if re.fullmatch(r"[A-Z][A-Z0-9]{3}-\d+", texto):
            boletas.append(texto)
    return list(dict.fromkeys(boletas))   # sin duplicados y en el orden del XML


def _motivo_para_frenar(boletas: list, fecha: str) -> str:
    """
    Por qué NO se debería armar otro resumen ahora mismo, o "" si se puede.

    El 2026-09-09 el ciclo armó, mandó y descartó 84 resúmenes en un día sin que nada
    lo notara: cada vuelta declaraba otra vez las mismas 143 boletas, y ninguna alarma
    distinguía eso de la operación normal. Frenar y pedir intervención cuesta una
    demora; seguir girando cuesta una comunicación de baja por cada boleta duplicada.

    Se mira cuántas veces se declaró cada boleta candidata y cuántos resúmenes lleva el
    día. Lo primero ataja el lote concreto que está girando en falso —es la señal más
    directa—; lo segundo es la red de seguridad por si el bucle vuelve de otra forma.
    """
    veces = {}
    buscadas = set(boletas)
    for entrada in _leer_resumenes().get("resumenes", {}).values():
        for b in buscadas.intersection(entrada.get("boletas", [])):
            veces[b] = veces.get(b, 0) + 1

    repetidas = sorted(b for b, v in veces.items() if v >= MAX_DECLARACIONES_BOLETA)
    if repetidas:
        muestra = ", ".join(repetidas[:_MAX_BLOQUEADOS_LOG])
        if len(repetidas) > _MAX_BLOQUEADOS_LOG:
            muestra += f" ... y {len(repetidas) - _MAX_BLOQUEADOS_LOG} mas"
        return (
            f"{len(repetidas)} boleta(s) ya se declararon {MAX_DECLARACIONES_BOLETA} "
            f"veces o mas sin cerrarse: {muestra}"
        )

    prefijo = f"{_TIPO_RC}-{fecha}-"
    del_dia = sum(1 for n in _leer_resumenes().get("resumenes", {}) if n.startswith(prefijo))
    if del_dia >= MAX_RESUMENES_DIA:
        return f"ya se generaron {del_dia} resúmenes hoy (tope {MAX_RESUMENES_DIA})"
    return ""


def _resumen_vencido(numeracion_rc: str, entrada: dict, ahora: datetime) -> bool:
    """
    True si esta entrada ya cumplió su función y superó el margen de retención.

    Una entrada hace falta mientras su resumen pueda todavía resolverse: hasta que su
    CDR llegue y cierre sus boletas, más un margen holgado por si llega tarde —que es
    justamente el caso que _olvidar_resumen() viene a cubrir—.

    Un resumen SIN resolver no se poda por viejo que sea: ahí la antigüedad es
    exactamente la señal de que algo quedó trabado, y borrarlo perdería el único
    registro de qué boletas retiene.
    """
    try:
        generado = datetime.strptime(_texto(entrada.get("generado")), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False   # sin fecha legible no se toca nada
    if (ahora - generado).days < DIAS_RETENCION_RESUMENES:
        return False
    if entrada.get("cerrado") or entrada.get("descartado"):
        return True
    # Sin marca propia, el CDR en disco alcanza como prueba de que se resolvió: cubre
    # las entradas anteriores a que existieran esas marcas.
    return _tiene_cdr(EMISOR_RUC_OVERRIDE, _TIPO_RC, numeracion_rc)


def _descartar_archivos_de_resumen(ruc_emisor: str, numeracion_rc: str):
    """
    Saca de DATA el .RDI/.TRD de un resumen que se descarta.

    Descartar un resumen libera sus boletas para que se reagrupen en uno nuevo, y eso
    solo es seguro si el original no puede volver a salir por su cuenta. Borrar la fila
    de DOCUMENTO no alcanza: esa tabla es lo que el daemon mira, pero quien realmente
    envía es el SFS, y el SFS trabaja sobre los archivos de DATA. Peor todavía, es el
    propio daemon quien se los vuelve a servir: sincronizar_bandeja_sfs() lo obliga a
    releer DATA en cada ciclo, el archivo huérfano se registra de nuevo en la bandeja
    con su numeración original, y _activar_pendientes_sfs_bd() lo manda.

    El resultado son dos envíos del mismo contenido —el resumen original resucitado y
    el nuevo que armó el daemon con las mismas boletas— sin que ninguna vía sepa de la
    otra. Producción, 2026-09-10: el original salió aceptado y el nuevo volvió con
    "2282: Existe documento ya informado anteriormente". No hubo declaración doble
    porque SUNAT dedupica por contenido, pero eso es suerte, no garantía.

    Es el mismo par que ya hace recuperar_cdr_pendientes() cuando SUNAT dice no tener
    un comprobante: se borra la fila Y los archivos, juntos. Ver también
    _borrar_si_existe(), que documenta este mismo riesgo para el caso de regenerar.
    """
    _eliminar_data_files(_nombre_archivo_rc(ruc_emisor, numeracion_rc))
