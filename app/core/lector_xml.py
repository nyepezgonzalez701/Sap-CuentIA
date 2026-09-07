import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class XMLInvalidoError(Exception):
    """Se lanza cuando el XML no tiene la estructura esperada de una factura del SRI."""
    pass


@dataclass
class DetalleFactura:
    descripcion: str
    cantidad: Optional[float] = None
    precio_unitario: Optional[float] = None
    monto: Optional[float] = None  # precioTotalSinImpuesto -- ya incluye descuentos, es el valor a usar en el Debe del asiento


@dataclass
class DatosFactura:
    ruc_proveedor: str
    razon_social: str
    numero_factura: str
    clave_acceso: str
    fecha_emision: object
    subtotal: float
    impuestos: float
    total: float
    detalle: list[DetalleFactura] = field(default_factory=list)


def _obtener_texto(elemento, ruta):
    nodo = elemento.find(ruta)
    if nodo is None or nodo.text is None:
        raise XMLInvalidoError(f"Falta el campo obligatorio: {ruta}")
    return nodo.text.strip()


def _extraer_nodo_factura(xml_bytes):
    try:
        raiz = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise XMLInvalidoError(f"El archivo no es un XML valido: {e}")

    if raiz.tag == "autorizacion":
        comprobante_nodo = raiz.find("comprobante")
        if comprobante_nodo is None or comprobante_nodo.text is None:
            raise XMLInvalidoError("El nodo 'autorizacion' no contiene un comprobante.")
        try:
            return ET.fromstring(comprobante_nodo.text)
        except ET.ParseError as e:
            raise XMLInvalidoError(f"El comprobante interno no es un XML valido: {e}")

    if raiz.tag == "factura":
        return raiz

    raise XMLInvalidoError(
        f"Estructura de XML no reconocida (raiz: '{raiz.tag}'). Se esperaba 'factura' o 'autorizacion'."
    )


def leer_factura(xml_bytes: bytes) -> DatosFactura:
    factura = _extraer_nodo_factura(xml_bytes)

    info_tributaria = factura.find("infoTributaria")
    info_factura = factura.find("infoFactura")

    if info_tributaria is None:
        raise XMLInvalidoError("Falta el nodo 'infoTributaria'.")
    if info_factura is None:
        raise XMLInvalidoError("Falta el nodo 'infoFactura'.")

    ruc = _obtener_texto(info_tributaria, "ruc")
    razon_social = _obtener_texto(info_tributaria, "razonSocial")
    clave_acceso = _obtener_texto(info_tributaria, "claveAcceso")
    estab = _obtener_texto(info_tributaria, "estab")
    pto_emi = _obtener_texto(info_tributaria, "ptoEmi")
    secuencial = _obtener_texto(info_tributaria, "secuencial")
    numero_factura = f"{estab}-{pto_emi}-{secuencial}"

    if len(ruc) != 13:
        raise XMLInvalidoError(f"RUC con formato invalido: '{ruc}' (debe tener 13 digitos)")

    fecha_texto = _obtener_texto(info_factura, "fechaEmision")
    try:
        fecha_emision = datetime.strptime(fecha_texto, "%d/%m/%Y").date()
    except ValueError:
        raise XMLInvalidoError(f"Fecha de emision con formato inesperado: '{fecha_texto}'")

    subtotal_texto = _obtener_texto(info_factura, "totalSinImpuestos")
    total_texto = _obtener_texto(info_factura, "importeTotal")

    try:
        subtotal = float(subtotal_texto)
        total = float(total_texto)
    except ValueError:
        raise XMLInvalidoError("Los valores de subtotal o total no son numericos.")

    impuestos = 0.0
    for impuesto_nodo in info_factura.findall("totalConImpuestos/totalImpuesto"):
        valor_nodo = impuesto_nodo.find("valor")
        if valor_nodo is not None and valor_nodo.text:
            impuestos += float(valor_nodo.text)

    detalle = _extraer_detalle(factura)

    return DatosFactura(
        ruc_proveedor=ruc,
        razon_social=razon_social,
        numero_factura=numero_factura,
        clave_acceso=clave_acceso,
        fecha_emision=fecha_emision,
        subtotal=round(subtotal, 2),
        impuestos=round(impuestos, 2),
        total=round(total, 2),
        detalle=detalle,
    )


def _extraer_detalle(factura) -> list[DetalleFactura]:
    """
    Extrae la descripcion de cada item de <detalles><detalle>. No es obligatorio:
    si el nodo falta o un item no tiene descripcion, simplemente se omite -- no
    invalida la factura, ya que hoy nada mas del sistema depende de este dato.
    """
    items = []
    for detalle_nodo in factura.findall("detalles/detalle"):
        descripcion_nodo = detalle_nodo.find("descripcion")
        if descripcion_nodo is None or descripcion_nodo.text is None:
            continue

        cantidad = None
        cantidad_nodo = detalle_nodo.find("cantidad")
        if cantidad_nodo is not None and cantidad_nodo.text:
            try:
                cantidad = float(cantidad_nodo.text)
            except ValueError:
                cantidad = None

        precio_unitario = None
        precio_nodo = detalle_nodo.find("precioUnitario")
        if precio_nodo is not None and precio_nodo.text:
            try:
                precio_unitario = float(precio_nodo.text)
            except ValueError:
                precio_unitario = None

        monto = None
        monto_nodo = detalle_nodo.find("precioTotalSinImpuesto")
        if monto_nodo is not None and monto_nodo.text:
            try:
                monto = round(float(monto_nodo.text), 2)
            except ValueError:
                monto = None

        items.append(DetalleFactura(
            descripcion=descripcion_nodo.text.strip(),
            cantidad=cantidad,
            precio_unitario=precio_unitario,
            monto=monto,
        ))
    return items