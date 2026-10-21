"""
Pruebas del login por sesion (app/core/auth.py) y del vigilante de carpeta
integrado al servidor (app/core/vigilante_carpeta.py + las rutas de
app/main.py). Usan una base SQLite en un archivo temporal (no :memory:,
porque el hilo de fondo abre su propia conexion en otro hilo y necesita ver
los mismos datos que la request que lo arranco) y una carpeta temporal en vez
de la carpeta real de OneDrive.
"""
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main as main_module
import app.core.vigilante_carpeta as vigilante_carpeta
from app.core.auth import hash_contrasena
from app.core.models import Usuario, HistorialVigilancia, Factura, FacturaDetalleLinea
from app.db.config import Base

USUARIO_PRUEBA = "contadora_prueba"
CONTRASENA_PRUEBA = "clave-correcta-123"


@pytest.fixture()
def cliente(tmp_path, monkeypatch):
    """
    Arma un servidor de pruebas con su propia base SQLite (archivo temporal,
    compartido entre el hilo principal y el hilo de vigilancia) y una carpeta
    vigilada temporal en vez de CARPETA_POR_DEFECTO.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    monkeypatch.setattr(main_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(vigilante_carpeta, "SessionLocal", TestingSessionLocal)

    carpeta = tmp_path / "entrada"
    carpeta.mkdir()
    monkeypatch.setattr(vigilante_carpeta, "CARPETA_POR_DEFECTO", str(carpeta))
    monkeypatch.setattr(vigilante_carpeta, "INTERVALO_REVISION_SEGUNDOS", 1)

    db = TestingSessionLocal()
    try:
        db.add(
            Usuario(
                nombre_usuario=USUARIO_PRUEBA,
                hash_contrasena=hash_contrasena(CONTRASENA_PRUEBA),
                rol="contadora",
                activo=True,
            )
        )
        db.commit()
    finally:
        db.close()

    yield TestClient(main_module.app), TestingSessionLocal, carpeta

    vigilante_carpeta.detener_monitoreo()
    engine.dispose()


def _iniciar_sesion(cliente: TestClient) -> None:
    respuesta = cliente.post(
        "/login",
        data={"usuario": USUARIO_PRUEBA, "contrasena": CONTRASENA_PRUEBA},
        follow_redirects=False,
    )
    assert respuesta.status_code == 303
    assert "sesion" in respuesta.cookies


def test_login_correcto_crea_sesion(cliente):
    client, _, _ = cliente
    respuesta = client.post(
        "/login",
        data={"usuario": USUARIO_PRUEBA, "contrasena": CONTRASENA_PRUEBA},
        follow_redirects=False,
    )
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/panel-vigilancia"
    assert "sesion" in respuesta.cookies

    respuesta_panel = client.get("/panel-vigilancia")
    assert respuesta_panel.status_code == 200


def test_login_incorrecto_no_crea_sesion(cliente):
    client, _, _ = cliente
    respuesta = client.post(
        "/login",
        data={"usuario": USUARIO_PRUEBA, "contrasena": "clave-equivocada"},
        follow_redirects=False,
    )
    assert respuesta.status_code == 401
    assert "sesion" not in respuesta.cookies
    assert "Usuario o contrase" in respuesta.text


@pytest.mark.parametrize(
    "ruta",
    ["/facturas", "/aprobadas", "/historial", "/panel-vigilancia"],
)
def test_rutas_protegidas_redirigen_a_login_sin_sesion(cliente, ruta):
    client, _, _ = cliente
    respuesta = client.get(ruta, follow_redirects=False)
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/login"


def test_no_permite_iniciar_monitoreo_dos_veces(cliente):
    _, _, carpeta = cliente
    try:
        ok_primero, _ = vigilante_carpeta.iniciar_monitoreo(str(carpeta))
        ok_segundo, mensaje_segundo = vigilante_carpeta.iniciar_monitoreo(str(carpeta))

        assert ok_primero is True
        assert ok_segundo is False
        assert "ya esta activo" in mensaje_segundo.lower()
    finally:
        vigilante_carpeta.detener_monitoreo()


def test_iniciar_monitoreo_registra_historial_con_usuario(cliente):
    client, SessionLocal, _ = cliente
    _iniciar_sesion(client)

    respuesta = client.post("/admin/revisar-carpeta", follow_redirects=False)
    assert respuesta.status_code == 303

    db = SessionLocal()
    try:
        registro = (
            db.query(HistorialVigilancia)
            .filter(HistorialVigilancia.accion == "iniciar_monitoreo")
            .order_by(HistorialVigilancia.id.desc())
            .first()
        )
        assert registro is not None
        assert registro.usuario == USUARIO_PRUEBA
        assert registro.resultado == "ok"
    finally:
        db.close()


def test_revisar_carpeta_usa_la_ruta_escrita_en_el_panel(cliente, tmp_path):
    """La persona puede escribir una ruta distinta a CARPETA_POR_DEFECTO en el campo
    "carpeta" del panel -- esa es la que debe quedar guardada como carpeta_monitoreada."""
    client, SessionLocal, _ = cliente
    _iniciar_sesion(client)

    carpeta_elegida = tmp_path / "otra_carpeta"
    carpeta_elegida.mkdir()

    try:
        respuesta = client.post(
            "/admin/revisar-carpeta",
            data={"carpeta": str(carpeta_elegida)},
            follow_redirects=False,
        )
        assert respuesta.status_code == 303

        db = SessionLocal()
        try:
            estado = vigilante_carpeta.leer_estado(db)
            assert estado.carpeta_monitoreada == str(carpeta_elegida)
            assert estado.estado != "error"
        finally:
            db.close()
    finally:
        vigilante_carpeta.detener_monitoreo()


def test_revisar_carpeta_con_ruta_invalida_muestra_error_sin_caerse(cliente, tmp_path):
    """Si la ruta escrita no existe o no es un directorio, el panel debe mostrar un
    error claro en vez de que la request se caiga."""
    client, SessionLocal, _ = cliente
    _iniciar_sesion(client)

    ruta_inexistente = str(tmp_path / "no_existe")
    respuesta = client.post(
        "/admin/revisar-carpeta",
        data={"carpeta": ruta_inexistente},
        follow_redirects=False,
    )
    assert respuesta.status_code == 303

    db = SessionLocal()
    try:
        estado = vigilante_carpeta.leer_estado(db)
        assert estado.estado == "error"
        assert ruta_inexistente in (estado.mensaje_error or "")
    finally:
        db.close()

    panel = client.get("/panel-vigilancia")
    assert panel.status_code == 200
    assert "Error" in panel.text


def test_vigilante_ignora_archivos_que_no_son_xml(cliente):
    """Archivos .pdf/.jpg/etc deben ser invisibles para el vigilante: no se mueven a
    errores/, no generan mensaje, simplemente se ignoran (el filtro va antes de
    cualquier validacion de contenido)."""
    _, _, carpeta = cliente

    (carpeta / "no_es_factura.pdf").write_bytes(b"%PDF-1.4 contenido de prueba")
    (carpeta / "imagen.JPG").write_bytes(b"contenido binario")

    try:
        ok, _ = vigilante_carpeta.iniciar_monitoreo(str(carpeta))
        assert ok is True
        time.sleep(2)

        assert (carpeta / "no_es_factura.pdf").is_file()
        assert (carpeta / "imagen.JPG").is_file()
        assert not (carpeta / "errores").is_dir() or not any((carpeta / "errores").iterdir())
        assert vigilante_carpeta.contar_pendientes(str(carpeta)) == 0
    finally:
        vigilante_carpeta.detener_monitoreo()


def test_reintentar_mueve_archivo_de_errores_a_entrada(cliente):
    client, SessionLocal, carpeta = cliente
    _iniciar_sesion(client)

    carpeta_errores = carpeta / "errores"
    carpeta_errores.mkdir()
    (carpeta_errores / "factura_con_error.xml").write_text("<xml/>", encoding="utf-8")
    (carpeta_errores / "factura_con_error.xml.txt").write_text("motivo de prueba", encoding="utf-8")

    db = SessionLocal()
    try:
        estado = vigilante_carpeta.leer_estado(db)
        estado.carpeta_monitoreada = str(carpeta)
        db.commit()
    finally:
        db.close()

    respuesta = client.post(
        "/admin/reintentar-archivo",
        data={"nombre_archivo": "factura_con_error.xml"},
        follow_redirects=False,
    )
    assert respuesta.status_code == 303

    assert (carpeta / "factura_con_error.xml").is_file()
    assert not (carpeta_errores / "factura_con_error.xml").exists()
    assert not (carpeta_errores / "factura_con_error.xml.txt").exists()


def _crear_factura_pendiente(SessionLocal, numero="001-001-000000001") -> int:
    db = SessionLocal()
    try:
        factura = Factura(
            ruc_proveedor="1790012345001",
            razon_social_emisor="Proveedor de prueba S.A.",
            numero_factura=numero,
            fecha_emision=date(2026, 1, 15),
            subtotal=100, impuestos=15, total=115,
            estado="pendiente",
        )
        db.add(factura)
        db.commit()
        db.refresh(factura)
        return factura.id
    finally:
        db.close()


def test_motivo_de_rechazo_con_html_se_escapa_en_la_respuesta(cliente):
    """
    Regresion del XSS encontrado en la auditoria: el motivo de rechazo es texto libre
    del usuario y se mostraba sin escapar en la respuesta htmx -- un motivo con
    <script> se ejecutaba en el navegador de quien viera esa tarjeta.
    """
    client, SessionLocal, _ = cliente
    _iniciar_sesion(client)
    factura_id = _crear_factura_pendiente(SessionLocal)

    motivo_malicioso = '<script>alert("xss")</script>'
    respuesta = client.post(
        f"/facturas/{factura_id}/rechazar",
        data={"motivo": motivo_malicioso},
    )

    assert respuesta.status_code == 200
    assert "<script>" not in respuesta.text
    assert "&lt;script&gt;" in respuesta.text


def test_mensaje_de_error_de_aprobacion_invalida_se_escapa(cliente):
    """
    Misma clase de bug: aprobar_factura_lineas arma el mensaje de AccionInvalidaError
    interpolando linea.descripcion, que viene del XML de la factura -- un proveedor
    podria craftear una factura con una descripcion maliciosa. Nunca se debe interpolar
    crudo en la respuesta HTML.
    """
    client, SessionLocal, _ = cliente
    _iniciar_sesion(client)
    factura_id = _crear_factura_pendiente(SessionLocal)

    db = SessionLocal()
    try:
        db.add(FacturaDetalleLinea(
            factura_id=factura_id, orden=1,
            descripcion='<img src=x onerror=alert(1)>', monto=115,
        ))
        db.commit()
    finally:
        db.close()

    # Un campo "codigo_cuenta_linea_0" (id que no es el de la linea real) alcanza para
    # que la ruta tome el camino multilinea (aprobar_factura_lineas) sin cubrir la linea
    # real, que entonces lanza AccionInvalidaError con linea.descripcion tal cual. descripcion de  los caps de de vigilencia 
    respuesta = client.post(f"/facturas/{factura_id}/aprobar", data={"codigo_cuenta_linea_0": "x"})

    assert respuesta.status_code == 200
    assert "<img" not in respuesta.text
    assert "&lt;img" in respuesta.text
