"""
Prepara el entorno de QA de cero: crea todas las tablas, un usuario de prueba
y datos base (proveedores/cuentas de ejemplo) en la base definida en .env.qa.

No toca .env ni la base de desarrollo -- lee .env.qa con override, sin
importar si ya hay un DATABASE_URL distinto cargado en la sesion.

Uso:
    python setup_entorno_qa.py
"""
from dotenv import load_dotenv

load_dotenv(".env.qa", override=True)

from app.db.config import Base, engine, SessionLocal  # noqa: E402
from app.core.models import (  # noqa: E402, F401
    Factura,
    Proveedor,
    CuentaContable,
    FacturaDetalleLinea,
    HistorialAccion,
    Usuario,
    EstadoVigilante,
    HistorialVigilancia,
)
from app.core.auth import hash_contrasena  # noqa: E402

USUARIO_QA = "qa"
CONTRASENA_QA = "Qa12345"


def crear_tablas():
    Base.metadata.create_all(bind=engine)
    print(f"Tablas creadas/verificadas en: {engine.url.render_as_string(hide_password=False)}")


def crear_usuario_qa():
    db = SessionLocal()
    try:
        if db.query(Usuario).filter(Usuario.nombre_usuario == USUARIO_QA).first():
            print(f"Usuario '{USUARIO_QA}' ya existe, se omite.")
            return
        db.add(Usuario(
            nombre_usuario=USUARIO_QA,
            hash_contrasena=hash_contrasena(CONTRASENA_QA),
            rol="administrador",
            activo=True,
        ))
        db.commit()
        print(f"Usuario '{USUARIO_QA}' creado (contrasena: {CONTRASENA_QA})")
    finally:
        db.close()


def cargar_datos_ejemplo():
    db = SessionLocal()
    try:
        proveedores = [
            Proveedor(ruc="1790012345001", razon_social="PROVEEDOR DE EJEMPLO S.A.", estado="activo"),
            Proveedor(ruc="1790099999001", razon_social="PROVEEDOR INACTIVO CIA. LTDA.", estado="inactivo"),
        ]
        for p in proveedores:
            if not db.query(Proveedor).filter(Proveedor.ruc == p.ruc).first():
                db.add(p)
                print(f"Proveedor agregado: {p.ruc} - {p.razon_social}")

        cuentas = [
            CuentaContable(codigo="5.1.01", nombre="Gastos de administracion", activa=True),
            CuentaContable(codigo="5.1.02", nombre="Gastos varios", activa=True),
            CuentaContable(codigo="5.1.99", nombre="Gastos por clasificar", activa=True),
        ]
        for c in cuentas:
            if not db.query(CuentaContable).filter(CuentaContable.codigo == c.codigo).first():
                db.add(c)
                print(f"Cuenta agregada: {c.codigo} - {c.nombre}")

        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    crear_tablas()
    crear_usuario_qa()
    cargar_datos_ejemplo()
    print("\nEntorno de QA listo.")
    print("Para levantar la app contra esta base, en la misma sesion de PowerShell:")
    print("  uvicorn app.main:app --reload --port 8001")
