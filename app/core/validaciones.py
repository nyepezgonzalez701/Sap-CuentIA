from enum import Enum
from sqlalchemy.orm import Session

from app.core.models import Proveedor, Factura


class EstadoProveedor(str, Enum):
    REGISTRADO = "registrado"
    NO_REGISTRADO = "no_registrado"


def validar_proveedor(db: Session, ruc: str) -> tuple[EstadoProveedor, str | None]:
    """
    Unica pregunta que importa: el RUC esta o no esta en el maestro de proveedores.
    No se distingue activo/inactivo -- estar en el maestro es suficiente.
    """
    proveedor = db.query(Proveedor).filter(Proveedor.ruc == ruc).first()

    if proveedor is None:
        return EstadoProveedor.NO_REGISTRADO, None

    return EstadoProveedor.REGISTRADO, proveedor.razon_social


def detectar_duplicado(db: Session, numero_factura: str) -> bool:
    existente = db.query(Factura).filter(Factura.numero_factura == numero_factura).first()
    return existente is not None


# Provincias 01-24 (las 24 provincias del Ecuador) + 30 (sociedades/contribuyentes
# extranjeros sin domicilio en el pais). TODO: confirma con tu contador que 30 es el
# unico codigo especial que usan tus proveedores -- si aparece otro (ej. exportadores
# con codigos distintos) hay que ampliar este set.
_PROVINCIAS_RUC_VALIDAS = {f"{i:02d}" for i in range(1, 25)} | {"30"}
# Tercer digito del RUC: 0-5 persona natural, 6 sociedad publica, 9 sociedad privada
# (incluye sucursales/sociedades extranjeras). 7 y 8 no son validos segun el SRI.
_TERCER_DIGITO_RUC_VALIDO = {"0", "1", "2", "3", "4", "5", "6", "9"}


def ruc_estructuralmente_valido(ruc: str) -> bool:
    """
    Chequeo minimo, siempre cierto: 13 digitos numericos. No mira provincia ni tipo
    de contribuyente -- es la unica regla que usa el registro manual en la aprobacion,
    donde ya hay una persona revisando la factura y confirmando la razon social; ahi
    no tiene sentido rechazar un RUC real solo porque no calzo con nuestra heuristica
    de provincias/tipo (ver formato_ruc_valido, mas estricta, para el registro
    automatico sin supervision en /subir).
    """
    return bool(ruc) and len(ruc) == 13 and ruc.isdigit()


def formato_ruc_valido(ruc: str) -> bool:
    """
    Valida la estructura completa del RUC (largo, provincia, tipo de contribuyente) --
    NO valida el digito verificador (modulo 10/11 segun tipo), porque no fue pedido
    y anadirlo sin confirmar el algoritmo exacto para cada tipo podria rechazar RUCs
    validos por error. Es la regla que usa el registro automatico SIN supervision
    (ver registrar_proveedor_automatico llamado desde /subir) -- para el registro
    manual durante la aprobacion, donde ya hay una persona revisando, se usa la
    version mas laxa ruc_estructuralmente_valido.
    """
    if not ruc_estructuralmente_valido(ruc):
        return False
    if ruc[:2] not in _PROVINCIAS_RUC_VALIDAS:
        return False
    if ruc[2] not in _TERCER_DIGITO_RUC_VALIDO:
        return False
    return True

# para registrar un proveedor nuevo en el maestro, se requiere que el RUC tenga formato valido (ver formato_ruc_valido). La razon social es obligatoria, el correo es opcional. El estado inicial del proveedor es "activo" y el saldo inicial es 0. Esta funcion se llama desde la aprobacion de facturas, cuando una persona confirma que un RUC no registrado es valido y quiere agregarlo al maestro.


def registrar_proveedor(db: Session, ruc: str, razon_social: str, correo: str | None = None) -> Proveedor:
    """
    Crea en el maestro un proveedor nuevo. Nunca se llama sola -- siempre la dispara
    una persona confirmando la razon social en /proveedores/nuevo (ver main.py). No hay
    registro automatico sin supervision: un RUC con formato valido que no esta en el
    maestro sigue de largo en /subir hacia la clasificacion de la IA sin registrarse.
    """
    proveedor = Proveedor(
        ruc=ruc,
        razon_social=razon_social,
        estado="activo",
        correo=correo or None,
        saldo=0,
    )
    db.add(proveedor)
    db.flush()
    return proveedor