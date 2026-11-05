import random
import string
from datetime import date

from app.adaptadores.sap.base import ClienteSAP, LineaFacturaSAP, ResultadoEnvioSAP


RUCS_BLOQUEADOS_EN_SAP = {
    "1790099999001": "El proveedor no tiene codigo activo en SAP (PN inexistente).",
}

MONTO_MAXIMO_AUTOMATICO = 5000.00


class SimuladorSAP(ClienteSAP):
    def enviar_factura(
        self,
        ruc_proveedor: str,
        numero_factura: str,
        total: float,
        *,
        fecha_emision: date | None = None,
        lineas: list[LineaFacturaSAP] | None = None,
    ) -> ResultadoEnvioSAP:
        if ruc_proveedor in RUCS_BLOQUEADOS_EN_SAP:
            return ResultadoEnvioSAP(
                exitoso=False,
                referencia=None,
                mensaje=RUCS_BLOQUEADOS_EN_SAP[ruc_proveedor],
            )

        if total > MONTO_MAXIMO_AUTOMATICO:
            return ResultadoEnvioSAP(
                exitoso=False,
                referencia=None,
                mensaje=f"El monto (${total:.2f}) supera el limite de aprobacion automatica (${MONTO_MAXIMO_AUTOMATICO:.2f}).",
            )

        folio = "FacPro-" + "".join(random.choices(string.digits, k=5))
        return ResultadoEnvioSAP(
            exitoso=True,
            referencia=folio,
            mensaje="Factura registrada correctamente en SAP.",
        )
