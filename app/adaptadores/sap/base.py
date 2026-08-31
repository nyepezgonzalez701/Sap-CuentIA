from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ResultadoEnvioSAP:
    exitoso: bool
    referencia: str | None
    mensaje: str


class ClienteSAP(ABC):
    @abstractmethod
    def enviar_factura(self, ruc_proveedor: str, numero_factura: str, total: float) -> ResultadoEnvioSAP:
        ...
