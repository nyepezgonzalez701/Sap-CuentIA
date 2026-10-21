"""
Pruebas del guardado preliminar ("Guardar", estado "guardada") en
app/core/aprobaciones.py -- la tercera opcion junto a Aprobar/Rechazar para
facturas recurrentes que la contadora revisa varias veces antes de decidir.
Usan una base SQLite en memoria (ver tests/conftest.py), no la Postgres real.
"""
from datetime import date

from app.core.aprobaciones import (
    obtener_facturas_pendientes,
    aprobar_factura,
    aprobar_factura_lineas,
    rechazar_factura,
    guardar_factura_preliminar,
    es_candidata_auto_aprobacion,
    AccionInvalidaError,
)
from app.core.envios_sap import obtener_facturas_aprobadas_pendientes_envio
from app.core.models import Factura, CuentaContable, FacturaDetalleLinea, HistorialAccion
from app.core.validaciones import EstadoProveedor
from app.main import _renderizar_tarjeta_legacy

RUC_PRUEBA = "1790012345001"


def _crear_cuenta(db, codigo="510101", nombre="Gastos varios"):
    cuenta = CuentaContable(codigo=codigo, nombre=nombre, tipo_cuenta="D", activa=True)
    db.add(cuenta)
    db.commit()
    db.refresh(cuenta)
    return cuenta


def _crear_historial(db, ruc_proveedor, accion, cuenta_utilizada=None, numero="varias"):
    """
    Inserta una fila de historial ligada a una factura nueva con ese RUC -- suficiente
    para las funciones de conteo/deteccion de es_candidata_auto_aprobacion, que solo
    miran Factura.ruc_proveedor + HistorialAccion.accion/cuenta_utilizada via join.
    """
    numero_unico = f"{numero}-{next(_contador_facturas)}"
    factura = Factura(
        ruc_proveedor=ruc_proveedor,
        numero_factura=numero_unico,
        fecha_emision=date(2026, 1, 1),
        subtotal=100,
        impuestos=15,
        total=115,
        estado="aprobada" if accion == "aprobada" else "rechazada",
    )
    db.add(factura)
    db.commit()
    db.refresh(factura)
    historial = HistorialAccion(
        factura_id=factura.id, accion=accion, usuario="Nicole", cuenta_utilizada=cuenta_utilizada
    )
    db.add(historial)
    db.commit()
    return factura


_contador_facturas = iter(range(1, 100_000))


def _crear_factura(db, numero="001-001-000000001", estado="pendiente"):
    factura = Factura(
        ruc_proveedor="1790012345001",
        razon_social_emisor="Proveedor de prueba S.A.",
        numero_factura=numero,
        fecha_emision=date(2026, 1, 15),
        subtotal=100,
        impuestos=15,
        total=115,
        estado=estado,
    )
    db.add(factura)
    db.commit()
    db.refresh(factura)
    return factura


class TestGuardarFacturaPreliminar:
    def test_guarda_estado_cuenta_y_usuario_sin_tocar_sap(self, db_session):
        cuenta = _crear_cuenta(db_session)
        factura = _crear_factura(db_session)

        resultado = guardar_factura_preliminar(db_session, factura.id, "Nicole", cuenta.codigo)

        assert resultado.estado == "guardada"
        assert resultado.cuenta_final == cuenta.codigo
        assert resultado.usuario_guardado == "Nicole"
        # nunca debe enviarse a SAP ni cerrar el ciclo
        assert resultado.referencia_sap is None
        assert obtener_facturas_aprobadas_pendientes_envio(db_session) == []

        historial = db_session.query(HistorialAccion).filter_by(factura_id=factura.id).all()
        assert len(historial) == 1
        assert historial[0].accion == "guardada"

    def test_factura_sin_usuario_falla(self, db_session):
        cuenta = _crear_cuenta(db_session)
        factura = _crear_factura(db_session)
        try:
            guardar_factura_preliminar(db_session, factura.id, "", cuenta.codigo)
            assert False, "debia lanzar AccionInvalidaError"
        except AccionInvalidaError:
            pass

    def test_multilinea_admite_guardado_parcial(self, db_session):
        cuenta = _crear_cuenta(db_session, codigo="520101", nombre="Servicios")
        factura = _crear_factura(db_session)
        linea_1 = FacturaDetalleLinea(factura_id=factura.id, orden=1, descripcion="Item 1", monto=60)
        linea_2 = FacturaDetalleLinea(factura_id=factura.id, orden=2, descripcion="Item 2", monto=40)
        db_session.add_all([linea_1, linea_2])
        db_session.commit()
        db_session.refresh(linea_1)
        db_session.refresh(linea_2)

        # solo se completa la primera linea -- el guardado preliminar no debe exigir
        # que las dos esten clasificadas (a diferencia de aprobar_factura_lineas) las facturas guardados pueden decidirse desúes, asi que no se
        resultado = guardar_factura_preliminar(
            db_session, factura.id, "Nicole", codigos_por_linea={linea_1.id: cuenta.codigo}
        )

        assert resultado.estado == "guardada"
        db_session.refresh(linea_1)
        db_session.refresh(linea_2)
        assert linea_1.codigo_cuenta_final == cuenta.codigo
        assert linea_2.codigo_cuenta_final is None


class TestFacturaGuardadaSigueEnPendientes:
    def test_aparece_en_obtener_facturas_pendientes(self, db_session):
        cuenta = _crear_cuenta(db_session)
        factura = _crear_factura(db_session)
        guardar_factura_preliminar(db_session, factura.id, "Nicole", cuenta.codigo)

        pendientes = obtener_facturas_pendientes(db_session)

        assert [f.id for f in pendientes] == [factura.id]
        assert pendientes[0].estado == "guardada"


class TestReabrirFacturaGuardadaPrecargaFormulario:
    # El usuario ya no se pide por formulario ("Tu nombre") -- sale de la sesion
    # (request.state.usuario), asi que solo la cuenta se precarga aqui.
    def test_formulario_precarga_cuenta(self, db_session):
        cuenta = _crear_cuenta(db_session)
        _crear_cuenta(db_session, codigo="510102", nombre="Otra cuenta")  # distractor, no debe quedar seleccionada
        factura = _crear_factura(db_session)
        guardar_factura_preliminar(db_session, factura.id, "Nicole", cuenta.codigo)
        db_session.refresh(factura)

        cuentas_activas = db_session.query(CuentaContable).order_by(CuentaContable.codigo).all()
        html_tarjeta = _renderizar_tarjeta_legacy(
            factura, cuentas_activas, estado_proveedor=None, razon_social="Proveedor de prueba S.A.", sugerencia_actual=None
        )

        assert f'value="{cuenta.codigo}" selected' in html_tarjeta
        assert 'value="510102" selected' not in html_tarjeta

    def test_formulario_no_pide_nombre_por_texto_libre(self, db_session):
        cuenta = _crear_cuenta(db_session)
        factura = _crear_factura(db_session)  # nunca se llamo guardar_factura_preliminar

        html_tarjeta = _renderizar_tarjeta_legacy(
            factura, [cuenta], estado_proveedor=None, razon_social="Proveedor de prueba S.A.", sugerencia_actual=None
        )

        assert 'placeholder="Tu nombre"' not in html_tarjeta


class TestFacturaGuardadaPuedeDecidirseDespues:
    def test_guardada_puede_aprobarse(self, db_session):
        cuenta = _crear_cuenta(db_session)
        factura = _crear_factura(db_session)
        guardar_factura_preliminar(db_session, factura.id, "Nicole", cuenta.codigo)

        resultado = aprobar_factura(db_session, factura.id, "Nicole", cuenta.codigo)

        assert resultado.estado == "aprobada"

        # En este ejemplo guarda de manera prelimninar y leugo lo aprueba 

    def test_guardada_puede_rechazarse(self, db_session):
        cuenta = _crear_cuenta(db_session)
        factura = _crear_factura(db_session)
        guardar_factura_preliminar(db_session, factura.id, "Nicole", cuenta.codigo)

        resultado = rechazar_factura(db_session, factura.id, "Nicole", "no corresponde")

        assert resultado.estado == "rechazada"

    def test_guardada_multilinea_puede_aprobarse(self, db_session):
        cuenta = _crear_cuenta(db_session, codigo="520101", nombre="Servicios")
        factura = _crear_factura(db_session)
        linea = FacturaDetalleLinea(factura_id=factura.id, orden=1, descripcion="Item 1", monto=115)
        db_session.add(linea)
        db_session.commit()
        db_session.refresh(linea)

        guardar_factura_preliminar(db_session, factura.id, "Nicole", codigos_por_linea={linea.id: cuenta.codigo})
        resultado = aprobar_factura_lineas(db_session, factura.id, "Nicole", {linea.id: cuenta.codigo})

        assert resultado.estado == "aprobada"


class TestEsCandidataAutoAprobacion:
    """
    Fase 1 de automatizacion: solo deteccion, no aprueba nada. Candidata = proveedor
    registrado + sin rechazos previos de ese RUC + cada linea con confianza "alta" o
    3+ aprobaciones previas del mismo RUC+cuenta.
    """

    def test_proveedor_no_registrado_nunca_es_candidata(self, db_session):
        candidata = es_candidata_auto_aprobacion(
            db_session, RUC_PRUEBA, EstadoProveedor.NO_REGISTRADO, [("510101", "alta")]
        )
        assert candidata is False

#ejemplo de previo del Ruc cuando no es canditado de auto aprobacion
    def test_con_rechazo_previo_del_ruc_nunca_es_candidata(self, db_session):
        _crear_historial(db_session, RUC_PRUEBA, "rechazada")
#ejemplo de reistro nuevo RUC
        candidata = es_candidata_auto_aprobacion(
            db_session, RUC_PRUEBA, EstadoProveedor.REGISTRADO, [("510101", "alta")]
        )
        assert candidata is False

    def test_confianza_alta_es_candidata_sin_necesitar_historial(self, db_session):
        candidata = es_candidata_auto_aprobacion(
            db_session, RUC_PRUEBA, EstadoProveedor.REGISTRADO, [("510101", "alta")]
        )
        assert candidata is True

    def test_recurrente_con_3_o_mas_aprobaciones_previas_es_candidata(self, db_session):
        for _ in range(3):
            _crear_historial(db_session, RUC_PRUEBA, "aprobada", cuenta_utilizada="510101")

        candidata = es_candidata_auto_aprobacion(
            db_session, RUC_PRUEBA, EstadoProveedor.REGISTRADO, [("510101", "baja")]
        )
        assert candidata is True

# aprobaciones previas de de si la cuenta teiene confiza baja o no 
    def test_con_solo_2_aprobaciones_previas_y_confianza_baja_no_es_candidata(self, db_session):
        for _ in range(2):
            _crear_historial(db_session, RUC_PRUEBA, "aprobada", cuenta_utilizada="510101")

        candidata = es_candidata_auto_aprobacion(
            db_session, RUC_PRUEBA, EstadoProveedor.REGISTRADO, [("510101", "baja")]
        )
        assert candidata is False

    def test_multilinea_una_sola_linea_sin_calificar_descarta_toda_la_factura(self, db_session):
        for _ in range(3):
            _crear_historial(db_session, RUC_PRUEBA, "aprobada", cuenta_utilizada="510101")

        # la primera linea califica por confianza alta, la segunda no tiene ni
        # confianza alta ni recurrencia suficiente -- la factura completa no califica.
        candidata = es_candidata_auto_aprobacion(
            db_session,
            RUC_PRUEBA,
            EstadoProveedor.REGISTRADO,
            [("510101", "alta"), ("520202", "baja")],
        )
        assert candidata is False

    def test_linea_sin_cuenta_descarta_la_factura(self, db_session):
        candidata = es_candidata_auto_aprobacion(
            db_session, RUC_PRUEBA, EstadoProveedor.REGISTRADO, [(None, "alta")]
        )
        assert candidata is False
