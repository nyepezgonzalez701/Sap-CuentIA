"""
Vigila una carpeta local y procesa automaticamente cada factura XML que
aparezca en ella, sin pasar por la pantalla de subida manual (/subir).

Uso:
    python vigilar_carpeta.py
    python vigilar_carpeta.py "C:\\ruta\\a\\otra\\carpeta"

Cada XML se procesa exactamente una vez y despues se mueve:
    - a la subcarpeta "procesadas/" si se guardo correctamente
    - a la subcarpeta "errores/" si fue invalido, duplicado o fallo el guardado
      (el motivo queda en un archivo <nombre>.xml.txt junto al xml movido)

Requiere la base de datos y las variables de entorno (.env, DATABASE_URL,
credenciales de la IA) configuradas igual que para correr la app web -- usa
exactamente la misma logica de guardado (ver app/core/ingesta.py).
"""
import sys
import time
import shutil
import logging
from pathlib import Path

from app.db.config import SessionLocal
from app.core.ingesta import procesar_factura_xml

CARPETA_POR_DEFECTO = r"C:\Users\nicole.yepez\OneDrive - ITRONIK SERVICES SA\Documentos\Facturas"
INTERVALO_REVISION_SEGUNDOS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vigilante")


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

#apartdo de pendientes

def _xml_pendientes(carpeta: Path):
    return sorted(
        (p for p in carpeta.iterdir() if p.is_file() and p.suffix.lower() == ".xml"),
        key=lambda p: p.stat().st_mtime,
    )

#en caso de que la acarpeta no exista, se crea y se crean las subcarpetas procesadas y errores
def vigilar(carpeta_str: str) -> None:
    carpeta = Path(carpeta_str)
    if not carpeta.is_dir():
        log.error("La carpeta no existe: %s", carpeta)
        sys.exit(1)

    carpeta_procesadas = carpeta / "procesadas"
    carpeta_errores = carpeta / "errores"
    carpeta_procesadas.mkdir(exist_ok=True)
    carpeta_errores.mkdir(exist_ok=True)

    log.info("Vigilando carpeta: %s (revision cada %ss)", carpeta, INTERVALO_REVISION_SEGUNDOS)

    while True:
        for ruta in _xml_pendientes(carpeta):
            procesar_archivo(ruta, carpeta_procesadas, carpeta_errores)
        time.sleep(INTERVALO_REVISION_SEGUNDOS)


#vigilar carpeta por defecto si se ejecuta directamente
#ejemplo: python vigilar_carpeta.py "C:\\ruta\\a\\otra\\carpeta"
#2.0.3
#ejmemplo de html version 1 d
if __name__ == "__main__":
    carpeta_objetivo = sys.argv[1] if len(sys.argv) > 1 else CARPETA_POR_DEFECTO
    try:
        vigilar(carpeta_objetivo)
    except KeyboardInterrupt:
        log.info("Vigilante detenido.")
