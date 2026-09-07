from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime, Boolean, ForeignKey, Text
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
    cuenta_sugerida = Column(String, nullable=True)
    cuenta_sugerida_confianza = Column(String, nullable=True)  # "alta" | "media" | "baja"
    cuenta_sugerida_justificacion = Column(Text, nullable=True)
    cuenta_final = Column(String, nullable=True)
    referencia_sap = Column(String, nullable=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())


class Proveedor(Base):
    __tablename__ = "proveedores"

    id = Column(Integer, primary_key=True, index=True)
    ruc = Column(String, nullable=False, unique=True, index=True)
    razon_social = Column(String, nullable=False)
    estado = Column(String, nullable=False, default="activo")
    creado_en = Column(DateTime(timezone=True), server_default=func.now())


class CuentaContable(Base):
    __tablename__ = "cuentas_contables"

    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String, nullable=False, unique=True, index=True)
    nombre = Column(String, nullable=False)
    tipo_cuenta = Column(String, nullable=False, default="D")  # T=total (agrupadora), D=detalle (registrable)
    activa = Column(Boolean, nullable=False, default=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())


class FacturaDetalleLinea(Base):
    """
    Una linea de detalle de una factura (item del XML), con la clasificacion que sugirio
    la IA (tipo, destino, cuenta) y la cuenta final que confirma o corrige el aprobador.
    Se crea al subir la factura (ver SugeridorClaudeIA.sugerir_asiento en claude_ia.py) y
    codigo_cuenta_final se llena recien al aprobar. El IVA y la cuenta por pagar a
    proveedores NO se guardan aqui -- se recalculan siempre desde Factura.impuestos y
    Factura.total via construir_asiento_contable(), nunca dependen de la IA.
    """
    __tablename__ = "factura_detalle_lineas"

    id = Column(Integer, primary_key=True, index=True)
    factura_id = Column(Integer, ForeignKey("facturas.id"), nullable=False, index=True)
    orden = Column(Integer, nullable=False)
    descripcion = Column(String, nullable=False)
    monto = Column(Numeric(12, 2), nullable=False)
    tipo = Column(String, nullable=True)  # "bien" | "servicio"
    destino = Column(String, nullable=True)  # "gasto" | "inventario_reventa" | "activo_fijo"
    codigo_cuenta_sugerido = Column(String, nullable=True)
    confianza = Column(String, nullable=True)
    justificacion = Column(Text, nullable=True)
    codigo_cuenta_final = Column(String, nullable=True)


class HistorialAccion(Base):
    __tablename__ = "historial_acciones"

    id = Column(Integer, primary_key=True, index=True)
    factura_id = Column(Integer, ForeignKey("facturas.id"), nullable=False, index=True)
    accion = Column(String, nullable=False)
    usuario = Column(String, nullable=False)
    cuenta_utilizada = Column(String, nullable=True)
    motivo = Column(Text, nullable=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())
