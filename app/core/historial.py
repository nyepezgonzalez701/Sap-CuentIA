from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.models import HistorialAccion, Factura

FILAS_POR_PAGINA = 25


@dataclass
class FilaHistorial:
    id: int
    accion: str
    usuario: str
    cuenta_utilizada: Optional[str]
    motivo: Optional[str]
    creado_en: datetime
    factura_id: int
    numero_factura: str
    ruc_proveedor: str


@dataclass
class PaginaHistorial:
    filas: list[FilaHistorial]
    pagina: int
    hay_pagina_siguiente: bool


def obtener_historial_paginado(db: Session, pagina: int = 1) -> PaginaHistorial:
    """
    Trae historial_acciones + facturas en una sola query (join) para evitar
    N+1. Pide FILAS_POR_PAGINA + 1 filas para saber si hay pagina siguiente
    sin necesitar un COUNT(*) sobre una tabla que solo crece.
    """
    pagina = max(pagina, 1)
    offset = (pagina - 1) * FILAS_POR_PAGINA

    filas_query = (
        db.query(
            HistorialAccion.id,
            HistorialAccion.accion,
            HistorialAccion.usuario,
            HistorialAccion.cuenta_utilizada,
            HistorialAccion.motivo,
            HistorialAccion.creado_en,
            Factura.id.label("factura_id"),
            Factura.numero_factura,
            Factura.ruc_proveedor,
        )
        .join(Factura, Factura.id == HistorialAccion.factura_id)
        .order_by(HistorialAccion.creado_en.desc())
        .offset(offset)
        .limit(FILAS_POR_PAGINA + 1)
        .all()
    )

    hay_pagina_siguiente = len(filas_query) > FILAS_POR_PAGINA
    filas_query = filas_query[:FILAS_POR_PAGINA]

    filas = [FilaHistorial(**fila._asdict()) for fila in filas_query]
    return PaginaHistorial(filas=filas, pagina=pagina, hay_pagina_siguiente=hay_pagina_siguiente)
