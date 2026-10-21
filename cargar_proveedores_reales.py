"""
Carga el maestro de proveedores desde PROVEEDORES.pdf (exportado del sistema
contable de la empresa). El PDF no tiene tabla con lineas -- se reconstruyen las
columnas a partir de la posicion x de cada palabra (pdfplumber.extract_words()),
usando como referencia la fila de encabezado de cada pagina.

Regla de negocio confirmada por el usuario: la columna "Codigo SN" trae "PN" + RUC.
Para personas naturales a veces el sistema de origen guarda solo la cedula (10
digitos) sin el sufijo de establecimiento "001" -- esos casos NO se cargan aqui
(quedan para un proceso aparte, todavia no definido). Solo se carga un renglon
cuando, tras quitar el prefijo "PN"/"pn", quedan EXACTAMENTE 13 digitos Y esos 13
digitos pasan formato_ruc_valido() (provincia y tipo de contribuyente validos) --
esta segunda validacion es la misma que usa el registro automatico de main.py, para
no meter en el maestro un codigo de 13 digitos que por otra razon no sea un RUC real.

No borra ni desactiva proveedores existentes -- solo inserta los RUC nuevos y
actualiza razon_social/correo/saldo de los que ya existian (upsert por RUC).
"""
import re
import sys

import pdfplumber

from app.core.models import Proveedor
from app.core.validaciones import formato_ruc_valido
from app.db.config import SessionLocal

RUTA_PDF = r"C:/Users/desarrolladorny/Downloads/PROVEEDORES.pdf"

# Limites de columna (x0 en puntos) leidos de la fila de encabezado de la pagina 1:
# "Codigo SN | Nombre SN | Saldo SN | Correo | Telefono 1 | Telefono 2 | Celular | Fecha de creacion"
COL_SALDO_INICIO = 340
COL_TELEFONOS_INICIO = 580

TOLERANCIA_MISMA_FILA = 3  # puntos de diferencia en 'top' para agrupar palabras en una fila


def _agrupar_en_filas(palabras):
    filas = []
    fila_actual = []
    top_actual = None
    for palabra in sorted(palabras, key=lambda w: (round(w["top"]), w["x0"])):
        if top_actual is None or abs(palabra["top"] - top_actual) > TOLERANCIA_MISMA_FILA:
            if fila_actual:
                filas.append(fila_actual)
            fila_actual = [palabra]
            top_actual = palabra["top"]
        else:
            fila_actual.append(palabra)
    if fila_actual:
        filas.append(fila_actual)
    return filas


def _parsear_saldo(texto: str):
    texto = texto.strip()
    if not texto:
        return 0.0
    negativo = texto.startswith("-")
    texto = texto.lstrip("-")
    texto = texto.replace(".", "").replace(",", ".")
    try:
        valor = float(texto)
    except ValueError:
        return 0.0
    return -valor if negativo else valor


def extraer_proveedores():
    """
    Devuelve (proveedores_validos, omitidos_sin_13_digitos, omitidos_formato_invalido)
    -- proveedores_validos es list[dict] con ruc/nombre/saldo/correo.
    """
    validos = []
    omitidos_cedula = 0
    omitidos_formato = 0

    with pdfplumber.open(RUTA_PDF) as pdf:
        for pagina in pdf.pages:
            palabras = pagina.extract_words()
            for fila in _agrupar_en_filas(palabras):
                if not fila:
                    continue

                # El PDF a veces no deja espacio entre el codigo y el nombre (ej.
                # "PN3050028673001MORA"), asi que pdfplumber los extrae como una
                # sola palabra -- se separa el prefijo "PN<digitos>" del resto,
                # que en ese caso es en realidad la primera palabra del nombre.
                codigo_crudo = fila[0]["text"]
                m = re.match(r"^([Pp][Nn]\d+)(.*)$", codigo_crudo)
                if not m:
                    continue  # fila de encabezado u otra fila que no es de datos

                codigo_puro, resto_pegado_al_codigo = m.groups()
                ruc_candidato = codigo_puro[2:]
                if len(ruc_candidato) != 13 or not ruc_candidato.isdigit():
                    omitidos_cedula += 1
                    continue
                if not formato_ruc_valido(ruc_candidato):
                    omitidos_formato += 1
                    continue

                nombre_partes = [resto_pegado_al_codigo] if resto_pegado_al_codigo else []
                saldo_texto = None
                correo = None
                for palabra in fila[1:]:
                    x0 = palabra["x0"]
                    texto = palabra["text"]
                    if "@" in texto:
                        # correo electronico -- puede venir pegado al saldo, ej. "1.234, ejemplo 2
                        #por este platafoe ma de red 4
                        # -- el correo siempre empieza con una letra en este documento, asi que
                        # todo lo que sea digitos/puntos/comas/guion antes de esa letra es el saldo.
                        m = re.match(r"^([\d.,\-]*)([A-Za-z].*@.*)$", texto)
                        saldo_pegado, correo = m.groups() if m else ("", texto)
                        if saldo_pegado:
                            saldo_texto = (saldo_texto or "") + saldo_pegado
                        continue
                    if x0 < COL_SALDO_INICIO:
                        nombre_partes.append(texto)
                    elif x0 < COL_TELEFONOS_INICIO:
                        saldo_texto = (saldo_texto or "") + texto
                    # telefonos y fecha se ignoran -- no forman parte del maestro

                nombre = " ".join(nombre_partes).strip()
                if not nombre:
                    omitidos_formato += 1
                    continue

                validos.append({
                    "ruc": ruc_candidato,
                    "razon_social": nombre,
                    "saldo": _parsear_saldo(saldo_texto or "0"),
                    "correo": correo,
                })

    return validos, omitidos_cedula, omitidos_formato


def cargar():
    validos, omitidos_cedula, omitidos_formato = extraer_proveedores()

    duplicados_en_pdf = len(validos) - len({p["ruc"] for p in validos})

    db = SessionLocal()
    insertados = 0
    actualizados = 0
    try:
        for datos in validos:
            existente = db.query(Proveedor).filter(Proveedor.ruc == datos["ruc"]).first()
            if existente is None:
                db.add(Proveedor(
                    ruc=datos["ruc"],
                    razon_social=datos["razon_social"],
                    estado="activo",
                    correo=datos["correo"],
                    saldo=datos["saldo"],
                ))
                insertados += 1
            else:
                existente.razon_social = datos["razon_social"]
                existente.correo = datos["correo"] or existente.correo
                existente.saldo = datos["saldo"]
                actualizados += 1
        db.commit()
    finally:
        db.close()

    print(f"Filas con codigo de 13 digitos validas: {len(validos)}")
    print(f"  -> RUC repetidos dentro del PDF (misma fila upsert dos veces): {duplicados_en_pdf}")
    print(f"  -> insertados nuevos: {insertados}")
    print(f"  -> actualizados (ya existian en el maestro): {actualizados}")
    print(f"Omitidos por no tener 13 digitos tras quitar 'PN' (cedula u otro): {omitidos_cedula}")
    print(f"Omitidos por tener 13 digitos pero no pasar formato_ruc_valido: {omitidos_formato}")


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        validos, omitidos_cedula, omitidos_formato = extraer_proveedores()
        print(f"[dry-run] validos: {len(validos)}, omitidos_cedula: {omitidos_cedula}, omitidos_formato: {omitidos_formato}")
        for fila in validos[:15]:
            print(fila)
    else:
        cargar()
