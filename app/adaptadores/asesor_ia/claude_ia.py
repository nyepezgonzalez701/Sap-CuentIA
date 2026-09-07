import json
from typing import Optional

import anthropic
from sqlalchemy.orm import Session

from app.adaptadores.asesor_ia.base import SugeridorCuenta, SugerenciaCuenta
from app.core.models import CuentaContable

MODELO = "claude-sonnet-4-6"

_SIN_SUGERENCIA = "SIN_SUGERENCIA"

# TODO: confirma con tu contador que estos codigos son los que se deben usar.
# Catalogo real de la empresa (cargar_catalogo_real.py), no el generico de la
# Superintendencia. Hay variantes por tasa/tipo que no se estan usando por defecto:
# IVA: "101050102" (13%), "101050104" (15%), "101050109" (15% en Activos Fijos).
# Cuentas por pagar: "201010101"/"201010102" (relacionados local/exterior),
# "201010202" (no relacionados exterior) -- se uso "no relacionados local" porque
# es el caso tipico de una factura de compra a un proveedor externo cualquiera.
IVA_COMPRAS_CODIGO = "101050101"
IVA_COMPRAS_NOMBRE = "IVA en compras de bienes y servicios"
CUENTAS_POR_PAGAR_PROVEEDORES_CODIGO = "201010201"
CUENTAS_POR_PAGAR_PROVEEDORES_NOMBRE = "Cuentas y documentos por pagar Proveedores no relacionados Local"

_PROMPT_SISTEMA = """Eres un asesor contable que sugiere la cuenta contable mas apropiada \
para registrar una factura de compra a un proveedor, usando EXCLUSIVAMENTE el catalogo de \
cuentas autorizado que se entrega a continuacion. Nunca debes proponer un codigo que no este \
en ese catalogo -- si ninguna cuenta encaja con confianza razonable, responde con \
codigo_cuenta="{sin_sugerencia}" y confianza "baja".

Catalogo de cuentas autorizado (codigo - nombre):
{catalogo}
"""

_RESPUESTA_VACIA = {
    "codigo_cuenta": None,
    "descripcion_cuenta": None,
    "confianza": "baja",
    "justificacion": None,
}


def _construir_catalogo_texto(catalogo: list[dict]) -> str:
    return "\n".join(f"{c['codigo']} - {c['nombre']}" for c in catalogo)


def _construir_schema(codigos_validos: list[str]) -> dict:
    # La API de Claude no soporta "oneOf"/tipos nulos en output_config.format, asi que en
    # vez de null se usa el valor centinela _SIN_SUGERENCIA dentro del mismo enum -- sigue
    # siendo un valor cerrado y validable, nunca un codigo libre.
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "codigo_cuenta": {"type": "string", "enum": codigos_validos + [_SIN_SUGERENCIA]},
                "descripcion_cuenta": {"type": "string"},
                "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
                "justificacion": {"type": "string"},
            },
            "required": ["codigo_cuenta", "descripcion_cuenta", "confianza", "justificacion"],
            "additionalProperties": False,
        },
    }


def sugerir_cuenta_contable(factura: dict, catalogo: list[dict]) -> dict:
    """
    Sugiere UNA cuenta contable para toda la factura usando la API de Claude.

    NOTA: esta funcion es el diseño original (una sola cuenta para toda la factura). Se
    mantiene sin cambios porque hoy /subir en main.py todavia la usa a traves de
    SugeridorClaudeIA.sugerir(). El rediseño multi-linea (clasificar_items_factura +
    construir_asiento_contable, mas abajo) es el reemplazo recomendado pero aun no esta
    conectado al resto del sistema.

    factura: {"proveedor": str, "ruc": str, "detalle": [{"descripcion": str, ...}, ...], "total": float}
    catalogo: [{"codigo": str, "nombre": str}, ...] -- cuentas de detalle (tipo_cuenta="D") activas.

    Nunca lanza excepcion: ante cualquier error de red/API, respuesta no parseable, o si la
    IA propone (o el JSON trae) un codigo fuera del catalogo, devuelve codigo_cuenta=None --
    esto activa el flujo de "sin sugerencia" que la pantalla de aprobacion ya maneja.
    """
    if not catalogo:
        return dict(_RESPUESTA_VACIA)

    codigos_validos = [c["codigo"] for c in catalogo]

    items = factura.get("detalle") or []
    detalle_texto = "\n".join(
        f"- {item.get('descripcion', 'sin descripcion')}"
        + (f" (cantidad: {item['cantidad']})" if item.get("cantidad") is not None else "")
        for item in items
    ) or "(la factura no tiene items detallados)"

    mensaje_usuario = (
        f"Proveedor: {factura.get('proveedor', 'desconocido')}\n"
        f"RUC: {factura.get('ruc', 'desconocido')}\n"
        f"Total: {factura.get('total', 'desconocido')}\n\n"
        f"Items de la factura:\n{detalle_texto}\n\n"
        "Elige la cuenta contable del catalogo que mejor corresponda a esta compra."
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODELO,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": _PROMPT_SISTEMA.format(
                        catalogo=_construir_catalogo_texto(catalogo),
                        sin_sugerencia=_SIN_SUGERENCIA,
                    ),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": mensaje_usuario}],
            output_config={"format": _construir_schema(codigos_validos)},
        )
    except Exception:
        # Cualquier fallo de red/API/autenticacion: nunca debe tumbar el flujo de la factura.
        return dict(_RESPUESTA_VACIA)

    texto = next((b.text for b in response.content if b.type == "text"), None)
    if texto is None:
        return dict(_RESPUESTA_VACIA)

    try:
        resultado = json.loads(texto)
    except (json.JSONDecodeError, TypeError):
        return dict(_RESPUESTA_VACIA)

    codigo = resultado.get("codigo_cuenta")
    if codigo == _SIN_SUGERENCIA or codigo not in codigos_validos:
        # Defensa adicional: aunque el schema ya restringe los valores posibles, nunca se
        # confia ciegamente en la respuesta de la IA para esta regla de negocio no negociable.
        return dict(_RESPUESTA_VACIA)

    # La descripcion mostrada siempre viene del catalogo oficial, no del texto libre de la IA.
    nombre_oficial = next(c["nombre"] for c in catalogo if c["codigo"] == codigo)
    confianza = resultado.get("confianza")
    if confianza not in ("alta", "media", "baja"):
        confianza = "baja"

    return {
        "codigo_cuenta": codigo,
        "descripcion_cuenta": nombre_oficial,
        "confianza": confianza,
        "justificacion": resultado.get("justificacion"),
    }


class SugeridorClaudeIA(SugeridorCuenta):
    """
    Adaptador que usa la API de Claude para sugerir la cuenta contable, en vez de las
    reglas fijas de SugeridorSimulado. Implementa la misma interfaz SugeridorCuenta y por
    eso es intercambiable con SugeridorSimulado sin tocar el resto del sistema.

    `detalle` y `total` son opcionales para que este adaptador siga siendo un reemplazo
    directo de SugeridorSimulado.sugerir(ruc, razon_social) sin cambiar los puntos de
    llamada existentes.
    """

    def __init__(self, db: Session):
        self.db = db

    def _catalogo_activo(self) -> list[dict]:
        return [
            {"codigo": c.codigo, "nombre": c.nombre}
            for c in self.db.query(CuentaContable)
            .filter(CuentaContable.activa == True, CuentaContable.tipo_cuenta == "D")  # noqa: E712
            .all()
        ]

    def sugerir(
        self,
        ruc_proveedor: str,
        razon_social: str,
        detalle: Optional[list[dict]] = None,
        total: Optional[float] = None,
    ) -> Optional[SugerenciaCuenta]:
        """
        Metodo original: una sola cuenta para toda la factura. Sigue siendo el que usa
        /subir en main.py hoy -- no se toca hasta que main.py migre a sugerir_asiento().
        """
        factura = {
            "proveedor": razon_social,
            "ruc": ruc_proveedor,
            "detalle": detalle or [],
            "total": total,
        }

        resultado = sugerir_cuenta_contable(factura, self._catalogo_activo())

        if resultado["codigo_cuenta"] is None:
            return None

        return SugerenciaCuenta(
            codigo=resultado["codigo_cuenta"],
            nombre=resultado["descripcion_cuenta"],
            confianza=resultado["confianza"],
            justificacion=resultado["justificacion"],
        )

    def sugerir_asiento(self, items: list[dict], iva_total: float, importe_total: float) -> dict:
        """
        Metodo nuevo (rediseño multi-linea): clasifica cada item de la factura con la IA
        (clasificar_items_factura) y arma el asiento Debe/Haber completo con IVA y
        cuentas por pagar calculados por el codigo, no por la IA (construir_asiento_contable).

        Todavia NO esta conectado a ningun punto de llamada de main.py/aprobaciones.py --
        es el punto de entrada para cuando se decida migrar la pantalla de aprobacion a
        mostrar el asiento completo en vez de una sola cuenta.

        items: [{"descripcion": str, "cantidad": float|None, "monto": float}, ...]
        """
        lineas_clasificadas = clasificar_items_factura(items, self._catalogo_activo())

        lineas_para_asiento = [
            {
                "codigo_cuenta": linea["codigo_cuenta"],
                "descripcion_cuenta": linea["descripcion_cuenta"],
                "monto": item.get("monto") or 0.0,
            }
            for linea, item in zip(lineas_clasificadas, items)
        ]

        asiento = construir_asiento_contable(lineas_para_asiento, iva_total, importe_total)
        return {"lineas_clasificadas": lineas_clasificadas, "asiento": asiento}


# ---------------------------------------------------------------------------
# Rediseño multi-linea: clasificacion por item (IA) + asiento completo (deterministico)
# ---------------------------------------------------------------------------

_LINEA_VACIA = {
    "tipo": None,
    "destino": None,
    "codigo_cuenta": None,
    "descripcion_cuenta": None,
    "confianza": "baja",
    "justificacion": None,
}

_PROMPT_SISTEMA_LINEAS = """Eres un asesor contable. Para cada linea de detalle de una \
factura de compra a un proveedor, debes clasificarla y elegir la cuenta contable que \
corresponde, usando EXCLUSIVAMENTE los catalogos autorizados que se entregan a \
continuacion. Nunca debes proponer un codigo que no este en el catalogo del destino \
elegido para esa linea.

Para cada linea determina:
- tipo: "servicio" o "bien".
- destino:
  - "gasto": la mayoria de servicios, y bienes de consumo interno que no se guardan como \
activo (ej. papeleria, suministros de oficina).
  - "inventario_reventa": bienes o materia prima que la empresa compra para revender o \
transformar y vender (mercaderia, materia prima, suministros de produccion).
  - "activo_fijo": bienes de uso duradero para la operacion propia de la empresa (ej. \
maquinaria, equipo de computo, muebles, vehiculos) -- NUNCA para revender.
- codigo_cuenta: el codigo del catalogo del destino elegido que mejor corresponde.

Si no tienes confianza razonable sobre una linea, responde destino="{sin_sugerencia}" y \
codigo_cuenta="{sin_sugerencia}" para esa linea, con confianza "baja". No inventes ni \
mezcles codigos de un catalogo con el destino de otro.

Catalogo de GASTO (solo valido si destino="gasto"):
{catalogo_gasto}

Catalogo de INVENTARIO para reventa/produccion (solo valido si destino="inventario_reventa"):
{catalogo_inventario}

Catalogo de PROPIEDAD, PLANTA Y EQUIPO (solo valido si destino="activo_fijo"):
{catalogo_activo_fijo}

El usuario te va a dar cada linea numerada como "indice. descripcion". Responde con un \
objeto JSON con una lista "lineas", una entrada por cada linea que se te dio, cada una \
con su "indice" exacto (el mismo numero que antecede a su descripcion) para poder \
emparejar la respuesta sin depender del orden en que la devuelvas.
"""


def destino_de_codigo(codigo: str) -> Optional[str]:
    """
    Clasifica un codigo de cuenta por su prefijo en el destino que le corresponde para
    una linea de factura de compra. Nunca incluye IVA, pasivos, patrimonio ni cuentas
    por pagar -- esos ni siquiera entran al catalogo que ve la IA (ver
    filtrar_catalogo_por_destino), asi que no hay forma de que los sugiera.

    Prefijos del catalogo REAL de la empresa (cargar_catalogo_real.py), no del
    catalogo generico de la Superintendencia:
    - "601" Gastos de Ventas y "602" Gastos Administrativos -> gasto
    - "10106" Inventarios -> inventario_reventa
    - "10201" Propiedades Planta y Equipo (incluye propiedades de inversion) y
      "10202" Activos Intangibles (ej. licencias de software) -> activo_fijo

    Publica (sin "_") porque main.py tambien la usa para armar los desplegables de
    correccion manual por linea en la pantalla de aprobacion.
    """
    if codigo.startswith("601") or codigo.startswith("602"):
        return "gasto"
    if codigo.startswith("10106"):
        return "inventario_reventa"
    if codigo.startswith("10201") or codigo.startswith("10202"):
        return "activo_fijo"
    return None


def filtrar_catalogo_por_destino(catalogo: list[dict]) -> dict[str, list[dict]]:
    resultado = {"gasto": [], "inventario_reventa": [], "activo_fijo": []}
    for cuenta in catalogo:
        destino = destino_de_codigo(cuenta["codigo"])
        if destino is not None:
            resultado[destino].append(cuenta)
    return resultado


def _construir_schema_lineas(codigos_por_destino: dict[str, list[str]]) -> dict:
    codigos_todos = [
        codigo
        for lista in codigos_por_destino.values()
        for codigo in lista
    ] + [_SIN_SUGERENCIA]

    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "lineas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "indice": {"type": "integer"},
                            "tipo": {"type": "string", "enum": ["bien", "servicio"]},
                            "destino": {
                                "type": "string",
                                "enum": ["gasto", "inventario_reventa", "activo_fijo", _SIN_SUGERENCIA],
                            },
                            "codigo_cuenta": {"type": "string", "enum": codigos_todos},
                            "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
                            "justificacion": {"type": "string"},
                        },
                        "required": ["indice", "tipo", "destino", "codigo_cuenta", "confianza", "justificacion"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["lineas"],
            "additionalProperties": False,
        },
    }


def clasificar_items_factura(items: list[dict], catalogo: list[dict]) -> list[dict]:
    """
    Clasifica cada item (linea de detalle) de una factura: tipo (bien/servicio), destino
    (gasto/inventario_reventa/activo_fijo) y codigo_cuenta, usando la API de Claude.

    items: [{"descripcion": str, "cantidad": float|None}, ...]
    catalogo: catalogo completo de cuentas activas de detalle (se filtra internamente por
    destino -- gasto=prefijo 52, inventario_reventa=prefijo 10103, activo_fijo=prefijo
    10201 -- nunca se le manda a la IA cuentas de IVA, pasivos, patrimonio o cuentas por
    pagar, asi que no puede llegar a sugerirlas).

    Devuelve una lista del MISMO largo y en el MISMO orden que `items`. Cada entrada:
    {"tipo": str|None, "destino": str|None, "codigo_cuenta": str|None,
     "descripcion_cuenta": str|None, "confianza": str, "justificacion": str|None}

    Nunca lanza excepcion. Si la IA falla, la respuesta no es JSON valido, la cantidad de
    lineas devueltas no coincide con `items`, o los indices devueltos no son exactamente
    0..n-1, se descarta TODA la respuesta y se devuelven todas las lineas sin clasificar
    -- no se confia en el orden de la lista para emparejar, siempre se usa el indice
    explicito que la IA debe devolver por linea.
    """
    n = len(items)
    if n == 0:
        return []

    sin_clasificar = [dict(_LINEA_VACIA) for _ in range(n)]

    catalogo_por_destino = filtrar_catalogo_por_destino(catalogo)
    codigos_por_destino = {
        destino: {c["codigo"] for c in cuentas}
        for destino, cuentas in catalogo_por_destino.items()
    }
    nombre_por_codigo = {
        c["codigo"]: c["nombre"]
        for cuentas in catalogo_por_destino.values()
        for c in cuentas
    }

    if not nombre_por_codigo:
        return sin_clasificar

    items_texto = "\n".join(
        f"{i}. {item.get('descripcion', 'sin descripcion')}"
        + (f" (cantidad: {item['cantidad']})" if item.get("cantidad") is not None else "")
        for i, item in enumerate(items)
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODELO,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": _PROMPT_SISTEMA_LINEAS.format(
                        catalogo_gasto=_construir_catalogo_texto(catalogo_por_destino["gasto"]),
                        catalogo_inventario=_construir_catalogo_texto(catalogo_por_destino["inventario_reventa"]),
                        catalogo_activo_fijo=_construir_catalogo_texto(catalogo_por_destino["activo_fijo"]),
                        sin_sugerencia=_SIN_SUGERENCIA,
                    ),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{
                "role": "user",
                "content": f"Items de la factura:\n{items_texto}\n\nClasifica cada linea.",
            }],
            output_config={"format": _construir_schema_lineas(
                {destino: list(codigos) for destino, codigos in codigos_por_destino.items()}
            )},
        )
    except Exception:
        return sin_clasificar

    texto = next((b.text for b in response.content if b.type == "text"), None)
    if texto is None:
        return sin_clasificar

    try:
        resultado = json.loads(texto)
        lineas_ia = resultado["lineas"]
    except (json.JSONDecodeError, TypeError, KeyError):
        return sin_clasificar

    if not isinstance(lineas_ia, list) or len(lineas_ia) != n:
        return sin_clasificar

    indices_recibidos = {linea.get("indice") for linea in lineas_ia if isinstance(linea, dict)}
    if indices_recibidos != set(range(n)):
        # No se confia en el orden de la lista: si los indices no cubren exactamente
        # 0..n-1 (faltan, sobran o se repiten), no hay forma segura de saber a que linea
        # de la factura corresponde cada clasificacion.
        return sin_clasificar

    lineas_por_indice = {linea["indice"]: linea for linea in lineas_ia}

    resultado_final = []
    for i in range(n):
        linea = lineas_por_indice[i]
        destino = linea.get("destino")
        codigo = linea.get("codigo_cuenta")
        tipo = linea.get("tipo")

        codigos_validos_destino = codigos_por_destino.get(destino)
        if not codigos_validos_destino or codigo not in codigos_validos_destino:
            # Defensa adicional: el schema ya restringe los valores posibles, pero ademas
            # se verifica que el codigo pertenezca al catalogo del destino declarado --
            # nunca se confia ciegamente en que la IA no mezclo codigo y destino.
            resultado_final.append(dict(_LINEA_VACIA))
            continue

        confianza = linea.get("confianza")
        if confianza not in ("alta", "media", "baja"):
            confianza = "baja"

        resultado_final.append({
            "tipo": tipo if tipo in ("bien", "servicio") else None,
            "destino": destino,
            "codigo_cuenta": codigo,
            "descripcion_cuenta": nombre_por_codigo[codigo],
            "confianza": confianza,
            "justificacion": linea.get("justificacion"),
        })

    return resultado_final


def construir_asiento_contable(lineas: list[dict], iva_total: float, importe_total: float) -> dict:
    """
    Arma el asiento Debe/Haber completo de una factura. NUNCA llama a la IA: las lineas
    ya vienen clasificadas (clasificar_items_factura) y el IVA/importe total ya vienen
    calculados del XML de la factura -- son la unica fuente de verdad para los montos,
    nunca se le piden a un modelo de lenguaje.

    lineas: [{"codigo_cuenta": str|None, "descripcion_cuenta": str|None, "monto": float}, ...]
    iva_total: valor total del IVA de la factura (de infoFactura/totalConImpuestos).
    importe_total: importeTotal de la factura (subtotal + iva, ya calculado en el XML).

    Las lineas sin codigo_cuenta (no clasificadas) igual se incluyen en el Debe, con
    codigo=None, para que el total del asiento siga cuadrando -- quedan marcadas para que
    un humano les asigne la cuenta manualmente antes de aprobar, igual que ya pasa hoy con
    cuenta_sugerida=None en la pantalla de aprobacion.
    """
    debe = []
    todas_clasificadas = True
    for linea in lineas:
        codigo = linea.get("codigo_cuenta")
        if codigo is None:
            todas_clasificadas = False
        debe.append({
            "codigo": codigo,
            "nombre": linea.get("descripcion_cuenta") or "SIN CLASIFICAR -- seleccionar manualmente",
            "monto": round(linea.get("monto") or 0.0, 2),
        })

    debe.append({
        "codigo": IVA_COMPRAS_CODIGO,
        "nombre": IVA_COMPRAS_NOMBRE,
        "monto": round(iva_total, 2),
    })

    haber = [{
        "codigo": CUENTAS_POR_PAGAR_PROVEEDORES_CODIGO,
        "nombre": CUENTAS_POR_PAGAR_PROVEEDORES_NOMBRE,
        "monto": round(importe_total, 2),
    }]

    total_debe = round(sum(f["monto"] for f in debe), 2)
    total_haber = round(sum(f["monto"] for f in haber), 2)

    return {
        "debe": debe,
        "haber": haber,
        "todas_lineas_clasificadas": todas_clasificadas,
        "cuadra": total_debe == total_haber,
        "total_debe": total_debe,
        "total_haber": total_haber,
    }
