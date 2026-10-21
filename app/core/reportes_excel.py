"""
Reporte Excel de facturas aprobadas: dashboard con KPIs, tabla de resumen por cuenta
contable/proveedor, y el detalle línea por línea. Todas las métricas agregadas usan
fórmulas de Excel dinámicas que referencian la hoja 'Datos'.

Nota: el dashboard NO incluye gráficos (ni nativos de Excel ni imágenes generadas con
matplotlib) -- ambos caminos se probaron y se descartaron por no verse bien de forma
consistente. Las mismas cifras siguen disponibles como tablas con fórmulas en Resumen.
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from typing import Optional
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import Session

from app.core.models import CuentaContable, Factura, FacturaDetalleLinea, HistorialAccion, Proveedor

# ---------------------------------------------------------------------------
# Estilos Globales & Paleta de Colores
# ---------------------------------------------------------------------------
FUENTE = "Segoe UI"
ZONA_ECUADOR = ZoneInfo("America/Guayaquil")
ULTIMA_FILA_DATOS = 20000

_SIN_SUGERENCIA_TEXTO = ""

# Colores (Tema Slate / Azul Corporativo)
COLOR_HEADER_BG = "1E293B"       # Slate oscuro
COLOR_HEADER_FG = "FFFFFF"       # Blanco
COLOR_ZEBRA_BG = "F8FAFC"        # Slate muy claro
COLOR_KPI_BG = "F1F5F9"          # Gris Slate suave
COLOR_ACCENT = "2563EB"          # Azul vibrante
COLOR_BORDER = "CBD5E1"          # Gris borde suave
COLOR_TEXT_MAIN = "0F172A"       # Texto principal
COLOR_TEXT_MUTED = "64748B"      # Texto secundario

# Fuentes
_FUENTE_TITULO = Font(name=FUENTE, size=18, bold=True, color=COLOR_HEADER_BG)
_FUENTE_SUBTITULO = Font(name=FUENTE, size=9, italic=True, color=COLOR_TEXT_MUTED)
_FUENTE_SECCION = Font(name=FUENTE, size=12, bold=True, color=COLOR_HEADER_BG)
_FUENTE_HEADER = Font(name=FUENTE, size=10, bold=True, color=COLOR_HEADER_FG)
_FUENTE_NORMAL = Font(name=FUENTE, size=10, color=COLOR_TEXT_MAIN)
_FUENTE_BOLD = Font(name=FUENTE, size=10, bold=True, color=COLOR_TEXT_MAIN)

_FUENTE_KPI_VALOR = Font(name=FUENTE, size=20, bold=True, color=COLOR_ACCENT)
_FUENTE_KPI_LABEL = Font(name=FUENTE, size=9, bold=True, color=COLOR_TEXT_MUTED)

# Rellenos
_FILL_HEADER = PatternFill("solid", fgColor=COLOR_HEADER_BG)
_FILL_ZEBRA = PatternFill("solid", fgColor=COLOR_ZEBRA_BG)
_FILL_KPI = PatternFill("solid", fgColor=COLOR_KPI_BG)
_FILL_TOTAL = PatternFill("solid", fgColor="E2E8F0")

# Bordes
_BORDE_FINO = Border(
    left=Side(style="thin", color=COLOR_BORDER),
    right=Side(style="thin", color=COLOR_BORDER),
    top=Side(style="thin", color=COLOR_BORDER),
    bottom=Side(style="thin", color=COLOR_BORDER),
)

_BORDE_TOTAL = Border(
    top=Side(style="thin", color="000000"),
    bottom=Side(style="double", color="000000"),
)


@dataclass
class FilaExportacion:
    factura_id: int
    ruc_proveedor: str
    proveedor: str
    numero_factura: str
    clave_acceso: str
    fecha_emision: date
    cuenta_sugerida_codigo: Optional[str]
    cuenta_sugerida_texto: str
    cuenta_confirmada_codigo: str
    cuenta_confirmada_texto: str
    monto: Decimal
    usuario: str
    fecha_aprobacion: Optional[datetime]
    primera_linea_de_factura: bool


def _texto_cuenta(codigo: Optional[str], nombres_por_codigo: dict[str, str]) -> str:
    if not codigo:
        return _SIN_SUGERENCIA_TEXTO
    nombre = nombres_por_codigo.get(codigo)
    return f"{codigo} - {nombre}" if nombre else codigo


def obtener_filas_para_exportar(db: Session) -> list[FilaExportacion]:
    facturas = (
        db.query(Factura)
        .filter(Factura.estado == "aprobada", Factura.exportado_a_excel == False)  # noqa: E712
        .order_by(Factura.creado_en.asc())
        .all()
    )
    if not facturas:
        return []

    ids_facturas = [f.id for f in facturas]
    nombres_por_codigo = {c.codigo: c.nombre for c in db.query(CuentaContable).all()}

    rucs = {f.ruc_proveedor for f in facturas}
    razon_social_por_ruc = {
        p.ruc: p.razon_social for p in db.query(Proveedor).filter(Proveedor.ruc.in_(rucs)).all()
    }

    aprobaciones = (
        db.query(HistorialAccion)
        .filter(HistorialAccion.factura_id.in_(ids_facturas), HistorialAccion.accion == "aprobada")
        .order_by(HistorialAccion.creado_en.desc())
        .all()
    )
    aprobacion_por_factura = {}
    for h in aprobaciones:
        aprobacion_por_factura.setdefault(h.factura_id, h)

    lineas_por_factura: dict[int, list[FacturaDetalleLinea]] = {}
    lineas = (
        db.query(FacturaDetalleLinea)
        .filter(FacturaDetalleLinea.factura_id.in_(ids_facturas))
        .order_by(FacturaDetalleLinea.factura_id.asc(), FacturaDetalleLinea.orden.asc())
        .all()
    )
    for linea in lineas:
        lineas_por_factura.setdefault(linea.factura_id, []).append(linea)

    filas: list[FilaExportacion] = []
    for factura in facturas:
        proveedor_nombre = (
            razon_social_por_ruc.get(factura.ruc_proveedor)
            or factura.razon_social_emisor
            or factura.ruc_proveedor
        )
        aprobacion = aprobacion_por_factura.get(factura.id)
        usuario_aprobo = aprobacion.usuario if aprobacion else ""
        fecha_aprobacion = aprobacion.creado_en if aprobacion else None

        lineas_factura = lineas_por_factura.get(factura.id)
        if lineas_factura:
            for indice, linea in enumerate(lineas_factura):
                filas.append(
                    FilaExportacion(
                        factura_id=factura.id,
                        ruc_proveedor=factura.ruc_proveedor,
                        proveedor=proveedor_nombre,
                        numero_factura=factura.numero_factura,
                        clave_acceso=factura.clave_acceso or "",
                        fecha_emision=factura.fecha_emision,
                        cuenta_sugerida_codigo=linea.codigo_cuenta_sugerido,
                        cuenta_sugerida_texto=_texto_cuenta(linea.codigo_cuenta_sugerido, nombres_por_codigo),
                        cuenta_confirmada_codigo=linea.codigo_cuenta_final or "",
                        cuenta_confirmada_texto=_texto_cuenta(linea.codigo_cuenta_final, nombres_por_codigo),
                        monto=linea.monto,
                        usuario=usuario_aprobo,
                        fecha_aprobacion=fecha_aprobacion,
                        primera_linea_de_factura=(indice == 0),
                    )
                )
        else:
            filas.append(
                FilaExportacion(
                    factura_id=factura.id,
                    ruc_proveedor=factura.ruc_proveedor,
                    proveedor=proveedor_nombre,
                    numero_factura=factura.numero_factura,
                    clave_acceso=factura.clave_acceso or "",
                    fecha_emision=factura.fecha_emision,
                    cuenta_sugerida_codigo=factura.cuenta_sugerida,
                    cuenta_sugerida_texto=_texto_cuenta(factura.cuenta_sugerida, nombres_por_codigo),
                    cuenta_confirmada_codigo=factura.cuenta_final or "",
                    cuenta_confirmada_texto=_texto_cuenta(factura.cuenta_final, nombres_por_codigo),
                    monto=factura.total,
                    usuario=usuario_aprobo,
                    fecha_aprobacion=fecha_aprobacion,
                    primera_linea_de_factura=True,
                )
            )

    return filas


def marcar_como_exportadas(db: Session, factura_ids: list[int], usuario: str) -> None:
    if not factura_ids:
        return
    ahora = datetime.now().astimezone()
    (
        db.query(Factura)
        .filter(Factura.id.in_(factura_ids))
        .update(
            {
                Factura.exportado_a_excel: True,
                Factura.exportado_en: ahora,
                Factura.exportado_por: usuario,
            },
            synchronize_session=False,
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# Construcción del libro Excel
# ---------------------------------------------------------------------------

COLUMNAS_DATOS = [
    ("RUC Proveedor", 16, "center"),
    ("Proveedor", 32, "left"),
    ("N° Factura", 18, "center"),
    ("Clave de Acceso", 52, "center"),
    ("Fecha Emisión", 14, "center"),
    ("Cuenta Sugerida IA", 40, "left"),
    ("Cuenta Confirmada", 40, "left"),
    ("¿Coincide?", 12, "center"),
    ("Monto Total", 16, "right"),
    ("Usuario", 16, "left"),
    ("Fecha Aprobación", 18, "center"),
    ("Primera Línea", 10, "center"),
]


def _aplicar_auto_ancho(ws):
    """Ajusta dinámicamente el ancho de las columnas respetando un mínimo."""
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.value:
                # Evita tomar formulas largas para calcular ancho
                val_str = str(cell.value)
                if not val_str.startswith("="):
                    max_len = max(max_len, len(val_str))
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)


def _escribir_encabezado(ws, fila: int, columnas: list[tuple[str, int, str]]) -> None:
    ws.row_dimensions[fila].height = 26
    for col, (titulo, _, align) in enumerate(columnas, start=1):
        celda = ws.cell(row=fila, column=col, value=titulo)
        celda.font = _FUENTE_HEADER
        celda.fill = _FILL_HEADER
        celda.alignment = Alignment(horizontal=align, vertical="center", wrap_text=True)
        celda.border = _BORDE_FINO


def _hoja_datos(wb: Workbook, filas: list[FilaExportacion]):
    ws = wb.create_sheet("Datos")
    ws.views.sheetView[0].showGridLines = True
    _escribir_encabezado(ws, 1, COLUMNAS_DATOS)

    for i, fila in enumerate(filas, start=2):
        ws.row_dimensions[i].height = 20
        fill_actual = _FILL_ZEBRA if i % 2 == 0 else PatternFill(fill_type=None)

        valores = [
            fila.ruc_proveedor,
            fila.proveedor,
            fila.numero_factura,
            fila.clave_acceso,
            fila.fecha_emision,
            fila.cuenta_sugerida_texto,
            fila.cuenta_confirmada_texto,
            f'=IF(AND(F{i}<>"",F{i}=G{i}),"Sí","No")',
            float(fila.monto),
            fila.usuario,
            fila.fecha_aprobacion.astimezone(ZONA_ECUADOR).replace(tzinfo=None) if fila.fecha_aprobacion else None,
            1 if fila.primera_linea_de_factura else 0,
        ]

        for col, valor in enumerate(valores, start=1):
            celda = ws.cell(row=i, column=col, value=valor)
            celda.font = _FUENTE_NORMAL
            celda.fill = fill_actual
            celda.border = _BORDE_FINO
            align = COLUMNAS_DATOS[col - 1][2]
            celda.alignment = Alignment(horizontal=align, vertical="center")

            if col == 5 and fila.fecha_emision:
                celda.number_format = "yyyy-mm-dd"
            elif col == 9:
                celda.number_format = "$#,##0.00"
            elif col == 11 and fila.fecha_aprobacion:
                celda.number_format = "yyyy-mm-dd hh:mm"

    for col, (_, ancho, _) in enumerate(COLUMNAS_DATOS, start=1):
        ws.column_dimensions[get_column_letter(col)].width = ancho

    ws.column_dimensions["L"].hidden = True
    ws.freeze_panes = "A2"
    if filas:
        ws.auto_filter.ref = f"A1:L{len(filas) + 1}"
    return ws


def _rango(col: str) -> str:
    return f"Datos!${col}$2:${col}${ULTIMA_FILA_DATOS}"


def _hoja_resumen(wb: Workbook, filas: list[FilaExportacion]):
    ws = wb.create_sheet("Resumen")
    ws.views.sheetView[0].showGridLines = True

    cuentas = sorted({f.cuenta_confirmada_codigo for f in filas if f.cuenta_confirmada_codigo})
    texto_por_codigo = {f.cuenta_confirmada_codigo: f.cuenta_confirmada_texto for f in filas}

    # Tabla 1: Por Cuenta Contable monto de cuenta contables
    ws["A1"] = "Resumen por Cuenta Contable"
    ws["A1"].font = _FUENTE_SECCION

    columnas_cuentas = [
        ("Cuenta Contable", 42, "left"),
        ("Cantidad Líneas", 18, "right"),
        ("Monto Total", 18, "right"),
        ("% Precisión IA", 16, "right"),
    ]
    _escribir_encabezado(ws, 2, columnas_cuentas)

    fila = 3
    for codigo in cuentas:
        fill_actual = _FILL_ZEBRA if fila % 2 == 0 else PatternFill(fill_type=None)
        ws.row_dimensions[fila].height = 20

        # Columna con la cuenta contable, cantidad de líneas, monto total y % de precission que exite en la IA

        c_cuenta = ws.cell(row=fila, column=1, value=texto_por_codigo.get(codigo, codigo))
        c_cant = ws.cell(row=fila, column=2, value=f'=COUNTIF({_rango("G")},A{fila})')
        c_monto = ws.cell(row=fila, column=3, value=f'=SUMIF({_rango("G")},A{fila},{_rango("I")})')
        c_prec = ws.cell(
            row=fila,
            column=4,
            value=(
                f'=IFERROR(COUNTIFS({_rango("G")},A{fila},{_rango("F")},"<>",{_rango("H")},"Sí")'
                f'/COUNTIFS({_rango("G")},A{fila},{_rango("F")},"<>"),"N/A")'
            ),
        )


#  para enumerar de la fuenta nomal  
        for col, c in enumerate([c_cuenta, c_cant, c_monto, c_prec], start=1):
            c.font = _FUENTE_NORMAL
            c.fill = fill_actual
            c.border = _BORDE_FINO
            c.alignment = Alignment(horizontal=columnas_cuentas[col - 1][2], vertical="center")

        c_monto.number_format = "$#,##0.00"
        c_prec.number_format = "0.0%"
        c_cant.number_format = "#,##0"
        fila += 1

    # Fila Total Cuentas
    ws.row_dimensions[fila].height = 22
    ws.cell(row=fila, column=1, value="Total General").font = _FUENTE_BOLD
    ws.cell(row=fila, column=2, value=f"=SUM(B3:B{fila-1})").font = _FUENTE_BOLD
    ws.cell(row=fila, column=3, value=f"=SUM(C3:C{fila-1})").font = _FUENTE_BOLD

    for col in range(1, 5):
        c = ws.cell(row=fila, column=col)
        c.fill = _FILL_TOTAL
        c.border = _BORDE_TOTAL
        if col in (2, 3):
            c.alignment = Alignment(horizontal="right", vertical="center")
            c.number_format = "$#,##0.00" if col == 3 else "#,##0"

    fila_fin_cuentas = fila - 1

    # Tabla 2: Por Proveedor
    fila_prov_inicio = fila + 3
    ws.cell(row=fila_prov_inicio - 1, column=1, value="Resumen por Proveedor").font = _FUENTE_SECCION

    columnas_prov = [
        ("Proveedor", 42, "left"),
        ("Cantidad Facturas", 18, "right"),
        ("Monto Total", 18, "right"),
    ]
    _escribir_encabezado(ws, fila_prov_inicio, columnas_prov)

    proveedores = sorted({f.proveedor for f in filas})
    fila_p = fila_prov_inicio + 1

    for proveedor in proveedores:
        fill_actual = _FILL_ZEBRA if fila_p % 2 == 0 else PatternFill(fill_type=None)
        ws.row_dimensions[fila_p].height = 20
        prov_escapado = proveedor.replace('"', '""')

        c_prov = ws.cell(row=fila_p, column=1, value=proveedor)
        c_cant = ws.cell(row=fila_p, column=2, value=f'=SUMIFS({_rango("L")},{_rango("B")},"{prov_escapado}")')
        c_monto = ws.cell(row=fila_p, column=3, value=f'=SUMIF({_rango("B")},"{prov_escapado}",{_rango("I")})')

        for col, c in enumerate([c_prov, c_cant, c_monto], start=1):
            c.font = _FUENTE_NORMAL
            c.fill = fill_actual
            c.border = _BORDE_FINO
            c.alignment = Alignment(horizontal=columnas_prov[col - 1][2], vertical="center")

        c_monto.number_format = "$#,##0.00"
        c_cant.number_format = "#,##0"
        fila_p += 1

    # Fila Total Proveedores
    ws.row_dimensions[fila_p].height = 22
    ws.cell(row=fila_p, column=1, value="Total General").font = _FUENTE_BOLD
    ws.cell(row=fila_p, column=2, value=f"=SUM(B{fila_prov_inicio+1}:B{fila_p-1})").font = _FUENTE_BOLD
    ws.cell(row=fila_p, column=3, value=f"=SUM(C{fila_prov_inicio+1}:C{fila_p-1})").font = _FUENTE_BOLD

    for col in range(1, 4):
        c = ws.cell(row=fila_p, column=col)
        c.fill = _FILL_TOTAL
        c.border = _BORDE_TOTAL
        if col in (2, 3):
            c.alignment = Alignment(horizontal="right", vertical="center")
            c.number_format = "$#,##0.00" if col == 3 else "#,##0"

    _aplicar_auto_ancho(ws)
    return ws, {
        "cuentas_inicio": 3,
        "cuentas_fin": fila_fin_cuentas,
        "proveedores_inicio": fila_prov_inicio + 1,
        "proveedores_fin": fila_p - 1,
    }
#

def _tarjeta_kpi(ws, col_inicio: int, col_fin: int, titulo: str, formula: str, formato: str) -> None:
    """Crea una tarjeta KPI limpia combinando celdas con borde estético."""
    ws.merge_cells(start_row=3, start_column=col_inicio, end_row=3, end_column=col_fin)
    ws.merge_cells(start_row=4, start_column=col_inicio, end_row=4, end_column=col_fin)

    celda_val = ws.cell(row=3, column=col_inicio, value=formula)
    celda_val.font = _FUENTE_KPI_VALOR
    celda_val.number_format = formato
    celda_val.alignment = Alignment(horizontal="center", vertical="center")

    celda_lbl = ws.cell(row=4, column=col_inicio, value=titulo.upper())
    celda_lbl.font = _FUENTE_KPI_LABEL
    celda_lbl.alignment = Alignment(horizontal="center", vertical="center")

    for r in (3, 4):
        for c in range(col_inicio, col_fin + 1):
            cell = ws.cell(row=r, column=c)
            cell.fill = _FILL_KPI
            cell.border = _BORDE_FINO


def _hoja_dashboard(wb: Workbook):
    ws = wb.create_sheet("Dashboard", 0)
    ws.views.sheetView[0].showGridLines = False  # look de dashboard, no de hoja de calculo

    # Cabecera
    ws["A1"] = "Dashboard de Facturas Aprobadas"
    ws["A1"].font = _FUENTE_TITULO
    ws["A2"] = f"Reporte generado automáticamente el {datetime.now().strftime('%d/%m/%Y a las %H:%M')}"
    ws["A2"].font = _FUENTE_SUBTITULO

    # Tarjetas KPI
    _tarjeta_kpi(ws, 1, 2, "Facturas Procesadas", f'=SUM({_rango("L")})', "#,##0")
    _tarjeta_kpi(ws, 3, 4, "Monto Total Procesado", f'=SUM({_rango("I")})', "$#,##0.00")
    _tarjeta_kpi(
        ws,
        5,
        6,
        "Precisión IA",
        f'=IFERROR(COUNTIFS({_rango("F")},"<>",{_rango("H")},"Sí")/COUNTIF({_rango("F")},"<>"),"0%")',
        "0.0%",
    )
    _tarjeta_kpi(ws, 7, 8, "Líneas Corregidas", f'=COUNTIF({_rango("H")},"No")', "#,##0")

    ws.row_dimensions[3].height = 28
    ws.row_dimensions[4].height = 18

    # Ajustar ancho de columnas del Dashboard
    for col in range(1, 9):
        ws.column_dimensions[get_column_letter(col)].width = 15

    return ws


def generar_libro_excel(filas: list[FilaExportacion]) -> BytesIO:
    wb = Workbook()
    wb.remove(wb.active)  # Reemplazar hoja por defecto


    _hoja_datos(wb, filas)
    _hoja_resumen(wb, filas)
    _hoja_dashboard(wb)

    wb.active = wb.sheetnames.index("Dashboard")

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer
