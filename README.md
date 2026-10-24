# Facturador

Daemon de Facturación Electrónica SUNAT.

Corre en segundo plano y emite comprobantes sin intervención: lee los pendientes de
la base de datos de la aplicación y se los manda a
[**facturador-api**](https://facturador.peru-hub.com) — el servicio en la nube que
firma cada comprobante y lo envía a SUNAT a través de SFS. La respuesta (aceptado,
rechazado, hash, QR) llega en la misma llamada.

Antes este daemon manejaba un SFS local en la misma PC (escribía archivos, vigilaba
una carpeta a la espera del CDR, consultaba tickets de SUNAT por SOAP). Todo eso vive
ahora del lado de facturador-api — ver `aplicacion/ciclo_generacion.py` y
`fuelhub_api/cliente.py`.

Factura, nota de crédito y nota de débito se envían y se resuelven de a una; la
boleta se manda igual, pero facturador-api la firma sin enviarla sola — queda a la
espera del resumen diario, que este mismo daemon dispara una vez al día.

Trabaja con cuatro hilos (ver `aplicacion/hilos.py`):

- **Generador**: cada `INTERVALO_GENERACION_SEG` (60s) manda los comprobantes
  pendientes a facturador-api, y una vez al día arma el resumen de boletas.
- **Cierres**, **PDF**, **Reporte**: hablan con **FuelHub core** (otro servicio,
  sin relación con SUNAT) — cierres de turno/día, subida del PDF de cada comprobante,
  y el reporte semanal antifraude top-facturas. Solo corren en instalaciones de
  grifo con SQL Server; ver sus propios módulos en `aplicacion/`.

## Tipos de comprobante

Factura (`01`), nota de crédito (`07`), nota de débito (`08`) y boleta (`03`). Para
anular un comprobante ya aceptado se emite una nota de crédito.

Las notas exigen la referencia al documento que corrigen, y esos campos salen de la
BD de la aplicación:

| Campo del payload | Columna |
|---|---|
| `tipo_nota` | `tipoNota` (catálogo 09 para NC, 10 para ND) |
| `motivo_documento_afectado` | `motivoDocumentoAfectado` (si está vacío, la descripción del catálogo) |
| `tipo_documento_afectado` | `tipoDocumentoAfectado` |
| `numeracion_documento_afectado` | `numeracionDocumentoAfectado` |

Si a una nota le falta `tipo_nota`, `tipo_documento_afectado` o
`numeracion_documento_afectado`, **no se emite**: queda un WARNING y se reintenta
cuando alguien complete el dato. El motivo no se deduce ni se rellena por defecto
—salvo que se configure `MOTIVO_NOTA_POR_DEFECTO`, ver más abajo— porque una nota
con el motivo equivocado es una declaración incorrecta ante SUNAT.

**Nota sobre una boleta.** No se manda hasta que esa boleta esté efectivamente
aceptada por SUNAT (`_referencia_boleta_lista` en `ciclo_generacion.py`): acá la
boleta queda `enviado=1` apenas se firma, pero SUNAT recién la conoce cuando su
resumen diario la declara y es aceptado, que puede tardar horas — mandar la nota
antes referenciaría un documento que SUNAT todavía no tiene. Se consulta con un
`GET` en cada ciclo hasta que la boleta quede `aceptado`; mientras tanto, la nota
queda esperando sin contar como rechazo. Una nota sobre una factura no tiene este
problema, se cierra en la misma llamada del `POST`.

## Validaciones previas

Antes de mandar cualquier comprobante a facturador-api, pasa por
`_validar_campos_obligatorios()` (`dominio/comprobante.py`): numeración, fecha de
emisión válida y total. Si falta algo **no se envía nada**: WARNING con el detalle
y reintento en el próximo ciclo, cuando se corrija el dato.

Eso incluye las ventas que la aplicación cobró pero nunca numeró. Antes se
descartaban en el SQL de las consultas de pendientes y **desaparecían sin dejar una
línea en el log**; ahora llegan a la validación, que las identifica por su `id` de
fila ya que no tienen numeración. El aviso sale **una vez por corrida**, no en cada
ciclo: el dato no se corrige solo y repetir la misma línea cada 60 segundos llenaría
el log. Si sigue sin resolverse, reaparece en el próximo arranque.

No valida dígito verificador de RUC/DNI ni cuadre de totales. Tampoco mira el IGV
de cada ítem: el detalle se arma siempre como gravado al 18%, correcto para los
servicios de estética que es lo que se factura acá, pero un ítem exonerado o de
cortesía haría que SUNAT rechace. Resolverlo exige una columna de tipo de afectación
en `FacturaItem`, que la aplicación no tiene.

## Fecha de emisión y zona horaria

La aplicación guarda sus fechas con el reloj de su servidor de base de datos, que
suele correr en **UTC**; SUNAT espera la hora local del emisor. Sin corregirlo, toda
venta entre las 19:00 y la medianoche (Perú es UTC−5) queda con la fecha del día
siguiente: a SUNAT se le declararía una fecha que todavía no llegó.

Por eso el daemon **mide** el desfase en vez de asumirlo: al inicio de cada ciclo
compara el reloj de la BD con la hora local y aplica esa corrección
(`utilidades_timer.py`: `detectar_desfase_bd()` → `fecha_local()`). Si cambian la
zona horaria del servidor, se ajusta solo en el ciclo siguiente; si la medición
falla, conserva la última corrección conocida en vez de arriesgar una fecha
equivocada. Se puede forzar con `DESFASE_BD_HORAS`.

## Manejo de la respuesta de facturador-api

El `POST /empresas/{ruc}/comprobantes` se hace siempre con `?incluir=xml` —
para los dos tipos por igual, no solo boleta: es la única oportunidad de
conseguir el XML de una boleta, porque una vez que el resumen diario la
consume su propia consulta (`GET`) empieza a devolver `404`. El CDR no se pide
ahí; se consulta aparte (ver `ciclo_pdf.py`). Ver
`aplicacion/ciclo_generacion.py` para el detalle completo — en resumen:

| `estado` de la respuesta | Qué hace el daemon |
|---|---|
| `aceptado` / `aceptado_con_observaciones` | Guarda hash, QR, XML y url (`guardar_respuesta`); marca `enviado=1` |
| `generado` (boleta) | Igual que aceptado — una boleta nunca pasa por "aceptado" individual, éste ya es su cierre |
| `generado` (factura/nota) | No se toca nada: sigue `enviado=0` y el próximo ciclo reintenta el `POST` solo |
| `enviado` / `validando` / `pendiente` | SUNAT ya la tiene, falta el CDR: guarda el XML si vino, no marca enviado, se reconsulta por `GET` en cada ciclo hasta tener veredicto |
| `rechazado` / HTTP 422 | Guarda el motivo (`codigo_error`+`descripcion_error`) en `errors`; **no** reintenta solo más de `MAX_REINTENTOS_RECHAZO` veces |
| `anulado` | Venció el plazo de 7 días sin que SUNAT lo recibiera: se trata igual que un rechazo (hace falta un número nuevo, nunca va a poder reenviarse con el mismo) |
| `error` | Ambiguo por diseño: se deja tal cual, SIN backoff propio — la API ya reintenta sola (5 intentos cada 10 min, hasta 3 días) y verifica con SUNAT antes de reenviar |
| HTTP 404 (RUC sin alta) | Error crítico: corta el ciclo entero |
| HTTP 502 | La API ya verificó que no se envió nada: se reintenta en el próximo ciclo, sin backoff propio ni gastar presupuesto de rechazos |
| Sin respuesta HTTP (timeout real) | Ambiguo de verdad: se resuelve con un `GET` antes de seguir — nunca se reenvía a ciegas |

**Tope de reenvíos.** Un reenvío manda exactamente los mismos datos, así que si
facturador-api rechazó por un dato mal armado el resultado no cambia. De ahí el
tope (`MAX_REINTENTOS_RECHAZO`, 3 por defecto); al agotarlo el comprobante queda
bloqueado hasta que se corrija el dato. Un `error`/`502`/timeout, en cambio, **no
tiene backoff propio**: eso ahora lo hace la API sola (ver más abajo), así que el
daemon simplemente lo vuelve a intentar en el próximo ciclo sin contar nada. El
único backoff que queda de este lado es el del **ciclo entero**
(`ESPERA_MAX_CICLO_MIN`, 2 horas por defecto): si el daemon directamente no puede
llegar a facturador-api —sin internet en la estación, el servicio caído de
verdad—, reintentar cada `INTERVALO_GENERACION_SEG` no tiene sentido y solo llena
el log, así que la pausa entre corridas crece hasta ese techo. El conteo de
rechazos y de fallos de ciclo vive en `envio_api.json` (no se versiona), junto
con la idempotencia ante un corte a mitad de un envío y los comprobantes
esperando CDR — ver `estado/envio_api.py`.

## Resumen diario de boletas

Ninguna boleta se envía individualmente — facturador-api la firma pero la agrupa en
un resumen diario (`RC`) del lado del servidor, sola: todos los días a las 00:10
hora de Lima arma y envía un resumen por cada fecha pendiente, resuelve el ticket
de SUNAT y deja el CDR, sin que el daemon tenga que dispararlo ni seguirlo de a
uno (antes el propio daemon llamaba `POST /resumenes-diarios`; ya no).

Lo único que hace el daemon, una vez al día, es **conciliar**: `GET
/empresas/{ruc}/resumenes` trae los resúmenes recientes con sus boletas y su
`estado`.

- **Aceptado**: nada que hacer — cada boleta ya quedó `enviado=1` desde que se
  firmó individualmente (`generado`), con su propio XML ya guardado.
- **Rechazado**: las boletas de ese resumen vuelven a `enviado=0` con el motivo
  guardado, para que el próximo ciclo las regenere (nuevo `POST` individual,
  nuevo XML) y entren a un resumen nuevo. Como `/resumenes` trae el historial
  completo (no solo lo pendiente), el daemon recuerda en `envio_api.json` qué
  resumen rechazado ya reencoló, para no repetirlo cada vez que lo vuelva a ver
  en la lista.

## Requisitos

Python 3.10+, la base de la aplicación accesible (PostgreSQL o SQL Server), acceso
de red a `https://facturador.peru-hub.com` (o la URL que corresponda) y
[PM2](https://pm2.keymetrics.io/) (opcional, para gestionar el proceso).

**`xml_envio` tiene que ser `NVARCHAR(MAX)` (o `TEXT` en PostgreSQL), no un tipo
acotado.** Un XML UBL firmado mide unos 11.000 caracteres; con la columna más
chica (se vio en producción con `NVARCHAR(4000)`) el `UPDATE` de
`guardar_respuesta` falla entero —no solo el XML: también se pierden hash, QR y
url— de forma silenciosa si nadie revisa el log. Chequear esto antes de
desplegar contra una base existente:

```sql
ALTER TABLE dbo.Comprobantes ALTER COLUMN xml_envio NVARCHAR(MAX) NULL;
```

## De dónde salen los comprobantes

El daemon no sabe con qué base está hablando. Pide siempre las mismas once
operaciones —los pendientes, el receptor, los ítems, marcar enviado, guardar la
respuesta de facturador-api— y recibe diccionarios con el mismo vocabulario, venga
de donde venga. Cada motor tiene su adaptador en [`repositorio/`](repositorio/),
elegido por el esquema de `DATABASE_URL`.

Lo que cambia de un cliente a otro vive ahí adentro y en ningún otro lado: nombres
de tablas y columnas, y las diferencias de dialecto. Entre PostgreSQL y SQL Server
son cuatro: los marcadores (`%s` contra `?`), el booleano (`enviado` es `BIT`, así
que "no enviado" es `= 0 OR IS NULL`), el `ANY(lista)` de un cierre en lote —que en
SQL Server hay que armar como `IN (?,?,…)`— y el `NULLS LAST`, que se suple con un
`CASE`.

Para un cliente con su propio esquema se copia el adaptador más parecido y se le
cambian las consultas. **No hay un mapeador genérico configurable, y es
deliberado**: un archivo de sesenta líneas con su SQL a la vista se depura
leyéndolo; un mapeo indirecto hay que descifrarlo justo cuando algo está fallando.

**Qué tiene que entregar la base para que el facturador funcione está en
[`repositorio/README.md`](repositorio/README.md)**: las once operaciones, las
catorce claves de cada comprobante, cuáles son obligatorias y qué se calcula cuando
faltan. Es el documento que se le pasa a un cliente cuyo sistema no es el nuestro.

## Instalación

```bash
pip install -r requirements.txt
```

El daemon corre con el Python de la máquina: PM2 lo arranca como `main.py` con
`interpreter: "python"`. Por eso las dependencias van instaladas en el sistema.

Son tres: `python-dotenv` siempre, y de los dos drivers de base —`psycopg2-binary`
y `pyodbc`— cada instalación usa solo el de su motor (se instalan los dos igual, es
más simple que preguntar antes de saber a qué base va a apuntar).

## Configuración

Un archivo `.env` en la raíz (no se versiona). Solo la primera es obligatoria; el
resto tiene valor por defecto.

```env
# Base de la aplicación. El motor sale del esquema de la URL —no hay una segunda
# variable que pueda quedar en desacuerdo con esta— y los parámetros de Prisma
# (?schema=...) se traducen solos.
#   postgresql://usuario:clave@host:5432/base?schema=public
#   sqlserver://usuario:clave@host:1433/base
#   sqlserver://host:1433/base?trusted=yes      (autenticación de Windows)
#   sqlserver://host/base?instancia=SQLEXPRESS  (instancia con nombre: sin puerto)
DATABASE_URL=postgresql://usuario:clave@host:5432/postgres?schema=public

EMISOR_RUC=                           # vacío = se lee de la BD; también es el RUC
                                       # con el que se llama a facturador-api
FACTURADOR_API_URL=https://facturador.peru-hub.com
FACTURADOR_API_TIMEOUT_SEG=120        # hasta 4 llamadas internas a SFS de 30s cada una
FACTURADOR_API_KEY=                   # opcional, hoy facturador-api no exige auth

INTERVALO_GENERACION_SEG=60
MAX_REINTENTOS_RECHAZO=3              # reenvíos de un comprobante rechazado, antes de bloquearlo
ESPERA_MAX_CICLO_MIN=120              # techo del backoff cuando el daemon no llega a facturador-api
MOTIVO_NOTA_POR_DEFECTO=              # ver "Tipos de comprobante" más arriba

DESFASE_BD_HORAS=auto                 # "auto" lo mide en cada ciclo
LOG_MAX_MB=5                          # rotación de facturador.log
LOG_ARCHIVOS=5
NOTIFICADOR_COMPROBANTES=noop         # ver integraciones/README.md; "noop" no hace nada

# FuelHub core (cierres de turno/día, PDF, reporte top-facturas) — solo en
# instalaciones de grifo con SQL Server. Ver cada módulo en aplicacion/ para el
# resto de las variables (INTERVALO_CIERRES_SEG, INTERVALO_PDF_SEG, etc.).
FUELHUB_CORE_CLIENT_ID=
FUELHUB_CORE_CLIENT_SECRET=
```

## Uso

```bash
pm2 start main.py --name facturador --interpreter python
pm2 save
```

Para que arranque solo al encender la PC (Windows no tiene init system, así que
`pm2 startup` no alcanza):

```bash
npm install -g pm2-windows-startup
pm2-startup install
pm2 save
```

Sin PM2: `python main.py`.

Los logs van a `facturador.log` y, con PM2, también a `logs/out.log` y
`logs/error.log`. `facturador.log` rota a los 5 MB y conserva 5 archivos —unos
25 MB—, suficiente para investigar un rechazo semanas después sin crecer sin
límite en una PC que va a estar años emitiendo.

## Flujo del comprobante

```
Comprobante (enviado=false)
        ↓
ciclo_generacion() cada 60s
        ↓
POST /empresas/{ruc}/comprobantes a facturador-api
        ↓
  ┌─────┴─────┐
  ↓           ↓
Aceptado   Rechazado/ambiguo/caído
  ↓           ↓
enviado=1   según la tabla de arriba
(hash, QR,
 url guardados)

Boletas: la API arma y declara el resumen
diario sola (00:10 hora Lima). El daemon
solo concilia una vez al día con
GET /empresas/{ruc}/resumenes para
reencolar las boletas de un resumen
rechazado.
```

## Integraciones salientes

Para lo que necesite reaccionar a un comprobante aceptado sin conocer nada del
flujo de facturación —hoy previsto: subir a S3 el PDF del comprobante— está
[`integraciones/`](integraciones/README.md): mismo patrón que `repositorio/`, un
puerto por operación y un adaptador por destino, elegido por la variable
`NOTIFICADOR_COMPROBANTES` del `.env` (`noop` por defecto, que no hace nada).

Se dispara desde un único punto, en `aplicacion/ciclo_generacion.py`
(`_cerrar_aceptado`), justo cuando un comprobante (o un resumen) ya quedó aceptado
y se reflejó en la BD de la app. Es best-effort por diseño —envuelto en su propio
try/except—, así que una falla ahí nunca impide que el comprobante se cierre ni
que el ciclo siga.

## Pruebas

```bash
python pruebas/correr_todo.py
```

Corre cada `pruebas/test_*.py` en su propio proceso, sin necesitar base de datos
ni red — todas reemplazan la BD y el cliente de facturador-api con dobles.
