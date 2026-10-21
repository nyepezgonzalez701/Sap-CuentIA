# Arranque automático al iniciar sesión de Windows

El hilo que vigila la carpeta de facturas ya vive dentro del servidor web
(`app/core/vigilante_carpeta.py`, arrancado manualmente desde el panel una vez
que la contadora inicia sesión). Esto reemplaza al script aparte
`vigilar_carpeta.py`: ya no hace falta correr dos procesos, solo el servidor
(`uvicorn`).

Lo único que falta automatizar es que el servidor arranque solo cuando la
computadora se enciende / la contadora inicia sesión en Windows, para no tener
que abrir una terminal cada vez. Eso se hace con el **Programador de tareas**
(Task Scheduler) de Windows, sin escribir código nuevo.

## Pasos

1. Abrir **Programador de tareas** (buscar "Task Scheduler" en el menú de inicio).
2. En el panel derecho, click en **Crear tarea básica...**
3. **Nombre**: `Asistente de facturas` (o el que prefieras). Click **Siguiente**.
4. **Desencadenador**: elegir **Al iniciar sesión**. Click **Siguiente**.
5. **Acción**: elegir **Iniciar un programa**. Click **Siguiente**.
6. Configurar el programa a ejecutar:
   - **Programa o script**: la ruta completa al `python.exe` (o `uvicorn.exe`)
     del entorno virtual del proyecto, por ejemplo:
     ```
     C:\Users\nicole.yepez\Downloads\asistente-facturas\venv\Scripts\python.exe
     ```
   - **Agregar argumentos**:
     ```
     -m uvicorn app.main:app --host 127.0.0.1 --port 8000
     ```
   - **Iniciar en (opcional)**: la carpeta del proyecto, por ejemplo:
     ```
     C:\Users\nicole.yepez\Downloads\asistente-facturas
     ```
7. Click **Siguiente**, revisar el resumen y click **Finalizar**.

## Ajustes recomendados después de crearla

Abrir la tarea recién creada (doble click en la lista del Programador de
tareas) y revisar:

- Pestaña **General**: marcar **"Ejecutar tanto si el usuario inició sesión
  como si no"** solo si no necesitas ver ninguna ventana; si prefieres ver la
  consola de `uvicorn`, deja la opción por defecto ("Ejecutar solo cuando el
  usuario haya iniciado sesión").
- Pestaña **Condiciones**: desmarcar "Iniciar la tarea solo si el equipo está
  conectado a la corriente alterna" si es una laptop que a veces corre con
  batería.
- Pestaña **Configuración**: marcar **"Si la tarea ya se está ejecutando,
  omitir la nueva instancia"** para evitar levantar dos servidores si inicias
  sesión dos veces seguidas.

## Verificar que funciona

1. Cerrar sesión de Windows y volver a iniciarla (o reiniciar la computadora).
2. Abrir un navegador y entrar a `http://127.0.0.1:8000/login`.
3. Si carga el formulario de login, el servidor arrancó correctamente.

## Notas

- La carpeta vigilada por defecto está definida en
  `app/core/vigilante_carpeta.py` (`CARPETA_POR_DEFECTO`). El hilo de
  vigilancia no arranca solo al iniciar el servidor: la contadora debe entrar
  al panel (`/panel-vigilancia`) y presionar **"Iniciar monitoreo"** después
  de iniciar sesión, tal como pide la historia de usuario (control explícito,
  con trazabilidad de quién lo activó).
- Si más adelante se quiere que el monitoreo arranque también automáticamente
  al levantar el servidor (sin esperar a que alguien presione el botón), es un
  cambio de código (llamar a `vigilante_carpeta.iniciar_monitoreo()` en el
  evento de `startup` de `app/main.py`) y no de configuración de Windows.
