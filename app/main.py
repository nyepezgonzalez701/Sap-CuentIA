import html
import os
import urllib.parse
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.core import auth
from app.core import vigilante_carpeta
from app.core import descarga_factible
from app.core.models import DescargaFactible, HistorialVigilancia
from app.core.ingesta import procesar_factura_xml
from app.core.validaciones import (
    validar_proveedor,
    ruc_estructuralmente_valido,
    registrar_proveedor,
    EstadoProveedor,
)
from app.core.aprobaciones import (
    obtener_facturas_pendientes,
    obtener_cuentas_activas,
    obtener_lineas_detalle,
    aprobar_factura,
    aprobar_factura_lineas,
    rechazar_factura,
    guardar_factura_preliminar,
    es_candidata_auto_aprobacion,
    evaluar_elegibilidad_lote,
    filtrar_facturas_pendientes,
    aprobar_lote,
    FiltrosFacturas,
    AccionInvalidaError,
)
from app.core.envios_sap import (
    obtener_facturas_aprobadas_pendientes_envio,
    enviar_a_sap,
    EnvioInvalidoError,
)
from app.core.validacion_sri import validar_en_sri, ValidacionInvalidaError
from app.core.reportes_excel import (
    obtener_filas_para_exportar,
    generar_libro_excel,
    marcar_como_exportadas,
)
from app.core.historial import obtener_historial_paginado, FilaHistorial, PaginaHistorial
from app.db.config import SessionLocal
from app.adaptadores.asesor_ia.base import SugerenciaCuenta
from app.adaptadores.asesor_ia.claude_ia import (
    construir_asiento_contable,
    filtrar_catalogo_por_destino,
    IVA_COMPRAS_CODIGO,
    IVA_COMPRAS_NOMBRE,
    CUENTAS_POR_PAGAR_PROVEEDORES_CODIGO,
    CUENTAS_POR_PAGAR_PROVEEDORES_NOMBRE,
)

app = FastAPI(title="Asistente de facturas")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["css_version"] = lambda: int(
    os.path.getmtime("app/static/style.css")
)
templates.env.globals["logo_version"] = lambda: int(
    os.path.getmtime("app/static/logo.png")
)

ZONA_ECUADOR = ZoneInfo("America/Guayaquil")

RUTAS_PUBLICAS = {"/login"}


class SesionMiddleware(BaseHTTPMiddleware):
    """
    Protege todas las rutas salvo /login y /static/*: sin una cookie de sesion
    valida, redirige a /login en vez de dejar pasar la request. Cuando la sesion
    es valida, deja los datos del usuario (id/nombre_usuario/rol) en
    request.state.usuario para que las rutas y las plantillas los usen.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path in RUTAS_PUBLICAS or request.url.path.startswith("/static"):
            request.state.usuario = None
            return await call_next(request)

        cookie = request.cookies.get(auth.COOKIE_NAME)
        datos_sesion = auth.leer_cookie_sesion(cookie) if cookie else None
        if datos_sesion is None:
            return RedirectResponse(url="/login", status_code=303)

        request.state.usuario = datos_sesion
        return await call_next(request)


app.add_middleware(SesionMiddleware)


@app.on_event("startup")
def _al_arrancar_servidor() -> None:
    # El hilo de vigilancia vive en memoria y no sobrevive a un reinicio del
    # servidor -- el estado real al arrancar siempre es "en_espera".
    vigilante_carpeta.reiniciar_estado_al_arrancar()
    # A diferencia del vigilante, el programador de Factible si arranca solo: la
    # descarga es automatica cada manana (FACTIBLE_DESCARGA_AUTOMATICA/HORA_DESCARGA)
    # y ademas retoma los tokens que quedaron PENDIENTE antes de un reinicio.
    _, mensaje = descarga_factible.iniciar_programador()
    print(f"Factible: {mensaje}")

ETIQUETAS_ESTADO = {
    EstadoProveedor.REGISTRADO: "Proveedor registrado",
    EstadoProveedor.NO_REGISTRADO: "No registrado",
}


@app.get("/", response_class=HTMLResponse)
def inicio(request: Request):
    return templates.TemplateResponse(request, "subir.html")


@app.post("/subir", response_class=HTMLResponse)
async def subir_factura(archivo: UploadFile = File(...)):
    contenido = await archivo.read()

    if not archivo.filename.lower().endswith(".xml"):
        return """<div class="alerta"><strong>Archivo invalido</strong>El archivo debe tener extension .xml</div>"""

    db = SessionLocal()
    try:
        resultado = procesar_factura_xml(db, contenido)
    finally:
        db.close()

    if not resultado.ok:
        return f"""<div class="alerta"><strong>{resultado.mensaje}</strong></div>"""

    datos = resultado.datos
    estado_proveedor = resultado.estado_proveedor
    razon_social_registrada = resultado.razon_social_registrada
    resultado_asiento = resultado.resultado_asiento
    sugerencia = resultado.sugerencia
    items = datos.detalle

    etiqueta = ETIQUETAS_ESTADO[estado_proveedor]
    clase_sello = f"sello-{estado_proveedor.value}"
    razon_mostrada = razon_social_registrada or datos.razon_social

    if items:
        n_clasificadas = sum(1 for l in resultado_asiento["lineas_clasificadas"] if l["codigo_cuenta"])
        fila_cuenta = f"""<div class="ledger-fila"><span class="ledger-label">Lineas de detalle</span><span class="ledger-valor">{n_clasificadas} de {len(items)} clasificadas automaticamente &middot; revisar en la mesa de aprobacion</span></div>"""
    elif sugerencia:
        fila_cuenta = f"""<div class="ledger-fila"><span class="ledger-label">Cuenta sugerida</span><span class="ledger-valor">{sugerencia.codigo} - {sugerencia.nombre}</span></div>"""
    else:
        fila_cuenta = """<div class="ledger-fila"><span class="ledger-label">Cuenta sugerida</span><span class="ledger-valor">Sin sugerencia (revisar catalogo)</span></div>"""

    return f"""
    <div class="registro">
        <span class="sello {clase_sello}">{etiqueta}</span>
        <p class="registro-titulo">Factura electronica valida &middot; pendiente de aprobacion</p>
        <p class="registro-nombre">{razon_mostrada}</p>
        <div class="ledger">
            <div class="ledger-fila"><span class="ledger-label">RUC proveedor</span><span class="ledger-valor">{datos.ruc_proveedor}</span></div>
            <div class="ledger-fila"><span class="ledger-label">Numero de factura</span><span class="ledger-valor">{datos.numero_factura}</span></div>
            <div class="ledger-fila"><span class="ledger-label">Clave de acceso</span><span class="ledger-valor">{datos.clave_acceso}</span></div>
            <div class="ledger-fila"><span class="ledger-label">Fecha de emision</span><span class="ledger-valor">{datos.fecha_emision}</span></div>
            <div class="ledger-fila"><span class="ledger-label">Subtotal</span><span class="ledger-valor">$ {datos.subtotal:.2f}</span></div>
            <div class="ledger-fila"><span class="ledger-label">Impuestos</span><span class="ledger-valor">$ {datos.impuestos:.2f}</span></div>
            <div class="ledger-fila"><span class="ledger-label">Total</span><span class="ledger-valor destacado">$ {datos.total:.2f}</span></div>
            {fila_cuenta}
        </div>
        <p style="margin-top: 16px;"><a href="/facturas" style="color: var(--primary); font-size: 13px;">Ir a la mesa de aprobacion &rarr;</a></p>
    </div>
    """


_BADGE_CANDIDATA = '<div class="criterio criterio-candidata">Candidata a auto-aprobacion: proveedor conocido y cuenta confirmada.</div>'


def _criterio_proveedor(estado_proveedor, razon_social) -> tuple[str, str]:
    if estado_proveedor == EstadoProveedor.REGISTRADO:
        return ("criterio-ok", f"Proveedor registrado en el maestro ({razon_social}).")
    return ("criterio-alerta", "El RUC no esta registrado en el maestro de proveedores. Validar identidad antes de aprobar.")


def _criterios_evaluacion(estado_proveedor, razon_social, sugerencia):
    """
    Construye la franja de criterios visibles que ayudan a decidir --
    el sistema nunca decide, solo hace visible la evidencia. Usada solo por el camino
    legacy de una sola cuenta; el camino multi-linea arma sus propios criterios por linea
    (ver _renderizar_linea_asiento).
    """
    criterios = [_criterio_proveedor(estado_proveedor, razon_social)]

    if sugerencia:
        etiqueta_confianza = "alta" if sugerencia.confianza == "alta" else "baja"
        clase = "criterio-ok" if etiqueta_confianza == "alta" else "criterio-alerta"
        texto = f"Cuenta sugerida con confianza {etiqueta_confianza}: {sugerencia.codigo} - {sugerencia.nombre}."
        if etiqueta_confianza == "baja":
            texto += " Se recomienda confirmar la cuenta manualmente."
        criterios.append((clase, texto))
    else:
        criterios.append(("criterio-riesgo", "Sin cuenta sugerida. Seleccionar una cuenta manualmente antes de aprobar."))

    filas = "".join(
        f'<div class="criterio {clase}">{texto}</div>' for clase, texto in criterios
    )
    return f'<div class="criterios">{filas}</div>'


def _boton_registrar_proveedor(factura, estado_proveedor) -> str:
    """
    Cuando el proveedor no esta en el maestro, la tarjeta de aprobacion enlaza a una
    pagina aparte (/proveedores/nuevo) para registrarlo -- registrar un proveedor es
    una accion propia, separada de aprobar una factura puntual.
    """
    if estado_proveedor != EstadoProveedor.NO_REGISTRADO:
        return ""
    ruc = urllib.parse.quote(factura.ruc_proveedor)
    razon_social = urllib.parse.quote(factura.razon_social_emisor or "")
    return f'<p><a class="btn btn-registrar" href="/proveedores/nuevo?ruc={ruc}&razon_social={razon_social}">Registrar proveedor &rarr;</a></p>'


def _checkbox_lote(factura, elegibilidad) -> str:
    """
    Checkbox de seleccion para la aprobacion masiva. Vive FUERA del <form> de
    aprobar/rechazar/guardar de la tarjeta (esos siguen siendo por factura) y se asocia
    al formulario de lote de facturas.html via el atributo form="form-lote" -- asi puede
    estar dentro del HTML de la tarjeta sin anidar un <form> dentro de otro.
    """
    if elegibilidad.elegible:
        return f"""
        <label class="lote-checkbox">
            <input type="checkbox" name="factura_ids" value="{factura.id}" form="form-lote" checked>
            Incluir en aprobación masiva
        </label>
        """
    motivos = html.escape("; ".join(elegibilidad.motivos_bloqueo))
    return f"""
    <label class="lote-checkbox lote-checkbox-deshabilitado" title="{motivos}">
        <input type="checkbox" disabled>
        No elegible para lote &middot; {motivos}
    </label>
    """


def _renderizar_tarjeta_legacy(factura, cuentas_activas, estado_proveedor, razon_social, sugerencia_actual, candidata: bool = False, elegibilidad=None):
    """Camino original: una sola cuenta para toda la factura (facturas sin <detalles> en el XML)."""
    # factura.cuenta_final ya puede tener un valor aqui si la factura esta en estado
    # "guardada" (ver guardar_factura_preliminar) -- en ese caso precede a la sugerencia
    # de la IA, porque refleja una decision explicita de la contadora por enciam de la sugerencia automatica.
    cuenta_precargada = factura.cuenta_final or factura.cuenta_sugerida
    opciones = "".join(
        f'<option value="{c.codigo}" {"selected" if c.codigo == cuenta_precargada else ""}>{c.codigo} - {c.nombre}</option>'
        for c in cuentas_activas
    )
    sugerida_texto = factura.cuenta_sugerida or "sin sugerencia"
    badge_candidata = _BADGE_CANDIDATA if candidata else ""
    criterios_html = _criterios_evaluacion(estado_proveedor, razon_social, sugerencia_actual)
    boton_registro = _boton_registrar_proveedor(factura, estado_proveedor)
    checkbox_lote_html = _checkbox_lote(factura, elegibilidad) if elegibilidad is not None else ""

    return f"""
    <div class="tarjeta" id="factura-{factura.id}">
        <div class="tarjeta-nombre">{html.escape(razon_social)}</div>
        <div class="tarjeta-meta">
            <div>RUC {factura.ruc_proveedor}</div>
            <div>Factura {factura.numero_factura}</div>
            <div>{factura.fecha_emision} &middot; sugerida: {sugerida_texto}</div>
        </div>
        <div class="tarjeta-monto-fila"><span class="tarjeta-monto-grande">$ {factura.total:.2f}</span></div>
        {badge_candidata}
        {checkbox_lote_html}
        {criterios_html}
        {boton_registro}
        <form hx-post="/facturas/{factura.id}/aprobar" hx-target="#factura-{factura.id}" hx-swap="outerHTML">
            <div class="fila-accion">
                <select name="codigo_cuenta">{opciones}</select>
                <button type="submit" class="btn btn-aprobar">Aprobar</button>
                <button type="button" class="btn btn-guardar" hx-post="/facturas/{factura.id}/guardar" hx-target="#factura-{factura.id}" hx-swap="outerHTML">Guardar</button>
                <button type="button" class="btn btn-rechazar" onclick="alternarMotivo({factura.id})">Rechazar</button>
            </div>
        </form>
        <div class="rechazo-motivo" id="motivo-{factura.id}">
            <form hx-post="/facturas/{factura.id}/rechazar" hx-target="#factura-{factura.id}" hx-swap="outerHTML">
                <textarea name="motivo" placeholder="Motivo del rechazo (obligatorio)" required></textarea>
                <div class="fila-accion">
                    <button type="submit" class="btn btn-rechazar">Confirmar rechazo</button>
                </div>
            </form>
        </div>
    </div>
    """


def _sugerencia_guardada(factura, cuentas_por_codigo) -> "SugerenciaCuenta | None":
    """
    Reconstruye la sugerencia a partir de lo guardado en la factura (calculado una sola
    vez, al subirla). No se vuelve a llamar a la IA aqui: /facturas se visita muchas veces
    y cada llamada a Claude tiene costo y latencia -- solo se llama una vez, en /subir.
    """
    if not factura.cuenta_sugerida:
        return None
    cuenta = cuentas_por_codigo.get(factura.cuenta_sugerida)
    if cuenta is None:
        return None
    return SugerenciaCuenta(
        codigo=cuenta.codigo,
        nombre=cuenta.nombre,
        confianza=factura.cuenta_sugerida_confianza or "baja",
        justificacion=factura.cuenta_sugerida_justificacion,
    )


ETIQUETAS_DESTINO = {
    "gasto": "Gasto",
    "inventario_reventa": "Inventario (reventa)",
    "activo_fijo": "Activo fijo",
}


def _renderizar_linea_asiento(linea, cuentas_por_destino):
    if linea.destino and linea.destino in cuentas_por_destino:
        opciones_cuentas = cuentas_por_destino[linea.destino]
    else:
        opciones_cuentas = [c for lista in cuentas_por_destino.values() for c in lista]

    # codigo_cuenta_final puede venir de un guardado preliminar (ver
    # guardar_factura_preliminar) -- una eleccion explicita precede a la sugerencia de la IA.
    cuenta_precargada = linea.codigo_cuenta_final or linea.codigo_cuenta_sugerido
    opciones_html = '<option value="">-- seleccionar cuenta --</option>' + "".join(
        f'<option value="{c.codigo}" {"selected" if c.codigo == cuenta_precargada else ""}>{c.codigo} - {c.nombre}</option>'
        for c in opciones_cuentas
    )

    if linea.destino and linea.codigo_cuenta_sugerido:
        etiqueta_destino = ETIQUETAS_DESTINO.get(linea.destino, linea.destino)
        clase = "criterio-ok" if linea.confianza == "alta" else "criterio-alerta"
        texto = f"{etiqueta_destino} &middot; confianza {linea.confianza}: {linea.codigo_cuenta_sugerido}"
        badge = f'<div class="criterio {clase}">{texto}</div>'
    else:
        badge = '<div class="criterio criterio-riesgo">Sin clasificar. Seleccionar cuenta manualmente.</div>'

    return f"""
    <div class="linea-asiento">
        <div class="linea-desc">{html.escape(linea.descripcion)} &middot; $ {float(linea.monto):.2f}</div>
        {badge}
        <select name="codigo_cuenta_linea_{linea.id}">{opciones_html}</select>
    </div>
    """


def _renderizar_tarjeta_multilinea(factura, lineas, cuentas_por_destino, estado_proveedor, razon_social, candidata: bool = False, elegibilidad=None):
    """Camino nuevo: una cuenta por linea de detalle + IVA y cuentas por pagar calculados."""
    badge_candidata = _BADGE_CANDIDATA if candidata else ""
    clase_prov, texto_prov = _criterio_proveedor(estado_proveedor, razon_social)
    criterios_html = f'<div class="criterios"><div class="criterio {clase_prov}">{texto_prov}</div></div>'

    lineas_para_asiento = [
        {"codigo_cuenta": l.codigo_cuenta_sugerido, "descripcion_cuenta": None, "monto": float(l.monto)}
        for l in lineas
    ]
    asiento = construir_asiento_contable(
        lineas_para_asiento, iva_total=float(factura.impuestos), importe_total=float(factura.total)
    )

    lineas_html = "".join(_renderizar_linea_asiento(l, cuentas_por_destino) for l in lineas)

    fijas_html = f"""
    <div class="linea-fija">IVA compras ({IVA_COMPRAS_CODIGO} - {IVA_COMPRAS_NOMBRE}) &middot; $ {asiento['debe'][-1]['monto']:.2f}</div>
    <div class="linea-fija">Cuentas por pagar ({CUENTAS_POR_PAGAR_PROVEEDORES_CODIGO} - {CUENTAS_POR_PAGAR_PROVEEDORES_NOMBRE}) &middot; $ {asiento['total_haber']:.2f}</div>
    """

    boton_registro = _boton_registrar_proveedor(factura, estado_proveedor)
    checkbox_lote_html = _checkbox_lote(factura, elegibilidad) if elegibilidad is not None else ""

    return f"""
    <div class="tarjeta" id="factura-{factura.id}">
        <div class="tarjeta-nombre">{html.escape(razon_social)}</div>
        <div class="tarjeta-meta">
            <div>RUC {factura.ruc_proveedor}</div>
            <div>Factura {factura.numero_factura}</div>
            <div>{factura.fecha_emision} &middot; {len(lineas)} linea(s) de detalle</div>
        </div>
        <div class="tarjeta-monto-fila"><span class="tarjeta-monto-grande">$ {factura.total:.2f}</span></div>
        {badge_candidata}
        {checkbox_lote_html}
        {criterios_html}
        {boton_registro}
        <form hx-post="/facturas/{factura.id}/aprobar" hx-target="#factura-{factura.id}" hx-swap="outerHTML">
            {lineas_html}
            {fijas_html}
            <div class="fila-accion">
                <button type="submit" class="btn btn-aprobar">Aprobar</button>
                <button type="button" class="btn btn-guardar" hx-post="/facturas/{factura.id}/guardar" hx-target="#factura-{factura.id}" hx-swap="outerHTML">Guardar</button>
                <button type="button" class="btn btn-rechazar" onclick="alternarMotivo({factura.id})">Rechazar</button>
            </div>
        </form>
        <div class="rechazo-motivo" id="motivo-{factura.id}">
            <form hx-post="/facturas/{factura.id}/rechazar" hx-target="#factura-{factura.id}" hx-swap="outerHTML">
                <textarea name="motivo" placeholder="Motivo del rechazo (obligatorio)" required></textarea>
                <div class="fila-accion">
                    <button type="submit" class="btn btn-rechazar">Confirmar rechazo</button>
                </div>
            </form>
        </div>
    </div>
    """


def _construir_contexto_cuentas(db):
    """Cuentas activas + su agrupacion por destino, reusado por cada tarjeta pendiente
    (lista completa en /facturas, o una sola al re-renderizarla tras un guardado)."""
    cuentas_activas = obtener_cuentas_activas(db)
    cuentas_por_codigo = {c.codigo: c for c in cuentas_activas}
    cuentas_por_destino = filtrar_catalogo_por_destino(
        [{"codigo": c.codigo, "nombre": c.nombre} for c in cuentas_activas]
    )
    # filtrar_catalogo_por_destino trabaja con dicts; reconstruye con los objetos ORM
    # originales para poder seguir usando c.codigo/c.nombre igual que en el resto del archivo.
    cuentas_por_destino = {
        destino: [cuentas_por_codigo[c["codigo"]] for c in lista]
        for destino, lista in cuentas_por_destino.items()
    }
    return cuentas_activas, cuentas_por_codigo, cuentas_por_destino


def _renderizar_tarjeta_pendiente(db, factura, cuentas_activas, cuentas_por_codigo, cuentas_por_destino) -> str:
    estado_proveedor, razon_social = validar_proveedor(db, factura.ruc_proveedor)
    razon_social = razon_social or factura.razon_social_emisor or factura.ruc_proveedor
    lineas = obtener_lineas_detalle(db, factura.id)
   

    if lineas:
        cuentas_confianza = [(l.codigo_cuenta_final or l.codigo_cuenta_sugerido, l.confianza) for l in lineas]
    else:
        cuentas_confianza = [(factura.cuenta_final or factura.cuenta_sugerida, factura.cuenta_sugerida_confianza)]
    candidata = es_candidata_auto_aprobacion(db, factura.ruc_proveedor, estado_proveedor, cuentas_confianza)
    elegibilidad = evaluar_elegibilidad_lote(db, factura, estado_proveedor, lineas)

    if lineas:
        return _renderizar_tarjeta_multilinea(
            factura, lineas, cuentas_por_destino, estado_proveedor, razon_social, candidata, elegibilidad
        )
    sugerencia_actual = _sugerencia_guardada(factura, cuentas_por_codigo)
    return _renderizar_tarjeta_legacy(
        factura, cuentas_activas, estado_proveedor, razon_social, sugerencia_actual, candidata, elegibilidad
    )


def _renderizar_lista_pendientes(db, pendientes):
    if not pendientes:
        return '<div class="vacio">No hay facturas pendientes de aprobacion.</div>'

    cuentas_activas, cuentas_por_codigo, cuentas_por_destino = _construir_contexto_cuentas(db)
    return "".join(
        _renderizar_tarjeta_pendiente(db, factura, cuentas_activas, cuentas_por_codigo, cuentas_por_destino)
        for factura in pendientes
    )


def _parsear_filtros_facturas(
    proveedor: str, cuenta: str, fecha_desde: str, fecha_hasta: str, monto_min: str, monto_max: str
) -> FiltrosFacturas:
    def _fecha(valor: str):
        try:
            return date.fromisoformat(valor) if valor else None
        except ValueError:
            return None

    def _monto(valor: str):
        try:
            return Decimal(valor) if valor else None
        except (InvalidOperation, ValueError):
            return None

    return FiltrosFacturas(
        proveedor=proveedor.strip() or None,
        cuenta=cuenta.strip() or None,
        fecha_desde=_fecha(fecha_desde),
        fecha_hasta=_fecha(fecha_hasta),
        monto_min=_monto(monto_min),
        monto_max=_monto(monto_max),
    )

#


@app.get("/facturas", response_class=HTMLResponse)
def ver_facturas_pendientes(
    request: Request,
    proveedor: str = "",
    cuenta: str = "",
    fecha_desde: str = "",
    fecha_hasta: str = "",
    monto_min: str = "",
    monto_max: str = "",
    lote_aprobadas: int | None = None,
    lote_omitidas: int | None = None,
):
    filtros = _parsear_filtros_facturas(proveedor, cuenta, fecha_desde, fecha_hasta, monto_min, monto_max)

    db = SessionLocal()
    try:
        pendientes = obtener_facturas_pendientes(db)
        pendientes_filtradas = filtrar_facturas_pendientes(db, pendientes, filtros)
        resumen = {"cantidad": len(pendientes_filtradas), "cantidad_total": len(pendientes)}
        contenido_lista = _renderizar_lista_pendientes(db, pendientes_filtradas)
    finally:
        db.close()
    return templates.TemplateResponse(
        request,
        "facturas.html",
        {
            "contenido_lista": contenido_lista,
            "resumen": resumen,
            "filtros": {
                "proveedor": proveedor,
                "cuenta": cuenta,
                "fecha_desde": fecha_desde,
                "fecha_hasta": fecha_hasta,
                "monto_min": monto_min,
                "monto_max": monto_max,
            },
            "lote_aprobadas": lote_aprobadas,
            "lote_omitidas": lote_omitidas,
        },
    )


@app.post("/facturas/aprobar-lote", response_class=HTMLResponse)
async def aprobar_lote_facturas(request: Request):
    """
    Aprobacion masiva (seleccion multiple): recibe los factura_ids marcados en la
    mesa de aprobacion y los aprueba solo si, revalidados ahora mismo en el servidor,
    siguen pasando evaluar_elegibilidad_lote -- ver app/core/aprobaciones.py para las
    reglas de seguridad. Nunca confia en que el checkbox marcado en el navegador siga
    siendo valido.
    """
    usuario = request.state.usuario["nombre_usuario"]
    form = await request.form()
    factura_ids = [int(v) for v in form.getlist("factura_ids")]

    db = SessionLocal()
    try:
        if not factura_ids:
            resultado = {"aprobadas": [], "omitidas": []}
        else:
            resultado = aprobar_lote(db, factura_ids, usuario)
    finally:
        db.close()

    params = urllib.parse.urlencode({
        "lote_aprobadas": len(resultado["aprobadas"]),
        "lote_omitidas": len(resultado["omitidas"]),
    })
    return RedirectResponse(url=f"/facturas?{params}", status_code=303)


def _formulario_registrar_proveedor(ruc: str, razon_social: str, error: str | None = None) -> str:
    error_html = f'<div class="resultado-accion resultado-error">{html.escape(error)}</div>' if error else ""
    return f"""
    {error_html}
    <form method="post" action="/proveedores/nuevo">
        <div class="fila-usuario">
            <label for="ruc">RUC</label>
            <input type="text" id="ruc" name="ruc" value="{html.escape(ruc)}" maxlength="13" required>
        </div>
        <div class="fila-usuario">
            <label for="razon_social">Razon social</label>
            <input type="text" id="razon_social" name="razon_social" value="{html.escape(razon_social)}" required>
        </div>
        <div class="fila-usuario">
            <label for="correo">Correo (opcional)</label>
            <input type="email" id="correo" name="correo">
        </div>
        <div class="fila-accion">
            <button type="submit" class="btn btn-aprobar">Registrar proveedor</button>
        </div>
    </form>
    """


@app.get("/proveedores/nuevo", response_class=HTMLResponse)
def formulario_nuevo_proveedor(request: Request, ruc: str = "", razon_social: str = ""):
    contenido = _formulario_registrar_proveedor(ruc, razon_social)
    return templates.TemplateResponse(request, "proveedor_nuevo.html", {"contenido": contenido})


@app.post("/proveedores/nuevo", response_class=HTMLResponse)
async def registrar_nuevo_proveedor(request: Request):
    form = await request.form()
    ruc = (form.get("ruc") or "").strip()
    razon_social = (form.get("razon_social") or "").strip()
    correo = (form.get("correo") or "").strip() or None

    error = None
    if not razon_social:
        error = "Debes indicar la razon social del proveedor."
    elif not ruc_estructuralmente_valido(ruc):
        error = "El RUC debe tener 13 digitos numericos."

    db = SessionLocal()
    try:
        if error is None:
            estado_proveedor, _ = validar_proveedor(db, ruc)
            if estado_proveedor == EstadoProveedor.REGISTRADO:
                error = f"El RUC {ruc} ya esta registrado en el maestro."

        if error:
            contenido = _formulario_registrar_proveedor(ruc, razon_social, error=error)
            return templates.TemplateResponse(request, "proveedor_nuevo.html", {"contenido": contenido})

        registrar_proveedor(db, ruc, razon_social, correo=correo)
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/facturas", status_code=303)


@app.post("/facturas/{factura_id}/aprobar", response_class=HTMLResponse)
async def aprobar(request: Request, factura_id: int):
    form = await request.form()
    usuario = request.state.usuario["nombre_usuario"]

    # Una tarjeta multi-linea manda varios campos "codigo_cuenta_linea_<id>" (uno por
    # linea de detalle); una tarjeta legacy manda un solo "codigo_cuenta". El nombre de
    # los campos que llegaron es lo que decide que camino de aprobacion usar.
    codigos_por_linea = {
        int(clave.removeprefix("codigo_cuenta_linea_")): valor
        for clave, valor in form.items()
        if clave.startswith("codigo_cuenta_linea_")
    }

    db = SessionLocal()
    try:
        try:
            if codigos_por_linea:
                factura = aprobar_factura_lineas(db, factura_id, usuario, codigos_por_linea)
            else:
                codigo_cuenta = form.get("codigo_cuenta", "")
                factura = aprobar_factura(db, factura_id, usuario, codigo_cuenta)
            return f"""<div class="resultado-accion resultado-aprobada"><strong>Aprobada</strong> por {html.escape(usuario)} &middot; cuenta final: {html.escape(factura.cuenta_final or "")}. <a href="/aprobadas">Ir a enviar a SAP &rarr;</a></div>"""
        except AccionInvalidaError as e:
            return f'<div class="resultado-accion resultado-error"><strong>No se pudo aprobar.</strong> {html.escape(str(e))}</div>'
    finally:
        db.close()


@app.post("/facturas/{factura_id}/rechazar", response_class=HTMLResponse)
def rechazar(request: Request, factura_id: int, motivo: str = Form(...)):
    usuario = request.state.usuario["nombre_usuario"]
    db = SessionLocal()
    try:
        try:
            factura = rechazar_factura(db, factura_id, usuario, motivo)
            return f"""<div class="resultado-accion resultado-rechazada"><strong>Rechazada</strong> por {html.escape(usuario)}. Motivo: {html.escape(motivo)}</div>"""
        except AccionInvalidaError as e:
            return f'<div class="resultado-accion resultado-error"><strong>No se pudo rechazar.</strong> {html.escape(str(e))}</div>'
    finally:
        db.close()


@app.post("/facturas/{factura_id}/guardar", response_class=HTMLResponse)
async def guardar(request: Request, factura_id: int):
    """
    Guardado preliminar (ver guardar_factura_preliminar): no cierra el ciclo de la
    factura, asi que en vez de reemplazar la tarjeta por un mensaje de resultado como
    aprobar/rechazar, se re-renderiza la misma tarjeta -- sigue en la mesa de
    aprobacion, ahora con la cuenta y el nombre que se acaban de guardar precargados.
    """
    form = await request.form()
    usuario = request.state.usuario["nombre_usuario"]
    codigo_cuenta = form.get("codigo_cuenta", "")
    codigos_por_linea = {
        int(clave.removeprefix("codigo_cuenta_linea_")): valor
        for clave, valor in form.items()
        if clave.startswith("codigo_cuenta_linea_")
    }

    db = SessionLocal()
    try:
        try:
            factura = guardar_factura_preliminar(db, factura_id, usuario, codigo_cuenta, codigos_por_linea)
            cuentas_activas, cuentas_por_codigo, cuentas_por_destino = _construir_contexto_cuentas(db)
            return _renderizar_tarjeta_pendiente(db, factura, cuentas_activas, cuentas_por_codigo, cuentas_por_destino)
        except AccionInvalidaError as e:
            return f'<div class="resultado-accion resultado-error"><strong>No se pudo guardar.</strong> {html.escape(str(e))}</div>'
    finally:
        db.close()


def _renderizar_tarjeta_aprobada(factura):
    # Sin acciones por tarjeta a proposito: la factura ya se aprobo en /facturas (Pendientes),
    # asi que aqui solo se muestra como referencia -- la unica accion de esta pantalla es el
    # boton de exportar a Excel arriba de la lista (ver aprobadas.html). El envio a SAP sigue
    # existiendo en el backend (POST /facturas/{id}/enviar-sap) para cuando se retome ese flujo,
    # solo se quito el boton de esta vista.
    etiqueta_exportada = (
        '<span class="badge badge-gris">Ya exportada</span>' if factura.exportado_a_excel else ""
    )
    return f"""
    <div class="tarjeta" id="aprobada-{factura.id}">
        <div class="tarjeta-header">
            <span class="tarjeta-nombre">{factura.ruc_proveedor}</span>
            <span class="tarjeta-monto">$ {factura.total:.2f}</span>
        </div>
        <div class="tarjeta-meta">Factura {factura.numero_factura} &middot; cuenta: {factura.cuenta_final}</div>
        {etiqueta_exportada}
    </div>
    """


def _renderizar_lista_aprobadas(db):
    aprobadas = obtener_facturas_aprobadas_pendientes_envio(db)
    if not aprobadas:
        return '<div class="vacio">No hay facturas aprobadas pendientes de enviar a SAP.</div>'
    return "".join(_renderizar_tarjeta_aprobada(f) for f in aprobadas)


@app.get("/aprobadas", response_class=HTMLResponse)
def ver_facturas_aprobadas(request: Request, sin_exportar: bool = False):
    db = SessionLocal()
    try:
        contenido_lista = _renderizar_lista_aprobadas(db)
        cantidad_para_exportar = len(obtener_filas_para_exportar(db))
    finally:
        db.close()
    return templates.TemplateResponse(
        request,
        "aprobadas.html",
        {
            "contenido_lista": contenido_lista,
            "cantidad_para_exportar": cantidad_para_exportar,
            "sin_exportar": sin_exportar,
        },
    )


@app.post("/aprobadas/exportar-excel")
def exportar_excel_aprobadas(request: Request):
    """
    Genera el reporte Excel (dashboard + resumen + detalle) de facturas aprobadas que
    todavia no se han exportado, y las marca como exportadas -- ver
    app/core/reportes_excel.py. No toca el estado de aprobacion ni el envio a SAP: es
    puramente un reporte de lo que ya se aprobo.
    """
    usuario = request.state.usuario["nombre_usuario"]
    db = SessionLocal()
    try:
        filas = obtener_filas_para_exportar(db)
        if not filas:
            return RedirectResponse(url="/aprobadas?sin_exportar=true", status_code=303)

        buffer = generar_libro_excel(filas)
        factura_ids = sorted({f.factura_id for f in filas})
        marcar_como_exportadas(db, factura_ids, usuario)
    finally:
        db.close()

    nombre_archivo = f"reporte_facturas_aprobadas_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre_archivo}"'},
    )


@app.post("/facturas/{factura_id}/enviar-sap", response_class=HTMLResponse)
def enviar_sap(request: Request, factura_id: int):
    usuario = request.state.usuario["nombre_usuario"]
    db = SessionLocal()
    try:
        try:
            factura = enviar_a_sap(db, factura_id, usuario)
            return f"""<div class="resultado-accion resultado-aprobada"><strong>Registrada en SAP</strong> &middot; referencia: {factura.referencia_sap}</div>"""
        except EnvioInvalidoError as e:
            return f'<div class="resultado-accion resultado-error"><strong>SAP rechazo el envio.</strong> {e}</div>'
    finally:
        db.close()


@app.post("/facturas/{factura_id}/validar-sri", response_class=HTMLResponse)
def validar_sri(request: Request, factura_id: int):
    """
    Mockup para evaluar si conviene conectar al webservice real de Autorizacion del
    SRI: hoy solo consulta un simulador (ver app/core/validacion_sri.py). No cambia el
    estado de la factura, solo deja constancia en el historial de que se consulto.
    """
    usuario = request.state.usuario["nombre_usuario"]
    db = SessionLocal()
    try:
        try:
            factura, estado_sri = validar_en_sri(db, factura_id, usuario)
            return f'<div class="resultado-accion resultado-aprobada"><strong>{estado_sri}</strong> &middot; clave de acceso: {factura.clave_acceso}</div>'
        except ValidacionInvalidaError as e:
            return f'<div class="resultado-accion resultado-error"><strong>No se pudo consultar el SRI.</strong> {e}</div>'
    finally:
        db.close()


BADGES_ACCION = {
    "aprobada": ("badge-exito", "Aprobada"),
    "registrada_sap": ("badge-exito", "Registrada en SAP"),
    "consultada_sri": ("badge-neutro", "Consultada en SRI"),
    "rechazada": ("badge-peligro", "Rechazada"),
    "error_envio_sap": ("badge-peligro", "Error al enviar a SAP"),
    "guardada": ("badge-neutro", "Guardada"),
}
BADGE_DEFECTO = ("badge-neutro", None)


def _formatear_fecha_ecuador(momento) -> str:
    if momento is None:
        return "-"
    return momento.astimezone(ZONA_ECUADOR).strftime("%Y-%m-%d %H:%M:%S")


def _renderizar_fila_historial(fila: FilaHistorial) -> str:
    clase_badge, etiqueta = BADGES_ACCION.get(fila.accion, BADGE_DEFECTO)
    etiqueta = etiqueta or html.escape(fila.accion)
    usuario_seguro = html.escape(fila.usuario)
    cuenta_texto = (
        f' &middot; cuenta: {html.escape(fila.cuenta_utilizada)}' if fila.cuenta_utilizada else ""
    )
    motivo_html = (
        f'<div class="fila-motivo">{html.escape(fila.motivo)}</div>' if fila.motivo else ""
    )
    fecha_texto = _formatear_fecha_ecuador(fila.creado_en)

    return f"""
    <div class="tarjeta" id="historial-{fila.id}">
        <div class="tarjeta-header">
            <span class="badge {clase_badge}">{etiqueta}</span>
            <span class="tarjeta-monto">{fecha_texto}</span>
        </div>
        <div class="tarjeta-meta">Factura {html.escape(fila.numero_factura)} &middot; RUC {html.escape(fila.ruc_proveedor)} &middot; por {usuario_seguro}{cuenta_texto}</div>
        {motivo_html}
    </div>
    """


def _renderizar_lista_historial(resultado: PaginaHistorial) -> str:
    if not resultado.filas:
        if resultado.pagina > 1:
            return '<div class="vacio">No hay mas registros. <a href="/historial">&larr; Volver a la primera pagina</a></div>'
        return '<div class="vacio">Todavia no hay acciones registradas en el historial.</div>'

    filas_html = "".join(_renderizar_fila_historial(f) for f in resultado.filas)

    nav = ['<div class="paginacion">']
    if resultado.pagina > 1:
        nav.append(f'<a href="/historial?pagina={resultado.pagina - 1}">&larr; Anterior</a>')
    else:
        nav.append("<span></span>")
    nav.append(f'<span class="pagina-actual">Pagina {resultado.pagina}</span>')
    if resultado.hay_pagina_siguiente:
        nav.append(f'<a href="/historial?pagina={resultado.pagina + 1}">Siguiente &rarr;</a>')
    else:
        nav.append("<span></span>")
    nav.append("</div>")

    return filas_html + "".join(nav)


@app.get("/historial", response_class=HTMLResponse)
def ver_historial(request: Request, pagina: int = 1):
    db = SessionLocal()
    try:
        resultado = obtener_historial_paginado(db, pagina)
        contenido_lista = _renderizar_lista_historial(resultado)
    finally:
        db.close()
    return templates.TemplateResponse(request, "historial.html", {"contenido_lista": contenido_lista})


# ---------- login / logout ----------

@app.get("/login", response_class=HTMLResponse)
def formulario_login(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def procesar_login(request: Request):
    form = await request.form()
    nombre_usuario = (form.get("usuario") or "").strip()
    contrasena = form.get("contrasena") or ""

    db = SessionLocal()
    try:
        usuario = auth.autenticar_usuario(db, nombre_usuario, contrasena)
    finally:
        db.close()

    if usuario is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Usuario o contraseña incorrectos."},
            status_code=401,
        )

    respuesta = RedirectResponse(url="/panel-vigilancia", status_code=303)
    respuesta.set_cookie(
        auth.COOKIE_NAME,
        auth.crear_cookie_sesion(usuario),
        httponly=True,
        samesite="lax",
        max_age=auth.MAX_EDAD_SESION_SEGUNDOS,
    )
    return respuesta


@app.post("/logout")
def cerrar_sesion():
    respuesta = RedirectResponse(url="/login", status_code=303)
    respuesta.delete_cookie(auth.COOKIE_NAME)
    return respuesta


# ---------- panel de vigilancia ----------

BADGES_ESTADO_VIGILANTE = {
    "en_espera": ("badge-gris", "En espera"),
    "vigilando": ("badge-azul", "Vigilando"),
    "procesando": ("badge-ambar", "Procesando"),
    "error": ("badge-rojo", "Error"),
}


@app.get("/panel-vigilancia", response_class=HTMLResponse)
def ver_panel_vigilancia(request: Request):
    db = SessionLocal()
    try:
        estado = vigilante_carpeta.leer_estado(db)
        estado_actual = estado.estado
        carpeta_monitoreada = estado.carpeta_monitoreada or vigilante_carpeta.CARPETA_POR_DEFECTO
        mensaje_error = estado.mensaje_error
        descargas_factible = descarga_factible.listar_ultimas_descargas(db)
        for d in descargas_factible:
            db.expunge(d)
        factible_en_curso = next((d for d in descargas_factible if d.estado == "PENDIENTE"), None)
    finally:
        db.close()

    config_factible = descarga_factible.leer_config()
    clase_badge, etiqueta_badge = BADGES_ESTADO_VIGILANTE.get(estado_actual, ("badge-gris", estado_actual))
    pendientes = vigilante_carpeta.contar_pendientes(carpeta_monitoreada)
    procesados_hoy = vigilante_carpeta.contar_procesados_hoy(carpeta_monitoreada)
    ultimos_errores = vigilante_carpeta.listar_ultimos_errores(carpeta_monitoreada)

    return templates.TemplateResponse(
        request,
        "panel_vigilancia.html",
        {
            "estado_actual": estado_actual,
            "clase_badge": clase_badge,
            "etiqueta_badge": etiqueta_badge,
            "mensaje_error": mensaje_error,
            "carpeta_monitoreada": carpeta_monitoreada,
            "pendientes": pendientes,
            "procesados_hoy": procesados_hoy,
            "ultimos_errores": ultimos_errores,
            "puede_iniciar": estado_actual in ("en_espera", "error"),
            "factible_configurado": bool(config_factible.modo),
            "factible_modo": config_factible.modo,
            "factible_automatica": config_factible.descarga_automatica,
            "factible_hora": config_factible.hora_descarga.strftime("%H:%M"),
            "factible_fecha_sugerida": (datetime.now(ZONA_ECUADOR).date() - timedelta(days=1)).isoformat(),
            "descargas_factible": descargas_factible,
            "factible_en_curso": factible_en_curso,
            "badges_factible": BADGES_ESTADO_FACTIBLE,
        },
    )


BADGES_ESTADO_FACTIBLE = {
    "PENDIENTE": ("badge-ambar", "En proceso"),
    "FINALIZADO": ("badge-azul", "Descargada"),
    "SIN_DOCUMENTOS": ("badge-gris", "Sin documentos"),
    "ERROR": ("badge-rojo", "Error"),
}


@app.get("/admin/factible/estado/{descarga_id}")
def estado_descarga_factible(descarga_id: int):
    """Lo consulta el spinner del panel de vigilancia cada pocos segundos para saber
    si la descarga termino y si el vigilante ya proceso los XML que dejo en la carpeta."""
    db = SessionLocal()
    try:
        descarga = db.get(DescargaFactible, descarga_id)
        if descarga is None:
            return JSONResponse({"error": "Descarga no encontrada."}, status_code=404)
        datos = {
            "estado": descarga.estado,
            "mensaje": descarga.mensaje,
            "xml_guardados": descarga.xml_guardados or 0,
        }
    finally:
        db.close()
    return datos | estado_vigilancia()


@app.get("/admin/vigilancia/estado")
def estado_vigilancia():
    """Archivos que faltan procesar en la carpeta. Se lee el estado guardado en la BD y no
    vigilante_carpeta.esta_activo(): este proceso puede no ser el que tiene el hilo."""
    db = SessionLocal()
    try:
        estado = vigilante_carpeta.leer_estado(db)
        carpeta = estado.carpeta_monitoreada or vigilante_carpeta.CARPETA_POR_DEFECTO
        activo = estado.estado in ("vigilando", "procesando")
    finally:
        db.close()
    return {
        "pendientes_carpeta": vigilante_carpeta.contar_pendientes(carpeta),
        "procesados_hoy": vigilante_carpeta.contar_procesados_hoy(carpeta),
        "vigilante_activo": activo,
    }


@app.post("/admin/factible/descargar")
async def descargar_de_factible(request: Request):
    """Pide a Factible las facturas recibidas de una fecha (por defecto, ayer). Solo
    genera el token; el programador lo consulta y deja los XML en la carpeta vigilada."""
    usuario = request.state.usuario
    form = await request.form()
    try:
        fecha = date.fromisoformat((form.get("fecha") or "").strip())
    except ValueError:
        fecha = None

    if fecha is None:
        ok, mensaje = False, "Fecha invalida."
    else:
        resultado = descarga_factible.descargar_ahora(fecha, usuario["nombre_usuario"])
        if isinstance(resultado, str):
            ok, mensaje = False, resultado
        else:
            ok = resultado.estado != "ERROR"
            mensaje = f"{fecha:%d/%m/%Y}: {resultado.mensaje}"

    db = SessionLocal()
    try:
        db.add(
            HistorialVigilancia(
                usuario=usuario["nombre_usuario"],
                accion="descargar_factible",
                resultado="ok" if ok else "error",
                mensaje=mensaje,
            )
        )
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/panel-vigilancia", status_code=303)


@app.post("/admin/elegir-carpeta")
def elegir_carpeta(carpeta_actual: str = Form("")):
    """
    Abre el explorador de carpetas nativo de Windows en el propio equipo donde
    corre el servidor y devuelve la ruta elegida como JSON, para que el JS del
    panel la ponga en el campo de texto sin que la persona tenga que escribirla
    a mano. Pensado para uso local (navegador y servidor en la misma maquina) --
    no tiene sentido en un despliegue donde el servidor y quien lo usa estan en
    equipos distintos, ya el dialogo se abriria en la pantalla del servidor.

    Se importa tkinter aqui adentro (no arriba del archivo) para no forzar esa
    dependencia del sistema en el resto de la app si algun dia corre sin entorno
    grafico. Si la persona cancela el dialogo, devuelve carpeta vacia.
    """
    import tkinter as tk
    from tkinter import filedialog

    inicio = vigilante_carpeta.CARPETA_POR_DEFECTO
    if carpeta_actual and Path(carpeta_actual).is_dir():
        inicio = carpeta_actual

    raiz = tk.Tk()
    raiz.withdraw()
    raiz.attributes("-topmost", True)
    try:
        carpeta = filedialog.askdirectory(
            initialdir=inicio if Path(inicio).is_dir() else None,
            title="Elegir carpeta a vigilar",
        )
    finally:
        raiz.destroy()

    return {"carpeta": carpeta or ""}


@app.post("/admin/revisar-carpeta")
async def revisar_carpeta(request: Request):
    """
    Inicia el hilo de vigilancia de carpeta y deja registro de quien lo hizo,
    cuando, y con que resultado -- ver HistorialVigilancia. Si ya estaba
    vigilando otra carpeta, la detiene primero para poder cambiar de ruta sin
    que la persona tenga que apretar "Detener" aparte.

    La ruta la escribe la persona en el panel (campo "carpeta"); si la deja vacia,
    se usa la ultima carpeta guardada o CARPETA_POR_DEFECTO (ver iniciar_monitoreo).
    """
    usuario = request.state.usuario
    form = await request.form()
    carpeta_ingresada = (form.get("carpeta") or "").strip()
    if vigilante_carpeta.esta_activo():
        vigilante_carpeta.detener_monitoreo()
    ok, mensaje = vigilante_carpeta.iniciar_monitoreo(carpeta_ingresada or None)

    db = SessionLocal()
    try:
        if ok:
            # El hilo tambien guarda esto en su primera vuelta, pero eso pasa en
            # otro hilo -- lo escribimos ya mismo para que el panel refleje la
            # carpeta elegida sin esperar esa primera vuelta.
            estado = vigilante_carpeta.leer_estado(db)
            estado.carpeta_monitoreada = carpeta_ingresada or estado.carpeta_monitoreada or vigilante_carpeta.CARPETA_POR_DEFECTO
        db.add(
            HistorialVigilancia(
                usuario=usuario["nombre_usuario"],
                accion="iniciar_monitoreo",
                resultado="ok" if ok else "error",
                mensaje=mensaje,
            )
        )
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/panel-vigilancia", status_code=303)


@app.post("/admin/detener-carpeta")
async def detener_carpeta(request: Request):
    """Detiene el hilo de vigilancia de carpeta si esta activo, para poder cambiar
    de ruta o simplemente dejar de procesar."""
    usuario = request.state.usuario
    ok, mensaje = vigilante_carpeta.detener_monitoreo()

    db = SessionLocal()
    try:
        db.add(
            HistorialVigilancia(
                usuario=usuario["nombre_usuario"],
                accion="detener_monitoreo",
                resultado="ok" if ok else "error",
                mensaje=mensaje,
            )
        )
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/panel-vigilancia", status_code=303)


@app.post("/admin/reintentar-archivo")
async def reintentar_archivo_error(request: Request):
    """Mueve un archivo de errores/ de vuelta a la carpeta de entrada para que el
    hilo de vigilancia lo procese de nuevo en la proxima pasada."""
    usuario = request.state.usuario
    form = await request.form()
    nombre_archivo = (form.get("nombre_archivo") or "").strip()

    db = SessionLocal()
    try:
        estado = vigilante_carpeta.leer_estado(db)
        carpeta_monitoreada = estado.carpeta_monitoreada or vigilante_carpeta.CARPETA_POR_DEFECTO
        ok = vigilante_carpeta.reintentar_archivo(carpeta_monitoreada, nombre_archivo) if nombre_archivo else False
        db.add(
            HistorialVigilancia(
                usuario=usuario["nombre_usuario"],
                accion="reintentar_archivo",
                resultado="ok" if ok else "error",
                mensaje=nombre_archivo or "(sin nombre de archivo)",
            )
        )
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/panel-vigilancia", status_code=303)


