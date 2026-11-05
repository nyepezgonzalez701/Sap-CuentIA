import os

# Antes de importar la app: app.core.auth exige SECRET_KEY al importarse. Asi las pruebas
# no dependen de un .env ni usan la clave real.
os.environ["SECRET_KEY"] = "solo-para-pruebas"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.adaptadores.sap.fabrica as fabrica_sap
from app.db.config import Base
# Los modelos deben estar importados para que Base los conozca al crear las tablas.
from app.core.models import Factura, Proveedor, CuentaContable, FacturaDetalleLinea, HistorialAccion  # noqa: F401


@pytest.fixture(autouse=True)
def _sin_openrouter(monkeypatch):
    # Las pruebas mockean anthropic.Anthropic; si .env trae OPENROUTER_API_KEY se
    # irian por OpenRouter y harian llamadas reales.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _sin_sap_real(monkeypatch):
    # app.db.config carga el .env al importarse: sin esto, un SAP_MODO=servicelayer real
    # en el .env haria que las pruebas apuntaran a ese SAP.
    for nombre in list(os.environ):
        if nombre.startswith("SAP_"):
            monkeypatch.delenv(nombre)
    monkeypatch.setattr(fabrica_sap, "_cliente", None)


@pytest.fixture()
def db_session():
    """
    Sesion contra una base SQLite en memoria, descartable, para no tocar la base
    Postgres real de desarrollo (DATABASE_URL) al correr las pruebas.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
