from app.db.config import SessionLocal
from app.core.models import CuentaContable

db = SessionLocal()

cuentas_ejemplo = [
    CuentaContable(codigo="5.1.01", nombre="Gastos de administracion", activa=True),
    CuentaContable(codigo="5.1.02", nombre="Gastos varios", activa=True),
    CuentaContable(codigo="5.1.99", nombre="Gastos por clasificar", activa=True),
]

for c in cuentas_ejemplo:
    existente = db.query(CuentaContable).filter(CuentaContable.codigo == c.codigo).first()
    if existente is None:
        db.add(c)
        print(f"Agregada: {c.codigo} - {c.nombre}")
    else:
        print(f"Ya existia, se omite: {c.codigo}")

db.commit()
db.close()
print("Listo.")