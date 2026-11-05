import base64
import io
import uuid
import zipfile

from app.adaptadores.factible.base import (
    ClienteFactible,
    ESTADO_FINALIZADO,
    ESTADO_PENDIENTE,
    RespuestaConsulta,
    RespuestaGenerar,
)

# Simula el ApiRest de Factible sin red, igual que SimuladorSRI/SimuladorSAP.
# Sirve para las pruebas y para usar la app con FACTIBLE_MODO=simulador mientras
# QA no tiene documentos recibidos cargados para el RUC.
#
# documentos_por_fecha: {"dd/MM/yyyy": {"nombre.xml": b"<xml...>"}}
# consultas_hasta_finalizar: cuantas consultas responden PENDIENTE antes de FINALIZADO.


class SimuladorFactible(ClienteFactible):
    def __init__(self, documentos_por_fecha: dict[str, dict[str, bytes]] | None = None, consultas_hasta_finalizar: int = 1):
        self.documentos_por_fecha = documentos_por_fecha or {}
        self.consultas_hasta_finalizar = consultas_hasta_finalizar
        self._tokens: dict[str, dict] = {}  # token -> {"ruc", "fecha", "consultas"}

    def _hay_pendiente(self, ruc: str) -> bool:
        return any(t["ruc"] == ruc and t["consultas"] < self.consultas_hasta_finalizar for t in self._tokens.values())

    def generar_recepcion(self, ruc_empresa: str, fecha: str) -> RespuestaGenerar:
        if self._hay_pendiente(ruc_empresa):
            return RespuestaGenerar(False, None, "", 0, "Ya existe un token pendiente.")
        documentos = self.documentos_por_fecha.get(fecha)
        if not documentos:
            return RespuestaGenerar(False, None, "", 0, f"No se encontraron claves de acceso para la fecha {fecha}.")
        token = str(uuid.uuid4())
        self._tokens[token] = {"ruc": ruc_empresa, "fecha": fecha, "consultas": 0}
        return RespuestaGenerar(
            True, token, ESTADO_PENDIENTE, len(documentos),
            f"Token generado correctamente. Se encontraron {len(documentos)} claves para la fecha {fecha}.",
        )

    def consultar(self, ruc_empresa: str, token: str) -> RespuestaConsulta:
        datos = self._tokens.get(token)
        if datos is None or datos["ruc"] != ruc_empresa:
            return RespuestaConsulta(False, "", None, "No se encontró token.")
        datos["consultas"] += 1
        if datos["consultas"] < self.consultas_hasta_finalizar:
            return RespuestaConsulta(True, ESTADO_PENDIENTE, None, "")

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            for nombre, contenido in self.documentos_por_fecha[datos["fecha"]].items():
                z.writestr(nombre, contenido)
        return RespuestaConsulta(True, ESTADO_FINALIZADO, base64.b64encode(buffer.getvalue()).decode(), "")
