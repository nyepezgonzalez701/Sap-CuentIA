from sqlalchemy import text

from app.db.config import engine

with engine.begin() as conn:
    conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS clave_acceso VARCHAR"))

print("Listo: columna 'clave_acceso' verificada en facturas.")
