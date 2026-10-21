from datetime import date
from app.db.config import SessionLocal
from app.core.models import Factura

db = SessionLocal()

factura_ejemplo = Factura(
    ruc_proveedor="1790012345001",
    numero_factura="001-001-000000123",
    fecha_emision=date(2026, 8, 15),
    subtotal=100.00,
    impuestos=15.00,
    total=115.00,
    estado="pendiente",
)

db.add(factura_ejemplo)
db.commit()
db.refresh(factura_ejemplo)



print(f"Factura guardada con id: {factura_ejemplo.id}")
db.close()