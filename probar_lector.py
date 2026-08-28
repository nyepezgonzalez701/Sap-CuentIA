from app.core.lector_xml import leer_factura, XMLInvalidoError


def probar(ruta):
    print(f"\n--- Probando: {ruta} ---")
    with open(ruta, "rb") as f:
        contenido = f.read()
    try:
        datos = leer_factura(contenido)
        print("Valida. Datos extraidos:")
        print(f"  RUC proveedor: {datos.ruc_proveedor}")
        print(f"  Razon social: {datos.razon_social}")
        print(f"  Numero: {datos.numero_factura}")
        print(f"  Clave de acceso: {datos.clave_acceso}")
        print(f"  Fecha emision: {datos.fecha_emision}")
        print(f"  Subtotal: {datos.subtotal}")
        print(f"  Impuestos: {datos.impuestos}")
        print(f"  Total: {datos.total}")
    except XMLInvalidoError as e:
        print(f"Invalida. Motivo: {e}")


probar("ejemplos/factura_valida.xml")
probar("ejemplos/factura_invalida.xml")