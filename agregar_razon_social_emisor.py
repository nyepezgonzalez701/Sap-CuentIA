from sqlalchemy import text

from app.db.config import engine

with engine.begin() as conn:
    conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS razon_social_emisor VARCHAR"))

print("Listo: columna 'razon_social_emisor' verificada en facturas.")
