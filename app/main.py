import html
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.lector_xml import leer_factura, XMLInvalidoError
from app.core.validaciones import validar_proveedor, detectar_duplicado, EstadoProveedor
from app.core.models import Factura
from app.core.aprobaciones import (
    obtener_facturas_pendientes,
    obtener_cuentas_activas,
    aprobar_factura,
    rechazar_factura,
    AccionInvalidaError,
)
from app.core.envios_sap import (
    obtener_facturas_aprobadas_pendientes_envio,
    enviar_a_sap,
    EnvioInvalidoError,
)
from app.core.historial import obtener_historial_paginado, FilaHistorial, PaginaHistorial
from app.db.config import SessionLocal
from app.adaptadores.asesor_ia.simulador import SugeridorSimulado

app = FastAPI(title="Asistente de facturas")
templates = Jinja2Templates(directory="app/templates")

ZONA_ECUADOR = ZoneInfo("America/Guayaquil")

ETIQUETAS_ESTADO = {
    EstadoProveedor.ACTIVO: "Proveedor activo",
    EstadoProveedor.INACTIVO: "Proveedor inactivo",
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

    try:
        datos = leer_factura(contenido)
    except XMLInvalidoError as e:
        return f"""<div class="alerta"><strong>Factura invalida</strong>{e}</div>"""

    db = SessionLocal()
    try:
        ya_existia = detectar_duplicado(db, datos.numero_factura)

        if ya_existia:
            db.close()
            return f"""<div class="alerta"><strong>Factura bloqueada: numero duplicado</strong>Ya existe una factura guardada con el numero {datos.numero_factura}. No puede volver a procesarse ni aparecer como pendiente otra vez.</div>"""

        estado_proveedor, razon_social_registrada = validar_proveedor(db, datos.ruc_proveedor)

        asesor = SugeridorSimulado(db)
        sugerencia = asesor.sugerir(datos.ruc_proveedor, datos.razon_social)

        nueva_factura = Factura(
            ruc_proveedor=datos.ruc_proveedor,
            numero_factura=datos.numero_factura,
            fecha_emision=datos.fecha_emision,
            subtotal=datos.subtotal,
            impuestos=datos.impuestos,
            total=datos.total,
            estado="pendiente",
            cuenta_sugerida=sugerencia.codigo if sugerencia else None,
        )
        db.add(nueva_factura)
        db.commit()
    finally:
        db.close()

    etiqueta = ETIQUETAS_ESTADO[estado_proveedor]
    clase_sello = f"sello-{estado_proveedor.value}"
    razon_mostrada = razon_social_registrada or datos.razon_social

    fila_cuenta = f"""<div class="ledger-fila"><span class="ledger-label">Cuenta sugerida</span><span class="ledger-valor">{sugerencia.codigo} - {sugerencia.nombre}</span></div>""" if sugerencia else """<div class="ledger-fila"><span class="ledger-label">Cuenta sugerida</span><span class="ledger-valor">Sin sugerencia (revisar catalogo)</span></div>"""

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
        <p style="margin-top: 16px;"><a href="/facturas" style="color: var(--accent); font-size: 13px;">Ir a la mesa de aprobacion &rarr;</a></p>
    </div>
    """


def _criterios_evaluacion(estado_proveedor, razon_social, sugerencia):
    """
    Construye la franja de criterios visibles que ayudan a decidir --
    el sistema nunca decide, solo hace visible la evidencia.
    """
    criterios = []

    if estado_proveedor == EstadoProveedor.ACTIVO:
        criterios.append(("criterio-ok", f"Proveedor activo en el maestro ({razon_social})."))
    elif estado_proveedor == EstadoProveedor.INACTIVO:
        criterios.append(("criterio-riesgo", f"Proveedor INACTIVO en el maestro ({razon_social}). Revisar antes de aprobar."))
    else:
        criterios.append(("criterio-alerta", "El RUC no esta registrado en el maestro de proveedores. Validar identidad antes de aprobar."))

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


def _renderizar_tarjeta_pendiente(factura, cuentas_activas, estado_proveedor, razon_social, sugerencia_actual):
    opciones = "".join(
        f'<option value="{c.codigo}" {"selected" if c.codigo == factura.cuenta_sugerida else ""}>{c.codigo} - {c.nombre}</option>'
        for c in cuentas_activas
    )
    sugerida_texto = factura.cuenta_sugerida or "sin sugerencia"
    criterios_html = _criterios_evaluacion(estado_proveedor, razon_social, sugerencia_actual)

    return f"""
    <div class="tarjeta" id="factura-{factura.id}">
        <div class="tarjeta-header">
            <span class="tarjeta-nombre">{factura.ruc_proveedor}</span>
            <span class="tarjeta-monto">$ {factura.total:.2f}</span>
        </div>
        <div class="tarjeta-meta">Factura {factura.numero_factura} &middot; {factura.fecha_emision} &middot; sugerida: {sugerida_texto}</div>
        {criterios_html}
        <form hx-post="/facturas/{factura.id}/aprobar" hx-target="#factura-{factura.id}" hx-swap="outerHTML">
            <div class="fila-usuario"><input type="text" name="usuario" placeholder="Tu nombre" required></div>
            <div class="fila-accion">
                <select name="codigo_cuenta">{opciones}</select>
                <button type="submit" class="btn btn-aprobar">Aprobar</button>
                <button type="button" class="btn btn-rechazar" onclick="alternarMotivo({factura.id})">Rechazar</button>
            </div>
        </form>
        <div class="rechazo-motivo" id="motivo-{factura.id}">
            <form hx-post="/facturas/{factura.id}/rechazar" hx-target="#factura-{factura.id}" hx-swap="outerHTML">
                <textarea name="motivo" placeholder="Motivo del rechazo (obligatorio)" required></textarea>
                <div class="fila-usuario">
                    <input type="text" name="usuario" placeholder="Tu nombre" required>
                    <button type="submit" class="btn btn-rechazar">Confirmar rechazo</button>
                </div>
            </form>
        </div>
    </div>
    """


def _renderizar_lista_pendientes(db):
    pendientes = obtener_facturas_pendientes(db)
    cuentas_activas = obtener_cuentas_activas(db)
    if not pendientes:
        return '<div class="vacio">No hay facturas pendientes de aprobacion.</div>'

    asesor = SugeridorSimulado(db)
    tarjetas = []
    for factura in pendientes:
        estado_proveedor, razon_social = validar_proveedor(db, factura.ruc_proveedor)
        sugerencia_actual = asesor.sugerir(factura.ruc_proveedor, razon_social or "")
        tarjetas.append(
            _renderizar_tarjeta_pendiente(factura, cuentas_activas, estado_proveedor, razon_social or factura.ruc_proveedor, sugerencia_actual)
        )
    return "".join(tarjetas)


@app.get("/facturas", response_class=HTMLResponse)
def ver_facturas_pendientes(request: Request):
    db = SessionLocal()
    try:
        contenido_lista = _renderizar_lista_pendientes(db)
    finally:
        db.close()
    return templates.TemplateResponse(request, "facturas.html", {"contenido_lista": contenido_lista})


@app.post("/facturas/{factura_id}/aprobar", response_class=HTMLResponse)
def aprobar(factura_id: int, usuario: str = Form(...), codigo_cuenta: str = Form(...)):
    db = SessionLocal()
    try:
        try:
            factura = aprobar_factura(db, factura_id, usuario, codigo_cuenta)
            return f"""<div class="resultado-accion resultado-aprobada"><strong>Aprobada</strong> por {usuario} &middot; cuenta final: {factura.cuenta_final}. <a href="/aprobadas">Ir a enviar a SAP &rarr;</a></div>"""
        except AccionInvalidaError as e:
            return f'<div class="resultado-accion resultado-error"><strong>No se pudo aprobar.</strong> {e}</div>'
    finally:
        db.close()


@app.post("/facturas/{factura_id}/rechazar", response_class=HTMLResponse)
def rechazar(factura_id: int, usuario: str = Form(...), motivo: str = Form(...)):
    db = SessionLocal()
    try:
        try:
            factura = rechazar_factura(db, factura_id, usuario, motivo)
            return f"""<div class="resultado-accion resultado-rechazada"><strong>Rechazada</strong> por {usuario}. Motivo: {motivo}</div>"""
        except AccionInvalidaError as e:
            return f'<div class="resultado-accion resultado-error"><strong>No se pudo rechazar.</strong> {e}</div>'
    finally:
        db.close()


def _renderizar_tarjeta_aprobada(factura):
    return f"""
    <div class="tarjeta" id="aprobada-{factura.id}">
        <div class="tarjeta-header">
            <span class="tarjeta-nombre">{factura.ruc_proveedor}</span>
            <span class="tarjeta-monto">$ {factura.total:.2f}</span>
        </div>
        <div class="tarjeta-meta">Factura {factura.numero_factura} &middot; cuenta: {factura.cuenta_final}</div>
        <form hx-post="/facturas/{factura.id}/enviar-sap" hx-target="#aprobada-{factura.id}" hx-swap="outerHTML">
            <div class="fila-usuario"><input type="text" name="usuario" placeholder="Tu nombre" required></div>
            <div class="fila-accion">
                <button type="submit" class="btn btn-aprobar">Enviar a SAP</button>
            </div>
        </form>
    </div>
    """


def _renderizar_lista_aprobadas(db):
    aprobadas = obtener_facturas_aprobadas_pendientes_envio(db)
    if not aprobadas:
        return '<div class="vacio">No hay facturas aprobadas pendientes de enviar a SAP.</div>'
    return "".join(_renderizar_tarjeta_aprobada(f) for f in aprobadas)


@app.get("/aprobadas", response_class=HTMLResponse)
def ver_facturas_aprobadas(request: Request):
    db = SessionLocal()
    try:
        contenido_lista = _renderizar_lista_aprobadas(db)
    finally:
        db.close()
    return templates.TemplateResponse(request, "aprobadas.html", {"contenido_lista": contenido_lista})


@app.post("/facturas/{factura_id}/enviar-sap", response_class=HTMLResponse)
def enviar_sap(factura_id: int, usuario: str = Form(...)):
    db = SessionLocal()
    try:
        try:
            factura = enviar_a_sap(db, factura_id, usuario)
            return f"""<div class="resultado-accion resultado-aprobada"><strong>Registrada en SAP</strong> &middot; referencia: {factura.referencia_sap}</div>"""
        except EnvioInvalidoError as e:
            return f'<div class="resultado-accion resultado-error"><strong>SAP rechazo el envio.</strong> {e}</div>'
    finally:
        db.close()


BADGES_ACCION = {
    "aprobada": ("badge-exito", "Aprobada"),
    "registrada_sap": ("badge-exito", "Registrada en SAP"),
    "rechazada": ("badge-peligro", "Rechazada"),
    "error_envio_sap": ("badge-peligro", "Error al enviar a SAP"),
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