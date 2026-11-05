from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.models import Factura, HistorialAccion, CuentaContable, FacturaDetalleLinea
from app.core.validaciones import EstadoProveedor, validar_proveedor


class AccionInvalidaError(Exception):
    pass


def _cuenta_valida(db: Session, codigo: str) -> CuentaContable | None:
    return (
        db.query(CuentaContable)
        .filter(
            CuentaContable.codigo == codigo,
            CuentaContable.activa == True,
            CuentaContable.tipo_cuenta == "D",
        )
        .first()
    )


# ---------- Fase 1 de automatizacion: solo deteccion, no actua sobre nada ----------
# Marca que facturas calificarian para auto-aprobacion (proveedor conocido, cuenta
# confirmada por confianza alta o por recurrencia), sin aprobar nada -- la contadora
# sigue haciendo el clic. Sirve para validar el criterio con datos reales antes de
# automatizar (fases futuras: aprobacion en lote, luego auto-aprobacion con kill-switch).

UMBRAL_RECURRENCIA_AUTO_APROBACION = 3


def _contar_aprobaciones_previas(db: Session, ruc_proveedor: str, codigo_cuenta: str) -> int:
    return (
        db.query(HistorialAccion)
        .join(Factura, Factura.id == HistorialAccion.factura_id)
        .filter(
            Factura.ruc_proveedor == ruc_proveedor,
            HistorialAccion.cuenta_utilizada == codigo_cuenta,
            HistorialAccion.accion == "aprobada",
        )
        .count()
    )


def _tiene_rechazos_previos(db: Session, ruc_proveedor: str) -> bool:
    # rechazar_factura no guarda cuenta_utilizada (queda None), asi que el chequeo es
    # por RUC completo, no por combinacion RUC+cuenta -- mas simple y mas conservador:
    # cualquier rechazo pasado de ese proveedor descarta la candidatura.
    return (
        db.query(HistorialAccion)
        .join(Factura, Factura.id == HistorialAccion.factura_id)
        .filter(Factura.ruc_proveedor == ruc_proveedor, HistorialAccion.accion == "rechazada")
        .first()
        is not None
    )


def es_candidata_auto_aprobacion(
    db: Session,
    ruc_proveedor: str,
    estado_proveedor,
    cuentas_confianza: list[tuple[str | None, str | None]],
) -> bool:
    """
    cuentas_confianza: una entrada (codigo_cuenta, confianza) por linea a aprobar -- o
    una sola entrada para el camino legacy. Todas las lineas deben calificar; una sola
    sin cuenta o sin confianza/recurrencia suficiente descarta la factura completa
    (la aprobacion es atomica, no tiene sentido marcar la factura si una linea todavia
    es dudosa).
    """
    if estado_proveedor != EstadoProveedor.REGISTRADO:
        return False
    if not cuentas_confianza:
        return False
    if _tiene_rechazos_previos(db, ruc_proveedor):
        return False
    for codigo, confianza in cuentas_confianza:
        if not codigo:
            return False
        if confianza == "alta":
            continue
        if _contar_aprobaciones_previas(db, ruc_proveedor, codigo) < UMBRAL_RECURRENCIA_AUTO_APROBACION:
            return False
    return True


# ---------- Aprobacion masiva (seleccion multiple en la mesa de aprobacion) ----------
#
# Reglas de seguridad (nivel 3): una factura NUNCA se ofrece para aprobacion masiva --
# ni siquiera revalidada en el servidor -- si:
#   1. Falta una cuenta contable en alguna linea (o en la factura, camino legacy).
#   2. El proveedor no esta validado en el maestro.
#   3. Hay una advertencia registrada sobre el proveedor (rechazo previo).
#   4. La confianza de la sugerencia de la IA es "baja" en alguna linea/factura.
# Deliberadamente NO es el mismo criterio que es_candidata_auto_aprobacion (esa
# tambien exige recurrencia historica para confianza media/baja) -- la aprobacion
# masiva sigue siendo una decision de la contadora via checkbox, asi que basta con
# que la factura este completa y sin senales de alerta, sin exigir historial previo.


@dataclass
class ElegibilidadLote:
    elegible: bool
    motivos_bloqueo: list[str] = field(default_factory=list)


def evaluar_elegibilidad_lote(
    db: Session,
    factura: Factura,
    estado_proveedor,
    lineas: list[FacturaDetalleLinea],
) -> ElegibilidadLote:
    motivos: list[str] = []

    if estado_proveedor != EstadoProveedor.REGISTRADO:
        motivos.append("El proveedor no esta registrado en el maestro.")
    if _tiene_rechazos_previos(db, factura.ruc_proveedor):
        motivos.append("El proveedor tiene un rechazo previo registrado (advertencia).")

    if lineas:
        for linea in lineas:
            codigo = linea.codigo_cuenta_final or linea.codigo_cuenta_sugerido
            if not codigo:
                motivos.append(f"Falta cuenta contable en la linea \"{linea.descripcion}\".")
            elif (linea.confianza or "baja") == "baja":
                motivos.append(f"Confianza baja de la IA en la linea \"{linea.descripcion}\".")
    else:
        codigo = factura.cuenta_final or factura.cuenta_sugerida
        if not codigo:
            motivos.append("Falta cuenta contable.")
        elif (factura.cuenta_sugerida_confianza or "baja") == "baja":
            motivos.append("Confianza baja de la IA.")

    return ElegibilidadLote(elegible=not motivos, motivos_bloqueo=motivos)

#Parte de filtro funcion adicional para la mesa de aprobacion, que permite filtrar por proveedor, cuenta, fecha y monto
@dataclass
class FiltrosFacturas:
    """Nivel 2: filtros de la mesa de aprobacion. Todos son opcionales y se combinan con AND."""
    proveedor: str | None = None
    cuenta: str | None = None
    fecha_desde: date | None = None
    fecha_hasta: date | None = None
    monto_min: Decimal | None = None
    monto_max: Decimal | None = None

    def esta_vacio(self) -> bool:
        return not any([
            self.proveedor, self.cuenta, self.fecha_desde, self.fecha_hasta,
            self.monto_min is not None, self.monto_max is not None,
        ])


def _codigos_cuenta_de_factura(factura: Factura, lineas: list[FacturaDetalleLinea]) -> set[str]:
    codigos = set()
    if lineas:
        for linea in lineas:
            codigo = linea.codigo_cuenta_final or linea.codigo_cuenta_sugerido
            if codigo:
                codigos.add(codigo)
    else:
        codigo = factura.cuenta_final or factura.cuenta_sugerida
        if codigo:
            codigos.add(codigo)
    return codigos


def filtrar_facturas_pendientes(db: Session, facturas: list[Factura], filtros: FiltrosFacturas) -> list[Factura]:
    if filtros.esta_vacio():
        return facturas

    resultado = []
    for factura in facturas:
        if filtros.proveedor:
            _, razon_social = validar_proveedor(db, factura.ruc_proveedor)
            razon_social = razon_social or factura.razon_social_emisor or ""
            texto = f"{factura.ruc_proveedor} {razon_social}".lower()
            if filtros.proveedor.lower() not in texto:
                continue

        if filtros.cuenta:
            lineas = obtener_lineas_detalle(db, factura.id)
            codigos = _codigos_cuenta_de_factura(factura, lineas)
            if not any(filtros.cuenta.lower() in codigo.lower() for codigo in codigos):
                continue

        if filtros.fecha_desde and factura.fecha_emision < filtros.fecha_desde:
            continue
        if filtros.fecha_hasta and factura.fecha_emision > filtros.fecha_hasta:
            continue
        if filtros.monto_min is not None and factura.total < filtros.monto_min:
            continue
        if filtros.monto_max is not None and factura.total > filtros.monto_max:
            continue

        resultado.append(factura)

    return resultado


def aprobar_lote(db: Session, factura_ids: list[int], usuario: str) -> dict:
    """
    Aprueba en lote solo lo que, revalidado en este mismo instante en el servidor,
    sigue pendiente/guardado y sigue pasando evaluar_elegibilidad_lote -- nunca se
    confia en que el checkbox marcado en el navegador siga siendo valido (la factura
    pudo cambiar de estado, o perder elegibilidad, entre que se pinto la pantalla y
    que se envio el formulario). Siempre usa la cuenta ya confirmada o sugerida: el
    lote nunca deja elegir una cuenta distinta, eso se sigue haciendo factura por
    factura si hace falta.

    Devuelve {"aprobadas": [id, ...], "omitidas": [(id, motivo), ...]} -- nunca lanza
    excepcion por una factura individual, para que una sola fallida no cancele el resto.
    """
    aprobadas: list[int] = []
    omitidas: list[tuple[int, str]] = []

    for factura_id in factura_ids:
        factura = db.query(Factura).filter(Factura.id == factura_id).first()
        if factura is None:
            omitidas.append((factura_id, "La factura ya no existe."))
            continue
        if factura.estado not in ("pendiente", "guardada"):
            omitidas.append((factura_id, f"Ya no esta pendiente (estado actual: {factura.estado})."))
            continue

        estado_proveedor, _ = validar_proveedor(db, factura.ruc_proveedor)
        lineas = obtener_lineas_detalle(db, factura_id)
        elegibilidad = evaluar_elegibilidad_lote(db, factura, estado_proveedor, lineas)
        if not elegibilidad.elegible:
            omitidas.append((factura_id, "; ".join(elegibilidad.motivos_bloqueo)))
            continue

        try:
            if lineas:
                codigos_por_linea = {
                    linea.id: (linea.codigo_cuenta_final or linea.codigo_cuenta_sugerido) for linea in lineas
                }
                aprobar_factura_lineas(db, factura_id, usuario, codigos_por_linea)
                
            else:
                codigo = factura.cuenta_final or factura.cuenta_sugerida
                aprobar_factura(db, factura_id, usuario, codigo)
            aprobadas.append(factura_id)
        except AccionInvalidaError as e:
            omitidas.append((factura_id, str(e)))

    return {"aprobadas": aprobadas, "omitidas": omitidas}


def obtener_facturas_pendientes(db: Session) -> list[Factura]:
    # "guardada" es un avance preliminar (ver guardar_factura_preliminar) -- no cierra
    # el ciclo de la factura, asi que sigue apareciendo en la mesa de aprobacion igual
    # que "pendiente".
    return (
        db.query(Factura)
        .filter(Factura.estado.in_(["pendiente", "guardada"]))
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
    if factura.estado not in ("pendiente", "guardada"):
        raise AccionInvalidaError(f"La factura ya fue procesada (estado actual: {factura.estado}).")
    if not usuario or not usuario.strip():
        raise AccionInvalidaError("Debes indicar quien aprueba.")

    cuenta = _cuenta_valida(db, codigo_cuenta)
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
# ejemplos de funcles para obntener detalles de facturas y lines de facturas 

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
    if factura.estado not in ("pendiente", "guardada"):
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

        cuenta = _cuenta_valida(db, codigo)
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
    if factura.estado not in ("pendiente", "guardada"):
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


def guardar_factura_preliminar(
    db: Session,
    factura_id: int,
    usuario: str,
    codigo_cuenta: str | None = None,
    codigos_por_linea: dict[int, str] | None = None,
) -> Factura:
    """
    Guardado preliminar para facturas recurrentes que la contadora revisa varias veces
    antes de decidir: persiste la cuenta elegida y quien la eligio, sin aprobar ni
    rechazar -- el ciclo de la factura sigue abierto y NUNCA dispara un envio a SAP.
    Reutiliza factura.cuenta_final / linea.codigo_cuenta_final para guardar la cuenta
    (los mismos campos que usa la aprobacion real; se sobrescriben sin problema cuando
    la factura finalmente se aprueba). A diferencia de aprobar_factura_lineas, una
    linea sin cuenta todavia no es un error: el guardado puede ser parcial.
    """
    factura = db.query(Factura).filter(Factura.id == factura_id).first()

    if factura is None:
        raise AccionInvalidaError("La factura no existe.")
    if factura.estado not in ("pendiente", "guardada"):
        raise AccionInvalidaError(f"La factura ya fue procesada (estado actual: {factura.estado}).")
    if not usuario or not usuario.strip():
        raise AccionInvalidaError("Debes indicar quien guarda y autoriza el avance.")

    lineas = obtener_lineas_detalle(db, factura_id)
    cuenta_utilizada = None

    if lineas:
        codigos_por_linea = codigos_por_linea or {}
        codigos_guardados = []
        for linea in lineas:
            codigo = codigos_por_linea.get(linea.id)
            if not codigo:
                continue
            cuenta = _cuenta_valida(db, codigo)
            if cuenta is None:
                raise AccionInvalidaError(
                    f"La cuenta '{codigo}' para la linea '{linea.descripcion}' no existe, no esta activa, "
                    "o es una cuenta totalizadora."
                )
            # Codigo cuenta final de la linea se guarda en el mismo campo que usa la aprobacion real, para cuando existe una ´petición de nievla 2  lina de codigo de cuenta final
            
            linea.codigo_cuenta_final = cuenta.codigo
            codigos_guardados.append(cuenta.codigo)
        if codigos_guardados:
            cuenta_utilizada = ", ".join(codigos_guardados)
    elif codigo_cuenta:
        cuenta = _cuenta_valida(db, codigo_cuenta)
        if cuenta is None:
            raise AccionInvalidaError("La cuenta seleccionada no existe, no esta activa, o es una cuenta totalizadora.")
        factura.cuenta_final = cuenta.codigo
        cuenta_utilizada = cuenta.codigo

        #Ejemplos de estado guardada

    factura.estado = "guardada"
    
    factura.usuario_guardado = usuario.strip()
    factura.fecha_guardado = date.today()

    historial = HistorialAccion(
        factura_id=factura.id,
        accion="guardada",
        usuario=usuario.strip(),
        cuenta_utilizada=cuenta_utilizada,
    )
    db.add(historial)
    db.commit()
    db.refresh(factura)
    return factura
