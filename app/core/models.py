from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.sql import func
from app.db.config import Base


class Factura(Base):
    __tablename__ = "facturas"

    id = Column(Integer, primary_key=True, index=True)
    ruc_proveedor = Column(String, nullable=False)
    razon_social_emisor = Column(String, nullable=True)  # razonSocial del XML, para registrar el proveedor si hace falta
    clave_acceso = Column(String, nullable=True)  # claveAcceso del XML (SRI), 49 digitos
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
    usuario_guardado = Column(String, nullable=True)  # nombre de quien hizo el ultimo guardado preliminar (estado "guardada")
    referencia_sap = Column(String, nullable=True)
    exportado_a_excel = Column(Boolean, nullable=False, default=False)
    exportado_en = Column(DateTime(timezone=True), nullable=True)
    exportado_por = Column(String, nullable=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())


class Proveedor(Base):
    __tablename__ = "proveedores"

    id = Column(Integer, primary_key=True, index=True)
    ruc = Column(String, nullable=False, unique=True, index=True)
    razon_social = Column(String, nullable=False)
    estado = Column(String, nullable=False, default="activo")
    correo = Column(String, nullable=True)
    saldo = Column(Numeric(12, 2), nullable=False, default=0)
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
    # codigo_cuenta_sugerido se llena una sola vez, al crear la linea (ver ingesta.py), y
    # nunca se sobrescribe despues -- es el registro permanente de lo que propuso la IA se genero el asiento contable,|
    # independiente de lo que el aprobador termine confirmando en codigo_cuenta_final. Aprobado por la contadora, el codigo_cuenta_final puede ser distinto al sugerido por la IA, y eso no borra ni cambia el registro de lo que la IA propuso originalmente. El reporte de precision de la IA (reportes_excel.py) depende de que esto siga siendo asi.
    # reporte de precision de la IA (reportes_excel.py) depende de que esto siga siendo asi.
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

#Usuario base en cado de contadora registre el ejemplo de cuenta contables
class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    nombre_usuario = Column(String, nullable=False, unique=True, index=True)
    hash_contrasena = Column(String, nullable=False)
    rol = Column(String, nullable=False, default="contadora")  # "contadora" | "administrador"
    activo = Column(Boolean, nullable=False, default=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())

    # ejemplo de usario nivel 2


class EstadoVigilante(Base):
    """
    Fila unica (id=1) con el estado actual del hilo de vigilancia de carpeta. Vive en la
    base de datos -- no en memoria -- para que el panel pueda mostrar el estado real
    incluso si el servidor se reinicio y el hilo ya no existe (ver app/core/vigilante_carpeta.py).
    """
    __tablename__ = "estado_vigilante"

    id = Column(Integer, primary_key=True)
    estado = Column(String, nullable=False, default="en_espera")  # en_espera | vigilando | procesando | error
    carpeta_monitoreada = Column(String, nullable=True)
    ultima_actualizacion = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    mensaje_error = Column(Text, nullable=True)


class HistorialVigilancia(Base):
    """Auditoria de quien inicio el monitoreo (o reintento un archivo) y cuando, con el resultado."""
    __tablename__ = "historial_vigilancia"

    id = Column(Integer, primary_key=True, index=True)
    usuario = Column(String, nullable=False)
    accion = Column(String, nullable=False)  # "iniciar_monitoreo" | "reintentar_archivo" en esta seccion s euentra el historail de vigilancia de la carpeta
    resultado = Column(String, nullable=False)  # "ok" | "error"
    mensaje = Column(Text, nullable=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())


class DescargaFactible(Base):
    """
    Una descarga de documentos RECIBIDOS desde el ApiRest de Factible para una fecha.
    Guarda el token apenas se genera porque la API no lo vuelve a entregar ("Ya existe
    un token pendiente" responde con Token=null) -- si el servidor se reinicia, el
    programador retoma desde aqui los tokens PENDIENTE (ver app/core/descarga_factible.py).
    """
    __tablename__ = "descargas_factible"

    id = Column(Integer, primary_key=True, index=True)
    ruc_empresa = Column(String, nullable=False)
    fecha_documentos = Column(Date, nullable=False, index=True)
    token = Column(String, nullable=True)
    estado = Column(String, nullable=False)  # PENDIENTE | FINALIZADO | SIN_DOCUMENTOS | ERROR
    origen = Column(String, nullable=False)  # "automatica" | "manual"
    usuario = Column(String, nullable=False)
    total_claves = Column(Integer, nullable=False, default=0)
    xml_guardados = Column(Integer, nullable=False, default=0)
    omitidos_duplicados = Column(Integer, nullable=False, default=0)
    omitidos_no_factura = Column(Integer, nullable=False, default=0)
    mensaje = Column(Text, nullable=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())
    actualizado_en = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
