"""
Pruebas del rediseño multi-linea del asesor de cuentas contables
(app/adaptadores/asesor_ia/claude_ia.py). Nunca llaman a la API real de Claude --
se mockea anthropic.Anthropic para simular respuestas controladas.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.adaptadores.asesor_ia import claude_ia

CATALOGO = [
    {"codigo": "602030201", "nombre": "Mantenimiento oficina"},
    {"codigo": "601020401", "nombre": "Publicidad medio Impreso"},
    {"codigo": "101060101", "nombre": "Inventario de materia prima"},
    {"codigo": "101060301", "nombre": "Inventario Mercaderia"},
    {"codigo": "102010108", "nombre": "Equipo de computacion"},
    # No pertenece a ningun destino permitido (no empieza con 601/602/10106/10201/10202)
    # -- sirve para probar que un codigo "mezclado" con un destino se descarta.
    {"codigo": "10101", "nombre": "EFECTIVO Y EQUIVALENTES AL EFECTIVO"},
]


def _fake_response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])


def _mock_client(payload: dict) -> MagicMock:
    cliente = MagicMock()
    cliente.messages.create.return_value = _fake_response(payload)
    return cliente


def _linea_ia(indice, tipo, destino, codigo, confianza="alta"):
    return {
        "indice": indice,
        "tipo": tipo,
        "destino": destino,
        "codigo_cuenta": codigo,
        "confianza": confianza,
        "justificacion": "justificacion de prueba",
    }


class TestClasificarItemsFactura:
    def test_bien_para_reventa_se_clasifica_como_inventario_reventa(self):
        items = [{"descripcion": "Cemento para revender en la ferreteria", "cantidad": 10}]
        payload = {"lineas": [_linea_ia(0, "bien", "inventario_reventa", "101060301")]}

        with patch.object(claude_ia.anthropic, "Anthropic", return_value=_mock_client(payload)):
            resultado = claude_ia.clasificar_items_factura(items, CATALOGO)

        assert len(resultado) == 1
        assert resultado[0]["tipo"] == "bien"
        assert resultado[0]["destino"] == "inventario_reventa"
        assert resultado[0]["codigo_cuenta"] == "101060301"
        assert resultado[0]["descripcion_cuenta"] == "Inventario Mercaderia"

    def test_bien_de_uso_interno_duradero_se_clasifica_como_activo_fijo(self):
        items = [{"descripcion": "Laptop para el area de contabilidad", "cantidad": 1}]
        payload = {"lineas": [_linea_ia(0, "bien", "activo_fijo", "102010108")]}

        with patch.object(claude_ia.anthropic, "Anthropic", return_value=_mock_client(payload)):
            resultado = claude_ia.clasificar_items_factura(items, CATALOGO)

        assert resultado[0]["tipo"] == "bien"
        assert resultado[0]["destino"] == "activo_fijo"
        assert resultado[0]["codigo_cuenta"] == "102010108"

    def test_servicio_se_clasifica_como_gasto(self):
        items = [{"descripcion": "Mantenimiento del vehiculo de reparto", "cantidad": 1}]
        payload = {"lineas": [_linea_ia(0, "servicio", "gasto", "602030201")]}

        with patch.object(claude_ia.anthropic, "Anthropic", return_value=_mock_client(payload)):
            resultado = claude_ia.clasificar_items_factura(items, CATALOGO)

        assert resultado[0]["tipo"] == "servicio"
        assert resultado[0]["destino"] == "gasto"
        assert resultado[0]["codigo_cuenta"] == "602030201"

    def test_codigo_fuera_del_catalogo_filtrado_se_descarta(self):
        # "10101" existe en el catalogo general (activo, detalle) pero no pertenece al
        # destino "activo_fijo" (ni a ningun destino permitido) -- si la IA lo devuelve
        # mezclado con un destino, la linea se debe descartar, no confiar en el schema.
        items = [{"descripcion": "Algo mal clasificado", "cantidad": 1}]
        payload = {"lineas": [_linea_ia(0, "bien", "activo_fijo", "10101")]}

        with patch.object(claude_ia.anthropic, "Anthropic", return_value=_mock_client(payload)):
            resultado = claude_ia.clasificar_items_factura(items, CATALOGO)

        assert resultado[0]["codigo_cuenta"] is None
        assert resultado[0]["destino"] is None
        assert resultado[0]["confianza"] == "baja"

    def test_numero_de_lineas_no_coincide_ninguna_linea_se_sugiere(self):
        items = [
            {"descripcion": "Item 1", "cantidad": 1},
            {"descripcion": "Item 2", "cantidad": 1},
        ]
        # La IA devuelve solo 1 linea para los 2 items que se le pidieron.
        payload = {"lineas": [_linea_ia(0, "bien", "gasto", "602030201")]}

        with patch.object(claude_ia.anthropic, "Anthropic", return_value=_mock_client(payload)):
            resultado = claude_ia.clasificar_items_factura(items, CATALOGO)

        assert len(resultado) == 2
        assert all(linea["codigo_cuenta"] is None for linea in resultado)

    def test_indices_repetidos_o_incompletos_ninguna_linea_se_sugiere(self):
        items = [
            {"descripcion": "Item 1", "cantidad": 1},
            {"descripcion": "Item 2", "cantidad": 1},
        ]
        # Mismo largo (2 lineas), pero los indices no cubren exactamente {0, 1}.
        payload = {
            "lineas": [
                _linea_ia(0, "bien", "gasto", "602030201"),
                _linea_ia(0, "bien", "gasto", "520111"),
            ]
        }

        with patch.object(claude_ia.anthropic, "Anthropic", return_value=_mock_client(payload)):
            resultado = claude_ia.clasificar_items_factura(items, CATALOGO)

        assert all(linea["codigo_cuenta"] is None for linea in resultado)

    def test_lista_vacia_no_llama_a_la_api(self):
        with patch.object(claude_ia.anthropic, "Anthropic") as mock_anthropic:
            resultado = claude_ia.clasificar_items_factura([], CATALOGO)

        assert resultado == []
        mock_anthropic.assert_not_called()

    def test_error_de_red_o_api_no_lanza_excepcion(self):
        cliente = MagicMock()
        cliente.messages.create.side_effect = Exception("fallo de red simulado")
        items = [{"descripcion": "Algo", "cantidad": 1}]

        with patch.object(claude_ia.anthropic, "Anthropic", return_value=cliente):
            resultado = claude_ia.clasificar_items_factura(items, CATALOGO)

        assert len(resultado) == 1
        assert resultado[0]["codigo_cuenta"] is None

    def test_respuesta_json_invalida_no_lanza_excepcion(self):
        cliente = MagicMock()
        cliente.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="esto no es JSON")]
        )
        items = [{"descripcion": "Algo", "cantidad": 1}]

        with patch.object(claude_ia.anthropic, "Anthropic", return_value=cliente):
            resultado = claude_ia.clasificar_items_factura(items, CATALOGO)

        assert resultado[0]["codigo_cuenta"] is None


class TestConstruirAsientoContable:
    def test_asiento_incluye_iva_y_cuentas_por_pagar_calculados_por_el_codigo(self):
        lineas = [
            {"codigo_cuenta": "602030201", "descripcion_cuenta": "Mantenimiento oficina", "monto": 100.0},
            {"codigo_cuenta": "102010108", "descripcion_cuenta": "Equipo de computacion", "monto": 200.0},
        ]

        with patch.object(claude_ia.anthropic, "Anthropic") as mock_anthropic:
            asiento = claude_ia.construir_asiento_contable(lineas, iva_total=45.0, importe_total=345.0)

        # Nunca debe llamar a la IA -- IVA y cuentas por pagar se calculan, no se preguntan.
        mock_anthropic.assert_not_called()

        iva_fila = next(f for f in asiento["debe"] if f["codigo"] == claude_ia.IVA_COMPRAS_CODIGO)
        assert iva_fila["monto"] == 45.0

        assert len(asiento["haber"]) == 1
        assert asiento["haber"][0]["codigo"] == claude_ia.CUENTAS_POR_PAGAR_PROVEEDORES_CODIGO
        assert asiento["haber"][0]["monto"] == 345.0

        assert asiento["total_debe"] == 345.0
        assert asiento["total_haber"] == 345.0
        assert asiento["cuadra"] is True
        assert asiento["todas_lineas_clasificadas"] is True

    def test_linea_sin_clasificar_no_rompe_el_cuadre_pero_lo_marca(self):
        lineas = [{"codigo_cuenta": None, "descripcion_cuenta": None, "monto": 50.0}]

        asiento = claude_ia.construir_asiento_contable(lineas, iva_total=6.0, importe_total=56.0)

        assert asiento["todas_lineas_clasificadas"] is False
        assert asiento["cuadra"] is True
        assert asiento["debe"][0]["codigo"] is None

    def test_asiento_no_cuadra_si_los_montos_no_coinciden(self):
        lineas = [{"codigo_cuenta": "602030201", "descripcion_cuenta": "Mantenimiento oficina", "monto": 100.0}]

        asiento = claude_ia.construir_asiento_contable(lineas, iva_total=10.0, importe_total=999.0)

        assert asiento["cuadra"] is False
