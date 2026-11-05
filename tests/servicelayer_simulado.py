"""
Servidor simulado del Service Layer de SAP Business One, solo para pruebas. Escucha
unicamente en 127.0.0.1, con HTTP y cookies reales. Imita el contrato conocido; NO
demuestra que un SAP real se comporte igual (ver puntos pendientes en el README).

A mano:  python -m tests.servicelayer_simulado --puerto 50000 --proveedor RUC=CARDCODE
"""
import argparse
import json
import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

COMPANY_DB = "SBO_SIMULADA"
USUARIO = "usuario-simulado"
CLAVE = "clave-simulada"
RUC_PROVEEDOR = "1790012345001"
CARD_CODE_PROVEEDOR = "P0001"
CUENTA_INEXISTENTE = "999999"

CAMPO_RUC = "FederalTaxID"
CAMPO_CUENTA = "AccountCode"
CAMPO_NUMERO = "NumAtCard"

CODIGO_VALIDACION = -10
CODIGO_REPETIDA = -5009


@dataclass
class Fallo:
    codigo: int
    http: int
    mensaje: str
    metodo: str
    recurso: str


@dataclass
class EstadoSimulado:
    proveedores: list[dict] = field(
        default_factory=lambda: [{CAMPO_RUC: RUC_PROVEEDOR, "CardCode": CARD_CODE_PROVEEDOR, "CardType": "cSupplier"}]
    )
    facturas: list[dict] = field(default_factory=list)
    sesiones_activas: set[str] = field(default_factory=set)
    sesiones_emitidas: list[str] = field(default_factory=list)
    peticiones: list[tuple[str, str, str]] = field(default_factory=list)  # metodo, recurso, $filter
    fallos: deque[Fallo] = field(default_factory=deque)
    codigo_sesion_expirada: int = -5002
    tasa_iva: Decimal = Decimal("0.15")
    cortar_tras_crear: bool = False
    cortar_sin_crear: bool = False
    siguiente_doc: int = 100

    def forzar_fallo(self, codigo: int, http: int = 400, mensaje: str = "Error simulado",
                     metodo: str = "POST", recurso: str = "PurchaseInvoices", veces: int = 1) -> None:
        for _ in range(veces):
            self.fallos.append(Fallo(codigo, http, mensaje, metodo, recurso))

    def expirar_sesiones(self) -> None:
        self.sesiones_activas.clear()

    def contar(self, metodo: str, recurso: str) -> int:
        return sum(1 for m, r, _ in self.peticiones if m == metodo and r == recurso)


def _condiciones(filtro: str) -> dict[str, str]:
    return {
        campo: valor.replace("''", "'")
        for campo, valor in re.findall(r"(\w+) eq '((?:[^']|'')*)'", filtro)
    }


def crear_manejador(estado: EstadoSimulado):
    class Manejador(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _json(self, http: int, cuerpo: dict, cookies: list[str] | None = None) -> None:
            datos = json.dumps(cuerpo).encode()
            self.send_response(http)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(datos)))
            for cookie in cookies or []:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(datos)

        def _error(self, http: int, codigo: int, mensaje: str) -> None:
            self._json(http, {"error": {"code": str(codigo), "message": mensaje}})

        def _cuerpo(self) -> dict:
            largo = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(largo) or b"{}")

        def _sesion_valida(self) -> bool:
            cookie = SimpleCookie(self.headers.get("Cookie") or "").get("B1SESSION")
            return cookie is not None and cookie.value in estado.sesiones_activas

        def _atender(self, metodo: str) -> None:
            partes = urlsplit(self.path)
            cuerpo = self._cuerpo() if metodo == "POST" else {}
            if not partes.path.startswith("/b1s/v2/"):
                return self._error(404, -1, "Ruta desconocida")
            recurso = partes.path.removeprefix("/b1s/v2/")
            filtro = (parse_qs(partes.query).get("$filter") or [""])[0]
            estado.peticiones.append((metodo, recurso, filtro))

            if (metodo, recurso) == ("POST", "Login"):
                return self._login(cuerpo)
            if not self._sesion_valida():
                return self._error(401, estado.codigo_sesion_expirada, "Sesion expirada")
            if estado.fallos and (estado.fallos[0].metodo, estado.fallos[0].recurso) == (metodo, recurso):
                fallo = estado.fallos.popleft()
                return self._error(fallo.http, fallo.codigo, fallo.mensaje)

            if (metodo, recurso) == ("GET", "BusinessPartners"):
                return self._consultar(filtro, estado.proveedores, {CAMPO_RUC, "CardType"})
            if (metodo, recurso) == ("GET", "PurchaseInvoices"):
                return self._consultar(filtro, estado.facturas, {"CardCode", CAMPO_NUMERO})
            if (metodo, recurso) == ("POST", "PurchaseInvoices"):
                return self._crear_factura(cuerpo)
            return self._error(404, -1, "Recurso desconocido")

        def _login(self, cuerpo: dict) -> None:
            if (cuerpo.get("CompanyDB"), cuerpo.get("UserName"), cuerpo.get("Password")) != (COMPANY_DB, USUARIO, CLAVE):
                return self._error(401, 100, "Credenciales incorrectas")
            sesion = str(uuid.uuid4())
            estado.sesiones_activas.add(sesion)
            estado.sesiones_emitidas.append(sesion)
            self._json(
                200,
                {"SessionId": sesion, "Version": "simulada", "SessionTimeout": 30},
                cookies=[f"B1SESSION={sesion}; Path=/; HttpOnly"],
            )

        def _consultar(self, filtro: str, filas: list[dict], campos_validos: set[str]) -> None:
            condiciones = _condiciones(filtro)
            desconocidos = set(condiciones) - campos_validos
            if desconocidos:
                return self._error(400, CODIGO_VALIDACION, f"Propiedad invalida: {sorted(desconocidos)}")
            coincidencias = [f for f in filas if all(f.get(c) == v for c, v in condiciones.items())]
            self._json(200, {"value": coincidencias})

        def _crear_factura(self, cuerpo: dict) -> None:
            if estado.cortar_sin_crear:
                estado.cortar_sin_crear = False
                self.close_connection = True
                return

            lineas = cuerpo.get("DocumentLines") or []
            if not (cuerpo.get("DocDate") and lineas):
                return self._error(400, CODIGO_VALIDACION, "Faltan DocDate o DocumentLines")
            if not any(p["CardCode"] == cuerpo.get("CardCode") for p in estado.proveedores):
                return self._error(400, CODIGO_VALIDACION, "Socio de negocios inexistente")
            if any(l.get(CAMPO_CUENTA) in (None, CUENTA_INEXISTENTE) for l in lineas):
                return self._error(400, CODIGO_VALIDACION, "Cuenta contable invalida")
            if any(
                f["CardCode"] == cuerpo["CardCode"] and f.get(CAMPO_NUMERO) == cuerpo.get(CAMPO_NUMERO)
                for f in estado.facturas
            ):
                return self._error(400, CODIGO_REPETIDA, "Factura de proveedor repetida")

            estado.siguiente_doc += 1
            subtotal = sum(Decimal(str(l["LineTotal"])) for l in lineas)
            factura = {
                **cuerpo,
                "DocEntry": estado.siguiente_doc,
                "DocNum": estado.siguiente_doc + 5000,
                "DocTotal": float(round(subtotal * (1 + estado.tasa_iva), 2)),
            }
            estado.facturas.append(factura)

            if estado.cortar_tras_crear:
                estado.cortar_tras_crear = False
                self.close_connection = True
                return
            self._json(201, factura)

        def do_GET(self):
            self._atender("GET")

        def do_POST(self):
            self._atender("POST")

    return Manejador


def iniciar_servidor(estado: EstadoSimulado, puerto: int = 0) -> tuple[ThreadingHTTPServer, str]:
    servidor = ThreadingHTTPServer(("127.0.0.1", puerto), crear_manejador(estado))
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    return servidor, f"http://127.0.0.1:{servidor.server_address[1]}/b1s/v2/"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--puerto", type=int, default=50000)
    parser.add_argument("--proveedor", action="append", default=[], help="RUC=CARDCODE; se puede repetir")
    args = parser.parse_args()

    estado = EstadoSimulado()
    for par in args.proveedor:
        ruc, card_code = par.split("=", 1)
        estado.proveedores.append({CAMPO_RUC: ruc, "CardCode": card_code, "CardType": "cSupplier"})

    servidor, url = iniciar_servidor(estado, args.puerto)
    print(f"Service Layer SIMULADO en {url} (Ctrl+C para salir)")
    print(f"SAP_URL_BASE={url}\nSAP_COMPANY_DB={COMPANY_DB}\nSAP_USUARIO={USUARIO}\nSAP_CLAVE={CLAVE}")
    print(f"SAP_CAMPO_RUC_PROVEEDOR={CAMPO_RUC}\nSAP_CAMPO_CUENTA_LINEA={CAMPO_CUENTA}\nSAP_CAMPO_NUMERO_FACTURA={CAMPO_NUMERO}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        servidor.shutdown()


if __name__ == "__main__":
    main()
