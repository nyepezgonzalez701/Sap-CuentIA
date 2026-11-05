import threading

import httpx

from app.adaptadores.factible.base import (
    ClienteFactible,
    ErrorFactible,
    RespuestaConsulta,
    RespuestaGenerar,
)

# Cliente real del ApiRest de Factible. El host se toma de FACTIBLE_URL_BASE porque
# cambia a mediados de noviembre 2026 (qa-webinterno -> qa-api) manteniendo las rutas.
#
# Login: POST /api/login/authenticate {username, password, ruc}. La ficha no documenta
# la respuesta; probado contra QA el 21/10/2026: {"Message": "<jwt>"}, valido 24 h.
# El JWT se guarda en memoria y se renueva solo cuando la API responde 401.

RUTA_LOGIN = "/api/login/authenticate"
RUTA_GENERAR_RECEPCION = "/api/ConsultaDocumento/generar/recepcion"
RUTA_CONSULTAR = "/api/ConsultaDocumento/consultar"


class ClienteRestFactible(ClienteFactible):
    def __init__(self, url_base: str, usuario: str, clave: str, ruc: str, timeout: float = 120, transport=None):
        self._usuario = usuario
        self._clave = clave
        self._ruc = ruc
        self._http = httpx.Client(base_url=url_base.rstrip("/"), timeout=timeout, transport=transport)
        self._jwt: str | None = None
        self._lock = threading.Lock()

    def _autenticar(self) -> str:
        try:
            r = self._http.post(RUTA_LOGIN, json={"username": self._usuario, "password": self._clave, "ruc": self._ruc})
        except httpx.HTTPError as e:
            raise ErrorFactible(f"No se pudo conectar con Factible (login): {e}")
        if r.status_code != 200:
            raise ErrorFactible(f"Login en Factible rechazado (HTTP {r.status_code}). Revise usuario/clave/RUC en el .env.")
        try:
            jwt = r.json().get("Message")
        except ValueError:
            jwt = None
        if not jwt or jwt.count(".") != 2:
            raise ErrorFactible("Login en Factible no devolvio un JWT en el campo 'Message'.")
        return jwt

    def _post(self, ruta: str, cuerpo: dict) -> dict:
        """POST autenticado. Reintenta UNA vez con login nuevo si la API responde 401
        (JWT ausente/expirado). Los 400 de la ficha traen un JSON valido con
        Success=false y Mensaje, asi que se devuelven tal cual para que el llamador decida."""
        for intento in range(2):
            with self._lock:
                if self._jwt is None or intento == 1:
                    self._jwt = self._autenticar()
                jwt = self._jwt
            try:
                r = self._http.post(ruta, json=cuerpo, headers={"Authorization": f"Bearer {jwt}"})
            except httpx.HTTPError as e:
                raise ErrorFactible(f"No se pudo conectar con Factible ({ruta}): {e}")
            if r.status_code == 401:
                continue
            if r.status_code not in (200, 400):
                raise ErrorFactible(f"Factible respondio HTTP {r.status_code} en {ruta}.")
            try:
                return r.json()
            except ValueError:
                raise ErrorFactible(f"Factible respondio algo que no es JSON en {ruta} (HTTP {r.status_code}).")
        raise ErrorFactible("Factible rechazo la autenticacion (HTTP 401) incluso con un token nuevo.")

    def generar_recepcion(self, ruc_empresa: str, fecha: str) -> RespuestaGenerar:
        d = self._post(RUTA_GENERAR_RECEPCION, {"rucempresa": ruc_empresa, "fecha": fecha})
        return RespuestaGenerar(
            exito=bool(d.get("Success")),
            token=d.get("Token"),
            estado=d.get("Estado") or "",
            total_claves=int(d.get("TotalClaves") or 0),
            mensaje=d.get("Mensaje") or "",
        )

    def consultar(self, ruc_empresa: str, token: str) -> RespuestaConsulta:
        d = self._post(RUTA_CONSULTAR, {"rucempresa": ruc_empresa, "token": token})
        return RespuestaConsulta(
            exito=bool(d.get("Success")),
            estado=d.get("Estado") or "",
            documento_zip_base64=d.get("DocumentoZip"),
            mensaje=d.get("Mensaje") or "",
        )
