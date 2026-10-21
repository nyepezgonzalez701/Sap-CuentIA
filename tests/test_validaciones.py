"""
Pruebas de formato_ruc_valido y ruc_estructuralmente_valido (app/core/validaciones.py)
-- las validaciones que deciden si un RUC desconocido se registra automaticamente
(formato_ruc_valido, mas estricta) o si se puede registrar manualmente en la
aprobacion (ruc_estructuralmente_valido, solo exige 13 digitos). No requieren base
de datos: son funciones puras sobre el string del RUC.
"""
from app.core.validaciones import formato_ruc_valido, ruc_estructuralmente_valido


class TestFormatoRucValido:
    def test_ruc_persona_natural_valido(self):
        assert formato_ruc_valido("1710034065001") is True

    def test_ruc_sociedad_privada_valida(self):
        assert formato_ruc_valido("1790012345001") is True

    def test_ruc_sociedad_publica_valida(self):
        assert formato_ruc_valido("1760001550001") is True

    def test_ruc_provincia_30_extranjero_valido(self):
        assert formato_ruc_valido("3090012345001") is True

    def test_ruc_con_menos_de_13_digitos_se_rechaza(self):
        assert formato_ruc_valido("179001234500") is False

    def test_ruc_con_mas_de_13_digitos_se_rechaza(self):
        assert formato_ruc_valido("17900123450011") is False

    def test_ruc_con_letras_se_rechaza(self):
        assert formato_ruc_valido("17900A2345001") is False

    def test_ruc_con_provincia_fuera_de_rango_se_rechaza(self):
        assert formato_ruc_valido("2590012345001") is False

    def test_ruc_con_provincia_00_se_rechaza(self):
        assert formato_ruc_valido("0090012345001") is False

    def test_ruc_con_tercer_digito_7_se_rechaza(self):
        assert formato_ruc_valido("1770012345001") is False

    def test_ruc_con_tercer_digito_8_se_rechaza(self):
        assert formato_ruc_valido("1780012345001") is False

    def test_ruc_vacio_se_rechaza(self):
        assert formato_ruc_valido("") is False

    def test_ruc_none_se_rechaza(self):
        assert formato_ruc_valido(None) is False


class TestRucEstructuralmenteValido:
    def test_ruc_con_13_digitos_es_valido_aunque_la_provincia_no_exista(self):
        # a diferencia de formato_ruc_valido, esta version no mira provincia/tipo --
        # es la que se usa cuando un humano ya reviso la factura en la aprobacion.
        assert ruc_estructuralmente_valido("2590012345001") is True

    def test_ruc_con_menos_de_13_digitos_se_rechaza(self):
        assert ruc_estructuralmente_valido("179001234500") is False

    def test_ruc_con_letras_se_rechaza(self):
        assert ruc_estructuralmente_valido("17900A2345001") is False

    def test_ruc_vacio_se_rechaza(self):
        assert ruc_estructuralmente_valido("") is False

    def test_ruc_none_se_rechaza(self):
        assert ruc_estructuralmente_valido(None) is False
