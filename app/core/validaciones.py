from enum import Enum
from sqlalchemy.orm import Session

from app.core.models import Proveedor


class EstadoProveedor(str, Enum):
    ACTIVO = "activo"
    INACTIVO = "inactivo"
    NO_REGISTRADO = "no_registrado"


def validar_proveedor(db: Session, ruc: str) -> tuple[EstadoProveedor, str | None]:
    proveedor = db.query(Proveedor).filter(Proveedor.ruc == ruc).first()

    if proveedor is None:
        return EstadoProveedor.NO_REGISTRADO, None

    if proveedor.estado == "activo":
        return EstadoProveedor.ACTIVO, proveedor.razon_social

    return EstadoProveedor.INACTIVO, proveedor.razon_social