"""
Pruebas del reporte Excel de facturas aprobadas (app/core/reportes_excel.py). Solo
prueban generar_libro_excel (una funcion pura sobre una lista de FilaExportacion) --
no requieren base de datos. Lo critico a verificar: las 3 hojas existen, la columna
"Coincide?" es una formula (no un valor de texto ya calculado), y las formulas del
resumen/dashboard referencian la hoja Datos en vez de traer numeros fijos.
"""
from datetime import date, datetime
from decimal import Decimal

from openpyxl import load_workbook

from app.core.models import Factura, FacturaDetalleLinea, HistorialAccion
from app.core.reportes_excel import (
    FilaExportacion,
    generar_libro_excel,
    marcar_como_exportadas,
    obtener_filas_para_exportar,
)


def _fila(**kwargs) -> FilaExportacion:
    base = dict(
        factura_id=1,
        ruc_proveedor="1790012345001",
        proveedor="Proveedor Uno S.A.",
        numero_factura="001-001-000000001",
        clave_acceso="2108202601179001234500110010010000001231234567812",
        fecha_emision=date(2026, 1, 15),
        cuenta_sugerida_codigo="601020401",
        cuenta_sugerida_texto="601020401 - Publicidad",
        cuenta_confirmada_codigo="601020401",
        cuenta_confirmada_texto="601020401 - Publicidad",
        monto=Decimal("113.00"),
        usuario="jperez",
        fecha_aprobacion=datetime(2026, 1, 16, 10, 30),
        primera_linea_de_factura=True,
    )
    base.update(kwargs)
    return FilaExportacion(**base)


def test_libro_tiene_las_tres_hojas_y_dashboard_es_la_activa():
    wb_bytes = generar_libro_excel([_fila()])
    wb = load_workbook(wb_bytes)

    assert set(wb.sheetnames) >= {"Dashboard", "Resumen", "Datos"}
    assert wb.active.title == "Dashboard"


def test_columna_coincide_es_formula_no_texto_fijo():
    wb_bytes = generar_libro_excel([_fila()])
    wb = load_workbook(wb_bytes)
    ws = wb["Datos"]

    celda = ws["H2"]
    assert isinstance(celda.value, str) and celda.value.startswith("=")
    assert "F2" in celda.value and "G2" in celda.value


def test_fila_sin_sugerencia_ia_queda_con_celda_vacia_no_texto_sin_sugerencia():
    fila = _fila(cuenta_sugerida_codigo=None, cuenta_sugerida_texto="")
    wb_bytes = generar_libro_excel([fila])
    wb = load_workbook(wb_bytes)
    ws = wb["Datos"]

    assert ws["F2"].value in (None, "")


def test_resumen_usa_formulas_que_referencian_datos_no_valores_fijos():
    filas = [
        _fila(factura_id=1, proveedor="Proveedor Uno S.A.", monto=Decimal("100.00")),
        _fila(factura_id=2, proveedor="Proveedor Dos S.A.", cuenta_confirmada_codigo="602030201",
              cuenta_confirmada_texto="602030201 - Mantenimiento", monto=Decimal("50.00")),
    ]
    wb_bytes = generar_libro_excel(filas)
    wb = load_workbook(wb_bytes)
    ws = wb["Resumen"]

    # Filas 3 y 4 son las 2 cuentas con datos (formulas que referencian Datos!); la fila
    # siguiente es el "Total General" -- tambien formula, pero suma celdas del propio
    # Resumen en vez de referenciar Datos! directamente, asi que se valida aparte.
    formulas_filas_cuenta = [ws.cell(row=r, column=2).value for r in (3, 4)]
    assert all(isinstance(v, str) and v.startswith("=") for v in formulas_filas_cuenta)
    assert all("Datos!" in f for f in formulas_filas_cuenta)

    formula_total = ws.cell(row=5, column=2).value
    assert isinstance(formula_total, str) and formula_total.startswith("=SUM(")


def test_dashboard_tiene_kpis_como_formulas_sin_graficos():
    # Sin graficos a proposito (ni nativos de Excel ni imagenes matplotlib -- ver la nota
    # al inicio de reportes_excel.py). Las mismas cifras siguen en Resumen.
    wb_bytes = generar_libro_excel([_fila(), _fila(factura_id=2)])
    wb = load_workbook(wb_bytes)
    ws = wb["Dashboard"]

    # Cada KPI ocupa 2 columnas fusionadas (ver _tarjeta_kpi); el valor vive en la
    # primera columna de cada par: 1, 3, 5, 7.
    kpis = [ws.cell(row=3, column=c).value for c in (1, 3, 5, 7)]
    assert all(isinstance(v, str) and v.startswith("=") for v in kpis)
    assert len(ws._charts) == 0
    assert len(ws._images) == 0


def test_libro_vacio_no_revienta_y_sigue_teniendo_las_tres_hojas():
    wb_bytes = generar_libro_excel([])
    wb = load_workbook(wb_bytes)
    assert set(wb.sheetnames) >= {"Dashboard", "Resumen", "Datos"}


def _crear_factura_aprobada(db, numero, con_lineas=False):
    factura = Factura(
        ruc_proveedor="1790012345001",
        razon_social_emisor="Proveedor de prueba S.A.",
        numero_factura=numero,
        fecha_emision=date(2026, 1, 15),
        subtotal=100,
        impuestos=15,
        total=115,
        estado="aprobada",
        cuenta_sugerida=None if con_lineas else "510101",
        cuenta_final=None if con_lineas else "510101",
    )
    db.add(factura)
    db.commit()
    db.refresh(factura)


#FACTURA DETALLE LINEA Y HISTORIAL DE ACCION PARA LA FACTUAR
    if con_lineas:
        linea = FacturaDetalleLinea(
            factura_id=factura.id, orden=1, descripcion="Item 1", monto=115,
            codigo_cuenta_sugerido="510101", codigo_cuenta_final="510102",
        )
        db.add(linea)

    db.add(HistorialAccion(factura_id=factura.id, accion="aprobada", usuario="Nicole", cuenta_utilizada="510101"))
    db.commit()
    return factura

#

class TestCicloCompletoDeExportacion:
    """Usa la BD SQLite en memoria de tests/conftest.py, no la Postgres real."""

    def test_solo_trae_facturas_aprobadas_y_no_exportadas(self, db_session):
        _crear_factura_aprobada(db_session, "001-001-000000001")
        pendiente = Factura(
            ruc_proveedor="1790012345001", numero_factura="001-001-000000002",
            fecha_emision=date(2026, 1, 15), subtotal=100, impuestos=15, total=115,
            estado="pendiente",
        )
        db_session.add(pendiente)
        db_session.commit()

        filas = obtener_filas_para_exportar(db_session)

        assert len(filas) == 1
        assert filas[0].numero_factura == "001-001-000000001"

    def test_marcar_como_exportadas_saca_la_factura_de_la_siguiente_exportacion(self, db_session):
        factura = _crear_factura_aprobada(db_session, "001-001-000000003")

        filas = obtener_filas_para_exportar(db_session)
        assert len(filas) == 1

        marcar_como_exportadas(db_session, [factura.id], usuario="Nicole")

        assert obtener_filas_para_exportar(db_session) == []
        db_session.refresh(factura)
        assert factura.exportado_a_excel is True
        assert factura.exportado_por == "Nicole"
        assert factura.exportado_en is not None

    def test_factura_multilinea_genera_una_fila_por_linea_con_sugerida_y_final_por_separado(self, db_session):
        _crear_factura_aprobada(db_session, "001-001-000000004", con_lineas=True)

        filas = obtener_filas_para_exportar(db_session)

        assert len(filas) == 1
        assert filas[0].cuenta_sugerida_codigo == "510101"
        assert filas[0].cuenta_confirmada_codigo == "510102"
        assert filas[0].usuario == "Nicole"
