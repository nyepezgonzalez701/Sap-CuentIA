from sqlalchemy import text

from app.db.config import engine

with engine.begin() as conn:
    conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS usuario_guardado VARCHAR"))

print("Listo: columna 'usuario_guardado' verificada en facturas.")
