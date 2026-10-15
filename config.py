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

# Base de datos de la aplicación (PostgreSQL). Se lee la misma DATABASE_URL que usa
# el sistema de SPAXION, para no mantener la conexión declarada en dos lugares.
DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_TIMEOUT_SEG = int(os.getenv("DB_TIMEOUT_SEG", "30"))

# Emisor override (opcional) — también es el RUC con el que se llama a
# facturador-api (ver fuelhub_api/cliente.py); debe coincidir con el que esa
# empresa tiene dado de alta en su empresas.yaml del lado del servidor.
EMISOR_RUC_OVERRIDE = os.getenv("EMISOR_RUC", "").strip()

# facturador-api: el servicio en la nube que firma y envía los comprobantes a
# SUNAT a través de SFS — reemplaza al SFS local que este daemon manejaba antes
# (ver fuelhub_api/cliente.py y aplicacion/ciclo_generacion.py). Timeout alto
# porque la respuesta de un comprobante individual llega recién cuando SUNAT ya
# contestó, dentro de la misma llamada — no es una API de solo-encolar. 120s
# por el propio contrato: hasta 4 llamadas internas a SFS de 30s cada una.
FACTURADOR_API_URL = os.getenv("FACTURADOR_API_URL", "https://facturador.peru-hub.com").rstrip("/")
FACTURADOR_API_TIMEOUT_SEG = int(os.getenv("FACTURADOR_API_TIMEOUT_SEG", "120"))
# Opcional: hoy facturador-api no tiene autenticación propia. Si alguna vez la
# suma, alcanza con setear esto — el cliente HTTP ya lo manda como Bearer si
# viene con algo.
FACTURADOR_API_KEY = os.getenv("FACTURADOR_API_KEY", "").strip()

# Qué hacer cuando un comprobante (o un resumen diario) queda aceptado por SUNAT,
# además del propio flujo de facturación — ver integraciones/README.md. "noop" por
# defecto: esta integración es best-effort y nunca puede ser la causa de que se deje
# de facturar, así que un valor no configurado o mal escrito no debe tumbar el daemon.
NOTIFICADOR_COMPROBANTES = os.getenv("NOTIFICADOR_COMPROBANTES", "noop").strip().lower()

# Cuántas veces se reenvía a facturador-api un comprobante que volvió
# rechazado/con error (HTTP 422, o 201 con estado "rechazado"/"error"). El
# reenvío manda exactamente los mismos datos, así que si el problema es un dato
# mal armado el resultado no cambia: sin tope, el daemon reenviaría cada ciclo
# indefinidamente. Al agotarse se reporta como bloqueado y espera corrección
# manual (ver estado/envio_api.py) — mismo criterio y mismo nombre de variable
# que ya usaba el daemon con el SFS local, para no cambiarle la expectativa a
# quien ya está acostumbrado a verlo en el log.
MAX_REINTENTOS_RECHAZO = int(os.getenv("MAX_REINTENTOS_RECHAZO", "3"))

# Techo del backoff con el que se reintenta un comprobante trabado por una falla
# de infraestructura (HTTP 502 de facturador-api, timeout, cualquier otro 5xx) —
# no gasta presupuesto de reintentos de MAX_REINTENTOS_RECHAZO, porque no es un
# problema del dato. Se aplana en 15 minutos para que el comprobante salga
# pronto cuando el servicio vuelva, sin quedar esperando media hora de más.
_ESPERA_MAX_INFRA_MIN = int(os.getenv("ESPERA_MAX_INFRA_MIN", "15"))

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

# Días que se suman a la fechaNegocio del cierre de día que se envía a FuelHub core.
# Sin la variable no se corre ninguna fecha.
CIERRE_DIA = int(os.getenv("CIERRE_DIA", "0") or "0")

# Estado propio de la emisión vía facturador-api: qué quedó "en vuelo" justo
# antes de perder la conexión (idempotencia), cuántas veces se rechazó cada
# comprobante (hasta bloquearlo) y el backoff de una falla de infraestructura —
# en disco por el mismo motivo que reporte_facturas.json: un reinicio de PM2 no
# puede perder de vista ese estado (ver estado/envio_api.py).
_ENVIO_API_PATH = os.path.join(_BASE, "envio_api.json")
# Qué semana de reporte top-facturas fue la última enviada a FuelHub core, y la
# Idempotency-Key en curso para la semana que se está intentando mandar — en
# disco por el mismo motivo: un reinicio de PM2 no puede perder de vista qué
# clave ya se usó (ver aplicacion/ciclo_reporte_facturas.py).
_REPORTE_FACTURAS_PATH = os.path.join(_BASE, "reporte_facturas.json")

# Cuántos bloqueados se detallan en el log antes de resumir; son estables entre
# ciclos y volcarlos todos cada 60s ahoga el resto del log.
_MAX_BLOQUEADOS_LOG = 10

# Tipos que facturador-api acepta (mismo catálogo que su propia validación,
# _TIPOS_VALIDOS en su app/esquemas.py). La tabla de origen suele guardar en la
# misma columna otros tipos de documento que esta aplicación nunca maneja —
# guías, liquidaciones, lo que sea—, y mandarlos sería un rechazo seguro que
# además quema los 3 reintentos hasta bloquearlos por nada: se descartan acá,
# antes de intentar nada, igual que hacía el daemon con el SFS local
# ("fuera de alcance", ver historial de aplicacion/ciclo_generacion.py).
_TIPOS_VALIDOS = {"01", "03", "07", "08"}

# Tipos de nota (SUNAT 07/08) — se usa para saber cuándo hace falta armar el
# objeto 'referencia' del payload (ver aplicacion/lecturas.py).
_TIPOS_NOTA = {"07", "08"}

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

# Estados de la columna Comprobante.enviado, que es boolean: no admite un estado
# intermedio — "enviado a facturador-api, esperando su respuesta" no se persiste
# en la BD de la app, solo en estado/envio_api.py (en_vuelo), porque es volátil y
# se resuelve en la misma corrida o en el arranque siguiente.
ENVIADO_PENDIENTE = False
ENVIADO_ACEPTADO  = True

# Recorte defensivo del motivo antes de guardarlo en Comprobante.errors. La columna
# es text y no tiene límite, pero un mensaje enorme no aporta nada.
_MAX_ERRORS_SQL = 4000

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

# Envío a FuelHub core del PDF de comprobantes (Comprobantes.pdf_bytes /
# pdf_enviado, ver fuelhub_core/bd.py y aplicacion/ciclo_pdf.py) para que la
# página web de consulta lo sirva desde S3 — mismas credenciales
# FUELHUB_CORE_CLIENT_ID/SECRET que los cierres, sin ninguna credencial de AWS
# en esta PC. Solo existe en instalaciones de grifo con SQL Server, igual que
# Cierreturnos/Cierredias.
INTERVALO_PDF_SEG = int(os.getenv("INTERVALO_PDF_SEG", "60"))

# Reporte semanal antifraude (POST v1/reportes/top-facturas): el top de clientes
# que más facturaron en la semana ya cerrada (lunes a domingo) y qué despachador
# generó cada factura — ver aplicacion/ciclo_reporte_facturas.py. Sin apuro de
# horario (a diferencia de cierres/pdf): un intervalo largo alcanza, el propio
# ciclo decide solo si ya le toca mandar la semana o si ya la mandó.
INTERVALO_REPORTE_FACTURAS_SEG = int(os.getenv("INTERVALO_REPORTE_FACTURAS_SEG", "3600"))
# Tope de clientes que entran al reporte — fijo por contrato con FuelHub core, no
# tiene sentido exponerlo como variable de entorno.
TOP_FACTURAS_CANTIDAD = 20

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
