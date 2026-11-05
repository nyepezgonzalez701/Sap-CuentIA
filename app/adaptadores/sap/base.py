from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass
class LineaFacturaSAP:
    descripcion: str
    monto: Decimal  # sin impuestos, igual que FacturaDetalleLinea.monto
    codigo_cuenta: str


@dataclass
class ResultadoEnvioSAP:
    exitoso: bool
    referencia: str | None
    mensaje: str
    ya_existia: bool = False  # la factura ya estaba en SAP: no se creo otra
    resumen_respuesta: str = ""  # solo datos no sensibles, va al historial


class ClienteSAP(ABC):
    @abstractmethod
    def enviar_factura(
        self,
        ruc_proveedor: str,
        numero_factura: str,
        total: float,
        *,
        fecha_emision: date | None = None,
        lineas: list[LineaFacturaSAP] | None = None,
    ) -> ResultadoEnvioSAP:
        ...
