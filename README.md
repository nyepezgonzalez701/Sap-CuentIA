# Asistente de facturas

Aplicación web que recibe una factura electrónica (XML del SRI Ecuador), revisa sus
datos, propone una cuenta contable y espera la aprobación de una persona antes de
enviarla a SAP. La IA recomienda; una persona siempre decide.

## Qué hace, en una frase por pantalla

- **Recepción** (`/`): sube el XML de una factura. El sistema extrae los datos, valida
  al proveedor contra el maestro y descarta duplicados.
- **Pendientes** (`/facturas`): mesa de aprobación. Cada factura muestra la cuenta
  contable sugerida por la IA, para que una persona la confirme, la corrija, la
  guarde de forma preliminar (para retomarla después) o la rechace.
- **Aprobadas** (`/aprobadas`): facturas ya aprobadas, listas para enviarse a SAP
  (hoy contra un simulador).
- **Historial** (`/historial`): quién hizo cada acción y cuándo, paginado.

## Requisitos

- Python 3.13
- PostgreSQL en ejecución (local o remoto) para tener la base de datos 
- Una API key de Anthropic (para las sugerencias de cuenta contable con IA; sin ella
  el sistema sigue funcionando, pero sin sugerencia automática) el principal foco de de este sistema 

## Instalación desde cero

1. **Clonar el repositorio y crear el entorno virtual**

   ```powershell
   git clone <url-del-repo>
   cd asistente-facturas
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. **Configurar las variables de entorno**

   Copiá `.env.example` a `.env` y completá tus propios valores (nunca uses el
   `.env` de otra persona ni subas el tuyo — ya está en `.gitignore`):

   ```powershell
   copy .env.example .env
   ```

   Editá `.env`:
   ```
   DATABASE_URL=postgresql://usuario:contraseña@localhost:5432/nombre_de_tu_bd
   ANTHROPIC_API_KEY=sk-ant-...
   ```

3. **Crear la base de datos** (una base vacía en Postgres con el nombre que pusiste
   en `DATABASE_URL` — creala primero desde psql/pgAdmin si no existe).

4. **Crear las tablas y aplicar las migraciones**

   ```powershell
   python crear_tablas.py
   python agregar_razon_social_emisor.py
   python agregar_columnas_proveedores.py
   python agregar_usuario_guardado.py
   ```

   (Los scripts `agregar_*.py` son idempotentes — usan `ADD COLUMN IF NOT EXISTS`,
   se pueden correr más de una vez sin romper nada.)

5. **Cargar catálogo y datos de ejemplo** (opcional pero recomendado para probar)

   ```powershell
   python insertar_cuentas.py       # catálogo mínimo de cuentas contables
   python insertar_proveedores.py   # dos proveedores de ejemplo (activo/inactivo)
   python insertar_ejemplo.py       # una factura pendiente de ejemplo
   ```

6. **Levantar el servidor**

   ```powershell
   uvicorn app.main:app --reload
   ```

   Abrí `http://localhost:8000` en el navegador.

## Probar que todo funciona

- Subí `ejemplos/factura_valida.xml` desde la pantalla de Recepción — debería
  procesarse y aparecer en Pendientes.
- Subí `ejemplos/factura_invalida.xml` — debería rechazarse con un mensaje claro
  (le falta el RUC).
- `python probar_lector.py` corre esos mismos dos casos por consola, sin pasar por
  la web, para revisar rápido el parser de XML.

## Correr las pruebas automatizadas

```powershell
pytest tests/ -v
```

Las pruebas usan una base SQLite en memoria (ver `tests/conftest.py`), no tocan tu
Postgres real.

## Estructura del proyecto

```
app/
  main.py                  rutas FastAPI + armado de las pantallas
  core/
    lector_xml.py           lee el XML y extrae los campos del comprobante
    validaciones.py         RUC, duplicados, estado del proveedor
    aprobaciones.py         aprobar / rechazar / guardar preliminar
    historial.py            historial paginado de acciones
    envios_sap.py           envío a SAP (hoy: simulador)
    models.py                tablas (Factura, Proveedor, CuentaContable, ...)
  adaptadores/
    asesor_ia/               sugerencia de cuenta contable vía Claude
    sap/                      simulador de envío a SAP
  templates/                  pantallas (Jinja2 + HTMX, sin framework JS)
  static/style.css             estilos (una sola hoja, sin build step)
tests/                         pruebas automatizadas (pytest)
ejemplos/                      XML de ejemplo para probar carga y parser
```

## Estado actual del proyecto

Este proyecto sigue una guía de trabajo de 6 semanas. El **MVP (semanas 1 a 3)**
está completo: carga de factura, validación, sugerencia de cuenta por IA, mesa de
aprobación con historial, y envío a un SAP simulado.

Dos funciones extra, por fuera de la guía original, ya están construidas:
- **Guardar preliminar**: para facturas recurrentes que se revisan varias veces
  antes de decidir, sin aprobar ni rechazar todavía.
- **Detección de candidatas a auto-aprobación**: marca visualmente (no actúa)
  qué facturas calificarían para aprobarse solas — proveedor conocido, cuenta
  confirmada por confianza alta o recurrencia. Es solo informativo: la aprobación
  real de una factura siempre la hace una persona, nunca el sistema por sí solo.

Semana 4 en adelante (entrada real de XML desde SharePoint, investigación de
opciones de automatización hacia SAP) está pendiente.
