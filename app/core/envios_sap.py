from sqlalchemy.orm import Session

from app.core.models import Factura, HistorialAccion
from app.adaptadores.sap.simulador import SimuladorSAP


class EnvioInvalidoError(Exception):
    pass


def obtener_facturas_aprobadas_pendientes_envio(db: Session) -> list[Factura]:
    return (
        db.query(Factura)
        .filter(Factura.estado == "aprobada")
        .order_by(Factura.creado_en.asc())
        .all()
    )


def enviar_a_sap(db: Session, factura_id: int, usuario: str) -> Factura:
    factura = db.query(Factura).filter(Factura.id == factura_id).first()

    if factura is None:
        raise EnvioInvalidoError("La factura no existe.")

    if factura.estado != "aprobada":
        raise EnvioInvalidoError(
            f"Esta factura no puede enviarse a SAP: su estado es '{factura.estado}', no 'aprobada'."
        )

    if not usuario or not usuario.strip():
        raise EnvioInvalidoError("Debes indicar quien envia.")

    cliente_sap = SimuladorSAP()
    resultado = cliente_sap.enviar_factura(
        ruc_proveedor=factura.ruc_proveedor,
        numero_factura=factura.numero_factura,
        total=float(factura.total),
    )

    if resultado.exitoso:
        factura.estado = "registrada"
        factura.referencia_sap = resultado.referencia
        historial = HistorialAccion(
            factura_id=factura.id,
            accion="registrada_sap",
            usuario=usuario.strip(),
            motivo=f"Referencia SAP: {resultado.referencia}",
        )
    else:
        historial = HistorialAccion(
            factura_id=factura.id,
            accion="error_envio_sap",
            usuario=usuario.strip(),
            motivo=resultado.mensaje,
        )

    db.add(historial)
    db.commit()
    db.refresh(factura)

    if not resultado.exitoso:
        raise EnvioInvalidoError(resultado.mensaje)

    return factura
