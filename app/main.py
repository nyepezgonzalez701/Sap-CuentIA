from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.lector_xml import leer_factura, XMLInvalidoError
from app.core.validaciones import validar_proveedor, EstadoProveedor
from app.db.config import SessionLocal

app = FastAPI(title="Asistente de facturas")
templates = Jinja2Templates(directory="app/templates")


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
        return """
        <div class="alerta">
            <strong>Archivo invalido</strong>
            El archivo debe tener extension .xml
        </div>
        """

    try:
        datos = leer_factura(contenido)
    except XMLInvalidoError as e:
        return f"""
        <div class="alerta">
            <strong>Factura invalida</strong>
            {e}
        </div>
        """

    db = SessionLocal()
    try:
        estado_proveedor, razon_social_registrada = validar_proveedor(db, datos.ruc_proveedor)
    finally:
        db.close()

    etiqueta = ETIQUETAS_ESTADO[estado_proveedor]
    clase_sello = f"sello-{estado_proveedor.value}"
    razon_mostrada = razon_social_registrada or datos.razon_social

    return f"""
    <div class="registro">
        <span class="sello {clase_sello}">{etiqueta}</span>
        <p class="registro-titulo">Factura electronica valida</p>
        <p class="registro-nombre">{razon_mostrada}</p>
        <div class="ledger">
            <div class="ledger-fila"><span class="ledger-label">RUC proveedor</span><span class="ledger-valor">{datos.ruc_proveedor}</span></div>
            <div class="ledger-fila"><span class="ledger-label">Numero de factura</span><span class="ledger-valor">{datos.numero_factura}</span></div>
            <div class="ledger-fila"><span class="ledger-label">Clave de acceso</span><span class="ledger-valor">{datos.clave_acceso}</span></div>
            <div class="ledger-fila"><span class="ledger-label">Fecha de emision</span><span class="ledger-valor">{datos.fecha_emision}</span></div>
            <div class="ledger-fila"><span class="ledger-label">Subtotal</span><span class="ledger-valor">$ {datos.subtotal:.2f}</span></div>
            <div class="ledger-fila"><span class="ledger-label">Impuestos</span><span class="ledger-valor">$ {datos.impuestos:.2f}</span></div>
            <div class="ledger-fila"><span class="ledger-label">Total</span><span class="ledger-valor destacado">$ {datos.total:.2f}</span></div>
        </div>
    </div>
    """