"""
Logica de guardado de una factura XML, compartida entre la subida manual
(POST /subir en app/main.py) y el vigilante de carpeta (vigilar_carpeta.py).
Contiene solo el trabajo de negocio (parseo, validaciones, clasificacion IA,
guardado en BD) -- no arma HTML ni sabe de donde vinieron los bytes del XML.
"""
from dataclasses import dataclass

from app.core.lector_xml import leer_factura, DatosFactura, XMLInvalidoError
from app.core.validaciones import validar_proveedor, detectar_duplicado, EstadoProveedor
from app.core.models import Factura, FacturaDetalleLinea
from app.adaptadores.asesor_ia.claude_ia import SugeridorClaudeIA

# los resultados de la ingesta de una factuara
@dataclass
class ResultadoIngesta:
    ok: bool
    mensaje: str
    datos: DatosFactura | None = None
    factura_id: int | None = None
    estado_proveedor: EstadoProveedor | None = None
    razon_social_registrada: str | None = None
    resultado_asiento: dict | None = None
    sugerencia: object | None = None


def procesar_factura_xml(db, contenido: bytes) -> ResultadoIngesta:
    try:
        datos = leer_factura(contenido)
    except XMLInvalidoError as e:
        return ResultadoIngesta(ok=False, mensaje=f"Factura invalida: {e}")

    if detectar_duplicado(db, datos.numero_factura):
        return ResultadoIngesta(
            ok=False,
            mensaje=f"Factura bloqueada: ya existe una factura guardada con el numero {datos.numero_factura}.",
            datos=datos,
        )

    # El proveedor NUNCA se registra solo aqui -- si el RUC no esta en el maestro,
    # la factura sigue de largo hacia la clasificacion de la IA igual (para no
    # bloquear el resto del flujo), pero el proveedor solo queda registrado cuando
    # alguien lo confirma a mano en /proveedores/nuevo.
    estado_proveedor, razon_social_registrada = validar_proveedor(db, datos.ruc_proveedor)

    asesor = SugeridorClaudeIA(db)

    nueva_factura = Factura(
        ruc_proveedor=datos.ruc_proveedor,
        razon_social_emisor=datos.razon_social,
        clave_acceso=datos.clave_acceso,
        numero_factura=datos.numero_factura,
        fecha_emision=datos.fecha_emision,
        subtotal=datos.subtotal,
        impuestos=datos.impuestos,
        total=datos.total,
        estado="pendiente",
    )
    db.add(nueva_factura)
    db.flush()  # asigna nueva_factura.id, necesario para las lineas de detalle

    items = [{"descripcion": d.descripcion, "cantidad": d.cantidad, "monto": d.monto} for d in datos.detalle]

    resultado_asiento = None
    sugerencia = None
    if items:
        # Camino nuevo: clasificacion por linea (bien/servicio/destino/cuenta) + asiento para poder clasificar los difretes  impuestos.
        resultado_asiento = asesor.sugerir_asiento(items, iva_total=datos.impuestos, importe_total=datos.total)
        for orden, (item, linea) in enumerate(zip(items, resultado_asiento["lineas_clasificadas"])):
            db.add(FacturaDetalleLinea(
                factura_id=nueva_factura.id,
                orden=orden,
                descripcion=item["descripcion"],
                monto=item.get("monto") or 0,
                tipo=linea["tipo"],
                destino=linea["destino"],
                codigo_cuenta_sugerido=linea["codigo_cuenta"],
                confianza=linea["confianza"],
                justificacion=linea["justificacion"],

              

            
            ))
    else:
        
        sugerencia = asesor.sugerir(datos.ruc_proveedor, datos.razon_social, detalle=[], total=datos.total)
        nueva_factura.cuenta_sugerida = sugerencia.codigo if sugerencia else None
        nueva_factura.cuenta_sugerida_confianza = sugerencia.confianza if sugerencia else None
        nueva_factura.cuenta_sugerida_justificacion = sugerencia.justificacion if sugerencia else None

    factura_id = nueva_factura.id  
    db.commit()
# Cuando la factura se guarda como pendiente, no se envia a SAP todavia -- eso se hace en otro paso posterior, cuando alguien la aprueba y la envia a SAP. y Provedores regsitardos 
    return ResultadoIngesta(
        ok=True,
        mensaje="Factura procesada y guardada como pendiente de aprobacion.",
        datos=datos,
        factura_id=factura_id,
        estado_proveedor=estado_proveedor,
        razon_social_registrada=razon_social_registrada,
        resultado_asiento=resultado_asiento,
        sugerencia=sugerencia,
    )
