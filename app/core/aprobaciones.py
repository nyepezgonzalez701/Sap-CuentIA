from sqlalchemy.orm import Session

from app.core.models import Factura, HistorialAccion, CuentaContable


class AccionInvalidaError(Exception):
    pass


def obtener_facturas_pendientes(db: Session) -> list[Factura]:
    return (
        db.query(Factura)
        .filter(Factura.estado == "pendiente")
        .order_by(Factura.creado_en.asc())
        .all()
    )

# Ejemplo para obtener las cuenats activas
def obtener_cuentas_activas(db: Session) -> list[CuentaContable]:
    return (
        db.query(CuentaContable)
        .filter(CuentaContable.activa == True, CuentaContable.tipo_cuenta == "D")
        .order_by(CuentaContable.codigo.asc())
        .all()
    )

# Procesar Factura con los campos 

def aprobar_factura(db: Session, factura_id: int, usuario: str, codigo_cuenta: str) -> Factura:
    factura = db.query(Factura).filter(Factura.id == factura_id).first()

    if factura is None:
        raise AccionInvalidaError("La factura no existe.")
    if factura.estado != "pendiente":
        raise AccionInvalidaError(f"La factura ya fue procesada (estado actual: {factura.estado}).")
    if not usuario or not usuario.strip():
        raise AccionInvalidaError("Debes indicar quien aprueba.")

    cuenta = (
        db.query(CuentaContable)
        .filter(
            CuentaContable.codigo == codigo_cuenta,
            CuentaContable.activa == True,
            CuentaContable.tipo_cuenta == "D",
        )
        .first()
    )
    if cuenta is None:
        raise AccionInvalidaError("La cuenta seleccionada no existe, no esta activa, o es una cuenta totalizadora (no se puede registrar directamente en una cuenta 'T').")

    factura.estado = "aprobada"
    factura.cuenta_final = cuenta.codigo

    historial = HistorialAccion(
        factura_id=factura.id,
        accion="aprobada",
        usuario=usuario.strip(),
        cuenta_utilizada=cuenta.codigo,
    )
    db.add(historial)                                                                                             
    db.commit()
    db.refresh(factura)
    return factura
# Rechazar factuara posible mejora para que el sistema detecte con los respectivos criterios cual factura rechazr y que reflejo los campos porque la idea es logra automatizar los preocesos de contabilidad en este caso 
#No que el sistema rechaza automaticamente cuando flata also si no que le muestre exactamente al susario que es lo que falta                                                                                           
def rechazar_factura(db: Session, factura_id: int, usuario: str, motivo: str) -> Factura:
    factura = db.query(Factura).filter(Factura.id == factura_id).first()

    if factura is None:
        raise AccionInvalidaError("La factura no existe.")
    if factura.estado != "pendiente":
        raise AccionInvalidaError(f"La factura ya fue procesada (estado actual: {factura.estado}).")
    if not usuario or not usuario.strip():
        raise AccionInvalidaError("Debes indicar quien rechaza.")
    if not motivo or not motivo.strip():
        raise AccionInvalidaError("Debes indicar un motivo de rechazo.")
                                                                                                                                                                                                                                                    
    factura.estado = "rechazada"



    historial = HistorialAccion(
        factura_id=factura.id,
        accion="rechazada",
        usuario=usuario.strip(),
        motivo=motivo.strip(),
    )
    db.add(historial)
    db.commit()
    db.refresh(factura)
    return factura
