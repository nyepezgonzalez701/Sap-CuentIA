"""
Pruebas de app/core/lector_xml.py -- el parseo del XML del SRI es el primer paso
de todo el pipeline (subida manual y vigilante de carpeta) y, hasta ahora, solo se
ejercitaba indirectamente con XML bien formado dentro de los e2e. Aqui se prueban
los casos raros que un archivo real puede traer: raiz "autorizacion" (como llega el
SRI en la practica) vs. "factura" pelada, nodos faltantes, campos no numericos,
fechas con formato distinto, y el detalle/correo adicional opcionales.
"""
import pytest

from app.core.lector_xml import leer_factura, XMLInvalidoError


XML_FACTURA_BASE = """<?xml version="1.0" encoding="UTF-8"?>
<factura id="comprobante" version="1.0.0">
  <infoTributaria>
    <razonSocial>PROVEEDOR PRUEBA S.A.</razonSocial>
    <ruc>1790012345001</ruc>
    <claveAcceso>2108202601179001234500110010010000001231234567812</claveAcceso>
    <estab>001</estab>
    <ptoEmi>002</ptoEmi>
    <secuencial>000000123</secuencial>
  </infoTributaria>
  <infoFactura>
    <fechaEmision>15/08/2026</fechaEmision>
    <totalSinImpuestos>100.00</totalSinImpuestos>
    <totalConImpuestos>
      <totalImpuesto><codigo>2</codigo><valor>15.00</valor></totalImpuesto>
    </totalConImpuestos>
    <importeTotal>115.00</importeTotal>
  </infoFactura>
</factura>"""


def _envolver_en_autorizacion(xml_comprobante: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<autorizacion>
  <estado>AUTORIZADO</estado>
  <comprobante><![CDATA[{xml_comprobante}]]></comprobante>
</autorizacion>"""


class TestCaminoFeliz:
    def test_factura_raiz_directa_se_parsea(self):
        datos = leer_factura(XML_FACTURA_BASE.encode("utf-8"))
        assert datos.ruc_proveedor == "1790012345001"
        assert datos.razon_social == "PROVEEDOR PRUEBA S.A."
        assert datos.clave_acceso == "2108202601179001234500110010010000001231234567812"

    def test_numero_factura_se_arma_con_estab_ptoemi_secuencial(self):
        datos = leer_factura(XML_FACTURA_BASE.encode("utf-8"))
        assert datos.numero_factura == "001-002-000000123"

    def test_fecha_emision_se_parsea_como_date(self):
        from datetime import date
        datos = leer_factura(XML_FACTURA_BASE.encode("utf-8"))
        assert datos.fecha_emision == date(2026, 8, 15)

    def test_montos_se_redondean_a_dos_decimales(self):
        xml = XML_FACTURA_BASE.replace("<totalSinImpuestos>100.00</totalSinImpuestos>", "<totalSinImpuestos>100.567</totalSinImpuestos>")
        datos = leer_factura(xml.encode("utf-8"))
        assert datos.subtotal == 100.57

    def test_suma_multiples_nodos_totalimpuesto(self):
        xml = XML_FACTURA_BASE.replace(
            "<totalImpuesto><codigo>2</codigo><valor>15.00</valor></totalImpuesto>",
            "<totalImpuesto><codigo>2</codigo><valor>10.00</valor></totalImpuesto>"
            "<totalImpuesto><codigo>3</codigo><valor>5.00</valor></totalImpuesto>",
        )
        datos = leer_factura(xml.encode("utf-8"))
        assert datos.impuestos == 15.0

    def test_sin_detalle_devuelve_lista_vacia(self):
        datos = leer_factura(XML_FACTURA_BASE.encode("utf-8"))
        assert datos.detalle == []

    def test_sin_correo_adicional_devuelve_none(self):
        datos = leer_factura(XML_FACTURA_BASE.encode("utf-8"))
        assert datos.correo_proveedor is None


class TestWrapperAutorizacion:
    def test_autorizacion_con_comprobante_valido_se_parsea_igual(self):
        xml = _envolver_en_autorizacion(XML_FACTURA_BASE)
        datos = leer_factura(xml.encode("utf-8"))
        assert datos.ruc_proveedor == "1790012345001"
        assert datos.numero_factura == "001-002-000000123"

    def test_autorizacion_sin_nodo_comprobante_falla(self):
        xml = """<?xml version="1.0"?><autorizacion><estado>AUTORIZADO</estado></autorizacion>"""
        with pytest.raises(XMLInvalidoError, match="comprobante"):
            leer_factura(xml.encode("utf-8"))

    def test_autorizacion_con_comprobante_interno_invalido_falla(self):
        xml = """<?xml version="1.0"?><autorizacion><comprobante><![CDATA[esto no es xml]]></comprobante></autorizacion>"""
        with pytest.raises(XMLInvalidoError):
            leer_factura(xml.encode("utf-8"))

    def test_raiz_desconocida_falla(self):
        xml = """<?xml version="1.0"?><notaCredito><algo/></notaCredito>"""
        with pytest.raises(XMLInvalidoError, match="no reconocida"):
            leer_factura(xml.encode("utf-8"))


class TestXmlMalformado:
    def test_bytes_que_no_son_xml_fallan(self):
        with pytest.raises(XMLInvalidoError):
            leer_factura(b"esto claramente no es un xml <<<")

    def test_xml_vacio_falla(self):
        with pytest.raises(XMLInvalidoError):
            leer_factura(b"")


class TestNodosObligatoriosFaltantes:
    def test_falta_infotributaria_falla(self):
        xml = """<?xml version="1.0"?><factura><infoFactura><fechaEmision>15/08/2026</fechaEmision></infoFactura></factura>"""
        with pytest.raises(XMLInvalidoError, match="infoTributaria"):
            leer_factura(xml.encode("utf-8"))

    def test_falta_infofactura_falla(self):
        xml = """<?xml version="1.0"?><factura><infoTributaria><ruc>1790012345001</ruc></infoTributaria></factura>"""
        with pytest.raises(XMLInvalidoError, match="infoFactura"):
            leer_factura(xml.encode("utf-8"))

    def test_falta_ruc_falla_con_mensaje_del_campo(self):
        xml = XML_FACTURA_BASE.replace("<ruc>1790012345001</ruc>", "")
        with pytest.raises(XMLInvalidoError, match="ruc"):
            leer_factura(xml.encode("utf-8"))

    def test_falta_razon_social_falla(self):
        xml = XML_FACTURA_BASE.replace("<razonSocial>PROVEEDOR PRUEBA S.A.</razonSocial>", "")
        with pytest.raises(XMLInvalidoError):
            leer_factura(xml.encode("utf-8"))


class TestFormatosInvalidos:
    def test_ruc_con_menos_de_13_digitos_falla(self):
        xml = XML_FACTURA_BASE.replace("<ruc>1790012345001</ruc>", "<ruc>17900123</ruc>")
        with pytest.raises(XMLInvalidoError, match="RUC"):
            leer_factura(xml.encode("utf-8"))

    def test_fecha_en_formato_iso_falla(self):
        # el SRI usa dd/mm/aaaa -- un XML con fecha ISO (aaaa-mm-dd) debe rechazarse
        # explicitamente en vez de parsearse mal o silenciosamente.
        xml = XML_FACTURA_BASE.replace("<fechaEmision>15/08/2026</fechaEmision>", "<fechaEmision>2026-08-15</fechaEmision>")
        with pytest.raises(XMLInvalidoError, match="Fecha"):
            leer_factura(xml.encode("utf-8"))

    def test_subtotal_no_numerico_falla(self):
        xml = XML_FACTURA_BASE.replace("<totalSinImpuestos>100.00</totalSinImpuestos>", "<totalSinImpuestos>cien</totalSinImpuestos>")
        with pytest.raises(XMLInvalidoError, match="numericos"):
            leer_factura(xml.encode("utf-8"))

    def test_total_no_numerico_falla(self):
        xml = XML_FACTURA_BASE.replace("<importeTotal>115.00</importeTotal>", "<importeTotal>N/A</importeTotal>")
        with pytest.raises(XMLInvalidoError, match="numericos"):
            leer_factura(xml.encode("utf-8"))

#Formato XML de detalle opcional y de correo adicional opcional, que no son obligatorios en el XML del SRI pero si se encuentran deben ser parseados correctamente.
class TestDetalleOpcional:
    XML_CON_DETALLE = XML_FACTURA_BASE.replace(
        "</infoFactura>",
        """</infoFactura>
  <detalles>
    <detalle>
      <descripcion>Item normal</descripcion>
      <cantidad>2</cantidad>
      <precioUnitario>50.00</precioUnitario>
      <precioTotalSinImpuesto>100.00</precioTotalSinImpuesto>
    </detalle>
    <detalle>
      <cantidad>1</cantidad>
      <precioTotalSinImpuesto>10.00</precioTotalSinImpuesto>
    </detalle>
    <detalle>
      <descripcion>Cantidad y precio no numericos</descripcion>
      <cantidad>varios</cantidad>
      <precioUnitario>n/a</precioUnitario>
      <precioTotalSinImpuesto>5.00</precioTotalSinImpuesto>
    </detalle>
  </detalles>""",
    )

    def test_item_sin_descripcion_se_omite_sin_romper(self):
        datos = leer_factura(self.XML_CON_DETALLE.encode("utf-8"))
        # el segundo <detalle> del fixture no tiene <descripcion> y debe omitirse:
        # quedan 2 de los 3 nodos declarados.
        assert len(datos.detalle) == 2

    def test_item_con_descripcion_conserva_montos(self):
        datos = leer_factura(self.XML_CON_DETALLE.encode("utf-8"))
        primero = datos.detalle[0]
        assert primero.descripcion == "Item normal"
        assert primero.cantidad == 2
        assert primero.monto == 100.0

    def test_cantidad_y_precio_no_numericos_quedan_en_none_sin_romper(self):
        datos = leer_factura(self.XML_CON_DETALLE.encode("utf-8"))
        item_raro = next(d for d in datos.detalle if d.descripcion == "Cantidad y precio no numericos")
        assert item_raro.cantidad is None
        assert item_raro.precio_unitario is None
        assert item_raro.monto == 5.0  # este campo si era numerico 


class TestCorreoAdicional:
    def _xml_con_campo_adicional(self, nombre: str, valor: str) -> str:
        return XML_FACTURA_BASE.replace(
            "</factura>",
            f"""<infoAdicional><campoAdicional nombre="{nombre}">{valor}</campoAdicional></infoAdicional></factura>""",
        )

    @pytest.mark.parametrize("nombre", ["email", "Email", "EMAIL", "correo", "Correo Electrónico", "correo electronico", "e-mail"])
    def test_variantes_de_nombre_de_campo_se_detectan(self, nombre):
        xml = self._xml_con_campo_adicional(nombre, "proveedor@ejemplo.com")
        datos = leer_factura(xml.encode("utf-8"))
        assert datos.correo_proveedor == "proveedor@ejemplo.com"

    def test_campo_adicional_no_relacionado_se_ignora(self):
        xml = self._xml_con_campo_adicional("Dirección", "Av. Siempre Viva 123")
        datos = leer_factura(xml.encode("utf-8"))
        assert datos.correo_proveedor is None
