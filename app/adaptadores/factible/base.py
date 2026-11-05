from abc import ABC, abstractmethod
from dataclasses import dataclass

# Contrato del ApiRest de Factible (ConsultaDocumento) para descargar los XML de
# documentos RECIBIDOS. Ver las fichas tecnicas "Generacion de token por recepcion"
# y "Consulta de token y descarga de respuesta".
#
# Flujo: generar_recepcion(fecha) -> token PENDIENTE -> consultar(token) hasta
# FINALIZADO -> el ZIP llega en Base64 en DocumentoZip.

ESTADO_PENDIENTE = "PENDIENTE"
ESTADO_PROCESANDO = "PROCESANDO"  # no esta en la ficha, pero Factible lo devuelve mientras arma el ZIP
ESTADO_FINALIZADO = "FINALIZADO"
ESTADOS_EN_CURSO = (ESTADO_PENDIENTE, ESTADO_PROCESANDO)


class ErrorFactible(Exception):
    """Falla de comunicacion o autenticacion con Factible (red, HTTP 5xx, 401 persistente)."""
    pass


@dataclass
class RespuestaGenerar:
    exito: bool
    token: str | None
    estado: str
    total_claves: int
    mensaje: str

    @property
    def sin_documentos(self) -> bool:
        return not self.exito and "no se encontraron claves" in self.mensaje.lower()

    @property
    def ya_hay_token_pendiente(self) -> bool:
        # La API NO devuelve cual es el token pendiente (Token viene null), por eso
        # el token se guarda en la tabla descargas_factible apenas se genera.
        return not self.exito and "token pendiente" in self.mensaje.lower()


@dataclass
class RespuestaConsulta:
    exito: bool
    estado: str
    documento_zip_base64: str | None
    mensaje: str

    @property
    def en_curso(self) -> bool:
        # Factible puede responder Success=false con Mensaje "Token en estado PROCESANDO"
        # y Estado vacio, asi que tambien se mira el mensaje.
        if self.estado in ESTADOS_EN_CURSO:
            return True
        mensaje = self.mensaje.lower()
        return any(f"estado {e.lower()}" in mensaje for e in ESTADOS_EN_CURSO)

    @property
    def token_no_encontrado(self) -> bool:
        # "No se encontró token." -- Factible descarto o vencio el token; no tiene sentido reintentarlo.
        mensaje = self.mensaje.lower()
        return not self.exito and "token" in mensaje and ("no se encontró" in mensaje or "no se encontro" in mensaje)


class ClienteFactible(ABC):
    @abstractmethod
    def generar_recepcion(self, ruc_empresa: str, fecha: str) -> RespuestaGenerar:
        """fecha en formato dd/MM/yyyy (estricto, segun la ficha)."""
        ...

    @abstractmethod
    def consultar(self, ruc_empresa: str, token: str) -> RespuestaConsulta:
        ...
