import json
from typing import Optional

import anthropic
from sqlalchemy.orm import Session

from app.adaptadores.asesor_ia.base import SugeridorCuenta, SugerenciaCuenta
from app.core.models import CuentaContable

MODELO = "claude-sonnet-4-6"

_SIN_SUGERENCIA = "SIN_SUGERENCIA"

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
    Sugiere una cuenta contable para una factura usando la API de Claude.

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
        # confia ciegamente en la respuesta de la IA para esta regla de negocio 
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
    llamada existentes. Hoy lector_xml.py no extrae el nodo <detalles> del XML del SRI,
    asi que sin pasar `detalle` explicitamente la IA solo vera RUC/proveedor/total -- para
    aprovechar la descripcion de items hay que extender lector_xml.py primero.
    """

    def __init__(self, db: Session):
        self.db = db

    def sugerir(
        self,
        ruc_proveedor: str,
        razon_social: str,
        detalle: Optional[list[dict]] = None,
        total: Optional[float] = None,
    ) -> Optional[SugerenciaCuenta]:
        catalogo = [
            {"codigo": c.codigo, "nombre": c.nombre}
            for c in self.db.query(CuentaContable)
            .filter(CuentaContable.activa == True, CuentaContable.tipo_cuenta == "D")  # noqa: E712
            .all()
        ]

        factura = {
            "proveedor": razon_social,
            "ruc": ruc_proveedor,
            "detalle": detalle or [],
            "total": total,
        }

        resultado = sugerir_cuenta_contable(factura, catalogo)

        if resultado["codigo_cuenta"] is None:
            return None

        return SugerenciaCuenta(
            codigo=resultado["codigo_cuenta"],
            nombre=resultado["descripcion_cuenta"],
            confianza=resultado["confianza"],
            justificacion=resultado["justificacion"],
        )
