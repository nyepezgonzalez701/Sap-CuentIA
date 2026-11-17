import logging
import re
import threading
import time
from datetime import date
from decimal import Decimal
from urllib.parse import quote

import httpx

from app.adaptadores.sap.base import ClienteSAP, LineaFacturaSAP, ResultadoEnvioSAP
from app.adaptadores.sap.mapeo_servicelayer import (
    CamposSAP,
    construir_factura,
    es_objeto_bloqueado,
    es_sesion_expirada,
    extraer_error,
    filtro_factura,
    filtro_proveedor,
)

# Cliente del Service Layer de SAP Business One. Todavia no se ha probado contra un SAP
# real, solo contra tests/servicelayer_simulado.py; lo no verificado vive en
# mapeo_servicelayer.py. La clave y la cookie B1SESSION nunca se registran: la cookie la
# maneja el httpx.Client y este codigo no la lee.

log = logging.getLogger("sap")

ESPERAS_OBJETO_BLOQUEADO = (1, 2, 4)  # segundos, un reintento por cada una
LARGO_MAXIMO_MENSAJE = 300
TOLERANCIA_TOTAL = Decimal("0.01")
_PATRON_COOKIE_SESION = re.compile(r"B1SESSION=[^;\s'\"]+")


class _FiltroCookieSesion(logging.Filter):
    """En nivel DEBUG, httpcore escribe las cabeceras de respuesta, incluido el Set-Cookie
    con la sesion. Se censura ahi mismo para que no llegue a ningun log."""

    def filter(self, record: logging.LogRecord) -> bool:
        mensaje = record.getMessage()
        if "B1SESSION=" in mensaje:
            record.msg = _PATRON_COOKIE_SESION.sub("B1SESSION=***", mensaje)
            record.args = ()
        return True


for _nombre_logger in ("httpcore.http11", "httpcore.http2"):
    logging.getLogger(_nombre_logger).addFilter(_FiltroCookieSesion())


class ErrorServiceLayer(Exception):
    def __init__(self, mensaje: str, codigo: str | None = None, incierto: bool = False):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.codigo = codigo
        # True si no se sabe si SAP proceso la peticion (timeout, corte, 5xx).
        self.incierto = incierto


class ServiceLayerSAP(ClienteSAP):
    def __init__(
        self,
        url_base: str,
        company_db: str,
        usuario: str,
        clave: str,
        campos: CamposSAP,
        verificar_tls: bool = True,
        timeout: float = 60,
        transport=None,
        espera=time.sleep,
    ):
        self._company_db = company_db
        self._usuario = usuario
        self._clave = clave
        self._campos = campos
        self._espera = espera
        self._sesion_activa = False
        self._lock = threading.Lock()
        if not verificar_tls:
            log.warning("SAP_VERIFICAR_TLS=false: no se verifica el certificado del Service Layer.")
        self._http = httpx.Client(
            base_url=url_base.rstrip("/") + "/", timeout=timeout, verify=verificar_tls, transport=transport
        )

    def cerrar(self) -> None:
        self._http.close()

    def _limpiar(self, texto: str) -> str:
        """Por si SAP o un proxy repiten en su mensaje algo sensible de la peticion."""
        if self._clave:
            texto = texto.replace(self._clave, "***")
        texto = _PATRON_COOKIE_SESION.sub("B1SESSION=***", texto)
        return texto[:LARGO_MAXIMO_MENSAJE]

    def _error_de_respuesta(self, r: httpx.Response) -> ErrorServiceLayer:
        try:
            codigo, mensaje = extraer_error(r.json())
        except ValueError:
            codigo, mensaje = None, ""
        mensaje = self._limpiar(mensaje) or "sin detalle"
        prefijo = f"SAP respondio HTTP {r.status_code}" + (f", codigo {codigo}" if codigo else "")
        return ErrorServiceLayer(f"{prefijo}: {mensaje}", codigo=codigo, incierto=r.status_code >= 500)

    def _iniciar_sesion(self) -> None:
        self._http.cookies.clear()
        self._sesion_activa = False
        try:
            r = self._http.post(
                "Login",
                json={"CompanyDB": self._company_db, "UserName": self._usuario, "Password": self._clave},
            )
        except httpx.HTTPError as e:
            raise ErrorServiceLayer(f"No se pudo conectar con SAP (login): {type(e).__name__}.")
        if r.status_code != 200:
            error = self._error_de_respuesta(r)
            raise ErrorServiceLayer(f"Login en SAP rechazado. {error.mensaje}", codigo=error.codigo)
        self._sesion_activa = True

    def _llamar(self, metodo: str, ruta: str, **kwargs) -> dict:
        """Una sesion expirada renueva el login y reintenta UNA vez; un objeto bloqueado
        reintenta con espera creciente. Cualquier otro error sube tal cual."""
        ya_renovo_sesion = False
        esperas = iter(ESPERAS_OBJETO_BLOQUEADO)
        while True:
            if not self._sesion_activa:
                self._iniciar_sesion()
            try:
                r = self._http.request(metodo, ruta, **kwargs)
            except httpx.HTTPError as e:
                raise ErrorServiceLayer(f"No se pudo completar la llamada a SAP: {type(e).__name__}.", incierto=True)

            if r.status_code < 400:
                try:
                    return r.json()
                except ValueError:
                    raise ErrorServiceLayer(f"SAP respondio algo que no es JSON (HTTP {r.status_code}).", incierto=True)

            error = self._error_de_respuesta(r)
            if es_sesion_expirada(r.status_code, error.codigo):
                if ya_renovo_sesion:
                    raise error
                ya_renovo_sesion = True
                self._sesion_activa = False
                continue

            if es_objeto_bloqueado(error.codigo):
                segundos = next(esperas, None)
                if segundos is None:
                    raise error
                self._espera(segundos)
                continue
            raise error

    def _consultar(self, recurso: str, filtro: str, campos_select: str) -> list[dict]:
        ruta = f"{recurso}?$filter={quote(filtro, safe='')}&$select={campos_select}"
        return self._llamar("GET", ruta).get("value") or []

    def _resolver_card_code(self, ruc: str) -> str:
        filtro = filtro_proveedor(self._campos, ruc)
        socios = self._consultar("BusinessPartners", filtro, "CardCode")
        if not socios:
            raise ErrorServiceLayer(f"El proveedor con RUC {ruc} no existe en SAP.")
        if len(socios) > 1:
            raise ErrorServiceLayer(f"Hay {len(socios)} proveedores con el RUC {ruc} en SAP; una persona debe revisarlo.")
        return socios[0]["CardCode"]

    def _buscar_factura(self, card_code: str, numero_factura: str) -> dict | None:
        filtro = filtro_factura(self._campos, card_code, numero_factura)
        facturas = self._consultar("PurchaseInvoices", filtro, "DocEntry,DocNum")
        if len(facturas) > 1:
            raise ErrorServiceLayer(
                f"Hay {len(facturas)} facturas {numero_factura} de este proveedor en SAP; una persona debe revisarlo."
            )
        return facturas[0] if facturas else None

    def _resultado_existente(self, factura: dict) -> ResultadoEnvioSAP:
        resumen = f"Ya existia en SAP: DocEntry={factura.get('DocEntry')}, DocNum={factura.get('DocNum')}. No se creo otra."
        return ResultadoEnvioSAP(
            exitoso=True,
            referencia=str(factura.get("DocEntry")),
            mensaje=resumen,
            ya_existia=True,
            resumen_respuesta=resumen,
        )

    def _crear_factura(
        self, card_code: str, numero_factura: str, total: float, fecha_emision: date, lineas: list[LineaFacturaSAP]
    ) -> ResultadoEnvioSAP:
        cuerpo = construir_factura(self._campos, card_code, numero_factura, fecha_emision, lineas)
        try:
            creada = self._llamar("POST", "PurchaseInvoices", json=cuerpo)
            if creada.get("DocEntry") is None:
                raise ErrorServiceLayer("SAP no devolvio el DocEntry de la factura.", incierto=True)
        except ErrorServiceLayer as e:
            if not e.incierto:
                raise
            return self._resolver_incierto(card_code, numero_factura, e)

        resumen = f"Creada en SAP: DocEntry={creada.get('DocEntry')}, DocNum={creada.get('DocNum')}."
        doc_total = creada.get("DocTotal")
        if doc_total is not None and abs(Decimal(str(doc_total)) - Decimal(str(total))) > TOLERANCIA_TOTAL:
            resumen += f" AVISO: DocTotal de SAP ({doc_total}) difiere del total del XML ({total:.2f}); revisar el IVA."
        return ResultadoEnvioSAP(
            exitoso=True, referencia=str(creada["DocEntry"]), mensaje=resumen, resumen_respuesta=resumen
        )

    def _resolver_incierto(self, card_code: str, numero_factura: str, causa: ErrorServiceLayer) -> ResultadoEnvioSAP:
        """El POST pudo haberse procesado aunque no llego la respuesta: se consulta una
        vez. Nunca se repite el POST a ciegas; el siguiente envio manual consulta primero."""
        try:
            existente = self._buscar_factura(card_code, numero_factura)
        except ErrorServiceLayer:
            existente = None
        if existente:
            return self._resultado_existente(existente)
        mensaje = f"Resultado incierto: {causa.mensaje} Verifique en SAP antes de reintentar."
        return ResultadoEnvioSAP(exitoso=False, referencia=None, mensaje=mensaje, resumen_respuesta=mensaje)

    def enviar_factura(
        self,
        ruc_proveedor: str,
        numero_factura: str,
        total: float,
        *,
        fecha_emision: date | None = None,
        lineas: list[LineaFacturaSAP] | None = None,
    ) -> ResultadoEnvioSAP:
        if fecha_emision is None or not lineas:
            mensaje = "Faltan la fecha de emision o las lineas de la factura."
            return ResultadoEnvioSAP(exitoso=False, referencia=None, mensaje=mensaje)

        with self._lock:
            try:
                card_code = self._resolver_card_code(ruc_proveedor)
                existente = self._buscar_factura(card_code, numero_factura)
                if existente:
                    return self._resultado_existente(existente)
                return self._crear_factura(card_code, numero_factura, total, fecha_emision, lineas)
            except ErrorServiceLayer as e:
                return ResultadoEnvioSAP(exitoso=False, referencia=None, mensaje=e.mensaje, resumen_respuesta=e.mensaje)
