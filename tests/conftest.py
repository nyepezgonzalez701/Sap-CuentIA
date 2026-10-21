import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.config import Base
# Los modelos deben estar importados para que Base los conozca al crear las tablas.
from app.core.models import Factura, Proveedor, CuentaContable, FacturaDetalleLinea, HistorialAccion  # noqa: F401


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
