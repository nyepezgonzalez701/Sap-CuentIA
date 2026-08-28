from app.db.config import SessionLocal
from app.core.models import Proveedor

db = SessionLocal()

proveedores_ejemplo = [
    Proveedor(ruc="1790012345001", razon_social="PROVEEDOR DE EJEMPLO S.A.", estado="activo"),
    Proveedor(ruc="1790099999001", razon_social="PROVEEDOR INACTIVO CIA. LTDA.", estado="inactivo"),
]

for p in proveedores_ejemplo:
    existente = db.query(Proveedor).filter(Proveedor.ruc == p.ruc).first()
    if existente is None:
        db.add(p)
        print(f"Agregado: {p.ruc} - {p.razon_social} ({p.estado})")
    else:
        print(f"Ya existia, se omite: {p.ruc}")

db.commit()
db.close()
print("Listo.")