from sqlalchemy import text

from app.db.config import engine

with engine.begin() as conn:
    conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS exportado_a_excel BOOLEAN NOT NULL DEFAULT FALSE"))
    conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS exportado_en TIMESTAMPTZ"))
    conn.execute(text("ALTER TABLE facturas ADD COLUMN IF NOT EXISTS exportado_por VARCHAR"))

print("Listo: columnas 'exportado_a_excel', 'exportado_en' y 'exportado_por' verificadas en facturas.")
