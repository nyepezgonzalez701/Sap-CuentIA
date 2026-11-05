import os
import re
import threading
from dataclasses import dataclass, field

from app.adaptadores.sap.base import ClienteSAP
from app.adaptadores.sap.mapeo_servicelayer import CamposSAP
from app.adaptadores.sap.servicelayer import ServiceLayerSAP
from app.adaptadores.sap.simulador import SimuladorSAP

# Elige el cliente segun SAP_MODO. Un valor invalido o una configuracion incompleta es
# un error: caer al simulador en silencio marcaria facturas como registradas sin que
# hayan llegado a SAP.

_cliente: ServiceLayerSAP | None = None
_lock = threading.Lock()

_NOMBRE_CAMPO_VALIDO = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfiguracionSAPError(Exception):
    pass


@dataclass
class ConfigServiceLayer:
    url_base: str
    company_db: str
    usuario: str
    clave: str = field(repr=False)
    verificar_tls: bool
    campos: CamposSAP


def _leer_verificar_tls() -> bool:
    valor = (os.getenv("SAP_VERIFICAR_TLS") or "true").strip().lower()
    if valor not in ("true", "false"):
        raise ConfiguracionSAPError("SAP_VERIFICAR_TLS debe ser true o false.")
    return valor == "true"


def leer_config_servicelayer() -> ConfigServiceLayer:
    obligatorias = [
        "SAP_URL_BASE", "SAP_COMPANY_DB", "SAP_USUARIO", "SAP_CLAVE",
        "SAP_CAMPO_RUC_PROVEEDOR", "SAP_CAMPO_CUENTA_LINEA", "SAP_CAMPO_NUMERO_FACTURA",
    ]
    faltantes = [nombre for nombre in obligatorias if not (os.getenv(nombre) or "").strip()]
    if faltantes:
        raise ConfiguracionSAPError(f"Faltan variables para SAP_MODO=servicelayer: {', '.join(faltantes)}.")

    # Estos nombres se insertan tal cual en los filtros OData.
    for nombre in ("SAP_CAMPO_RUC_PROVEEDOR", "SAP_CAMPO_CUENTA_LINEA", "SAP_CAMPO_NUMERO_FACTURA"):
        if not _NOMBRE_CAMPO_VALIDO.match(os.environ[nombre].strip()):
            raise ConfiguracionSAPError(f"{nombre} no es un nombre de campo valido.")

    url_base = os.environ["SAP_URL_BASE"].strip()
    if not url_base.startswith(("https://", "http://")):
        raise ConfiguracionSAPError("SAP_URL_BASE debe empezar con https:// o http://.")

    return ConfigServiceLayer(
        url_base=url_base,
        company_db=os.environ["SAP_COMPANY_DB"].strip(),
        usuario=os.environ["SAP_USUARIO"].strip(),
        clave=os.environ["SAP_CLAVE"],
        verificar_tls=_leer_verificar_tls(),
        campos=CamposSAP(
            ruc_proveedor=os.environ["SAP_CAMPO_RUC_PROVEEDOR"].strip(),
            cuenta_linea=os.environ["SAP_CAMPO_CUENTA_LINEA"].strip(),
            numero_factura=os.environ["SAP_CAMPO_NUMERO_FACTURA"].strip(),
        ),
    )


def obtener_cliente_sap() -> ClienteSAP:
    """Se reutiliza la misma instancia del Service Layer para conservar la sesion."""
    global _cliente
    modo = (os.getenv("SAP_MODO") or "simulador").strip().lower()
    if modo == "simulador":
        return SimuladorSAP()
    if modo != "servicelayer":
        raise ConfiguracionSAPError("SAP_MODO debe ser simulador o servicelayer.")

    with _lock:
        if _cliente is None:
            config = leer_config_servicelayer()
            _cliente = ServiceLayerSAP(
                url_base=config.url_base,
                company_db=config.company_db,
                usuario=config.usuario,
                clave=config.clave,
                campos=config.campos,
                verificar_tls=config.verificar_tls,
            )
        return _cliente
