from dataclasses import dataclass
from datetime import date

from app.adaptadores.sap.base import LineaFacturaSAP

# PENDIENTE DE VERIFICAR CON $metadata DEL SAP REAL.
# Este archivo es el UNICO lugar con lo que todavia no se ha comprobado contra un SAP
# real (no hay acceso a uno): si algo de aqui resulta distinto, se corrige aqui o en el
# .env, sin tocar servicelayer.py ni el flujo de negocio. Lista completa en el README.
#
# - Como se enlaza el RUC con el CardCode: SAP_CAMPO_RUC_PROVEEDOR.
# - Como va la cuenta contable en cada linea: SAP_CAMPO_CUENTA_LINEA.
# - Que campo lleva el numero de factura del proveedor: SAP_CAMPO_NUMERO_FACTURA.
# - DocType, LineTotal, ItemDescription y CardType: nombres supuestos, ver abajo.
# - Codigo de impuesto por linea: NO se envia todavia, asi que SAP podria calcular un
#   IVA distinto al del XML (por eso servicelayer.py compara DocTotal con el total).

# El pedido inicial indica -5002 = sesion expirada y -2028 = objeto bloqueado. La
# evidencia publica encontrada apunta a otra cosa: sesion invalida como 301 / HTTP 401,
# y -2028 como "No matching records found". Por eso son listas editables, 301 y HTTP 401
# tambien cuentan como sesion expirada, y al usuario siempre se le muestra el texto
# literal de SAP, nunca "bloqueado" por deduccion.
CODIGOS_SESION_EXPIRADA = {"-5002", "301"}
CODIGOS_OBJETO_BLOQUEADO = {"-2028"}

TIPO_DOCUMENTO_SERVICIO = "dDocument_Service"  # lineas por cuenta contable, no por articulo
TIPO_SOCIO_PROVEEDOR = "cSupplier"
LARGO_MAXIMO_DESCRIPCION = 100


@dataclass(frozen=True)
class CamposSAP:
    ruc_proveedor: str
    cuenta_linea: str
    numero_factura: str


def es_sesion_expirada(estado_http: int, codigo: str | None) -> bool:
    return estado_http == 401 or codigo in CODIGOS_SESION_EXPIRADA


def es_objeto_bloqueado(codigo: str | None) -> bool:
    return codigo in CODIGOS_OBJETO_BLOQUEADO


def extraer_error(cuerpo) -> tuple[str | None, str]:
    """Acepta {"message": "texto"} y {"message": {"lang": ..., "value": "texto"}}, porque
    el formato exacto de cada version tampoco se ha comprobado."""
    error = cuerpo.get("error") if isinstance(cuerpo, dict) else None
    if not isinstance(error, dict):
        return None, ""
    mensaje = error.get("message")
    if isinstance(mensaje, dict):
        mensaje = mensaje.get("value")
    codigo = error.get("code")
    return (str(codigo) if codigo is not None else None), str(mensaje or "")


def literal_odata(valor: str) -> str:
    return "'" + valor.replace("'", "''") + "'"


def filtro_proveedor(campos: CamposSAP, ruc: str) -> str:
    return f"{campos.ruc_proveedor} eq {literal_odata(ruc)} and CardType eq {literal_odata(TIPO_SOCIO_PROVEEDOR)}"


def filtro_factura(campos: CamposSAP, card_code: str, numero_factura: str) -> str:
    return f"CardCode eq {literal_odata(card_code)} and {campos.numero_factura} eq {literal_odata(numero_factura)}"


def construir_factura(
    campos: CamposSAP,
    card_code: str,
    numero_factura: str,
    fecha_emision: date,
    lineas: list[LineaFacturaSAP],
) -> dict:
    return {
        "CardCode": card_code,
        "DocDate": fecha_emision.isoformat(),
        "DocType": TIPO_DOCUMENTO_SERVICIO,
        campos.numero_factura: numero_factura,
        "DocumentLines": [
            {
                "ItemDescription": linea.descripcion[:LARGO_MAXIMO_DESCRIPCION],
                campos.cuenta_linea: linea.codigo_cuenta,
                "LineTotal": float(linea.monto),
            }
            for linea in lineas
        ],
    }
