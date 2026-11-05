"""
Descarga automatica de las facturas RECIBIDAS desde el ApiRest de Factible y las deja
en la carpeta que ya vigila app/core/vigilante_carpeta.py -- de ahi en adelante el
flujo es el mismo que si alguien hubiera copiado el XML a mano (ingesta, IA, etc.).

Flujo (ver las fichas tecnicas de Factible):
    1. generar/recepcion(ruc, fecha)  -> token PENDIENTE (se guarda en descargas_factible)
    2. consultar(ruc, token)          -> repetir mientras PENDIENTE/PROCESANDO, hasta FINALIZADO
    3. DocumentoZip (Base64)          -> se extraen SOLO las facturas (codDoc 01)

Un hilo de fondo (el "programador", ver iniciar_programador) hace dos cosas cada
FACTIBLE_INTERVALO_CONSULTA segundos:
    - consulta los tokens PENDIENTE guardados en la BD (tambien los que quedaron de
      antes de un reinicio del servidor)
    - si FACTIBLE_DESCARGA_AUTOMATICA=true y ya paso FACTIBLE_HORA_DESCARGA, pide la
      descarga del dia anterior (una vez por dia; si fallo, reintenta cada 30 min)
La descarga manual desde el panel solo genera el token; el mismo hilo lo consulta.
"""
import base64
import binascii
import io
import logging
import os
import threading
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time as hora_del_dia, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.db.config import SessionLocal
from app.core import vigilante_carpeta
from app.core.models import DescargaFactible, Factura
from app.adaptadores.factible.base import (
    ClienteFactible,
    ErrorFactible,
    ESTADO_FINALIZADO,
    ESTADO_PENDIENTE,
)

log = logging.getLogger("factible")

ZONA_ECUADOR = ZoneInfo("America/Guayaquil")
COD_DOC_FACTURA = "01"
TAMANO_MAXIMO_XML = 20 * 1024 * 1024  # defensa contra ZIPs malformados/enormes
REINTENTO_TRAS_ERROR = timedelta(minutes=30)
# Pasado este tiempo solo se avisa en el panel: cancelar localmente no libera el token en
# Factible (sigue bloqueando a la empresa), asi que se sigue consultando.
TIEMPO_AVISO_DEMORA = timedelta(hours=2)
MAXIMO_TOKENS_A_RETOMAR = 10
PREFIJO_TOKEN_NO_RECONOCIDO = "Factible ya no reconoce el token"
MENSAJE_TOKEN_AJENO = (
    "Factible indica que ya existe un token pendiente para la empresa, pero este sistema "
    "no lo tiene registrado (Factible no devuelve cual es). Se reintentara mas tarde."
)

ESTADO_SIN_DOCUMENTOS = "SIN_DOCUMENTOS"
ESTADO_ERROR = "ERROR"
ESTADOS_DIA_RESUELTO = (ESTADO_PENDIENTE, ESTADO_FINALIZADO, ESTADO_SIN_DOCUMENTOS)

_lock_operacion = threading.Lock()  # una sola operacion contra Factible a la vez
_lock_hilo = threading.Lock()
_hilo: threading.Thread | None = None
_detener = threading.Event()
_cliente: ClienteFactible | None = None


# ---------- configuracion (.env) ----------

@dataclass
class ConfigFactible:
    modo: str  # "real" | "simulador" | "" (desactivado)
    url_base: str
    usuario: str
    clave: str
    ruc: str
    descarga_automatica: bool
    hora_descarga: hora_del_dia
    intervalo_consulta: int


def leer_config() -> ConfigFactible:
    try:
        hh, mm = (os.getenv("FACTIBLE_HORA_DESCARGA") or "07:00").split(":")
        hora = hora_del_dia(int(hh), int(mm))
    except ValueError:
        hora = hora_del_dia(7, 0)
    return ConfigFactible(
        modo=(os.getenv("FACTIBLE_MODO") or "").strip().lower(),
        url_base=os.getenv("FACTIBLE_URL_BASE") or "https://qa-api.esfactible.com",
        usuario=os.getenv("FACTIBLE_USUARIO") or "",
        clave=os.getenv("FACTIBLE_CLAVE") or "",
        ruc=os.getenv("FACTIBLE_RUC") or "",
        descarga_automatica=(os.getenv("FACTIBLE_DESCARGA_AUTOMATICA") or "false").strip().lower() == "true",
        hora_descarga=hora,
        intervalo_consulta=int(os.getenv("FACTIBLE_INTERVALO_CONSULTA") or 30),
    )


def obtener_cliente(config: ConfigFactible) -> ClienteFactible | None:
    """Se reutiliza la misma instancia para conservar el JWT entre llamadas."""
    global _cliente
    if _cliente is None:
        if config.modo == "real":
            from app.adaptadores.factible.cliente_rest import ClienteRestFactible
            _cliente = ClienteRestFactible(config.url_base, config.usuario, config.clave, config.ruc)
        elif config.modo == "simulador":
            from app.adaptadores.factible.simulador import SimuladorFactible
            _cliente = SimuladorFactible()
    return _cliente


def carpeta_destino(db) -> Path:
    """La misma carpeta que muestra/vigila el panel de vigilancia."""
    estado = vigilante_carpeta.leer_estado(db)
    return Path(estado.carpeta_monitoreada or vigilante_carpeta.CARPETA_POR_DEFECTO)


def _ahora() -> datetime:
    return datetime.now(ZONA_ECUADOR)


def _con_zona(momento: datetime) -> datetime:
    return momento if momento.tzinfo else momento.replace(tzinfo=timezone.utc)  # SQLite no guarda zona horaria


# ---------- paso 1: generar token ----------

def descarga_en_curso(db) -> DescargaFactible | None:
    return db.query(DescargaFactible).filter(DescargaFactible.estado == ESTADO_PENDIENTE).first()


def solicitar_descarga(db, cliente: ClienteFactible, ruc: str, fecha: date, origen: str, usuario: str) -> DescargaFactible | None:
    """Genera el token para la fecha y deja registrada la descarga. No espera a que
    termine: el token PENDIENTE lo consulta el programador en sus siguientes vueltas.
    Devuelve None (sin registrar nada) si ya hay una descarga en curso."""
    if descarga_en_curso(db) is not None:
        # Factible solo permite un token pendiente por empresa -- no tiene sentido pedir otro,
        # y tampoco es un error: solo hay que esperar.
        return None

    descarga = DescargaFactible(ruc_empresa=ruc, fecha_documentos=fecha, estado=ESTADO_ERROR, origen=origen, usuario=usuario)
    try:
        respuesta = cliente.generar_recepcion(ruc, fecha.strftime("%d/%m/%Y"))
    except ErrorFactible as e:
        descarga.mensaje = str(e)
    else:
        descarga.mensaje = respuesta.mensaje
        if respuesta.exito and respuesta.token:
            descarga.estado = ESTADO_PENDIENTE
            descarga.token = respuesta.token
            descarga.total_claves = respuesta.total_claves
        elif respuesta.sin_documentos:
            descarga.estado = ESTADO_SIN_DOCUMENTOS
        elif respuesta.ya_hay_token_pendiente:
            retomada = retomar_tokens_registrados(db, cliente, ruc)
            if retomada is not None:
                return retomada
            anterior = (
                db.query(DescargaFactible)
                .filter(DescargaFactible.fecha_documentos == fecha, DescargaFactible.estado == ESTADO_ERROR,
                        DescargaFactible.mensaje.like(f"{MENSAJE_TOKEN_AJENO}%"))
                .order_by(DescargaFactible.id.desc())
                .first()
            )
            mensaje = f"{MENSAJE_TOKEN_AJENO} Ultimo intento: {_ahora():%d/%m/%Y %H:%M}."
            if anterior is not None:
                # Se actualiza la fila del intento anterior en vez de sumar una nueva cada 30 min.
                anterior.mensaje = mensaje
                anterior.origen, anterior.usuario = origen, usuario
                anterior.actualizado_en = datetime.now(timezone.utc)
                db.commit()
                log.info("Descarga Factible %s -> %s (reintento): %s", fecha, anterior.estado, mensaje)
                return anterior
            descarga.mensaje = mensaje

    db.add(descarga)
    db.commit()
    log.info("Descarga Factible %s -> %s: %s", fecha, descarga.estado, descarga.mensaje)
    return descarga


def retomar_tokens_registrados(db, cliente: ClienteFactible, ruc: str) -> DescargaFactible | None:
    """Factible dice que hay un token pendiente pero no dice cual: se consultan los tokens
    que este sistema genero y luego dio por perdidos (ERROR), y los que Factible aun tiene
    (en curso o FINALIZADO con el ZIP listo) vuelven a PENDIENTE para que el programador
    los termine. Devuelve el que esta bloqueando (o el primero retomado), o None."""
    candidatas = (
        db.query(DescargaFactible)
        .filter(DescargaFactible.ruc_empresa == ruc, DescargaFactible.estado == ESTADO_ERROR,
                DescargaFactible.token.isnot(None),
                ~DescargaFactible.mensaje.like(f"{PREFIJO_TOKEN_NO_RECONOCIDO}%"))
        .order_by(DescargaFactible.id.desc())
        .limit(MAXIMO_TOKENS_A_RETOMAR)
        .all()
    )
    bloqueante = primera = None
    for descarga in candidatas:
        try:
            respuesta = cliente.consultar(ruc, descarga.token)
        except ErrorFactible as e:
            log.warning("No se pudo consultar el token %s para retomarlo: %s", descarga.token, e)
            break
        if respuesta.token_no_encontrado:
            # Se marca para no volver a consultarlo cada vez que Factible responda "token pendiente".
            descarga.mensaje = f"{PREFIJO_TOKEN_NO_RECONOCIDO} ({respuesta.mensaje}); lo descarto o vencio."
            continue
        listo = respuesta.exito and respuesta.estado == ESTADO_FINALIZADO
        if not (respuesta.en_curso or listo):
            continue
        descarga.estado = ESTADO_PENDIENTE
        descarga.mensaje = (
            f"Retomado: Factible todavia tenia este token "
            f"({'ZIP listo, se descargara en la proxima consulta' if listo else respuesta.estado or respuesta.mensaje})."
        )
        log.info("Token %s (fecha %s) retomado: %s", descarga.token, descarga.fecha_documentos, descarga.mensaje)
        primera = primera or descarga
        if respuesta.en_curso and bloqueante is None:
            bloqueante = descarga
    db.commit()

    retomada = bloqueante or primera
    if retomada is not None:
        # Mensaje para el panel: la descarga pedida no se registro, se esta esperando otra fecha.
        retomada.mensaje = (
            f"Factible tenia pendiente la descarga del {retomada.fecha_documentos:%d/%m/%Y} que este sistema "
            f"habia dejado de consultar; se retomo. Vuelva a pedir la fecha cuando termine."
        )
        db.commit()
    return retomada


# ---------- paso 2: consultar token ----------

def _avisar_si_demora(db, descarga: DescargaFactible) -> None:
    if descarga.creado_en is None or _ahora() - _con_zona(descarga.creado_en) < TIEMPO_AVISO_DEMORA:
        return
    horas = int(TIEMPO_AVISO_DEMORA.total_seconds() // 3600)
    descarga.mensaje = (
        f"{descarga.mensaje} -- Lleva mas de {horas} horas sin terminar; si no avanza, pida a soporte "
        f"de Factible que libere el token {descarga.token}."
    )
    db.commit()
    log.warning("Descarga Factible %s lleva mas de %s horas (token %s).", descarga.fecha_documentos, horas, descarga.token)


def consultar_descarga(db, cliente: ClienteFactible, descarga: DescargaFactible, carpeta: Path) -> None:
    try:
        respuesta = cliente.consultar(descarga.ruc_empresa, descarga.token)
    except ErrorFactible as e:
        # Falla de red/servidor: el token sigue valido, se reintenta en la proxima vuelta.
        descarga.mensaje = f"Reintentando consulta: {e}"
        db.commit()
        _avisar_si_demora(db, descarga)
        return

    # Se anota la respuesta tal cual: la ficha solo documenta PENDIENTE/FINALIZADO y
    # Factible ya devolvio otros estados (PROCESANDO).
    log.info(
        "Consulta Factible token %s: Success=%s Estado=%r Mensaje=%r",
        descarga.token, respuesta.exito, respuesta.estado, respuesta.mensaje,
    )

    if respuesta.en_curso:
        # PENDIENTE o PROCESANDO: Factible sigue armando el ZIP, se consulta en la proxima vuelta.
        descarga.mensaje = (
            f"Ultima consulta {_ahora():%H:%M}: Estado='{respuesta.estado}', Mensaje='{respuesta.mensaje}'"
        )
        db.commit()
        _avisar_si_demora(db, descarga)
        return

    if respuesta.token_no_encontrado:
        descarga.estado = ESTADO_ERROR
        descarga.mensaje = (
            f"{PREFIJO_TOKEN_NO_RECONOCIDO} ({respuesta.mensaje}); lo descarto o vencio. "
            "Vuelva a pedir la fecha."
        )
        db.commit()
        log.warning("Descarga Factible %s: token %s no encontrado.", descarga.fecha_documentos, descarga.token)
        return

    if respuesta.exito and respuesta.estado == ESTADO_FINALIZADO:
        try:
            # Se quitan espacios y saltos de linea: algunos clientes parten el Base64 en lineas.
            zip_base64 = "".join((respuesta.documento_zip_base64 or "").split())
            zip_bytes = base64.b64decode(zip_base64, validate=True)
            resultado = extraer_facturas(db, zip_bytes, carpeta)
        except (binascii.Error, zipfile.BadZipFile) as e:
            descarga.estado = ESTADO_ERROR
            descarga.mensaje = f"Factible devolvio un ZIP invalido: {e}"
        except OSError as e:
            # No se pudo escribir en la carpeta: se deja PENDIENTE para reintentar
            # (Factible sigue entregando el ZIP del mismo token).
            descarga.mensaje = f"No se pudo escribir en {carpeta}: {e}"
        else:
            descarga.estado = ESTADO_FINALIZADO
            descarga.xml_guardados = resultado.guardados
            descarga.omitidos_duplicados = resultado.duplicados
            descarga.omitidos_no_factura = resultado.no_factura
            descarga.mensaje = (
                f"{resultado.guardados} factura(s) dejadas en la carpeta vigilada; "
                f"{resultado.duplicados} ya existian; {resultado.no_factura} no eran facturas."
            )
        db.commit()
        log.info("Descarga Factible %s -> %s: %s", descarga.fecha_documentos, descarga.estado, descarga.mensaje)
        return

    descarga.estado = ESTADO_ERROR
    descarga.mensaje = respuesta.mensaje or f"Estado inesperado de Factible: '{respuesta.estado}'."
    db.commit()


# ---------- paso 3: extraer el ZIP ----------

@dataclass
class ResultadoExtraccion:
    guardados: int = 0
    duplicados: int = 0
    no_factura: int = 0



def _identificar_comprobante(contenido: bytes) -> tuple[str | None, str | None]:
    """Devuelve (codDoc, claveAcceso). Acepta el XML del comprobante o el envuelto en
    <autorizacion><comprobante> (mismo criterio que app/core/lector_xml.py).
    (None, None) si no se puede leer -- se deja pasar para que el vigilante lo
    mande a errores/ con el motivo, en vez de perderlo en silencio."""
    try:
        raiz = ET.fromstring(contenido)
        if raiz.tag == "autorizacion":
            texto = raiz.findtext("comprobante")
            raiz = ET.fromstring(texto) if texto else raiz
        return raiz.findtext("infoTributaria/codDoc"), raiz.findtext("infoTributaria/claveAcceso")
    except ET.ParseError:
        return None, None



def _ya_existe(db, carpeta: Path, nombre: str, clave_acceso: str | None) -> bool:
    if clave_acceso and db.query(Factura.id).filter(Factura.clave_acceso == clave_acceso).first():
        return True
    return any((c / nombre).exists() for c in (carpeta, carpeta / "procesadas", carpeta / "errores"))


def extraer_facturas(db, zip_bytes: bytes, carpeta: Path) -> ResultadoExtraccion:
    resultado = ResultadoExtraccion()
    carpeta.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for info in z.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".xml"):
                continue
            if info.file_size > TAMANO_MAXIMO_XML:
                log.warning("Se omite %s del ZIP de Factible: demasiado grande.", info.filename)
                continue
            contenido = z.read(info)

            cod_doc, clave_acceso = _identificar_comprobante(contenido)
            if cod_doc is not None and cod_doc != COD_DOC_FACTURA:
                resultado.no_factura += 1
                continue

            # Solo el nombre base: nunca se usan las rutas internas del ZIP (evita zip-slip).
            nombre = f"{clave_acceso}.xml" if clave_acceso and clave_acceso.isdigit() else Path(info.filename).name
            if _ya_existe(db, carpeta, nombre, clave_acceso):
                resultado.duplicados += 1
                continue

            # Se escribe como .tmp y se renombra al final: el vigilante solo toma *.xml,
            # asi nunca lee un archivo a medio escribir.
            temporal = carpeta / f"{nombre}.tmp"
            temporal.write_bytes(contenido)
            os.replace(temporal, carpeta / nombre)
            resultado.guardados += 1

    return resultado


# ---------- programador (hilo de fondo) ----------

def ejecutar_ciclo(config: ConfigFactible, cliente: ClienteFactible) -> None:
    """Una vuelta del programador. Separada del bucle para poder probarla sin hilos."""
    with _lock_operacion:
        db = SessionLocal()
        try:
            carpeta = carpeta_destino(db)
            for descarga in db.query(DescargaFactible).filter(DescargaFactible.estado == ESTADO_PENDIENTE).all():
                consultar_descarga(db, cliente, descarga, carpeta)

            if config.descarga_automatica and _toca_descarga_automatica(db, config):
                ayer = _ahora().date() - timedelta(days=1)
                solicitar_descarga(db, cliente, config.ruc, ayer, origen="automatica", usuario="sistema")
        finally:
            db.close()


def _toca_descarga_automatica(db, config: ConfigFactible) -> bool:
    ahora = _ahora()
    if ahora.time() < config.hora_descarga or descarga_en_curso(db) is not None:
        return False
    ayer = ahora.date() - timedelta(days=1)
    intentos = db.query(DescargaFactible).filter(DescargaFactible.fecha_documentos == ayer).all()
    if any(d.estado in ESTADOS_DIA_RESUELTO for d in intentos):
        return False
    # actualizado_en y no creado_en: el aviso de "token pendiente" reutiliza la misma fila.
    ultimo_error = max((_con_zona(d.actualizado_en or d.creado_en) for d in intentos if d.actualizado_en or d.creado_en), default=None)
    if ultimo_error is not None and ahora - ultimo_error < REINTENTO_TRAS_ERROR:
        return False
    return True


def _bucle(config: ConfigFactible, cliente: ClienteFactible) -> None:
    log.info(
        "Programador Factible activo (modo %s, automatica=%s a las %s, consulta cada %ss).",
        config.modo, config.descarga_automatica, config.hora_descarga.strftime("%H:%M"), config.intervalo_consulta,
    )
    global _hilo
    try:
        while not _detener.is_set():
            try:
                ejecutar_ciclo(config, cliente)
            except Exception:
                # Un error puntual (BD caida, etc.) no debe matar el programador.
                log.exception("Error en el ciclo del programador Factible")
            _detener.wait(config.intervalo_consulta)
    finally:
        with _lock_hilo:
            _hilo = None


def iniciar_programador() -> tuple[bool, str]:
    global _hilo
    config = leer_config()
    cliente = obtener_cliente(config)
    if cliente is None:
        return False, "Integracion con Factible desactivada (FACTIBLE_MODO vacio)."
    if config.modo == "real" and not (config.usuario and config.clave and config.ruc):
        return False, "Faltan FACTIBLE_USUARIO / FACTIBLE_CLAVE / FACTIBLE_RUC en el .env."
    with _lock_hilo:
        if _hilo is not None and _hilo.is_alive():
            return False, "El programador de Factible ya esta activo."
        _detener.clear()
        _hilo = threading.Thread(target=_bucle, args=(config, cliente), daemon=True, name="programador-factible")
        _hilo.start()
    return True, "Programador de Factible iniciado."


def detener_programador() -> None:
    with _lock_hilo:
        hilo = _hilo
    if hilo is not None:
        _detener.set()
        hilo.join(timeout=10)
        _detener.clear()


def esta_activo() -> bool:
    with _lock_hilo:
        return _hilo is not None and _hilo.is_alive()


# ---------- usadas por el panel ----------

def descargar_ahora(fecha: date, usuario: str) -> DescargaFactible | str:
    """Descarga manual desde el panel. Devuelve la descarga registrada o un mensaje
    de error si la integracion no esta configurada."""
    config = leer_config()
    cliente = obtener_cliente(config)
    if cliente is None:
        return "La integracion con Factible no esta configurada (FACTIBLE_MODO en el .env)."
    with _lock_operacion:
        db = SessionLocal()
        try:
            descarga = solicitar_descarga(db, cliente, config.ruc, fecha, origen="manual", usuario=usuario)
            if descarga is None:
                en_curso = descarga_en_curso(db)
                return f"Ya hay una descarga en curso (fecha {en_curso.fecha_documentos:%d/%m/%Y}). Espere a que termine."
            db.refresh(descarga)  # el commit expira los atributos; se cargan antes de soltar la sesion
            db.expunge(descarga)
            return descarga
        finally:
            db.close()


def listar_ultimas_descargas(db, limite: int = 10) -> list[DescargaFactible]:
    return db.query(DescargaFactible).order_by(DescargaFactible.id.desc()).limit(limite).all()
