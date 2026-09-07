from sqlalchemy.orm import Session

from app.core.models import Factura, HistorialAccion, CuentaContable, FacturaDetalleLinea


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


def obtener_lineas_detalle(db: Session, factura_id: int) -> list[FacturaDetalleLinea]:
    return (
        db.query(FacturaDetalleLinea)
        .filter(FacturaDetalleLinea.factura_id == factura_id)
        .order_by(FacturaDetalleLinea.orden.asc())
        .all()
    )


def aprobar_factura_lineas(db: Session, factura_id: int, usuario: str, codigos_por_linea: dict[int, str]) -> Factura:
    """
    Version multi-linea de aprobar_factura: en vez de una sola cuenta para toda la
    factura, cada linea de factura_detalle_lineas necesita su propia cuenta confirmada
    (la que sugirio la IA, o una distinta que el aprobador elija). No aprueba nada si
    falta una linea por clasificar o si algun codigo no es valido -- un asiento
    incompleto o con una cuenta invalida nunca puede registrarse.
    """
    factura = db.query(Factura).filter(Factura.id == factura_id).first()

    if factura is None:
        raise AccionInvalidaError("La factura no existe.")
    if factura.estado != "pendiente":
        raise AccionInvalidaError(f"La factura ya fue procesada (estado actual: {factura.estado}).")
    if not usuario or not usuario.strip():
        raise AccionInvalidaError("Debes indicar quien aprueba.")

    lineas = obtener_lineas_detalle(db, factura_id)
    if not lineas:
        raise AccionInvalidaError("Esta factura no tiene lineas de detalle para aprobar.")

    codigos_finales = []
    for linea in lineas:
        codigo = codigos_por_linea.get(linea.id)
        if not codigo:
            raise AccionInvalidaError(f"Falta seleccionar una cuenta para la linea: {linea.descripcion}.")

        cuenta = (
            db.query(CuentaContable)
            .filter(
                CuentaContable.codigo == codigo,
                CuentaContable.activa == True,
                CuentaContable.tipo_cuenta == "D",
            )
            .first()
        )
        if cuenta is None:
            raise AccionInvalidaError(
                f"La cuenta '{codigo}' para la linea '{linea.descripcion}' no existe, no esta activa, "
                "o es una cuenta totalizadora."
            )
        codigos_finales.append((linea, cuenta.codigo))

    for linea, codigo_final in codigos_finales:
        linea.codigo_cuenta_final = codigo_final

    factura.estado = "aprobada"
    factura.cuenta_final = ", ".join(codigo for _, codigo in codigos_finales)

    historial = HistorialAccion(
        factura_id=factura.id,
        accion="aprobada",
        usuario=usuario.strip(),
        cuenta_utilizada=factura.cuenta_final,
    )
    db.add(historial)
    db.commit()
    db.refresh(factura)
    return factura


# Rechazar factura posible mejora para que el sistema detecte con los respectivos criterios cual factura rechazr y que reflejo los campos porque la idea es logra automatizar los preocesos de contabilidad en este caso 
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
