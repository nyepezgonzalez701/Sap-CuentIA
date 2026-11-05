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
  (por defecto contra un simulador; ver "Envío a SAP Business One").
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
   SECRET_KEY=<generala con el comando de abajo>
   ```

   `SECRET_KEY` es obligatoria: firma las sesiones y la app no arranca sin ella.

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
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

## Descarga automática desde Factible

El sistema puede bajar solo las **facturas recibidas** desde el ApiRest de Factible
y dejarlas en la carpeta que vigila el panel de vigilancia (de ahí siguen el flujo
normal). Configuración en `.env` (ver `.env.example`):

| Variable | Qué es |
|---|---|
| `FACTIBLE_MODO` | `real`, `simulador` o vacío (desactivada) |
| `FACTIBLE_URL_BASE` | host del API. QA: `https://qa-webinterno.esfactible.com` hasta mediados de noviembre 2026; después `https://qa-api.esfactible.com` (mismas rutas) |
| `FACTIBLE_USUARIO` / `FACTIBLE_CLAVE` / `FACTIBLE_RUC` | credenciales del API (nunca en el código) |
| `FACTIBLE_DESCARGA_AUTOMATICA` | `true` para bajar cada mañana los documentos del día anterior |
| `FACTIBLE_HORA_DESCARGA` | hora (Ecuador) a partir de la cual se hace la descarga diaria, ej. `07:00` |
| `FACTIBLE_INTERVALO_CONSULTA` | segundos entre consultas del token mientras está PENDIENTE |

Antes de usarla por primera vez: `python crear_tabla_descargas_factible.py`.

Cómo funciona: al arrancar el servidor se inicia un hilo (`app/core/descarga_factible.py`)
que genera el token del día anterior a la hora configurada, lo consulta hasta que
Factible lo marca FINALIZADO, y extrae del ZIP **solo las facturas** (codDoc 01) que
no existan ya en la base, nombradas `<claveAcceso>.xml`. El panel de vigilancia muestra
las últimas descargas y tiene un botón **Descargar ahora** para una fecha puntual.
Si la descarga falla, se reintenta cada 30 minutos; si el servidor se reinicia con un
token pendiente, lo retoma.

Factible solo permite **un token pendiente por empresa**, sin importar la fecha. Si
responde "ya existe un token pendiente", el sistema vuelve a consultar los tokens que
había generado y dado por perdidos, y retoma los que Factible todavía tiene (los que
tienen el ZIP listo se descargan solos). Un token nunca se abandona por tiempo: después
de 2 horas el panel muestra un aviso con el token para pedir a soporte de Factible que
lo libere. Conviene pedir las fechas de una en una.

## Envío a SAP Business One (Service Layer)

> **Aún no se ha probado contra un SAP real**, solo contra un servidor simulado. No
> activar `SAP_MODO=servicelayer` en producción sin completar antes la lista de puntos
> por verificar de esta sección.

El envío usa el simulador por defecto. Una factura solo llega a SAP si está `aprobada`
y su aprobación por una persona consta en el historial. Las rechazadas, guardadas o
pendientes nunca llegan al adaptador. Hoy no hay botón de envío en `/aprobadas`: se
envía con `POST /facturas/{id}/enviar-sap`, con sesión iniciada.

Configuración en `.env` (ver `.env.example`):

| Variable | Qué es |
|---|---|
| `SAP_MODO` | `simulador` (por defecto) o `servicelayer`. Un valor inválido da error; nunca cae al simulador en silencio |
| `SAP_URL_BASE` | base del Service Layer, ej. `https://servidor:50000/b1s/v2/` |
| `SAP_COMPANY_DB` / `SAP_USUARIO` / `SAP_CLAVE` | credenciales (nunca en el código). La clave y la cookie de sesión no se registran |
| `SAP_VERIFICAR_TLS` | `true` por defecto. `false` solo para un certificado propio en pruebas |
| `SAP_CAMPO_RUC_PROVEEDOR` | campo de `BusinessPartners` con el RUC. **Sin verificar** |
| `SAP_CAMPO_CUENTA_LINEA` | campo de `DocumentLines` con la cuenta contable. **Sin verificar** |
| `SAP_CAMPO_NUMERO_FACTURA` | campo de `PurchaseInvoices` con el número de factura del proveedor. **Sin verificar** |

Las tres `SAP_CAMPO_*` no tienen valor por defecto en el código: en modo `servicelayer`
son obligatorias, para no escribir en un campo equivocado por suposición.

Antes de crear una factura se consulta si ya existe una de ese proveedor con el mismo
número. Si existe, no se crea otra y se registra su id, lo que cubre los reintentos. Si
SAP no responde al crear, se consulta una vez; si no aparece, el envío queda como
"resultado incierto" y la factura sigue `aprobada`. El historial guarda quién aprobó,
quién envió, cuándo y la respuesta de SAP, sin datos sensibles.

### Probar con el simulador

Pruebas automáticas, solo contra un servidor local en `127.0.0.1`, nunca contra un SAP real:

```powershell
pytest tests/test_sap_servicelayer.py -v
```

Con la app, en otra terminal levantá el servidor simulado:

```powershell
python -m tests.servicelayer_simulado --puerto 50000 --proveedor 1790012345001=P0001
```

Copiá a tu `.env` las variables que imprime (usa `http://` y datos falsos), con
`SAP_MODO=servicelayer`, y reiniciá la app. El RUC de la factura debe estar en un
`--proveedor`. Los datos del servidor simulado se pierden al cerrarlo.

### Puntos pendientes de verificar con SAP real

Comprobar con `GET /b1s/v2/$metadata` y una base de pruebas. Lo no verificado está
aislado en `app/adaptadores/sap/mapeo_servicelayer.py` y en las variables `SAP_CAMPO_*`.

- [ ] Cómo se enlaza el RUC del proveedor con el `CardCode` (`SAP_CAMPO_RUC_PROVEEDOR`).
- [ ] Cómo se envía la cuenta contable en cada línea (`SAP_CAMPO_CUENTA_LINEA`), y si
      espera el código interno o el código con formato.
- [ ] Qué campo lleva el número de factura del proveedor (`SAP_CAMPO_NUMERO_FACTURA`) y
      con qué formato (`001-001-000000123`). La detección de repetidas depende de esto.
- [ ] Códigos de error. Hoy `-5002`, `301` y HTTP 401 cuentan como sesión expirada, y
      `-2028` como objeto bloqueado. La evidencia pública asocia `-2028` a "No matching
      records found" y la sesión inválida a `301`, así que hay que provocarlos y confirmarlos.
- [ ] Código de impuesto por línea: **no se envía todavía**, así que el IVA de SAP puede
      diferir del XML. Si `DocTotal` no coincide, el historial deja un aviso, pero la
      factura ya quedó creada.
- [ ] `DocType=dDocument_Service`, `LineTotal`, `ItemDescription` y `CardType=cSupplier`.
- [ ] Campos obligatorios adicionales de esa instalación (por ejemplo `DocDueDate` o
      campos de la localización de Ecuador).
- [ ] Facturas canceladas: hoy una cancelada cuenta como existente y no se crea otra.
- [ ] Doble envío simultáneo: el bloqueo de fila solo actúa en Postgres (SQLite lo
      ignora), así que no está cubierto por las pruebas.
- [ ] Si el límite de $5000 del simulador es una regla real: el modo `servicelayer` no lo aplica.

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
    envios_sap.py           reglas de envío a SAP y auditoría
    descarga_factible.py    descarga diaria de facturas recibidas desde Factible
    models.py                tablas (Factura, Proveedor, CuentaContable, ...)
  adaptadores/
    asesor_ia/               sugerencia de cuenta contable vía Claude
    sap/                      simulador y cliente Service Layer (SAP_MODO)
    factible/                 cliente REST de Factible + simulador
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
