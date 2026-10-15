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
| `error` | Ambiguo por diseño: backoff creciente antes de reintentar el mismo `POST` — la propia API verifica con SUNAT antes de reenviar, no hace falta un `GET` propio |
| HTTP 404 (RUC sin alta) | Error crítico: corta el ciclo entero |
| HTTP 502 | La API ya verificó que no se envió nada: reintenta con backoff, sin gastar presupuesto de rechazos |
| Sin respuesta HTTP (timeout real) | Ambiguo de verdad: se resuelve con un `GET` antes de seguir — nunca se reenvía a ciegas |

**Tope de reenvíos.** Un reenvío manda exactamente los mismos datos, así que si
facturador-api rechazó por un dato mal armado el resultado no cambia. De ahí el
tope (`MAX_REINTENTOS_RECHAZO`, 3 por defecto); al agotarlo el comprobante queda
bloqueado hasta que se corrija el dato. El conteo vive en `envio_api.json` (no se
versiona), junto con la idempotencia ante un corte a mitad de un envío, el
backoff de fallas de infraestructura, los comprobantes esperando CDR y los
resúmenes diarios sin veredicto — ver `estado/envio_api.py`.

## Resumen diario de boletas

Ninguna boleta se envía individualmente — facturador-api la firma pero la agrupa en
un resumen diario (`RC`) del lado del servidor. Una vez al día, el daemon:

1. Llama `POST /resumenes-diarios` repetidas veces hasta que facturador-api dice que
   no hay más boletas de días anteriores pendientes de agrupar, o hasta su propio
   tope diario. La respuesta trae la lista de boletas que agrupó.
2. Por cada resumen armado, consulta su estado con `GET` en cada ciclo hasta tener
   veredicto — esa consulta resuelve el ticket de SUNAT del lado del servidor, el
   daemon no habla SOAP con nadie.
3. **Aceptado**: nada más que hacer — cada boleta ya quedó `enviado=1` desde que
   se firmó individualmente (`generado`), con su propio XML ya guardado.
4. **Rechazado**: las boletas de la lista vuelven a `enviado=0` con el motivo
   guardado, para que el próximo ciclo las regenere (nuevo `POST` individual,
   nuevo XML) y entren a un resumen nuevo.

## Requisitos

Python 3.10+, la base de la aplicación accesible (PostgreSQL o SQL Server), acceso
de red a `https://facturador.peru-hub.com` (o la URL que corresponda) y
[PM2](https://pm2.keymetrics.io/) (opcional, para gestionar el proceso).

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
ESPERA_MAX_INFRA_MIN=15               # techo del backoff ante un 502/timeout de facturador-api
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

Boletas: además, una vez al día,
POST /empresas/{ruc}/resumenes-diarios
hasta agotar pendientes, y GET periódico
de cada resumen hasta tener veredicto.
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
