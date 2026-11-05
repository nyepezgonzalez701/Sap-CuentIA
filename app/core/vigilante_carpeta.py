"""
Vigila una carpeta local y procesa automaticamente cada factura XML que aparezca en
ella, sin pasar por la pantalla de subida manual (/subir). Corre como un hilo de
fondo dentro del propio proceso de FastAPI (ver iniciar_monitoreo() y su uso en
app/main.py), no como un script aparte.

Cada XML se procesa exactamente una vez y despues se mueve:
    - a la subcarpeta "procesadas/" si se guardo correctamente
    - a la subcarpeta "errores/" si fue invalido, duplicado o fallo el guardado
      (el motivo queda en un archivo <nombre>.xml.txt junto al xml movido)

El estado actual (en_espera/vigilando/procesando/error) se persiste en la tabla
estado_vigilante (fila unica, id=1) para que el panel pueda mostrarlo incluso
despues de que el servidor se reinicie -- en ese caso el hilo ya no existe, por
lo que reiniciar_estado_al_arrancar() lo vuelve a dejar en "en_espera".
"""
import shutil
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from app.db.config import SessionLocal
from app.core.ingesta import procesar_factura_xml
from app.core.models import EstadoVigilante

CARPETA_POR_DEFECTO = r"C:\Users\nicole.yepez\OneDrive - ITRONIK SERVICES SA\Documentos\Facturas"
INTERVALO_REVISION_SEGUNDOS = 5
# Cada factura espera la respuesta de la IA (varios segundos), asi que se procesan
# varias a la vez. Si dos archivos traen el mismo numero de factura, la restriccion
# unique de facturas.numero_factura hace que uno termine en errores/.
ARCHIVOS_EN_PARALELO = 4

log = logging.getLogger("vigilante")

_lock_hilo = threading.Lock()
_hilo: threading.Thread | None = None
_detener = threading.Event()


# ---------- estado persistido (tabla estado_vigilante) ----------

def _obtener_o_crear_estado(db) -> EstadoVigilante:
    estado = db.get(EstadoVigilante, 1)
    if estado is None:
        estado = EstadoVigilante(id=1, estado="en_espera")
        db.add(estado)
        try:
            db.commit()
        except IntegrityError:
            # El hilo de vigilancia y una request pueden crear la fila a la vez con la
            # tabla vacia: si el otro gano, se usa la suya.
            db.rollback()
            return db.get(EstadoVigilante, 1)
        db.refresh(estado)
    return estado


def _actualizar_estado(nuevo_estado: str, carpeta_monitoreada: str | None = None, mensaje_error: str | None = None) -> None:
    db = SessionLocal()
    try:
        estado = _obtener_o_crear_estado(db)
        estado.estado = nuevo_estado
        if carpeta_monitoreada is not None:
            estado.carpeta_monitoreada = carpeta_monitoreada
        estado.mensaje_error = mensaje_error
        estado.ultima_actualizacion = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def leer_estado(db) -> EstadoVigilante:
    """Usa la sesion del llamador -- pensado para leerse dentro de la misma request
    que arma el resto del contexto del panel (ver app/main.py)."""
    return _obtener_o_crear_estado(db)


def reiniciar_estado_al_arrancar() -> None:
    """
    Al arrancar el servidor no hay ningun hilo corriendo todavia (el hilo vive en
    memoria y no sobrevive a un reinicio), asi que el estado real es "en_espera"
    sin importar lo que haya quedado guardado antes del reinicio/caida.
    """
    db = SessionLocal()
    try:
        estado = _obtener_o_crear_estado(db)
        estado.estado = "en_espera"
        estado.mensaje_error = None
        estado.ultima_actualizacion = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def esta_activo() -> bool:
    with _lock_hilo:
        return _hilo is not None and _hilo.is_alive()
    # estimulos de que el hilo esta activo, se peude usar para el panel de control


# ---------- procesamiento de archivos (igual que el script original) ----------

def _esperar_archivo_estable(ruta: Path, intentos: int = 10, espera: float = 0.5) -> bytes | None:
    """
    OneDrive puede seguir escribiendo/sincronizando el archivo justo cuando aparece.
    Espera a que el tamano deje de cambiar entre dos lecturas seguidas antes de
    devolver el contenido -- evita procesar un XML a medio escribir.
    """
    tamano_anterior = -1
    for _ in range(intentos):
        try:
            tamano_actual = ruta.stat().st_size
        except FileNotFoundError:
            return None
        if tamano_actual == tamano_anterior and tamano_actual > 0:
            try:
                return ruta.read_bytes()
            except (PermissionError, OSError):
                pass
        tamano_anterior = tamano_actual
        time.sleep(espera)
    log.warning("El archivo %s no se estabilizo a tiempo, se reintentara en la siguiente pasada.", ruta.name)
    return None


def _mover_a(ruta: Path, subcarpeta: Path, motivo_error: str | None = None) -> None:
    subcarpeta.mkdir(exist_ok=True)
    destino = subcarpeta / ruta.name
    if destino.exists():
        destino = subcarpeta / f"{ruta.stem}_{int(time.time())}{ruta.suffix}"
    shutil.move(str(ruta), str(destino))
    if motivo_error:
        destino.with_name(destino.name + ".txt").write_text(motivo_error, encoding="utf-8")


def procesar_archivo(ruta: Path, carpeta_procesadas: Path, carpeta_errores: Path) -> None:
    contenido = _esperar_archivo_estable(ruta)
    if contenido is None:
        return

    db = SessionLocal()
    try:
        try:
            resultado = procesar_factura_xml(db, contenido)
        except Exception as e:
            log.exception("Error inesperado procesando %s", ruta.name)
            _mover_a(ruta, carpeta_errores, motivo_error=f"Error inesperado: {e}")
            return
    finally:
        db.close()

    if resultado.ok:
        log.info(
            "OK  %s -> factura %s (id %s) guardada como pendiente de aprobacion.",
            ruta.name, resultado.datos.numero_factura, resultado.factura_id,
        )
        _mover_a(ruta, carpeta_procesadas)
    else:
        log.warning("RECHAZADO  %s -> %s", ruta.name, resultado.mensaje)
        _mover_a(ruta, carpeta_errores, motivo_error=resultado.mensaje)


def _xml_pendientes(carpeta: Path) -> list[Path]:
    return sorted(
        (p for p in carpeta.iterdir() if p.is_file() and p.suffix.lower() == ".xml"),
        key=lambda p: p.stat().st_mtime,
    )


# ---------- consultas para el panel ----------

def contar_pendientes(carpeta_str: str) -> int:
    carpeta = Path(carpeta_str)
    if not carpeta.is_dir():
        return 0
    return len(_xml_pendientes(carpeta))


def _movidos_hoy(carpeta: Path) -> int:
    if not carpeta.is_dir():
        return 0
    hoy = date.today()
    return sum(
        1
        for p in carpeta.iterdir()
        if p.is_file() and p.suffix.lower() == ".xml"
        and date.fromtimestamp(p.stat().st_mtime) == hoy
    )


def contar_procesados_hoy(carpeta_str: str) -> int:
    carpeta = Path(carpeta_str)
    return _movidos_hoy(carpeta / "procesadas") + _movidos_hoy(carpeta / "errores")


@dataclass
class ArchivoError:
    nombre: str
    motivo: str | None
    fecha: datetime


def listar_ultimos_errores(carpeta_str: str, limite: int = 10) -> list[ArchivoError]:
    carpeta_errores = Path(carpeta_str) / "errores"
    if not carpeta_errores.is_dir():
        return []

    archivos = sorted(
        (p for p in carpeta_errores.iterdir() if p.is_file() and p.suffix.lower() == ".xml"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limite]

    resultado = []
    for p in archivos:
        archivo_motivo = p.with_name(p.name + ".txt")
        motivo = archivo_motivo.read_text(encoding="utf-8") if archivo_motivo.is_file() else None
        resultado.append(ArchivoError(nombre=p.name, motivo=motivo, fecha=datetime.fromtimestamp(p.stat().st_mtime)))
        
    return resultado


def reintentar_archivo(carpeta_str: str, nombre_archivo: str) -> bool:
    """Mueve un archivo de errores/ de vuelta a la carpeta de entrada para que se
    procese de nuevo en la proxima pasada del hilo. Devuelve False si no existia."""
    carpeta = Path(carpeta_str)
    origen = carpeta / "errores" / nombre_archivo
    if not origen.is_file():

       
        
        return False

    destino = carpeta / nombre_archivo
    if destino.exists():
        destino = carpeta / f"{origen.stem}_{int(time.time())}{origen.suffix}"
    shutil.move(str(origen), str(destino))

    archivo_motivo = origen.with_name(origen.name + ".txt")
    if archivo_motivo.is_file():
        archivo_motivo.unlink()
    return True


# ---------- ciclo de vida para procesar las carpetas  por ejemplo el bucle d ela priemera carpeta e ----------

def _bucle_vigilancia(carpeta_str: str) -> None:
    carpeta = Path(carpeta_str)
    carpeta_procesadas = carpeta / "procesadas"
    carpeta_errores = carpeta / "errores"
    carpeta_procesadas.mkdir(exist_ok=True)
    carpeta_errores.mkdir(exist_ok=True)

    def procesar_si_sigue_activo(ruta: Path) -> None:
        if not _detener.is_set():
            procesar_archivo(ruta, carpeta_procesadas, carpeta_errores)

    log.info("Vigilando carpeta: %s (revision cada %ss)", carpeta, INTERVALO_REVISION_SEGUNDOS)
    try:
        while not _detener.is_set():
            _actualizar_estado("vigilando", carpeta_monitoreada=str(carpeta))
            pendientes = _xml_pendientes(carpeta)
            if pendientes:
                _actualizar_estado("procesando", carpeta_monitoreada=str(carpeta))
                with ThreadPoolExecutor(max_workers=ARCHIVOS_EN_PARALELO, thread_name_prefix="vigilante") as ejecutor:
                    # list() para que una excepcion de un archivo se propague igual que antes.
                    list(ejecutor.map(procesar_si_sigue_activo, pendientes))
            if not _detener.is_set():
                _actualizar_estado("vigilando", carpeta_monitoreada=str(carpeta))
            _detener.wait(INTERVALO_REVISION_SEGUNDOS)
    except Exception as e:
        log.exception("El hilo de vigilancia se detuvo por un error inesperado")
        _actualizar_estado("error", carpeta_monitoreada=str(carpeta), mensaje_error=str(e))
    finally:
        global _hilo
        with _lock_hilo:
            _hilo = None
        if not _detener.is_set():
            log.info("Hilo de vigilancia detenido.")
        else:
            _actualizar_estado("en_espera", carpeta_monitoreada=str(carpeta))


def iniciar_monitoreo(carpeta_str: str | None = None) -> tuple[bool, str]:
    """
    Arranca el hilo de vigilancia si no esta ya corriendo. Devuelve (ok, mensaje)
    para que el endpoint que la llama pueda registrar el resultado en la auditoria
    (ver HistorialVigilancia en app/main.py).
    """
    global _hilo
    carpeta_str = carpeta_str or CARPETA_POR_DEFECTO

    with _lock_hilo:
        if _hilo is not None and _hilo.is_alive():
            return False, "El monitoreo ya esta activo."
# en caso de que la carpeta no exista, se actualiza el estado a "error" y se devuelve un mensaje de error.
        carpeta = Path(carpeta_str)
        if not carpeta.is_dir():
            mensaje = f"La carpeta no existe: {carpeta_str}"
            _actualizar_estado("error", carpeta_monitoreada=carpeta_str, mensaje_error=mensaje)
            return False, mensaje
#cuando el monitoreo se inicia 
        _detener.clear()
        _hilo = threading.Thread(target=_bucle_vigilancia, args=(carpeta_str,), daemon=True, name="vigilante-carpeta")
        _hilo.start()
        return True, "Monitoreo iniciado."


def detener_monitoreo() -> tuple[bool, str]:
    """Para el hilo de vigilancia si esta activo (usado tanto por el panel como por
    las pruebas para dejarlo limpio entre casos)."""
    with _lock_hilo:
        hilo = _hilo
    if hilo is None:
        return False, "El monitoreo no esta activo."

    _detener.set()
    hilo.join(timeout=INTERVALO_REVISION_SEGUNDOS + 2)
    _detener.clear()
    return True, "Monitoreo detenido."
