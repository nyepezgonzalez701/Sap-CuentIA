"""
Pruebas de app/core/validaciones.py contra base de datos real (detectar_duplicado
y validar_proveedor) -- test_validaciones.py solo cubre las funciones puras de
formato de RUC, estas necesitan sesion de BD (fixture db_session en conftest.py).

Incluye un test que documenta un limite conocido de detectar_duplicado: compara
solo por numero_factura (estab-ptoEmi-secuencial), sin RUC ni clave de acceso. Como
numero_factura es ademas UNIQUE a nivel de columna (ver Factura.numero_factura en
models.py), dos proveedores DISTINTOS que por coincidencia usen la misma combinacion
establecimiento/punto de emision/secuencial (ej. ambos arrancando su numeracion desde
001-001-000000001) chocan entre si aunque sean facturas legitimas de emisores
distintos. detectar_duplicado por si solo no distingue ese caso de un reenvio real
del mismo proveedor -- lo unico que evita el choque hoy es que sea poco probable en
la practica, no una regla explicita del sistema.
"""
from datetime import date
from decimal import Decimal

from app.core.models import Factura, Proveedor
from app.core.validaciones import detectar_duplicado, validar_proveedor, EstadoProveedor


def _crear_factura(db, numero_factura, ruc_proveedor="1790012345001"):
    factura = Factura(
        ruc_proveedor=ruc_proveedor,
        razon_social_emisor="PROVEEDOR DE PRUEBA S.A.",
        numero_factura=numero_factura,
        fecha_emision=date(2026, 1, 15),
        subtotal=Decimal("100.00"),
        impuestos=Decimal("15.00"),
        total=Decimal("115.00"),
        estado="pendiente",
    )
    db.add(factura)
    db.commit()
    return factura


class TestDetectarDuplicado:
    def test_numero_factura_nuevo_no_es_duplicado(self, db_session):
        assert detectar_duplicado(db_session, "001-001-000000001") is False

    def test_mismo_numero_factura_es_duplicado(self, db_session):
        _crear_factura(db_session, "001-001-000000001")
        assert detectar_duplicado(db_session, "001-001-000000001") is True

    def test_numero_factura_distinto_no_es_duplicado(self, db_session):
        _crear_factura(db_session, "001-001-000000001")
        assert detectar_duplicado(db_session, "001-001-000000002") is False

    def test_limite_conocido_mismo_numero_de_proveedores_distintos_se_marca_igual_como_duplicado(self, db_session):
        # Documenta el limite descrito arriba: la deteccion no distingue RUC, asi
        # que una factura legitima del "Proveedor B" queda bloqueada por error
        # solo porque coincide su numero_factura con una del "Proveedor A".
        _crear_factura(db_session, "001-001-000000001", ruc_proveedor="1790012345001")
        resultado = detectar_duplicado(db_session, "001-001-000000001")
        assert resultado is True  # bloquea aunque el RUC entrante sea otro (ej. "1790099999001")


class TestValidarProveedor:
    def test_ruc_no_registrado(self, db_session):
        estado, razon_social = validar_proveedor(db_session, "1790012345001")
        assert estado == EstadoProveedor.NO_REGISTRADO
        assert razon_social is None

    def test_ruc_registrado_devuelve_razon_social(self, db_session):
        db_session.add(Proveedor(ruc="1790012345001", razon_social="PROVEEDOR REGISTRADO S.A."))
        db_session.commit()

        estado, razon_social = validar_proveedor(db_session, "1790012345001")
        assert estado == EstadoProveedor.REGISTRADO
        assert razon_social == "PROVEEDOR REGISTRADO S.A."

    def test_proveedor_inactivo_igual_cuenta_como_registrado(self, db_session):
        # validar_proveedor no distingue activo/inactivo -- ver docstring en
        # app/core/validaciones.py: "estar en el maestro es suficiente" para la lista de proveedores se queda guardada en la tabla Proveedor, aunque su estado sea "inactivo" (ej. proveedor que ya no se usa pero que sigue en el maestro para referencia historica de facturas antiguas).
        db_session.add(Proveedor(ruc="1790012345001", razon_social="PROVEEDOR INACTIVO S.A.", estado="inactivo"))
        db_session.commit()
# proveerdor activo
        estado, _ = validar_proveedor(db_session, "1790012345001")
        assert estado == EstadoProveedor.REGISTRADO
