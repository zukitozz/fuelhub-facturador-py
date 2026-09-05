# Integraciones salientes

Lo que el daemon dispara cuando un comprobante queda aceptado por SUNAT, además del
propio flujo de facturación. Mismo patrón que [`repositorio/`](../repositorio/README.md):
un puerto por operación, un adaptador por destino posible, elegido por configuración.

## Por qué existe

El daemon solo sabía hablar con el SFS y con SUNAT. La primera integración prevista
—subir a S3 el PDF del comprobante ya aceptado— no tiene nada que ver con ese flujo:
no necesita conocer el SFS, ni DATA, ni RPTA. Meterla ahí adentro habría acoplado dos
cosas que cambian por razones distintas. Este paquete es el lugar para eso y para lo
que siga (el próximo previsto: cierres de turno/día a otro endpoint).

## El punto de enganche

`main.py`, dentro de `_barrer_rpta()`, justo cuando un CDR de aceptación ya se
reflejó en la BD de la app y antes de archivarlo en `RPTA/procesados/`. Es el único
lugar donde el daemon sabe con certeza que SUNAT aceptó ese comprobante (o ese
resumen diario) y que la app ya lo tiene marcado como enviado.

La llamada al puerto va envuelta en su propio try/except en `main.py`
(`_notificar_comprobante_aceptado`): una falla acá **nunca** debe impedir que el CDR
se archive ni que el ciclo siga. Todo lo que vive en `integraciones/` es best-effort
por definición — lo que no lo sea (por ejemplo, algo de lo que dependa declarar
correctamente un comprobante ante SUNAT) no pertenece acá, pertenece al flujo
principal en `main.py`.

## `NotificadorComprobanteAceptado` (`puertos.py`)

```python
notificar(ruc: str, tipo: str, numeracion: str) -> None
```

- `ruc`: RUC del emisor, tal como figura en el nombre del CDR.
- `tipo`: código SUNAT de dos dígitos (`01` factura, `03` boleta, `07` NC, `08` ND).
- `numeracion`: la numeración del comprobante (`F001-000123`), o el id de un resumen
  diario (`RC-20260826-001`) cuando lo que se aceptó fue un resumen completo — un
  resumen agrupa varias boletas y no tiene un PDF propio del mismo tipo que un
  comprobante individual. Distinguir uno de otro (`numeracion.startswith("RC-")`) y
  decidir qué hacer con cada caso es responsabilidad del adaptador, no del puerto.

Puede lanzar excepciones libremente: quien lo invoca ya las atrapa y las loggea sin
detener el ciclo.

## Cómo se elige el adaptador

Por la variable `NOTIFICADOR_COMPROBANTES` en `.env` (`noop` por defecto, que no hace
nada). Un valor no reconocido cae a `noop` con un WARNING en vez de tumbar el daemon:
esta integración nunca puede ser la causa de que se deje de facturar.

## Cómo agregar un adaptador nuevo

Un archivo acá con una clase que implemente el puerto que corresponda, y una entrada
en `_ADAPTADORES` (`__init__.py`). Por ejemplo, para S3: qué credenciales usa, de
dónde saca el PDF (todavía sin definir al momento de escribir esto — ver el propio
`main.py` para el estado del punto de enganche) y qué hace si el bucket no responde
(nunca debería propagar la excepción más allá de `notificar()`; ya la atrapa quien
llama, pero un reintento o backoff propio, si hace falta, vive acá adentro).
