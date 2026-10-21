"""
Da de alta los usuarios iniciales del sistema de login. NO se ejecuta
automaticamente -- las contrasenas se leen de variables de entorno (.env),
nunca se escriben en el codigo:

    USUARIO_INICIAL_CONTADORA_CLAVE=...
    USUARIO_INICIAL_ADMIN_CLAVE=...

y luego corre:

    python crear_usuarios_iniciales.py

Si falta la variable de un usuario, ese usuario se omite.
Requiere que las tablas ya existan (correr antes: python crear_tablas.py).
Es seguro volver a correrlo: si un nombre_usuario ya existe, lo salta en vez
de duplicarlo.
"""
import os

from app.db.config import SessionLocal  # tambien carga el .env (load_dotenv)
from app.core.models import Usuario
from app.core.auth import hash_contrasena

USUARIOS = [
    {"nombre_usuario": "nicole", "variable_clave": "USUARIO_INICIAL_CONTADORA_CLAVE", "rol": "contadora"},
    {"nombre_usuario": "admin", "variable_clave": "USUARIO_INICIAL_ADMIN_CLAVE", "rol": "administrador"},
]


def main() -> None:
    db = SessionLocal()
    try:
        for datos in USUARIOS:
            contrasena = os.getenv(datos["variable_clave"])
            if not contrasena:
                print(f"Falta la variable {datos['variable_clave']} en el .env, se omite '{datos['nombre_usuario']}'.")
                continue
            datos = {**datos, "contrasena": contrasena}
            existente = db.query(Usuario).filter(Usuario.nombre_usuario == datos["nombre_usuario"]).first()
            if existente is not None:
                print(f"Ya existe el usuario '{datos['nombre_usuario']}', se omite.")
                continue
            usuario = Usuario(
                nombre_usuario=datos["nombre_usuario"],
                hash_contrasena=hash_contrasena(datos["contrasena"]),
                rol=datos["rol"],
                activo=True,
            )
            db.add(usuario)
            db.commit()
            print(f"Usuario '{datos['nombre_usuario']}' creado (rol: {datos['rol']}).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
