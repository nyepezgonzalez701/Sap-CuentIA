from sqlalchemy.orm import Session

from app.adaptadores.asesor_ia.base import SugeridorCuenta, SugerenciaCuenta
from app.core.models import CuentaContable


REGLAS_POR_RUC = {
    "1790012345001": "5.1.01",  # Proveedor de ejemplo -> Gastos de administracion
    "1790099999001": "5.1.02",  # Proveedor inactivo -> Gastos varios
}

CUENTA_GENERICA_CODIGO = "5.1.99"  # Gastos por clasificar
# Ejemplo de Simulador con IA falta coenctar al flujo real 


class SugeridorSimulado(SugeridorCuenta):
    def __init__(self, db: Session):
        self.db = db

    def sugerir(self, ruc_proveedor: str, razon_social: str) -> SugerenciaCuenta | None:
        codigo_propuesto = REGLAS_POR_RUC.get(ruc_proveedor, CUENTA_GENERICA_CODIGO)
        confianza = "alta" if ruc_proveedor in REGLAS_POR_RUC else "baja"

        cuenta = (
            self.db.query(CuentaContable)
            .filter(
                CuentaContable.codigo == codigo_propuesto,
                CuentaContable.activa == True,  # noqa: E712
                CuentaContable.tipo_cuenta == "D",
            )
            .first()
        )

        if cuenta is None:
            return None

        return SugerenciaCuenta(codigo=cuenta.codigo, nombre=cuenta.nombre, confianza=confianza)

   