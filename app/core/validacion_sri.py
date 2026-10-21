from sqlalchemy.orm import Session

from app.core.models import Factura, HistorialAccion
from app.adaptadores.sri.simulador import SimuladorSRI
# Mockup para evaluar si vale la pena conectar al SRI real (webservice de Autorizacion):
# valida la clave de acceso de la factura contra un simulador, igual que enviar_a_sap
# valida contra SimuladorSAP. No cambia el estado de la factura -- es solo una consulta.


class ValidacionInvalidaError(Exception):
    pass


def validar_en_sri(db: Session, factura_id: int, usuario: str) -> tuple[Factura, str]:
    factura = db.query(Factura).filter(Factura.id == factura_id).first()

    if factura is None:
        raise ValidacionInvalidaError("La factura no existe.")

    if not factura.clave_acceso:
        raise ValidacionInvalidaError("Esta factura no tiene clave de acceso registrada.")

    if not usuario or not usuario.strip():
        raise ValidacionInvalidaError("Debes indicar quien consulta.")

    cliente_sri = SimuladorSRI()
    resultado = cliente_sri.consultar_autorizacion(factura.clave_acceso)

    historial = HistorialAccion(
        factura_id=factura.id,
        accion="consultada_sri",
        usuario=usuario.strip(),
        motivo=f"{resultado.estado}: {resultado.mensaje}",
    )
    db.add(historial)
    db.commit()
    db.refresh(factura)

    return factura, resultado.estado
