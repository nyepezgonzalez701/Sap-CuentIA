from sqlalchemy.orm import Session

from app.core.aprobaciones import obtener_lineas_detalle
from app.core.models import Factura, HistorialAccion
from app.adaptadores.sap.base import ClienteSAP, LineaFacturaSAP
from app.adaptadores.sap.fabrica import ConfiguracionSAPError, obtener_cliente_sap
#Clase de Envio de Facturas a SAP, con validaciones de estado y registro de historial
#El cliente (simulador o Service Layer) se elige con SAP_MODO, ver app/adaptadores/sap/fabrica.py

class EnvioInvalidoError(Exception):
    pass


def obtener_facturas_aprobadas_pendientes_envio(db: Session) -> list[Factura]:
    return (
        db.query(Factura)
        .filter(Factura.estado == "aprobada")
        .order_by(Factura.creado_en.asc())
        .all()
    )


def _obtener_aprobacion_humana(db: Session, factura_id: int) -> HistorialAccion:
    # El estado por si solo no basta: nada llega a SAP sin una persona identificada en el
    # historial que la haya aprobado.
    aprobacion = (
        db.query(HistorialAccion)
        .filter(HistorialAccion.factura_id == factura_id, HistorialAccion.accion == "aprobada")
        .order_by(HistorialAccion.creado_en.desc(), HistorialAccion.id.desc())
        .first()
    )
    if aprobacion is None or not (aprobacion.usuario or "").strip():
        raise EnvioInvalidoError("Esta factura no puede enviarse a SAP: no hay registro de quien la aprobo.")
    return aprobacion


def _construir_lineas_sap(db: Session, factura: Factura) -> list[LineaFacturaSAP]:
    lineas = obtener_lineas_detalle(db, factura.id)
    if not lineas:
        # Camino legacy: factura sin detalle, una sola cuenta para todo el subtotal.
        if not factura.cuenta_final:
            raise EnvioInvalidoError("Esta factura no tiene cuenta contable confirmada.")
        return [LineaFacturaSAP(f"Factura {factura.numero_factura}", factura.subtotal, factura.cuenta_final)]

    for linea in lineas:
        if not linea.codigo_cuenta_final:
            raise EnvioInvalidoError(f"Falta la cuenta contable de la linea: {linea.descripcion}.")

    # ingesta.py guarda monto=0 si el XML traia un valor ilegible: no se envia un asiento
    # que no cuadre con el comprobante.
    suma_lineas = sum(linea.monto for linea in lineas)
    if suma_lineas != factura.subtotal:
        raise EnvioInvalidoError(
            f"La suma de las lineas ({suma_lineas:.2f}) no coincide con el subtotal ({factura.subtotal:.2f})."
        )
    return [LineaFacturaSAP(linea.descripcion, linea.monto, linea.codigo_cuenta_final) for linea in lineas]


def enviar_a_sap(db: Session, factura_id: int, usuario: str, cliente: ClienteSAP | None = None) -> Factura:
    # El bloqueo de fila evita que un doble clic envie dos veces: la segunda peticion
    # espera a la primera y ya ve el estado "registrada". (SQLite lo ignora.)
    factura = db.query(Factura).filter(Factura.id == factura_id).with_for_update().first()

    if factura is None:
        raise EnvioInvalidoError("La factura no existe.")

    if factura.estado != "aprobada":
        raise EnvioInvalidoError(
            f"Esta factura no puede enviarse a SAP: su estado es '{factura.estado}', no 'aprobada'."
        )

    if not usuario or not usuario.strip():
        raise EnvioInvalidoError("Debes indicar quien envia.")

    aprobacion = _obtener_aprobacion_humana(db, factura.id)
    lineas = _construir_lineas_sap(db, factura)

    try:
        cliente_sap = cliente or obtener_cliente_sap()
    except ConfiguracionSAPError as e:
        raise EnvioInvalidoError(f"Configuracion de SAP incompleta: {e}")

    resultado = cliente_sap.enviar_factura(
        ruc_proveedor=factura.ruc_proveedor,
        numero_factura=factura.numero_factura,
        total=float(factura.total),
        fecha_emision=factura.fecha_emision,
        lineas=lineas,
    )

    fecha_aprobacion = aprobacion.creado_en.strftime("%Y-%m-%d %H:%M") if aprobacion.creado_en else "-"
    trazabilidad = f"Aprobada por {aprobacion.usuario} ({fecha_aprobacion}). Enviada por {usuario.strip()}."
    respuesta_sap = resultado.resumen_respuesta or resultado.mensaje

# La refenecia de Sap registardo usuario y el estado de la factura

    if resultado.exitoso:
        factura.estado = "registrada"
        factura.referencia_sap = resultado.referencia
        historial = HistorialAccion(
            factura_id=factura.id,
            accion="ya_existia_sap" if resultado.ya_existia else "registrada_sap",
            usuario=usuario.strip(),
            motivo=f"{trazabilidad} Referencia SAP: {resultado.referencia}. {respuesta_sap}",
        )
    else:
        historial = HistorialAccion(
            factura_id=factura.id,
            accion="error_envio_sap",
            usuario=usuario.strip(),
            motivo=f"{trazabilidad} {respuesta_sap}",
        )
# ejemplo de como se regsstra el historial


    db.add(historial)
    db.commit()
    db.refresh(factura)

    if not resultado.exitoso:
        raise EnvioInvalidoError(resultado.mensaje)

    return factura
