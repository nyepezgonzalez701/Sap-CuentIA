from app.adaptadores.sri.base import ClienteSRI, ResultadoValidacionSRI

# Simula la respuesta del webservice de Autorizacion del SRI
# (AutorizacionComprobantesOffline -> autorizacionComprobante) sin llamarlo de verdad.
# Mismo espiritu que SimuladorSAP: sirve para probar el flujo y la pantalla mientras
# no hay credenciales/certificado para conectar al SRI real.

CLAVES_NO_AUTORIZADAS_EN_SRI = {
    "2108202601179009999900110010010000009991234567810": "Clave de acceso no registrada en el SRI (comprobante no encontrado).",
}


class SimuladorSRI(ClienteSRI):
    def consultar_autorizacion(self, clave_acceso: str) -> ResultadoValidacionSRI:
        if not clave_acceso or len(clave_acceso) != 49:
            return ResultadoValidacionSRI(
                autorizado=False,
                estado="NO ENCONTRADO",
                mensaje="La clave de acceso no tiene el formato esperado (49 digitos).",
            )

        # Simula que el SRI no tiene registro de la clace de aceo

        if clave_acceso in CLAVES_NO_AUTORIZADAS_EN_SRI:
            return ResultadoValidacionSRI(
                autorizado=False,
                estado="NO AUTORIZADO",
                mensaje=CLAVES_NO_AUTORIZADAS_EN_SRI[clave_acceso],

            )

        # Resulatdo de la validcion del SRI
        

        return ResultadoValidacionSRI(
            autorizado=True,
            estado="AUTORIZADO",
            mensaje="Comprobante autorizado por el SRI.",
        )
