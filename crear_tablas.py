from app.db.config import Base, engine
from app.core.models import Factura

Base.metadata.create_all(bind=engine)
print("Tabla 'facturas' creada correctamente.")