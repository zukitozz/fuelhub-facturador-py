"""
Configuración del daemon: variables de entorno, rutas, catálogos e intervalos, más
el arranque del logging. Es el único módulo que TODOS los demás pueden importar sin
crear un ciclo — por eso concentra acá todo lo que antes vivía suelto al principio
de main.py.

Se importa una sola vez por proceso (Python cachea el módulo): quien lo importe
primero dispara la carga del .env y la configuración de logging.
"""
import logging
import logging.handlers   # submodulo aparte: 'import logging' no lo trae
import os
import sys

from dotenv import load_dotenv

_BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE, ".env"))

# ---------------------------------------------------------------------------
# Intervalos
# ---------------------------------------------------------------------------
INTERVALO_GENERACION_SEG = int(os.getenv("INTERVALO_GENERACION_SEG", "60"))
# Red de seguridad del hilo CDR: barrido completo de RPTA además de los eventos de
# watchdog (ver aplicacion/hilos.py: hilo_cdr).
INTERVALO_BARRIDO_RPTA_SEG = int(os.getenv("INTERVALO_BARRIDO_RPTA_SEG", "30"))

# Base de datos de la aplicación (PostgreSQL). Se lee la misma DATABASE_URL que usa
# el sistema de SPAXION, para no mantener la conexión declarada en dos lugares.
DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_TIMEOUT_SEG = int(os.getenv("DB_TIMEOUT_SEG", "30"))

# Rutas SFS
SFS_DATA_DIR = p if os.path.exists(p := os.getenv("SFS_DATA_DIR", r"C:\SFS_v-2.1\sunat_archivos\sfs\DATA")) else os.path.join(_BASE, "sunat_archivos", "DATA")
SFS_RPTA_DIR = p if os.path.exists(p := os.getenv("SFS_RPTA_DIR", r"C:\SFS_v-2.1\sunat_archivos\sfs\RPTA")) else os.path.join(_BASE, "sunat_archivos", "RPTA")

# Donde el SFS deja el XML firmado de cada documento. Se deriva de DATA en vez de
# configurarse aparte porque son hermanas dentro de sunat_archivos/sfs: si alguien
# mueve la instalacion, DATA ya trae la ruta nueva y esta la sigue sola.
_SFS_FIRMA_DIR = os.path.join(os.path.dirname(SFS_DATA_DIR), "FIRMA")

SFS_BD_PATH  = os.getenv("SFS_BD_PATH",  r"C:\SFS_v-2.1\bd\BDFacturador.db")
SFS_BASE_URL = os.getenv("SFS_BASE_URL", "http://localhost:9000")

# Configuración del SFS: de acá sale a qué ambiente de SUNAT está enviando
# (RUTA_SERV_CDP), que es donde hay que consultar el ticket de un resumen. Se
# deriva de SFS_DATA_DIR —son carpetas hermanas— para no declarar otra ruta que
# después quede desincronizada.
SFS_CONSTANTES_PATH = os.getenv(
    "SFS_CONSTANTES_PATH",
    os.path.join(os.path.dirname(SFS_DATA_DIR), "VALI", "constantes.properties"),
)

DIR_PROCESADOS = os.path.join(SFS_RPTA_DIR, "procesados")
DIR_ERRORES    = os.path.join(SFS_RPTA_DIR, "errores")

# Emisor override (opcional)
EMISOR_RUC_OVERRIDE = os.getenv("EMISOR_RUC", "").strip()

# Consulta a SUNAT del estado de un comprobante (servicio billConsultService).
# Sirve para saber si un documento llegó cuando se cortó la conexión y no se sabe
# si se envió: si está registrado devuelve su CDR, y si no, recién ahí se reenvía.
# OJO: SUNAT solo publica este servicio en producción — en beta responde 404. Aun
# así consultar no emite nada: es de solo lectura y es independiente del ambiente
# al que el SFS manda los comprobantes.
SUNAT_CONSULTA_URL = os.getenv(
    "SUNAT_CONSULTA_URL",
    "https://e-factura.sunat.gob.pe/ol-it-wsconscpegem/billConsultService",
)
SOL_USUARIO = os.getenv("SOL_USUARIO", "").strip()
SOL_CLAVE   = os.getenv("SOL_CLAVE", "").strip()

# Qué hacer cuando un comprobante (o un resumen diario) queda aceptado por SUNAT,
# además del propio flujo de facturación — ver integraciones/README.md. "noop" por
# defecto: esta integración es best-effort y nunca puede ser la causa de que se deje
# de facturar, así que un valor no configurado o mal escrito no debe tumbar el daemon.
NOTIFICADOR_COMPROBANTES = os.getenv("NOTIFICADOR_COMPROBANTES", "noop").strip().lower()

# Códigos que confirman que SUNAT NO tiene el comprobante, y por lo tanto habilitan
# a reenviarlo. Es una lista blanca a propósito: verificado contra el servicio real,
# el 0127 es el "no registrado" —aunque su texto diga "El ticket no existe", que es
# un mensaje genérico reutilizado—. Cualquier otro código se toma como incierto.
_CODIGOS_NO_REGISTRADO = ("0127",)

# Códigos que dicen que la consulta falló, no que el comprobante no exista.
# Verificado contra el catálogo de códigos de SUNAT:
#   0100  El sistema no puede responder su solicitud. Intente nuevamente
#   0125  No se pudo obtener la constancia
#   0126  El ticket no le pertenece al usuario
# Son fallas del lado de SUNAT al recuperar el CDR, así que la consulta se repite más
# tarde en vez de darla por perdida. Ojo con la distinción: que sean transitorios NO
# habilita a reenviar el comprobante —sigue sin saberse si SUNAT lo tiene—, solo a
# volver a preguntar. El único que autoriza el reenvío es el 0127, y vive aparte.
#
# El caso que lo motivó (2026-09-05): F003-006240 recibía 0125 en cada consulta y
# quedaba en 'desconocido' para siempre, repitiendo un WARNING que nadie termina de
# notar, mientras en SUNAT la factura estaba aceptada.
_CODIGOS_CONSULTA_FALLIDA = ("0100", "0125", "0126")

# Cuántas consultas seguidas pueden fallar antes de reportar el comprobante como
# bloqueado. Sin este tope, un servicio caído por días no se distingue de uno que
# tarda un minuto: los dos se ven igual en el log.
MAX_CONSULTAS_FALLIDAS = int(os.getenv("MAX_CONSULTAS_FALLIDAS", "10"))

# Cuánto esperar antes de preguntarle a SUNAT por un comprobante que ya se envió y
# sigue sin CDR. Por debajo de esto lo más probable es que el CDR solo esté demorando.
CONSULTA_SUNAT_TRAS_MIN = int(os.getenv("CONSULTA_SUNAT_TRAS_MIN", "10"))

# Cuántas horas puede un ticket contestar "todavía lo estoy procesando" antes de que
# el aviso escale. SUNAT normalmente tarda minutos, así que 3 horas es holgado de
# sobra; el numero importa por el otro lado, porque un resumen estuvo 24 horas asi
# --con 200 boletas retenidas y ya muerto del lado de SUNAT-- sin que nada lo
# señalara. El max(1, ...) evita que un 0 en el .env convierta cada consulta normal
# en una alarma.
HORAS_TICKET_EN_PROCESO = max(1, int(os.getenv("HORAS_TICKET_EN_PROCESO", "3")))

# Cuánto puede quedarse un CDR en 0 bytes antes de darlo por abandonado. Tiene que
# ser holgado frente a lo que tarda el SFS en escribir un ZIP —segundos— para no
# apartar uno que todavía se está escribiendo.
MINUTOS_CDR_VACIO = int(os.getenv("MINUTOS_CDR_VACIO", "10"))
# Cada cuánto se puede volver a consultar el mismo documento, para no golpear el
# servicio de SUNAT en cada ciclo por algo que sigue igual.
_COOLDOWN_CONSULTA_SEG = 900

# IND_SITU de la BD del SFS (sistema/facturador/util/Constantes del facturadorApp).
_NOMBRE_SITU = {
    "01": "por generar XML",
    "02": "XML generado",
    "03": "aceptado",
    "04": "aceptado con observaciones",
    "05": "anulado",
    "06": "con errores",
    "07": "XML por validar",
    "08": "enviado, por procesar",
    "09": "enviado, procesando",
    "10": "rechazado por SUNAT",
    "11": "CDR descargado",
    "12": "CDR descargado con observaciones",
}

# Estados SFS que se reintentan. El '10' (rechazado) deja el comprobante sin emitir,
# así que corresponde volver a intentarlo. El '06' ('con errores') se suma porque el
# SFS lo usa para dos cosas distintas: un dato mal armado —que reintentar no arregla—
# y cualquier falla de comunicación con SUNAT ("Hubo un problema al invocar servicio
# SUNAT: Could not send Message."), que sí se resuelve sola cuando vuelve la conexión.
# Sin reintentarlo, un corte de red dejaba comprobantes trabados en DOCUMENTO hasta
# que alguien borraba esas filas a mano. Para el '06' de dato el desenlace no cambia:
# agota los reintentos y termina reportado como bloqueado, igual que antes.
# El '05' NO va acá aunque antes estuviera — es ENVIADO_ANULADO, no un error, y
# reintentarlo significaba reenviar a SUNAT un documento que se había anulado.
_ESTADOS_ERROR = ("06", "10")
# Estados SFS terminales que el daemon NO puede resolver solo: o el SFS generó el XML
# pero no lo envió (boleta de más de 5 días que exige resumen diario, rechazo de
# validación), o el documento quedó anulado. Reintentar no sirve —el resultado sería
# el mismo—, así que se reportan en cada ciclo para que alguien los atienda a mano.
# El '06' y el '10' entran en la lista porque resetear_rechazados() corre ANTES del
# reporte y borra los que todavía tienen reintentos disponibles: si uno de esos dos
# sigue en la tabla al momento de reportar, es porque agotó el tope.
_ESTADOS_BLOQUEADO = ("05", "06", "10")
# Estados en los que el SFS ya cerró el documento con SUNAT: sus archivos de DATA
# no se vuelven a necesitar y hay que borrarlos. Son 5 por comprobante, así que a
# 300 diarios se acumulan 1500 archivos por día en la carpeta que el SFS relee en
# cada pasada.
_ESTADOS_CERRADOS = ("03", "04")

# Estados en los que un resumen sigue en juego: ya salio hacia SUNAT y todavia puede
# resolverse. Lo usan las dos puntas del mismo flujo --la consulta por ticket
# (_resumenes_con_ticket) y el cierre una vez procesado el CDR
# (_cerrar_resumen_en_sfs)--, y viven de una sola constante justamente porque se
# desincronizaron: al ampliar solo la consulta para rescatar los resumenes en '05',
# el cierre siguio exigiendo '08'/'09', asi que un resumen rescatado se consultaba
# para siempre y nunca podia cerrarse. Quedan afuera los cerrados, y tambien el '01'
# y el '02': ahi el resumen todavia no salio, y darlo por aceptado seria mentir.
_ESTADOS_RESUMEN_ABIERTO = ("05", "06", "08", "09", "10")

# Cuántas veces se reenvía un comprobante que SUNAT rechazó. El reenvío manda
# exactamente los mismos datos, así que si el rechazo es por un dato mal armado el
# resultado no cambia: sin tope, el daemon reenvía cada ciclo indefinidamente. Al
# agotarse se reporta como bloqueado y espera corrección manual.
MAX_REINTENTOS_RECHAZO = int(os.getenv("MAX_REINTENTOS_RECHAZO", "3"))

# Motivo (catalogo 09/10) que se le pone a una nota cuya aplicacion no lo guarda.
# Vacio por defecto: sin esto, el daemon NO inventa un motivo y la nota queda sin
# emitir, que es lo correcto cuando el sistema de origen sabe distinguir entre una
# anulacion, un descuento y un ajuste de valor.
#
# Se configura solo donde la aplicacion no puede generar esa diferencia. El caso que
# lo motivo: una pantalla de nota de credito que copia el total y los items del
# comprobante original sin permitir montos parciales, asi que toda nota que puede
# crear es una anulacion completa —"01"— y no hay ambiguedad que resolver. Poner un
# motivo por defecto donde SI se pueden emitir notas parciales es declararle a SUNAT
# algo distinto de lo que paso.
MOTIVO_NOTA_POR_DEFECTO = os.getenv("MOTIVO_NOTA_POR_DEFECTO", "").strip()
# SUNAT rechaza un resumen diario con mas de 500 boletas. El tope por defecto es
# 200 porque es el lote que recomienda el proveedor: un resumen mas chico se firma
# y se acepta mas rapido, y si SUNAT lo observa hay menos boletas que rehacer. Lo
# que sobra no se pierde, va en el resumen del ciclo siguiente.
# El max(1, ...) no es paranoia: con un 0 en el .env el resumen salia vacio, y con
# un negativo descartaba boletas en silencio.
MAX_BOLETAS_RESUMEN = max(1, min(int(os.getenv("MAX_BOLETAS_RESUMEN", "200")), 500))

# Frenos contra el bucle de redeclaración. Visto en produccion el 2026-09-09, tras un
# bloqueo de SUNAT de ~19 horas: 84 resumenes en un dia —lo normal son 2— y 143 boletas
# declaradas 40 veces cada una, sin que nada lo notara ni lo frenara en horas.
#
# El ciclo era: el envio falla sin ticket, se descarta el resumen, las boletas vuelven a
# la cola, se arma otro, SUNAT contesta "2282 - Existe documento ya informado
# anteriormente", y otra vez. Cada vuelta suma un duplicado ante SUNAT, y un duplicado
# solo se deshace con una comunicacion de baja: por eso acá conviene errar por frenar de
# mas. Detenerse y pedir intervencion cuesta una demora; seguir declarando cuesta un
# tramite por cada boleta.
#
# Son dos topes porque atajan el problema en momentos distintos: el de declaraciones
# frena el lote concreto que esta girando en falso, y el diario es la red de seguridad
# por si el bucle aparece de una forma que no previmos.
MAX_DECLARACIONES_BOLETA = max(1, int(os.getenv("MAX_DECLARACIONES_BOLETA", "3")))
MAX_RESUMENES_DIA = max(1, int(os.getenv("MAX_RESUMENES_DIA", "20")))

# Cuanto se conserva la entrada de un resumen ya resuelto en resumenes.json. El margen
# es amplio a proposito: ese archivo es el unico registro de que boletas llevo cada
# resumen, y es lo que permitio reconstruir las 143 del incidente del 2026-09-09.
# Perderlo temprano deja ciego al proximo diagnostico, y lo que se ahorra son kilobytes.
DIAS_RETENCION_RESUMENES = max(1, int(os.getenv("DIAS_RETENCION_RESUMENES", "30")))

# Techo del backoff con el que se reintenta un comprobante trabado por un corte de
# red. No gasta presupuesto de reintentos (ver _es_falla_de_red en estado/reintentos.py),
# así que necesita espaciarse solo: sin esto, un corte de dos horas son 120 reenvíos
# inútiles. Se aplana en 15 minutos para que el comprobante salga pronto cuando el
# servicio vuelva, sin quedar esperando media hora de más.
_ESPERA_MAX_RED_MIN = int(os.getenv("ESPERA_MAX_RED_MIN", "15"))

# Igual que reintentos.json: el correlativo del resumen y qué boletas lleva cada uno
# viven en disco, porque un reinicio de PM2 no puede repetir un RC-YYYYMMDD-NNN ya
# usado ni perder de vista qué boletas quedaron esperando su CDR.
_RESUMENES_PATH = os.path.join(_BASE, "resumenes.json")
# El contador vive en disco: en memoria, un reinicio de PM2 —que reinicia solo— haría
# arrancar la cuenta de cero y el bucle volvería a ser infinito.
_REINTENTOS_PATH = os.path.join(_BASE, "reintentos.json")

# Cuántos bloqueados se detallan en el log antes de resumir; son estables entre
# ciclos y volcarlos todos cada 60s ahoga el resto del log.
_MAX_BLOQUEADOS_LOG = 10
# Tipos que el daemon le entrega al SFS: factura, boleta, nota de credito, nota de
# debito y resumen diario de boletas. Las boletas nunca salen sueltas —van siempre
# por el resumen— asi que el 03 de este set cubre las que el SFS ya tiene en su
# bandeja, no la emision individual. RA (comunicacion de baja) queda fuera: el
# daemon no la emite.
_TIPOS_SFS = {"01", "03", "07", "08", "RC"}

# Notas: el SFS las parsea con PipeNotaCreditoParser / PipeNotaDebitoParser, que
# esperan una cabecera de 21 columnas —sin fecVencimiento y con
# codMotivo|desMotivo|tipDocAfectado|numDocAfectado después de moneda—. Esos 4
# campos son los que la plantilla del SFS convierte en el <cac:DiscrepancyResponse>
# y el <cac:BillingReference> del XML, que SUNAT exige en toda nota.
_TIPOS_NOTA = {"07", "08"}

# El archivo .PAG genera un cac:PaymentTerms con PaymentMeansID='Contado', y solo la
# factura lo admite así. Los demás validadores lo rechazan:
#   boleta (03): ValidaExprRegBoleta no conoce 'FormaPago' y lee ese nodo como
#                información de detracción -> error 3128 salvo operación 1001-1004.
#   notas (07/08): en una nota el nodo sirve únicamente para crédito y cuotas; el
#                valor debe ser 'Credito' o empezar con 'Cuota' -> error 3246.
# Si algún día se emiten notas al crédito, la forma de pago vuelve pero con ese
# formato, no con 'Contado'.
_TIPOS_SIN_FORMA_PAGO = {"03", "07", "08"}

# El SFS identifica cada documento por su archivo de cabecera, y la extensión cambia
# según el tipo (ver BandejaDocumentosServiceImpl): .CAB para factura y boleta, .NOT
# para las notas. Con la cabecera en el archivo equivocado el SFS ni siquiera
# reconoce el documento: responde "El archivo no existe: ...NOT".
_EXT_CABECERA = {"07": "NOT", "08": "NOT"}
_EXT_CABECERA_POR_DEFECTO = "cab"

# Todas las extensiones que el daemon puede escribir en DATA. Ningún comprobante
# las lleva todas: la cabecera es .cab o .NOT según el tipo, y el .PAG solo va en
# facturas. Se listan juntas para armar rutas y para barrer al limpiar.
_EXT_DATA = ("cab", "NOT", "det", "tri", "ley", "PAG")
# Las que además puede dejar el SFS (resumen y reversión); se barren al limpiar.
_EXT_DATA_SFS = ("RDI", "TRD", "DET")

# Catálogos SUNAT 09 (nota de crédito) y 10 (nota de débito). Solo se usan para
# completar desMotivo cuando la BD no trae descripción; el código siempre sale de
# Comprobante.tipoNota, nunca se infiere.
_MOTIVOS_NOTA = {
    "07": {
        "01": "ANULACION DE LA OPERACION",
        "02": "ANULACION POR ERROR EN EL RUC",
        "03": "CORRECCION POR ERROR EN LA DESCRIPCION",
        "04": "DESCUENTO GLOBAL",
        "05": "DESCUENTO POR ITEM",
        "06": "DEVOLUCION TOTAL",
        "07": "DEVOLUCION POR ITEM",
        "08": "BONIFICACION",
        "09": "DISMINUCION EN EL VALOR",
        "10": "OTROS CONCEPTOS",
        "11": "AJUSTES DE OPERACIONES DE EXPORTACION",
        "12": "AJUSTES AFECTOS AL IVAP",
        "13": "AJUSTES - MONTOS Y/O FECHAS DE PAGO",
    },
    "08": {
        "01": "INTERES POR MORA",
        "02": "AUMENTO EN EL VALOR",
        "03": "PENALIDADES / OTROS CONCEPTOS",
        "11": "AJUSTES DE OPERACIONES DE EXPORTACION",
        "12": "AJUSTES AFECTOS AL IVAP",
    },
}

# Un mismo documento no se reintenta antes de este lapso. Solo evita llamadas
# repetidas dentro del mismo ciclo: debe ser MENOR al intervalo de generación para
# que el ciclo siguiente pueda avanzar un documento que quedó a medio procesar.
# El reenvío queda acotado por el estado, no por el tiempo: _activar_pendientes_sfs_bd()
# solo mira IND_SITU '01'/'02' (aún sin enviar); ver _ESTADOS_BLOQUEADO para el resto.
_COOLDOWN_REENVIO_SEG = 45

# El SFS no registra un resumen diario en su propia bandeja apenas responde EXITO a
# GenerarComprobante.htm: lo escanea un job interno aparte, que tardó hasta ~90s en
# la práctica. Sin este resguardo, _boletas_en_resumenes_activos() no veía el
# resumen recién generado durante ese lapso y el siguiente ciclo (60s) generaba un
# segundo resumen con las mismas boletas — confirmado en beta: dos RC duplicados
# con el mismo pool de 5 boletas antes de que el primero apareciera en la bandeja.
_GRACIA_REGISTRO_RC_SEG = 300

# Pausas al conversar con el SFS. No son arbitrarias: el facturador procesa los
# archivos de DATA en background, así que hay que darle tiempo entre el pedido de
# generación y el de envío o responde "No existen datos que procesar".
_ESPERA_XML_SEG        = 2   # tras pedir la generación del XML
_ESPERA_REINTENTO_SEG  = 3   # antes de reintentar un envío que falló
_ESPERA_ENTRE_DOCS_SEG = 1   # para no saturar al SFS documento tras documento

# Estados de la columna Comprobante.enviado, que es boolean: no admite un estado
# intermedio. El "entregado al SFS, esperando CDR" se deduce de la BD del SFS,
# ver sfs/bd.py: _docs_en_vuelo().
ENVIADO_PENDIENTE = False   # por generar / reintentar
ENVIADO_ACEPTADO  = True    # SUNAT devolvió un CDR de aceptación

# Estados de CDR que dan por buena la emisión (SUNAT acepta con y sin observaciones)
_CDR_ACEPTADOS = {"ACEPTADO", "OBSERVADO"}

# Recorte defensivo del motivo antes de guardarlo en Comprobante.errors. La columna
# es text y no tiene límite, pero un mensaje enorme de SUNAT no aporta nada.
_MAX_ERRORS_SQL = 4000

# DOCUMENTO.DES_OBSE del SFS es VARCHAR(250). SQLite no lo hace cumplir, pero la
# aplicacion Java si lo lee con ese ancho: pasarse es arriesgarse a que lo corte de
# una forma que no controlamos.
_MAX_DES_OBSE = 250

# Códigos de getStatus (distintos de los de getStatusCdr): 0 y 99 traen el CDR —el
# 99 es el de un resumen procesado CON errores, y su CDR explica cuáles—, mientras
# que el 98 significa que SUNAT todavía lo está procesando.
# SUNAT no es consistente consigo mismo en este mismo servicio: verificado en
# producción el 2026-09-06, la aceptación vuelve como '0' —un carácter— y el "en
# proceso" como '0098' —cuatro—. Por eso los códigos se normalizan antes de comparar
# (ver _norm_codigo_ticket): con las constantes escritas a mano, '0098' == '98' daba
# False siempre y la rama de "en proceso" era código muerto.
_TICKET_CON_CDR    = ("0", "98", "99")
_TICKET_EN_PROCESO = "98"
# Un ticket se consume al consultarlo: a la segunda vez SUNAT responde con este
# código y ya no hay CDR que recuperar por esa vía. Es el único veredicto definitivo
# de la consulta de tickets —todo lo demás merece otro intento— y por eso vale la
# pena distinguirlo en vez de tratarlo como una falla más.
_TICKET_NO_EXISTE = "127"

# FuelHub core: recibe los cierres de turno y de día (Cierreturnos/Cierredias con
# enviado=0, ver aplicacion/ciclo_cierres.py). Solo existe en instalaciones de
# grifo con SQL Server — ver fuelhub_core/bd.py.
INTERVALO_CIERRES_SEG = int(os.getenv("INTERVALO_CIERRES_SEG", "60"))
FUELHUB_CORE_BASE_URL = os.getenv(
    "FUELHUB_CORE_BASE_URL",
    "https://6gy2rrty17.execute-api.us-east-2.amazonaws.com/prod",
)
# Token OAuth2 (client_credentials) de Cognito. El client_id/secret son de un
# cliente de la app registrada para este daemon — no hay valor por defecto: sin
# ellos el hilo de cierres queda inactivo (ver fuelhub_core/api.py).
FUELHUB_CORE_TOKEN_URL = os.getenv(
    "FUELHUB_CORE_TOKEN_URL",
    "https://us-east-2nq1gjcb0j.auth.us-east-2.amazoncognito.com/oauth2/token",
)
FUELHUB_CORE_CLIENT_ID     = os.getenv("FUELHUB_CORE_CLIENT_ID", "").strip()
FUELHUB_CORE_CLIENT_SECRET = os.getenv("FUELHUB_CORE_CLIENT_SECRET", "").strip()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# El log rota a los 5 MB y se conservan 5 archivos: unos 25 MB en total. Es el
# registro de qué pasó con cada comprobante, así que hay que poder mirar atrás
# —un rechazo puede investigarse semanas después—, pero sin que crezca sin
# límite en una PC que va a estar años emitiendo.
LOG_MAX_MB   = int(os.getenv("LOG_MAX_MB", "5"))
LOG_ARCHIVOS = int(os.getenv("LOG_ARCHIVOS", "5"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            os.path.join(_BASE, "facturador.log"),
            maxBytes=LOG_MAX_MB * 1024 * 1024,
            backupCount=LOG_ARCHIVOS,
            encoding="utf-8",
        ),
    ],
)
