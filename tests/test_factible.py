"""
Pruebas de la integracion con el ApiRest de Factible (app/adaptadores/factible y
app/core/descarga_factible.py). Ninguna llama a la red: el cliente REST se prueba
con httpx.MockTransport (respuestas copiadas de las fichas tecnicas) y el flujo
completo con SimuladorFactible.
"""
import base64
import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.core.descarga_factible as descarga_factible
import app.core.vigilante_carpeta as vigilante_carpeta
from app.adaptadores.factible.base import ErrorFactible, RespuestaConsulta, RespuestaGenerar
from app.adaptadores.factible.cliente_rest import ClienteRestFactible
from app.adaptadores.factible.simulador import SimuladorFactible
from app.core.models import DescargaFactible, Factura
from app.db.config import Base

RUC = "1791927338001"
JWT_FALSO = "aaa.bbb.ccc"
CLAVE_FACTURA = "2108202601179001691900120010010000001231234567811"
CLAVE_FACTURA_2 = "2108202601179001691900120010010000001241234567811"
CLAVE_RETENCION = "2108202607179001691900120010010000005551234567811"


def _factura_xml(clave: str, cod_doc: str = "01", envuelta: bool = False) -> bytes:
    comprobante = (
        f"<factura><infoTributaria><codDoc>{cod_doc}</codDoc>"
        f"<claveAcceso>{clave}</claveAcceso></infoTributaria></factura>"
    )
    if envuelta:
        return f"<autorizacion><estado>AUTORIZADO</estado><comprobante><![CDATA[{comprobante}]]></comprobante></autorizacion>".encode()
    return comprobante.encode()


def _zip(archivos: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for nombre, contenido in archivos.items():
            z.writestr(nombre, contenido)
    return buffer.getvalue()


# ---------- cliente REST (contra respuestas de las fichas) ----------

def _respuesta_error(mensaje: str) -> dict:
    return {
        "Success": False, "Token": None, "Estado": "", "ResultadoValidacion": "",
        "TotalClaves": 0, "TotalValidas": 0, "TotalInvalidas": 0,
        "NombreArchivoErrores": None, "Mensaje": mensaje,
    }


class ApiFalsa:
    """Imita las rutas de Factible y registra lo que recibio."""

    def __init__(self):
        self.logins = 0
        self.rechazar_proximo_bearer = False
        self.peticiones: list[tuple[str, dict, str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        cuerpo = json.loads(request.content or b"{}")
        auth = request.headers.get("Authorization")
        self.peticiones.append((request.url.path, cuerpo, auth))

        if request.url.path == "/api/login/authenticate":
            self.logins += 1
            if cuerpo.get("password") != "correcta":
                return httpx.Response(401)
            return httpx.Response(200, json={"Message": JWT_FALSO})

        if auth != f"Bearer {JWT_FALSO}" or self.rechazar_proximo_bearer:
            self.rechazar_proximo_bearer = False
            return httpx.Response(401)

        if request.url.path == "/api/ConsultaDocumento/generar/recepcion":
            if cuerpo["fecha"] == "07/08/2026":
                return httpx.Response(200, json={
                    "Success": True, "Token": "935b533b-4150-4444-b088-e70d5bbe69d9", "Estado": "PENDIENTE",
                    "ResultadoValidacion": "VALIDO", "TotalClaves": 225, "TotalValidas": 225, "TotalInvalidas": 0,
                    "NombreArchivoErrores": None,
                    "Mensaje": "Token generado correctamente. Se encontraron 225 claves para la fecha 07/08/2026.",
                })
            if cuerpo["fecha"] == "08/08/2026":
                return httpx.Response(200, json=_respuesta_error("Ya existe un token pendiente."))
            return httpx.Response(400, json=_respuesta_error(f"No se encontraron claves de acceso para la fecha {cuerpo['fecha']}."))

        if request.url.path == "/api/ConsultaDocumento/consultar":
            if cuerpo["token"] == "pendiente":
                return httpx.Response(200, json={"Success": True, "Token": None, "Estado": "PENDIENTE", "DocumentoZip": None, "Mensaje": ""})
            if cuerpo["token"] == "procesando":
                return httpx.Response(400, json=_respuesta_error("Token en estado PROCESANDO"))
            if cuerpo["token"] == "listo":
                return httpx.Response(200, json={"Success": True, "Token": None, "Estado": "FINALIZADO", "DocumentoZip": "UEAEAJABAADLXgkAAAA=", "Mensaje": ""})
            return httpx.Response(400, json=_respuesta_error("No se encontró token."))

        return httpx.Response(404)


def _cliente(api: ApiFalsa, clave: str = "correcta") -> ClienteRestFactible:
    return ClienteRestFactible("https://qa.ejemplo", "USUARIO", clave, RUC, transport=httpx.MockTransport(api))


def test_generar_envia_cuerpo_de_la_ficha_con_bearer():
    api = ApiFalsa()
    r = _cliente(api).generar_recepcion(RUC, "07/08/2026")

    assert r.exito and r.token == "935b533b-4150-4444-b088-e70d5bbe69d9"
    assert r.estado == "PENDIENTE" and r.total_claves == 225
    login, generar = api.peticiones
    assert login[1] == {"username": "USUARIO", "password": "correcta", "ruc": RUC}
    assert generar[1] == {"rucempresa": RUC, "fecha": "07/08/2026"}
    assert generar[2] == f"Bearer {JWT_FALSO}"


def test_generar_sin_documentos_y_token_pendiente():
    api = ApiFalsa()
    cliente = _cliente(api)

    sin_docs = cliente.generar_recepcion(RUC, "01/01/2026")
    assert not sin_docs.exito and sin_docs.sin_documentos and not sin_docs.ya_hay_token_pendiente

    pendiente = cliente.generar_recepcion(RUC, "08/08/2026")
    assert pendiente.ya_hay_token_pendiente and pendiente.token is None


def test_jwt_se_reutiliza_y_se_renueva_tras_401():
    api = ApiFalsa()
    cliente = _cliente(api)
    cliente.consultar(RUC, "pendiente")
    cliente.consultar(RUC, "pendiente")
    assert api.logins == 1

    api.rechazar_proximo_bearer = True  # simula JWT expirado
    r = cliente.consultar(RUC, "listo")
    assert api.logins == 2
    assert r.estado == "FINALIZADO" and r.documento_zip_base64 == "UEAEAJABAADLXgkAAAA="


def test_login_rechazado_lanza_error_claro():
    with pytest.raises(ErrorFactible, match="Login"):
        _cliente(ApiFalsa(), clave="equivocada").consultar(RUC, "listo")


def test_consultar_token_no_encontrado():
    r = _cliente(ApiFalsa()).consultar(RUC, "otro")
    assert not r.exito and r.mensaje == "No se encontró token."
    assert not r.en_curso


def test_consultar_token_procesando_sigue_en_curso():
    cliente = _cliente(ApiFalsa())
    assert cliente.consultar(RUC, "procesando").en_curso
    assert cliente.consultar(RUC, "pendiente").en_curso
    assert not cliente.consultar(RUC, "listo").en_curso


# ---------- extraccion del ZIP ----------

@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Sesion = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    sesion = Sesion()
    yield sesion, Sesion
    sesion.close()
    engine.dispose()


def test_extrae_solo_facturas_nuevas_con_nombre_por_clave(db, tmp_path):
    sesion, _ = db
    carpeta = tmp_path / "entrada"
    sesion.add(Factura(ruc_proveedor="1790016919001", numero_factura="001-001-000000124", clave_acceso=CLAVE_FACTURA_2,
                       fecha_emision=date(2026, 8, 21), subtotal=1, impuestos=0, total=1, estado="pendiente"))
    sesion.commit()

    zip_bytes = _zip({
        "subcarpeta/a.xml": _factura_xml(CLAVE_FACTURA, envuelta=True),
        "b.xml": _factura_xml(CLAVE_FACTURA_2),           # ya esta en la BD
        "c.xml": _factura_xml(CLAVE_RETENCION, "07"),     # retencion -> se ignora
        "d.pdf": b"%PDF",                                 # no es xml -> se ignora
    })
    r = descarga_factible.extraer_facturas(sesion, zip_bytes, carpeta)

    assert (r.guardados, r.duplicados, r.no_factura) == (1, 1, 1)
    assert sorted(p.name for p in carpeta.iterdir()) == [f"{CLAVE_FACTURA}.xml"]


def test_no_reescribe_lo_que_ya_paso_por_la_carpeta(db, tmp_path):
    sesion, _ = db
    carpeta = tmp_path / "entrada"
    (carpeta / "errores").mkdir(parents=True)
    (carpeta / "errores" / f"{CLAVE_FACTURA}.xml").write_bytes(b"x")

    r = descarga_factible.extraer_facturas(sesion, _zip({"a.xml": _factura_xml(CLAVE_FACTURA)}), carpeta)
    assert (r.guardados, r.duplicados) == (0, 1)
    assert not (carpeta / f"{CLAVE_FACTURA}.xml").exists()


def test_zip_slip_no_escribe_fuera_de_la_carpeta(db, tmp_path):
    sesion, _ = db
    carpeta = tmp_path / "entrada"
    r = descarga_factible.extraer_facturas(sesion, _zip({"../../malo.xml": b"<no es xml"}), carpeta)

    assert r.guardados == 1  # XML ilegible: se deja pasar para que el vigilante lo mande a errores/
    assert (carpeta / "malo.xml").exists()
    assert not (tmp_path / "malo.xml").exists()


# ---------- flujo completo con el simulador ----------

@pytest.fixture()
def entorno(db, tmp_path, monkeypatch):
    _, Sesion = db
    carpeta = tmp_path / "entrada"
    carpeta.mkdir()
    monkeypatch.setattr(descarga_factible, "SessionLocal", Sesion)
    monkeypatch.setattr(vigilante_carpeta, "CARPETA_POR_DEFECTO", str(carpeta))
    return Sesion, carpeta


def _config(**cambios) -> descarga_factible.ConfigFactible:
    base = dict(modo="simulador", url_base="", usuario="", clave="", ruc=RUC, descarga_automatica=False,
                hora_descarga=datetime.min.time(), intervalo_consulta=1)
    base.update(cambios)
    return descarga_factible.ConfigFactible(**base)


def test_token_pendiente_se_consulta_hasta_finalizar_y_deja_xml(entorno):
    Sesion, carpeta = entorno
    fecha = date(2026, 8, 7)
    simulador = SimuladorFactible({"07/08/2026": {"f.xml": _factura_xml(CLAVE_FACTURA)}}, consultas_hasta_finalizar=2)

    db = Sesion()
    descarga = descarga_factible.solicitar_descarga(db, simulador, RUC, fecha, "manual", "nicole")
    assert descarga.estado == "PENDIENTE" and descarga.token and descarga.total_claves == 1
    db.close()

    descarga_factible.ejecutar_ciclo(_config(), simulador)  # 1ra consulta: sigue PENDIENTE
    assert not any(carpeta.iterdir())
    descarga_factible.ejecutar_ciclo(_config(), simulador)  # 2da: FINALIZADO

    db = Sesion()
    final = db.query(DescargaFactible).one()
    assert final.estado == "FINALIZADO" and final.xml_guardados == 1
    db.close()
    assert (carpeta / f"{CLAVE_FACTURA}.xml").exists()


class SimuladorConProcesando(SimuladorFactible):
    """Antes de FINALIZADO responde como Factible real: Success=false + 'Token en estado PROCESANDO'."""

    def consultar(self, ruc_empresa, token):
        r = super().consultar(ruc_empresa, token)
        if r.estado == "PENDIENTE":
            return RespuestaConsulta(False, "", None, "Token en estado PROCESANDO")
        return r


def test_token_procesando_no_se_marca_como_error(entorno):
    Sesion, carpeta = entorno
    simulador = SimuladorConProcesando({"19/10/2026": {"f.xml": _factura_xml(CLAVE_FACTURA)}}, consultas_hasta_finalizar=3)
    db = Sesion()
    descarga_factible.solicitar_descarga(db, simulador, RUC, date(2026, 10, 19), "manual", "admin")
    db.close()

    descarga_factible.ejecutar_ciclo(_config(), simulador)  # PROCESANDO
    descarga_factible.ejecutar_ciclo(_config(), simulador)  # PROCESANDO
    db = Sesion()
    assert db.query(DescargaFactible).one().estado == "PENDIENTE"
    db.close()

    descarga_factible.ejecutar_ciclo(_config(), simulador)  # FINALIZADO
    db = Sesion()
    assert db.query(DescargaFactible).one().estado == "FINALIZADO"
    db.close()
    assert (carpeta / f"{CLAVE_FACTURA}.xml").exists()


class ClienteFijo:
    """Responde siempre lo mismo; sirve para probar consultar_descarga/solicitar_descarga."""

    def __init__(self, consulta: RespuestaConsulta | None = None, generar: RespuestaGenerar | None = None):
        self.consulta = consulta
        self.generar = generar

    def consultar(self, ruc_empresa, token):
        return self.consulta

    def generar_recepcion(self, ruc_empresa, fecha):
        return self.generar


def _descarga_pendiente(Sesion, creado_en: datetime | None = None) -> None:
    db = Sesion()
    db.add(DescargaFactible(ruc_empresa=RUC, fecha_documentos=date(2026, 10, 19), token="t-1", estado="PENDIENTE",
                            origen="manual", usuario="admin", total_claves=1, creado_en=creado_en))
    db.commit()
    db.close()


def test_zip_base64_con_saltos_de_linea_se_acepta(entorno):
    Sesion, carpeta = entorno
    _descarga_pendiente(Sesion)
    zip_base64 = base64.b64encode(_zip({"f.xml": _factura_xml(CLAVE_FACTURA)})).decode()
    partido = "\r\n".join(zip_base64[i:i + 76] for i in range(0, len(zip_base64), 76)) + " \n"

    descarga_factible.ejecutar_ciclo(_config(), ClienteFijo(RespuestaConsulta(True, "FINALIZADO", partido, "")))

    db = Sesion()
    assert db.query(DescargaFactible).one().estado == "FINALIZADO"
    db.close()
    assert (carpeta / f"{CLAVE_FACTURA}.xml").exists()


def test_consulta_en_curso_anota_estado_de_factible(entorno):
    Sesion, _ = entorno
    _descarga_pendiente(Sesion)
    descarga_factible.ejecutar_ciclo(_config(), ClienteFijo(RespuestaConsulta(False, "", None, "Token en estado PROCESANDO")))

    db = Sesion()
    d = db.query(DescargaFactible).one()
    assert d.estado == "PENDIENTE" and "Token en estado PROCESANDO" in d.mensaje
    db.close()


def test_pendiente_mas_de_2_horas_avisa_pero_no_se_abandona(entorno, monkeypatch):
    Sesion, _ = entorno
    creado = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)  # 11:00 Ecuador
    _descarga_pendiente(Sesion, creado_en=creado)
    cliente = ClienteFijo(RespuestaConsulta(True, "PENDIENTE", None, "Token en estado PENDIENTE"))

    monkeypatch.setattr(descarga_factible, "_ahora", lambda: datetime(2026, 10, 22, 12, 59, tzinfo=descarga_factible.ZONA_ECUADOR))
    descarga_factible.ejecutar_ciclo(_config(), cliente)
    db = Sesion()
    d = db.query(DescargaFactible).one()
    assert d.estado == "PENDIENTE" and "2 horas" not in d.mensaje
    db.close()

    monkeypatch.setattr(descarga_factible, "_ahora", lambda: datetime(2026, 10, 22, 13, 1, tzinfo=descarga_factible.ZONA_ECUADOR))
    descarga_factible.ejecutar_ciclo(_config(), cliente)
    db = Sesion()
    d = db.query(DescargaFactible).one()
    assert d.estado == "PENDIENTE" and "2 horas" in d.mensaje and "t-1" in d.mensaje
    db.close()


class ClientePorToken(ClienteFijo):
    """Responde la consulta segun el token; generar siempre dice 'ya existe un token pendiente'."""

    def __init__(self, consultas: dict[str, RespuestaConsulta]):
        super().__init__(generar=RespuestaGenerar(False, None, "", 0, "Ya existe un token pendiente."))
        self.consultas = consultas
        self.consultados: list[str] = []

    def consultar(self, ruc_empresa, token):
        self.consultados.append(token)
        return self.consultas[token]


def _descarga_abandonada(Sesion, token: str, fecha: date, mensaje: str = "se cancelo") -> None:
    db = Sesion()
    db.add(DescargaFactible(ruc_empresa=RUC, fecha_documentos=fecha, token=token, estado="ERROR",
                            origen="manual", usuario="admin", mensaje=mensaje))
    db.commit()
    db.close()


def test_token_pendiente_retoma_tokens_registrados(entorno):
    Sesion, carpeta = entorno
    zip_base64 = base64.b64encode(_zip({"f.xml": _factura_xml(CLAVE_FACTURA)})).decode()
    _descarga_abandonada(Sesion, "vencido", date(2026, 10, 1))
    _descarga_abandonada(Sesion, "listo", date(2026, 10, 19))
    _descarga_abandonada(Sesion, "trabado", date(2026, 10, 13))
    cliente = ClientePorToken({
        "vencido": RespuestaConsulta(False, "", None, "No se encontró token."),
        "listo": RespuestaConsulta(True, "FINALIZADO", zip_base64, ""),
        "trabado": RespuestaConsulta(True, "PENDIENTE", None, "Token en estado PENDIENTE"),
    })

    db = Sesion()
    d = descarga_factible.solicitar_descarga(db, cliente, RUC, date(2026, 10, 15), "manual", "nicole")
    assert d.token == "trabado" and d.estado == "PENDIENTE" and "13/10/2026" in d.mensaje
    assert db.query(DescargaFactible).count() == 3  # no se registra un ERROR nuevo para el 15/10
    estados = {x.token: x.estado for x in db.query(DescargaFactible)}
    assert estados == {"vencido": "ERROR", "listo": "PENDIENTE", "trabado": "PENDIENTE"}
    db.close()

    descarga_factible.ejecutar_ciclo(_config(), cliente)  # baja el ZIP del 19/10
    assert (carpeta / f"{CLAVE_FACTURA}.xml").exists()

    # El token que Factible ya no reconoce no se vuelve a consultar.
    cliente.consultados.clear()
    db = Sesion()
    descarga_factible.retomar_tokens_registrados(db, cliente, RUC)
    db.close()
    assert "vencido" not in cliente.consultados


def test_token_no_encontrado_cierra_la_descarga(entorno):
    Sesion, _ = entorno
    _descarga_pendiente(Sesion)
    descarga_factible.ejecutar_ciclo(_config(), ClienteFijo(RespuestaConsulta(False, "", None, "No se encontró token.")))

    db = Sesion()
    d = db.query(DescargaFactible).one()
    assert d.estado == "ERROR" and "ya no reconoce el token" in d.mensaje
    db.close()


def test_token_pendiente_ajeno_reutiliza_la_fila(entorno):
    Sesion, _ = entorno
    cliente = ClienteFijo(generar=RespuestaGenerar(False, None, "", 0, "Ya existe un token pendiente."))
    db = Sesion()
    primera = descarga_factible.solicitar_descarga(db, cliente, RUC, date(2026, 10, 20), "automatica", "sistema")
    segunda = descarga_factible.solicitar_descarga(db, cliente, RUC, date(2026, 10, 20), "automatica", "sistema")
    assert primera.id == segunda.id
    assert db.query(DescargaFactible).count() == 1
    assert "Ultimo intento" in segunda.mensaje
    db.close()


def test_fecha_sin_documentos_no_es_error(entorno):
    Sesion, _ = entorno
    db = Sesion()
    d = descarga_factible.solicitar_descarga(db, SimuladorFactible(), RUC, date(2026, 8, 7), "manual", "nicole")
    assert d.estado == "SIN_DOCUMENTOS"
    db.close()


def test_no_pide_otro_token_si_ya_hay_uno_en_curso(entorno):
    Sesion, _ = entorno
    simulador = SimuladorFactible({"07/08/2026": {"f.xml": _factura_xml(CLAVE_FACTURA)}}, consultas_hasta_finalizar=5)
    db = Sesion()
    descarga_factible.solicitar_descarga(db, simulador, RUC, date(2026, 8, 7), "manual", "nicole")
    segunda = descarga_factible.solicitar_descarga(db, simulador, RUC, date(2026, 8, 7), "manual", "nicole")
    assert segunda is None
    assert db.query(DescargaFactible).count() == 1  # no deja un ERROR por cada clic
    db.close()


def test_automatica_espera_si_hay_descarga_en_curso(entorno, monkeypatch):
    Sesion, _ = entorno
    monkeypatch.setattr(descarga_factible, "_ahora", lambda: datetime(2026, 10, 22, 9, 0, tzinfo=descarga_factible.ZONA_ECUADOR))
    simulador = SimuladorFactible({"19/10/2026": {"f.xml": _factura_xml(CLAVE_FACTURA)}}, consultas_hasta_finalizar=10)
    db = Sesion()
    descarga_factible.solicitar_descarga(db, simulador, RUC, date(2026, 10, 19), "manual", "admin")
    db.close()

    config = _config(descarga_automatica=True, hora_descarga=datetime(2026, 1, 1, 7, 0).time())
    descarga_factible.ejecutar_ciclo(config, simulador)
    db = Sesion()
    assert db.query(DescargaFactible).count() == 1
    db.close()


def test_descarga_automatica_una_vez_por_dia(entorno, monkeypatch):
    Sesion, _ = entorno
    ahora = datetime(2026, 10, 21, 8, 0, tzinfo=descarga_factible.ZONA_ECUADOR)
    monkeypatch.setattr(descarga_factible, "_ahora", lambda: ahora)
    ayer = "20/10/2026"
    simulador = SimuladorFactible({ayer: {"f.xml": _factura_xml(CLAVE_FACTURA)}})
    config = _config(descarga_automatica=True, hora_descarga=datetime(2026, 1, 1, 7, 0).time())

    descarga_factible.ejecutar_ciclo(config, simulador)  # genera token
    descarga_factible.ejecutar_ciclo(config, simulador)  # consulta -> FINALIZADO
    descarga_factible.ejecutar_ciclo(config, simulador)  # no vuelve a pedir

    db = Sesion()
    descargas = db.query(DescargaFactible).all()
    assert len(descargas) == 1
    assert descargas[0].origen == "automatica" and descargas[0].estado == "FINALIZADO"
    assert descargas[0].fecha_documentos == date(2026, 10, 20)
    db.close()


def test_descarga_automatica_espera_la_hora(entorno, monkeypatch):
    Sesion, _ = entorno
    monkeypatch.setattr(descarga_factible, "_ahora", lambda: datetime(2026, 10, 21, 6, 59, tzinfo=descarga_factible.ZONA_ECUADOR))
    config = _config(descarga_automatica=True, hora_descarga=datetime(2026, 1, 1, 7, 0).time())
    descarga_factible.ejecutar_ciclo(config, SimuladorFactible())

    db = Sesion()
    assert db.query(DescargaFactible).count() == 0
    db.close()


def test_descarga_automatica_reintenta_error_tras_30_min(entorno, monkeypatch):
    Sesion, _ = entorno
    db = Sesion()
    a_las_8 = datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc)  # 08:00 Ecuador
    db.add(DescargaFactible(ruc_empresa=RUC, fecha_documentos=date(2026, 10, 20), estado="ERROR", origen="automatica",
                            usuario="sistema", creado_en=a_las_8, actualizado_en=a_las_8))
    db.commit()
    db.close()
    config = _config(descarga_automatica=True, hora_descarga=datetime(2026, 1, 1, 7, 0).time())

    monkeypatch.setattr(descarga_factible, "_ahora", lambda: datetime(2026, 10, 21, 8, 10, tzinfo=descarga_factible.ZONA_ECUADOR))
    descarga_factible.ejecutar_ciclo(config, SimuladorFactible())
    db = Sesion()
    assert db.query(DescargaFactible).count() == 1
    db.close()

    monkeypatch.setattr(descarga_factible, "_ahora", lambda: datetime(2026, 10, 21, 8, 31, tzinfo=descarga_factible.ZONA_ECUADOR))
    descarga_factible.ejecutar_ciclo(config, SimuladorFactible())
    db = Sesion()
    assert db.query(DescargaFactible).count() == 2
    db.close()


# ---------- ruta del panel ----------

def test_boton_descargar_ahora_registra_descarga_y_auditoria(entorno, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main_module
    from app.core.auth import hash_contrasena
    from app.core.models import HistorialVigilancia, Usuario

    Sesion, _ = entorno
    monkeypatch.setattr(main_module, "SessionLocal", Sesion)
    monkeypatch.setattr(vigilante_carpeta, "SessionLocal", Sesion)
    monkeypatch.setenv("FACTIBLE_MODO", "simulador")
    monkeypatch.setattr(descarga_factible, "_cliente", None)
    db = Sesion()
    db.add(Usuario(nombre_usuario="nicole", hash_contrasena=hash_contrasena("x-123"), rol="contadora", activo=True))
    db.commit()
    db.close()

    client = TestClient(main_module.app)
    client.post("/login", data={"usuario": "nicole", "contrasena": "x-123"})
    r = client.post("/admin/factible/descargar", data={"fecha": "2026-10-20"}, follow_redirects=False)
    assert r.status_code == 303

    panel = client.get("/panel-vigilancia")
    assert "Descarga desde Factible" in panel.text and "Sin documentos" in panel.text

    db = Sesion()
    descarga = db.query(DescargaFactible).one()
    assert descarga.usuario == "nicole"
    assert db.query(HistorialVigilancia).one().accion == "descargar_factible"
    db.close()

    estado = client.get(f"/admin/factible/estado/{descarga.id}").json()
    assert estado["estado"] == "SIN_DOCUMENTOS" and estado["pendientes_carpeta"] == 0
    assert client.get("/admin/factible/estado/999").status_code == 404
    monkeypatch.setattr(descarga_factible, "_cliente", None)
