from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime
from sqlalchemy.sql import func
from app.db.config import Base


class Factura(Base):
    __tablename__ = "facturas"

    id = Column(Integer, primary_key=True, index=True)
    ruc_proveedor = Column(String, nullable=False)
    numero_factura = Column(String, nullable=False, unique=True)
    fecha_emision = Column(Date, nullable=False)
    subtotal = Column(Numeric(12, 2), nullable=False)
    impuestos = Column(Numeric(12, 2), nullable=False)
    total = Column(Numeric(12, 2), nullable=False)
    estado = Column(String, nullable=False, default="pendiente")
    creado_en = Column(DateTime(timezone=True), server_default=func.now())


class Proveedor(Base):
    __tablename__ = "proveedores"

    id = Column(Integer, primary_key=True, index=True)
    ruc = Column(String, nullable=False, unique=True, index=True)
    razon_social = Column(String, nullable=False)
    estado = Column(String, nullable=False, default="activo")  # activo | inactivo
    creado_en = Column(DateTime(timezone=True), server_default=func.now())