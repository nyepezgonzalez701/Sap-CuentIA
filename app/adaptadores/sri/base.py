from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ResultadoValidacionSRI:
    autorizado: bool
    estado: str  # "AUTORIZADO" | "NO AUTORIZADO" | "NO ENCONTRADO"
    mensaje: str
    


class ClienteSRI(ABC):
    @abstractmethod
    def consultar_autorizacion(self, clave_acceso: str) -> ResultadoValidacionSRI:
        ...
