import os, sys, sqlite3, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SFS_DATA_DIR"] = tempfile.mkdtemp()
import main, logging
import config
import aplicacion.ciclo_generacion as ciclo_generacion
import sfs.bd as sfs_bd
import estado.reintentos as reintentos
logging.disable(logging.CRITICAL)

TMP = tempfile.mkdtemp()
reintentos._REINTENTOS_PATH = os.path.join(TMP, "reintentos.json")
RUC = "20609785269"

def bd_falsa(filas):
    ruta = os.path.join(TMP, f"sfs_{len(os.listdir(TMP))}.db")
    c = sqlite3.connect(ruta)
    c.execute("CREATE TABLE DOCUMENTO (NUM_RUC TEXT, TIP_DOCU TEXT, NUM_DOCU TEXT, IND_SITU TEXT, DES_OBSE TEXT)")
    c.executemany("INSERT INTO DOCUMENTO VALUES (?,?,?,?,?)", filas)
    c.commit(); c.close()
    sfs_bd.SFS_BD_PATH = ruta
    ciclo_generacion.SFS_BD_PATH = ruta
    return ruta

MARCADOS = []
class FakeBD:
    @staticmethod
    def marcar_enviado(conn, num, enviado=True, limpiar_error=True):
        MARCADOS.append((num, enviado, limpiar_error)); return 1
ciclo_generacion._bd = lambda: FakeBD()

FALLAS = 0
def check(cond, msg):
    global FALLAS
    if not cond: FALLAS += 1
    print(("  OK    " if cond else "  FALLA "), msg)

def filas_restantes(ruta):
    c = sqlite3.connect(ruta); r = c.execute("SELECT NUM_DOCU, IND_SITU FROM DOCUMENTO").fetchall(); c.close(); return r

# --- 1) un 06 por corte de conexion vuelve a la cola ---
for p in (reintentos._REINTENTOS_PATH,):
    if os.path.exists(p): os.remove(p)
MARCADOS.clear()
ruta = bd_falsa([
    (RUC, "01", "F003-000100", "06", "Error al firmar archivo XML"),
    (RUC, "01", "F003-000101", "10", "Rechazado por SUNAT"),
    (RUC, "01", "F003-000102", "03", "aceptado"),
])
ciclo_generacion.resetear_rechazados(None, RUC)
nums = [m[0] for m in MARCADOS]
check("F003-000100" in nums, "el 06 (error de dato) vuelve a la cola")
check("F003-000101" in nums, "el 10 sigue volviendo a la cola, como antes")
check("F003-000102" not in nums, "un aceptado (03) no se toca")
check(all(m[1] is config.ENVIADO_PENDIENTE for m in MARCADOS), "vuelven como pendientes")
check(all(m[2] is False for m in MARCADOS), "sin limpiar el motivo (queda a la vista)")
rest = dict(filas_restantes(ruta))
check("F003-000100" not in rest, "la fila 06 se borro de DOCUMENTO para regenerarse")
check(reintentos._reintentos_de("F003-000100") == 1, "cuenta como intento 1")

# --- 2) tras agotar los 3, el 06 deja de reintentarse y queda para el reporte ---
for _ in range(2):
    bd_falsa([(RUC, "01", "F003-000100", "06", "Error al firmar archivo XML")])
    ciclo_generacion.resetear_rechazados(None, RUC)
check(reintentos._reintentos_de("F003-000100") == 3, f"llego a 3 intentos ({reintentos._reintentos_de('F003-000100')})")

MARCADOS.clear()
ruta = bd_falsa([(RUC, "01", "F003-000100", "06", "Error al firmar archivo XML")])
ciclo_generacion.resetear_rechazados(None, RUC)
check(MARCADOS == [], "agotado: ya no vuelve a la cola")
rest = dict(filas_restantes(ruta))
check(rest.get("F003-000100") == "06", "la fila queda en DOCUMENTO para el reporte de BLOQUEADOS")
check("06" in config._ESTADOS_BLOQUEADO, "y el 06 esta en _ESTADOS_BLOQUEADO")

if FALLAS:
    print(str(FALLAS) + ' FALLA(S)')
    sys.exit(1)
print('TODO OK')
