from app.db.config import Base, engine
from app.core.models import DescargaFactible

Base.metadata.create_all(bind=engine, tables=[DescargaFactible.__table__])
print("Listo: tabla 'descargas_factible' verificada.")
