from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class SugerenciaCuenta:
    codigo: str
    nombre: str
    confianza: str  # "alta" | "baja" -- que tan segura es la sugerencia


class SugeridorCuenta(ABC):
    """
    Interfaz que cualquier asesor de cuentas contables debe cumplir.
    El MVP usa SugeridorSimulado (reglas simples). Mas adelante se podria
    reemplazar por un adaptador que use IA real, sin tocar el resto del
    sistema -- ambos devuelven el mismo tipo de dato y respetan la misma
    regla: nunca sugerir un codigo que no exista en el catalogo autorizado.
    """

    @abstractmethod
    def sugerir(self, ruc_proveedor: str, razon_social: str) -> Optional[SugerenciaCuenta]:
        ...