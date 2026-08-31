from enum import Enum
from sqlalchemy.orm import Session

from app.core.models import Proveedor, Factura


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


def detectar_duplicado(db: Session, numero_factura: str) -> bool:
    existente = db.query(Factura).filter(Factura.numero_factura == numero_factura).first()
    return existente is not None