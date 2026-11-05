"""
Pruebas del adaptador del Service Layer (app/adaptadores/sap/) y de las reglas de envio
(app/core/envios_sap.py). Corren SOLO contra tests/servicelayer_simulado.py en 127.0.0.1;
nunca contra un SAP real. Pasar aqui no prueba que un SAP real se comporte igual.
"""
import logging
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.adaptadores.sap.fabrica as fabrica
import app.adaptadores.sap.servicelayer as servicelayer
import app.core.envios_sap as envios_sap
import app.main as main_module
from app.adaptadores.sap.base import ClienteSAP, LineaFacturaSAP, ResultadoEnvioSAP
from app.adaptadores.sap.mapeo_servicelayer import CamposSAP, extraer_error, literal_odata
from app.adaptadores.sap.servicelayer import ServiceLayerSAP
from app.adaptadores.sap.simulador import SimuladorSAP
from app.core.aprobaciones import aprobar_factura_lineas, guardar_factura_preliminar, rechazar_factura
from app.core.auth import hash_contrasena
from app.core.envios_sap import EnvioInvalidoError, enviar_a_sap
from app.core.models import CuentaContable, Factura, FacturaDetalleLinea, HistorialAccion, Usuario
from app.db.config import Base
from tests import servicelayer_simulado as sim

CAMPOS = CamposSAP(ruc_proveedor=sim.CAMPO_RUC, cuenta_linea=sim.CAMPO_CUENTA, numero_factura=sim.CAMPO_NUMERO)
NUMERO = "001-001-000000123"
FECHA = date(2026, 10, 1)
LINEAS = [LineaFacturaSAP("Servicio", Decimal("100.00"), "510101")]


@pytest.fixture()
def servidor(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    estado = sim.EstadoSimulado()
    http_server, url = sim.iniciar_servidor(estado)
    assert urlsplit(url).hostname == "127.0.0.1"  # nunca un SAP real
    yield estado, url
    http_server.shutdown()
    http_server.server_close()


@pytest.fixture()
def esperas():
    return []


@pytest.fixture()
def cliente(servidor, esperas):
    _, url = servidor
    c = ServiceLayerSAP(url, sim.COMPANY_DB, sim.USUARIO, sim.CLAVE, CAMPOS, timeout=5, espera=esperas.append)
    yield c
    c.cerrar()


def _enviar(c, numero=NUMERO, ruc=sim.RUC_PROVEEDOR, total=115.0, lineas=LINEAS):
    return c.enviar_factura(ruc, numero, total, fecha_emision=FECHA, lineas=lineas)


# ---------- adaptador: sesion ----------

def test_crea_factura_con_login_y_cookie(servidor, cliente):
    estado, _ = servidor
    resultado = _enviar(cliente)

    assert resultado.exitoso and not resultado.ya_existia
    assert resultado.referencia == str(estado.facturas[0]["DocEntry"])
    assert estado.contar("POST", "Login") == 1
    enviada = estado.facturas[0]
    assert enviada["CardCode"] == sim.CARD_CODE_PROVEEDOR
    assert enviada["DocDate"] == "2026-10-01"
    assert enviada[sim.CAMPO_NUMERO] == NUMERO
    assert enviada["DocumentLines"][0][sim.CAMPO_CUENTA] == "510101"
    assert enviada["DocumentLines"][0]["LineTotal"] == 100.0


def test_reutiliza_la_sesion_entre_envios(servidor, cliente):
    estado, _ = servidor
    _enviar(cliente, numero="A")
    _enviar(cliente, numero="B")
    assert estado.contar("POST", "Login") == 1
    assert len(estado.facturas) == 2


def test_sesion_expirada_vuelve_a_loguear_y_reintenta(servidor, cliente):
    estado, _ = servidor
    _enviar(cliente, numero="A")
    estado.expirar_sesiones()

    resultado = _enviar(cliente, numero="B")

    assert resultado.exitoso
    assert estado.contar("POST", "Login") == 2
    assert len(estado.facturas) == 2


def test_codigo_5002_en_el_post_reintenta_una_sola_vez(servidor, cliente):
    estado, _ = servidor
    estado.forzar_fallo(-5002, http=401)
    assert _enviar(cliente).exitoso
    assert estado.contar("POST", "Login") == 2
    assert estado.contar("POST", "PurchaseInvoices") == 2
    assert len(estado.facturas) == 1


def test_segundo_5002_seguido_falla_sin_mas_reintentos(servidor, cliente):
    estado, _ = servidor
    estado.forzar_fallo(-5002, http=400, mensaje="Texto literal de SAP", veces=2)

    resultado = _enviar(cliente)

    assert not resultado.exitoso
    assert "Texto literal de SAP" in resultado.mensaje
    assert estado.contar("POST", "PurchaseInvoices") == 2
    assert estado.facturas == []


def test_codigo_301_tambien_cuenta_como_sesion_expirada(servidor, cliente):
    estado, _ = servidor
    estado.codigo_sesion_expirada = 301
    _enviar(cliente, numero="A")
    estado.expirar_sesiones()
    assert _enviar(cliente, numero="B").exitoso
    assert estado.contar("POST", "Login") == 2


def test_login_rechazado_no_expone_la_clave(servidor, esperas):
    _, url = servidor
    c = ServiceLayerSAP(url, sim.COMPANY_DB, sim.USUARIO, "clave-equivocada", CAMPOS, timeout=5, espera=esperas.append)
    resultado = _enviar(c)
    c.cerrar()
    assert not resultado.exitoso
    assert "Login en SAP rechazado" in resultado.mensaje
    assert "clave-equivocada" not in resultado.mensaje


# ---------- adaptador: objeto bloqueado ----------

def test_objeto_bloqueado_se_resuelve_con_espera_creciente(servidor, cliente, esperas):
    estado, _ = servidor
    estado.forzar_fallo(-2028, veces=2)
    assert _enviar(cliente).exitoso
    assert esperas == [1, 2]
    assert len(estado.facturas) == 1


def test_objeto_bloqueado_agota_reintentos_y_muestra_texto_de_sap(servidor, cliente, esperas):
    estado, _ = servidor
    estado.forzar_fallo(-2028, mensaje="No matching records found", veces=10)

    resultado = _enviar(cliente)

    assert not resultado.exitoso
    assert esperas == [1, 2, 4]
    assert estado.contar("POST", "PurchaseInvoices") == 4
    assert "No matching records found" in resultado.mensaje
    assert "bloquead" not in resultado.mensaje.lower()


# ---------- adaptador: rechazos ----------

def test_proveedor_inexistente_no_intenta_crear(servidor, cliente):
    estado, _ = servidor
    resultado = _enviar(cliente, ruc="1799999999001")
    assert not resultado.exitoso
    assert "no existe en SAP" in resultado.mensaje
    assert estado.contar("POST", "PurchaseInvoices") == 0


def test_proveedor_duplicado_no_adivina(servidor, cliente):
    estado, _ = servidor
    estado.proveedores.append({sim.CAMPO_RUC: sim.RUC_PROVEEDOR, "CardCode": "P0002", "CardType": "cSupplier"})
    resultado = _enviar(cliente)
    assert not resultado.exitoso
    assert "una persona debe revisarlo" in resultado.mensaje
    assert estado.contar("POST", "PurchaseInvoices") == 0


def test_error_de_validacion_devuelve_el_texto_de_sap(servidor, cliente, esperas):
    estado, _ = servidor
    lineas = [LineaFacturaSAP("Servicio", Decimal("100.00"), sim.CUENTA_INEXISTENTE)]
    resultado = _enviar(cliente, lineas=lineas)
    assert not resultado.exitoso
    assert "Cuenta contable invalida" in resultado.mensaje
    assert esperas == []
    assert estado.facturas == []


def test_campo_mal_configurado_falla_sin_crear(servidor, esperas):
    # Si el campo no verificado resulta equivocado, la consulta falla y no se crea nada.
    estado, url = servidor
    campos = CamposSAP(sim.CAMPO_RUC, sim.CAMPO_CUENTA, "CampoQueNoExiste")
    c = ServiceLayerSAP(url, sim.COMPANY_DB, sim.USUARIO, sim.CLAVE, campos, timeout=5, espera=esperas.append)
    resultado = _enviar(c)
    c.cerrar()
    assert not resultado.exitoso
    assert estado.contar("POST", "PurchaseInvoices") == 0


def test_sin_fecha_o_lineas_no_llama_a_sap(servidor, cliente):
    estado, _ = servidor
    resultado = cliente.enviar_factura(sim.RUC_PROVEEDOR, NUMERO, 115.0)
    assert not resultado.exitoso
    assert estado.peticiones == []


def test_total_distinto_en_sap_deja_aviso(servidor, cliente):
    estado, _ = servidor
    estado.tasa_iva = Decimal("0")
    resultado = _enviar(cliente)
    assert resultado.exitoso
    assert "AVISO" in resultado.resumen_respuesta


# ---------- idempotencia ----------

def test_factura_repetida_no_crea_otra_y_devuelve_el_id(servidor, cliente):
    estado, _ = servidor
    primera = _enviar(cliente)
    posts = estado.contar("POST", "PurchaseInvoices")

    segunda = _enviar(cliente)

    assert segunda.exitoso and segunda.ya_existia
    assert segunda.referencia == primera.referencia
    assert estado.contar("POST", "PurchaseInvoices") == posts
    assert len(estado.facturas) == 1


def test_mismo_numero_de_otro_proveedor_si_se_crea(servidor, cliente):
    estado, _ = servidor
    estado.proveedores.append({sim.CAMPO_RUC: "1790000000001", "CardCode": "P0002", "CardType": "cSupplier"})
    _enviar(cliente)
    assert not _enviar(cliente, ruc="1790000000001").ya_existia
    assert len(estado.facturas) == 2


def test_si_falla_la_consulta_de_repetidas_no_crea(servidor, cliente):
    estado, _ = servidor
    estado.forzar_fallo(-1, http=400, metodo="GET", recurso="PurchaseInvoices")
    resultado = _enviar(cliente)
    assert not resultado.exitoso
    assert estado.contar("POST", "PurchaseInvoices") == 0


def test_repetida_detectada_por_sap_al_crear_no_se_marca_exitosa(servidor, cliente):
    estado, _ = servidor
    estado.facturas.append({"CardCode": sim.CARD_CODE_PROVEEDOR, sim.CAMPO_NUMERO: "OTRA", "DocEntry": 1})
    estado.forzar_fallo(sim.CODIGO_REPETIDA, mensaje="Factura de proveedor repetida")
    resultado = _enviar(cliente)
    assert not resultado.exitoso
    assert "repetida" in resultado.mensaje


def test_corte_tras_crear_detecta_la_factura_sin_duplicar(servidor, cliente):
    estado, _ = servidor
    estado.cortar_tras_crear = True

    resultado = _enviar(cliente)

    assert resultado.exitoso and resultado.ya_existia
    assert resultado.referencia == str(estado.facturas[0]["DocEntry"])
    assert estado.contar("POST", "PurchaseInvoices") == 1
    assert len(estado.facturas) == 1


def test_corte_sin_crear_informa_incierto_sin_repetir_el_post(servidor, cliente):
    estado, _ = servidor
    estado.cortar_sin_crear = True

    resultado = _enviar(cliente)

    assert not resultado.exitoso
    assert "incierto" in resultado.mensaje.lower()
    assert estado.contar("POST", "PurchaseInvoices") == 1
    assert estado.facturas == []
    assert _enviar(cliente).exitoso  # el reintento manual consulta primero y crea una sola
    assert len(estado.facturas) == 1


def test_error_5xx_en_post_se_trata_como_incierto(servidor, cliente):
    estado, _ = servidor
    estado.forzar_fallo(-1, http=500, mensaje="Error interno")
    resultado = _enviar(cliente)
    assert not resultado.exitoso
    assert "incierto" in resultado.mensaje.lower()


def test_filtro_escapa_comillas(servidor, cliente):
    estado, _ = servidor
    numero = "001'or'1'eq'1"
    assert _enviar(cliente, numero=numero).exitoso
    assert estado.facturas[0][sim.CAMPO_NUMERO] == numero
    assert not _enviar(cliente, numero="otro").ya_existia
    assert _enviar(cliente, numero=numero).ya_existia
    assert literal_odata("a'b") == "'a''b'"


def test_extraer_error_acepta_ambos_formatos():
    assert extraer_error({"error": {"code": -5002, "message": "x"}}) == ("-5002", "x")
    assert extraer_error({"error": {"code": "301", "message": {"lang": "en", "value": "y"}}}) == ("301", "y")
    assert extraer_error(["raro"]) == (None, "")


# ---------- reglas de envio y aprobacion humana ----------

class ClienteEspia(ClienteSAP):
    def __init__(self, resultado=None):
        self.llamadas = []
        self.resultado = resultado or ResultadoEnvioSAP(True, "1", "ok")

    def enviar_factura(self, ruc_proveedor, numero_factura, total, *, fecha_emision=None, lineas=None):
        self.llamadas.append((ruc_proveedor, numero_factura, total, fecha_emision, lineas))
        return self.resultado


def _factura_pendiente(db, numero=NUMERO, monto=Decimal("100.00")) -> tuple[Factura, FacturaDetalleLinea]:
    if db.query(CuentaContable).filter_by(codigo="510101").first() is None:
        db.add(CuentaContable(codigo="510101", nombre="Gasto", tipo_cuenta="D", activa=True))
    factura = Factura(
        ruc_proveedor=sim.RUC_PROVEEDOR, numero_factura=numero, fecha_emision=FECHA,
        subtotal=Decimal("100.00"), impuestos=Decimal("15.00"), total=Decimal("115.00"), estado="pendiente",
    )
    db.add(factura)
    db.flush()
    linea = FacturaDetalleLinea(factura_id=factura.id, orden=1, descripcion="Servicio", monto=monto)
    db.add(linea)
    db.commit()
    return factura, linea


def _factura_aprobada(db, numero=NUMERO, aprobador="ana") -> Factura:
    factura, linea = _factura_pendiente(db, numero)
    return aprobar_factura_lineas(db, factura.id, aprobador, {linea.id: "510101"})


def _historial(db, factura_id, accion) -> HistorialAccion:
    return db.query(HistorialAccion).filter_by(factura_id=factura_id, accion=accion).one()


def test_pendiente_nunca_llega_al_adaptador(db_session):
    factura, _ = _factura_pendiente(db_session)
    espia = ClienteEspia()
    with pytest.raises(EnvioInvalidoError):
        enviar_a_sap(db_session, factura.id, "luis", cliente=espia)
    assert espia.llamadas == []


def test_guardada_nunca_llega_al_adaptador(db_session):
    factura, linea = _factura_pendiente(db_session)
    guardar_factura_preliminar(db_session, factura.id, "ana", codigos_por_linea={linea.id: "510101"})
    espia = ClienteEspia()
    with pytest.raises(EnvioInvalidoError):
        enviar_a_sap(db_session, factura.id, "luis", cliente=espia)
    assert espia.llamadas == []


def test_rechazada_nunca_llega_al_adaptador(db_session):
    factura, _ = _factura_pendiente(db_session)
    rechazar_factura(db_session, factura.id, "ana", "Monto incorrecto")
    espia = ClienteEspia()
    with pytest.raises(EnvioInvalidoError):
        enviar_a_sap(db_session, factura.id, "luis", cliente=espia)
    assert espia.llamadas == []


def test_estado_aprobada_sin_aprobacion_en_historial_no_se_envia(db_session):
    factura, linea = _factura_pendiente(db_session)
    linea.codigo_cuenta_final = "510101"
    factura.estado = "aprobada"  # sin pasar por una persona
    db_session.commit()
    espia = ClienteEspia()
    with pytest.raises(EnvioInvalidoError, match="quien la aprobo"):
        enviar_a_sap(db_session, factura.id, "luis", cliente=espia)
    assert espia.llamadas == []


def test_sin_aprobacion_no_hay_ninguna_peticion_http(db_session, servidor, cliente):
    estado, _ = servidor
    factura, _ = _factura_pendiente(db_session)
    with pytest.raises(EnvioInvalidoError):
        enviar_a_sap(db_session, factura.id, "luis", cliente=cliente)
    assert estado.peticiones == []


def test_lineas_que_no_cuadran_con_subtotal_no_se_envian(db_session):
    factura, linea = _factura_pendiente(db_session, monto=Decimal("0"))
    aprobar_factura_lineas(db_session, factura.id, "ana", {linea.id: "510101"})
    espia = ClienteEspia()
    with pytest.raises(EnvioInvalidoError, match="no coincide con el subtotal"):
        enviar_a_sap(db_session, factura.id, "luis", cliente=espia)
    assert espia.llamadas == []


def test_exito_registra_quien_aprobo_quien_envio_cuando_y_respuesta(db_session, servidor, cliente):
    factura = _factura_aprobada(db_session)

    resultado = enviar_a_sap(db_session, factura.id, "luis", cliente=cliente)

    assert resultado.estado == "registrada"
    assert resultado.referencia_sap
    fila = _historial(db_session, factura.id, "registrada_sap")
    assert fila.usuario == "luis"
    assert fila.creado_en is not None
    assert "Aprobada por ana" in fila.motivo
    assert "Enviada por luis" in fila.motivo
    assert f"DocEntry={resultado.referencia_sap}" in fila.motivo


def test_factura_ya_existente_registra_el_id_existente(db_session, servidor, cliente):
    estado, _ = servidor
    primero = _enviar(cliente)
    factura = _factura_aprobada(db_session)

    resultado = enviar_a_sap(db_session, factura.id, "luis", cliente=cliente)

    assert resultado.estado == "registrada"
    assert resultado.referencia_sap == primero.referencia
    assert "No se creo otra" in _historial(db_session, factura.id, "ya_existia_sap").motivo
    assert len(estado.facturas) == 1


def test_reenvio_de_factura_registrada_no_llega_a_sap(db_session, servidor, cliente):
    estado, _ = servidor
    factura = _factura_aprobada(db_session)
    enviar_a_sap(db_session, factura.id, "luis", cliente=cliente)
    total_peticiones = len(estado.peticiones)
    with pytest.raises(EnvioInvalidoError):
        enviar_a_sap(db_session, factura.id, "luis", cliente=cliente)
    assert len(estado.peticiones) == total_peticiones


def test_error_de_sap_deja_aprobada_y_audita(db_session, servidor, cliente):
    estado, _ = servidor
    estado.forzar_fallo(sim.CODIGO_VALIDACION, mensaje="Periodo cerrado")
    factura = _factura_aprobada(db_session)

    with pytest.raises(EnvioInvalidoError, match="Periodo cerrado"):
        enviar_a_sap(db_session, factura.id, "luis", cliente=cliente)

    db_session.refresh(factura)
    assert factura.estado == "aprobada"
    assert factura.referencia_sap is None
    fila = _historial(db_session, factura.id, "error_envio_sap")
    assert "Aprobada por ana" in fila.motivo and "Periodo cerrado" in fila.motivo


# ---------- secretos ----------

def test_clave_y_cookie_no_aparecen_en_ningun_registro(db_session, servidor, cliente, caplog):
    caplog.set_level(logging.DEBUG)
    estado, _ = servidor
    factura = _factura_aprobada(db_session, numero="A")
    enviar_a_sap(db_session, factura.id, "luis", cliente=cliente)
    estado.expirar_sesiones()
    factura_2 = _factura_aprobada(db_session, numero="B")
    enviar_a_sap(db_session, factura_2.id, "luis", cliente=cliente)

    sesion = estado.sesiones_emitidas[0]
    estado.forzar_fallo(sim.CODIGO_VALIDACION, mensaje=f"eco {sim.CLAVE} B1SESSION={sesion}; fin")
    factura_3 = _factura_aprobada(db_session, numero="C")
    with pytest.raises(EnvioInvalidoError) as excepcion:
        enviar_a_sap(db_session, factura_3.id, "luis", cliente=cliente)

    motivos = " ".join(f.motivo or "" for f in db_session.query(HistorialAccion).all())
    textos = [caplog.text, motivos, str(excepcion.value), repr(cliente)]
    assert len(estado.sesiones_emitidas) == 2
    for texto in textos:
        assert sim.CLAVE not in texto
        for sesion_emitida in estado.sesiones_emitidas:
            assert sesion_emitida not in texto


def test_repr_de_la_config_no_muestra_la_clave(monkeypatch):
    _configurar_servicelayer(monkeypatch, "http://127.0.0.1:1/b1s/v2/")
    assert sim.CLAVE not in repr(fabrica.leer_config_servicelayer())


# ---------- fabrica y variables de entorno ----------

def _configurar_servicelayer(monkeypatch, url):
    monkeypatch.setenv("SAP_MODO", "servicelayer")
    monkeypatch.setenv("SAP_URL_BASE", url)
    monkeypatch.setenv("SAP_COMPANY_DB", sim.COMPANY_DB)
    monkeypatch.setenv("SAP_USUARIO", sim.USUARIO)
    monkeypatch.setenv("SAP_CLAVE", sim.CLAVE)
    monkeypatch.setenv("SAP_CAMPO_RUC_PROVEEDOR", sim.CAMPO_RUC)
    monkeypatch.setenv("SAP_CAMPO_CUENTA_LINEA", sim.CAMPO_CUENTA)
    monkeypatch.setenv("SAP_CAMPO_NUMERO_FACTURA", sim.CAMPO_NUMERO)


def test_sin_modo_usa_el_simulador():
    assert isinstance(fabrica.obtener_cliente_sap(), SimuladorSAP)


def test_modo_invalido_es_error_y_no_cae_al_simulador(monkeypatch):
    monkeypatch.setenv("SAP_MODO", "produccion")
    with pytest.raises(fabrica.ConfiguracionSAPError):
        fabrica.obtener_cliente_sap()


@pytest.mark.parametrize("faltante", [
    "SAP_URL_BASE", "SAP_COMPANY_DB", "SAP_USUARIO", "SAP_CLAVE",
    "SAP_CAMPO_RUC_PROVEEDOR", "SAP_CAMPO_CUENTA_LINEA", "SAP_CAMPO_NUMERO_FACTURA",
])
def test_configuracion_incompleta_nombra_la_variable(monkeypatch, faltante):
    _configurar_servicelayer(monkeypatch, "http://127.0.0.1:1/b1s/v2/")
    monkeypatch.delenv(faltante)
    with pytest.raises(fabrica.ConfiguracionSAPError, match=faltante):
        fabrica.obtener_cliente_sap()


def test_campo_con_caracteres_invalidos_es_error(monkeypatch):
    _configurar_servicelayer(monkeypatch, "http://127.0.0.1:1/b1s/v2/")
    monkeypatch.setenv("SAP_CAMPO_NUMERO_FACTURA", "NumAtCard eq 'x' or 1")
    with pytest.raises(fabrica.ConfiguracionSAPError):
        fabrica.obtener_cliente_sap()


def test_configuracion_incompleta_no_envia_y_explica(db_session, monkeypatch):
    monkeypatch.setenv("SAP_MODO", "servicelayer")
    factura = _factura_aprobada(db_session)
    with pytest.raises(EnvioInvalidoError, match="SAP_URL_BASE"):
        enviar_a_sap(db_session, factura.id, "luis")
    db_session.refresh(factura)
    assert factura.estado == "aprobada"


@pytest.mark.parametrize("valor,esperado", [(None, True), ("true", True), ("false", False)])
def test_verificar_tls_llega_a_httpx(monkeypatch, valor, esperado):
    _configurar_servicelayer(monkeypatch, "http://127.0.0.1:1/b1s/v2/")
    if valor is not None:
        monkeypatch.setenv("SAP_VERIFICAR_TLS", valor)
    capturado = {}
    cliente_original = httpx.Client

    def espia_cliente(**kwargs):
        capturado.update(kwargs)
        return cliente_original(**kwargs)

    monkeypatch.setattr(servicelayer.httpx, "Client", espia_cliente)
    fabrica.obtener_cliente_sap().cerrar()
    assert capturado["verify"] is esperado


def test_verificar_tls_invalido_es_error(monkeypatch):
    _configurar_servicelayer(monkeypatch, "http://127.0.0.1:1/b1s/v2/")
    monkeypatch.setenv("SAP_VERIFICAR_TLS", "quizas")
    with pytest.raises(fabrica.ConfiguracionSAPError):
        fabrica.obtener_cliente_sap()


def test_simulador_conserva_su_comportamiento():
    simulador = SimuladorSAP()
    assert simulador.enviar_factura("1790012345001", "N", 100.0).exitoso
    assert not simulador.enviar_factura("1790099999001", "N", 100.0).exitoso
    assert not simulador.enviar_factura("1790012345001", "N", 5000.01).exitoso
    con_detalle = simulador.enviar_factura("1790012345001", "N", 100.0, fecha_emision=FECHA, lineas=LINEAS)
    assert con_detalle.exitoso and con_detalle.referencia.startswith("FacPro-")


def test_envio_con_simulador_por_defecto_sigue_funcionando(db_session):
    factura = _factura_aprobada(db_session)
    resultado = enviar_a_sap(db_session, factura.id, "luis")
    assert resultado.estado == "registrada"
    assert resultado.referencia_sap.startswith("FacPro-")


# ---------- de punta a punta por la ruta HTTP ----------

@pytest.fixture()
def app_cliente(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'sap.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SesionPruebas = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(main_module, "SessionLocal", SesionPruebas)
    db = SesionPruebas()
    db.add(Usuario(nombre_usuario="luis", hash_contrasena=hash_contrasena("clave-123"), rol="contadora", activo=True))
    db.commit()
    db.close()
    prueba = TestClient(main_module.app)
    assert prueba.post("/login", data={"usuario": "luis", "contrasena": "clave-123"}, follow_redirects=False).status_code == 303
    yield prueba, SesionPruebas
    engine.dispose()


def test_ruta_envia_dos_veces_y_queda_una_sola_factura(app_cliente, servidor, monkeypatch):
    prueba, SesionPruebas = app_cliente
    estado, url = servidor
    _configurar_servicelayer(monkeypatch, url)
    db = SesionPruebas()
    factura_id = _factura_aprobada(db).id
    db.close()

    primera = prueba.post(f"/facturas/{factura_id}/enviar-sap")
    segunda = prueba.post(f"/facturas/{factura_id}/enviar-sap")
    fabrica.obtener_cliente_sap().cerrar()

    assert "Registrada en SAP" in primera.text
    assert "SAP rechazo el envio" in segunda.text and "registrada" in segunda.text
    assert len(estado.facturas) == 1
    db = SesionPruebas()
    assert "Enviada por luis" in _historial(db, factura_id, "registrada_sap").motivo
    db.close()


def test_ruta_escapa_el_texto_que_viene_de_sap(app_cliente, monkeypatch):
    prueba, SesionPruebas = app_cliente
    malicioso = "<img src=x onerror=alert(1)>"
    espia = ClienteEspia(ResultadoEnvioSAP(False, None, malicioso))
    monkeypatch.setattr(envios_sap, "obtener_cliente_sap", lambda: espia)
    db = SesionPruebas()
    factura_id = _factura_aprobada(db).id
    db.close()

    respuesta = prueba.post(f"/facturas/{factura_id}/enviar-sap")

    assert malicioso not in respuesta.text
    assert "&lt;img" in respuesta.text


def test_ruta_sin_sesion_no_envia(servidor, app_cliente, monkeypatch):
    _, SesionPruebas = app_cliente
    estado, url = servidor
    _configurar_servicelayer(monkeypatch, url)
    db = SesionPruebas()
    factura_id = _factura_aprobada(db).id
    db.close()

    sin_sesion = TestClient(main_module.app)
    respuesta = sin_sesion.post(f"/facturas/{factura_id}/enviar-sap", follow_redirects=False)

    assert respuesta.status_code == 303
    assert estado.peticiones == []
