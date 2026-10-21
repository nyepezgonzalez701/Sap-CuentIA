from sqlalchemy import text

from app.db.config import engine

with engine.begin() as conn:
    conn.execute(text("ALTER TABLE proveedores ADD COLUMN IF NOT EXISTS correo VARCHAR"))
    conn.execute(text("ALTER TABLE proveedores ADD COLUMN IF NOT EXISTS saldo NUMERIC(12, 2) NOT NULL DEFAULT 0"))

print("Listo: columnas 'correo' y 'saldo' verificadas en proveedores.")
