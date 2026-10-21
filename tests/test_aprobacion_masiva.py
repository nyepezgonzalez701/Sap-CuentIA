"""
Pruebas de la aprobacion masiva (app/core/aprobaciones.py): reglas de seguridad
(evaluar_elegibilidad_lote), filtros de la mesa de aprobacion (filtrar_facturas_pendientes)
y la ejecucion del lote (aprobar_lote). Usan la BD SQLite en memoria de conftest.py.
"""
from datetime import date
from decimal import Decimal

from app.core.aprobaciones import (
    FiltrosFacturas,
    aprobar_lote,
    evaluar_elegibilidad_lote,
    filtrar_facturas_pendientes,
    obtener_facturas_pendientes,
)
from app.core.models import CuentaContable, Factura, FacturaDetalleLinea, HistorialAccion, Proveedor
from app.core.validaciones import EstadoProveedor, validar_proveedor

RUC_REGISTRADO = "1790012345001"
RUC_NO_REGISTRADO = "1790099999001"


def _registrar_proveedor(db, ruc=RUC_REGISTRADO, razon_social="Proveedor de prueba S.A."):
    proveedor = Proveedor(ruc=ruc, razon_social=razon_social, estado="activo", saldo=0)
    db.add(proveedor)
    db.commit()
    return proveedor

# crear una cuenta para pruebas, con codigo y nombre por defecto. Se puede pasar un codigo distinto para crear varias cuentas.
def _crear_cuenta(db, codigo="510101", nombre="Gastos varios"):
    cuenta = CuentaContable(codigo=codigo, nombre=nombre, tipo_cuenta="D", activa=True)
    db.add(cuenta)
    db.commit()
    return cuenta


_contador = iter(range(1, 100_000))

#para crear facturas de prueba, con ruc, numero, total, fecha, cuenta_sugerida y cuenta_final opcionales. Se puede usar para crear facturas pendientes, aprobadas o rechazadas.
def _crear_factura(
    db, ruc=RUC_REGISTRADO, numero=None, total=115, fecha=date(2026, 1, 15),
    cuenta_sugerida=None, cuenta_final=None, confianza=None, estado="pendiente",
):
    numero = numero or f"001-001-{next(_contador):09d}"
    factura = Factura(
        ruc_proveedor=ruc,
        razon_social_emisor="Proveedor de prueba S.A.",
        numero_factura=numero,
        fecha_emision=fecha,
        subtotal=total, impuestos=0, total=total,
        estado=estado,
        cuenta_sugerida=cuenta_sugerida,
        cuenta_sugerida_confianza=confianza,
        cuenta_final=cuenta_final,
    )
    db.add(factura)
    db.commit()
    db.refresh(factura)
    return factura

#
class TestElegibilidadLote:
    def test_factura_completa_proveedor_registrado_confianza_alta_es_elegible(self, db_session):
        _registrar_proveedor(db_session)
        factura = _crear_factura(db_session, cuenta_sugerida="510101", confianza="alta")

        elegibilidad = evaluar_elegibilidad_lote(db_session, factura, EstadoProveedor.REGISTRADO, [])

        assert elegibilidad.elegible is True
        assert elegibilidad.motivos_bloqueo == []

    def test_bloquea_si_falta_cuenta_contable(self, db_session):
        _registrar_proveedor(db_session)
        factura = _crear_factura(db_session, cuenta_sugerida=None, confianza=None)

        elegibilidad = evaluar_elegibilidad_lote(db_session, factura, EstadoProveedor.REGISTRADO, [])

        assert elegibilidad.elegible is False
        assert any("cuenta contable" in m.lower() for m in elegibilidad.motivos_bloqueo)

    def test_bloquea_si_proveedor_no_esta_validado(self, db_session):
        factura = _crear_factura(db_session, ruc=RUC_NO_REGISTRADO, cuenta_sugerida="510101", confianza="alta")

        elegibilidad = evaluar_elegibilidad_lote(db_session, factura, EstadoProveedor.NO_REGISTRADO, [])

        assert elegibilidad.elegible is False
        assert any("no esta registrado" in m.lower() for m in elegibilidad.motivos_bloqueo)

    def test_bloquea_si_hay_advertencia_por_rechazo_previo(self, db_session):
        _registrar_proveedor(db_session)
        factura_rechazada = _crear_factura(db_session, estado="rechazada")
        db_session.add(HistorialAccion(factura_id=factura_rechazada.id, accion="rechazada", usuario="Nicole", motivo="x"))
        db_session.commit()

        factura = _crear_factura(db_session, cuenta_sugerida="510101", confianza="alta")
        elegibilidad = evaluar_elegibilidad_lote(db_session, factura, EstadoProveedor.REGISTRADO, [])

        assert elegibilidad.elegible is False
        assert any("rechazo previo" in m.lower() for m in elegibilidad.motivos_bloqueo)

    def test_bloquea_si_confianza_ia_es_baja(self, db_session):
        _registrar_proveedor(db_session)
        factura = _crear_factura(db_session, cuenta_sugerida="510101", confianza="baja")

        elegibilidad = evaluar_elegibilidad_lote(db_session, factura, EstadoProveedor.REGISTRADO, [])

        assert elegibilidad.elegible is False
        assert any("confianza baja" in m.lower() for m in elegibilidad.motivos_bloqueo)

    def test_confianza_media_no_bloquea(self, db_session):
        _registrar_proveedor(db_session)
        factura = _crear_factura(db_session, cuenta_sugerida="510101", confianza="media")

        elegibilidad = evaluar_elegibilidad_lote(db_session, factura, EstadoProveedor.REGISTRADO, [])

        assert elegibilidad.elegible is True

    def test_multilinea_bloquea_si_una_sola_linea_no_cumple(self, db_session):
        _registrar_proveedor(db_session)
        factura = _crear_factura(db_session, cuenta_sugerida=None)
        linea_ok = FacturaDetalleLinea(
            factura_id=factura.id, orden=1, descripcion="Item 1", monto=60,
            codigo_cuenta_sugerido="510101", confianza="alta",
        )
        linea_mala = FacturaDetalleLinea(
            factura_id=factura.id, orden=2, descripcion="Item 2", monto=55,
            codigo_cuenta_sugerido="510101", confianza="baja",
        )
        db_session.add_all([linea_ok, linea_mala])
        db_session.commit()

        elegibilidad = evaluar_elegibilidad_lote(db_session, factura, EstadoProveedor.REGISTRADO, [linea_ok, linea_mala])

        assert elegibilidad.elegible is False
        assert any("Item 2" in m for m in elegibilidad.motivos_bloqueo)


class TestFiltrarFacturasPendientes:
    def test_filtro_vacio_devuelve_todas(self, db_session):
        _registrar_proveedor(db_session)
        f1 = _crear_factura(db_session)
        f2 = _crear_factura(db_session)

        resultado = filtrar_facturas_pendientes(db_session, [f1, f2], FiltrosFacturas())

        assert resultado == [f1, f2]

    def test_filtro_por_proveedor_busca_en_ruc_y_razon_social(self, db_session):
        _registrar_proveedor(db_session, razon_social="Ferreteria El Tornillo")
        f1 = _crear_factura(db_session)
        f2 = _crear_factura(db_session, ruc=RUC_NO_REGISTRADO)

        resultado = filtrar_facturas_pendientes(
            db_session, [f1, f2], FiltrosFacturas(proveedor="tornillo")
        )

        assert resultado == [f1]

    def test_filtro_por_rango_de_monto(self, db_session):
        _registrar_proveedor(db_session)
        barata = _crear_factura(db_session, total=50)
        cara = _crear_factura(db_session, total=500)

        resultado = filtrar_facturas_pendientes(
            db_session, [barata, cara], FiltrosFacturas(monto_min=Decimal("100"))
        )

        assert resultado == [cara]

    def test_filtro_por_rango_de_fecha(self, db_session):
        _registrar_proveedor(db_session)
        vieja = _crear_factura(db_session, fecha=date(2025, 1, 1))
        nueva = _crear_factura(db_session, fecha=date(2026, 6, 1))

        resultado = filtrar_facturas_pendientes(
            db_session, [vieja, nueva], FiltrosFacturas(fecha_desde=date(2026, 1, 1))
        )

        assert resultado == [nueva]

    def test_filtro_por_cuenta(self, db_session):
        _registrar_proveedor(db_session)
        con_cuenta = _crear_factura(db_session, cuenta_sugerida="510101")
        otra_cuenta = _crear_factura(db_session, cuenta_sugerida="520202")

        resultado = filtrar_facturas_pendientes(
            db_session, [con_cuenta, otra_cuenta], FiltrosFacturas(cuenta="510101")
        )
# Consulta por cuenta contable solo devuelve las facturas que tienen esa cuenta sugerida, no las que tinen otra cuenta.
        assert resultado == [con_cuenta]


class TestAprobarLote:
    def test_aprueba_solo_las_elegibles_y_omite_el_resto(self, db_session):
        _registrar_proveedor(db_session)
        _crear_cuenta(db_session)
        elegible = _crear_factura(db_session, cuenta_sugerida="510101", confianza="alta")
        sin_cuenta = _crear_factura(db_session, cuenta_sugerida=None)

        resultado = aprobar_lote(db_session, [elegible.id, sin_cuenta.id], usuario="Nicole")

        assert resultado["aprobadas"] == [elegible.id]
        assert len(resultado["omitidas"]) == 1
        assert resultado["omitidas"][0][0] == sin_cuenta.id

        db_session.refresh(elegible)
        db_session.refresh(sin_cuenta)
        assert elegible.estado == "aprobada"
        assert elegible.cuenta_final == "510101"
        assert sin_cuenta.estado == "pendiente"

    def test_omite_factura_que_ya_no_esta_pendiente(self, db_session):
        _registrar_proveedor(db_session)
        _crear_cuenta(db_session)
        ya_aprobada = _crear_factura(db_session, cuenta_sugerida="510101", confianza="alta", estado="aprobada")

        resultado = aprobar_lote(db_session, [ya_aprobada.id], usuario="Nicole")

        assert resultado["aprobadas"] == []
        assert "aprobada" in resultado["omitidas"][0][1].lower()

    def test_omite_factura_inexistente(self, db_session):
        resultado = aprobar_lote(db_session, [999999], usuario="Nicole")

        assert resultado["aprobadas"] == []
        assert resultado["omitidas"][0][1] == "La factura ya no existe."
        # 

    def test_aprobacion_masiva_multilinea_usa_cuenta_sugerida_de_cada_linea(self, db_session):
        _registrar_proveedor(db_session)
        _crear_cuenta(db_session, codigo="510101")
        _crear_cuenta(db_session, codigo="510102", nombre="Otra cuenta")
        factura = _crear_factura(db_session, cuenta_sugerida=None)
        


        # por ejemplo, la IA sugiere una cuenta distinta para cada linea de la factura. La aprobacion masiva debe usar la cuenta sugerida de cada linea, y concatenarlas en el campo cuenta_final de la factura.
        linea_1 = FacturaDetalleLinea(
            factura_id=factura.id, orden=1, descripcion="Item 1", monto=60,
            codigo_cuenta_sugerido="510101", confianza="alta",
        )
        linea_2 = FacturaDetalleLinea(
            factura_id=factura.id, orden=2, descripcion="Item 2", monto=55,
            codigo_cuenta_sugerido="510102", confianza="alta",
        )
        db_session.add_all([linea_1, linea_2])
        db_session.commit()

        resultado = aprobar_lote(db_session, [factura.id], usuario="Nicole")

        assert resultado["aprobadas"] == [factura.id]
        db_session.refresh(factura)
        assert factura.estado == "aprobada"
        assert factura.cuenta_final == "510101, 510102"

    def test_no_confia_en_ids_repetidos_o_con_estado_cambiado_a_mitad_de_lote(self, db_session):
        # Simula que el mismo id llega dos veces en el formulario (doble clic, o el
        # navegador reenviando el POST): la segunda vez ya no esta pendiente y se omite,
        # nunca se aprueba dos veces ni se rompe la funcion.
        _registrar_proveedor(db_session)
        _crear_cuenta(db_session)
        factura = _crear_factura(db_session, cuenta_sugerida="510101", confianza="alta")

        resultado = aprobar_lote(db_session, [factura.id, factura.id], usuario="Nicole")

        assert resultado["aprobadas"] == [factura.id]
        assert len(resultado["omitidas"]) == 1
