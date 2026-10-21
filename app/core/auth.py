"""
Autenticacion por sesion basada en cookie firmada (itsdangerous) + contrasenas
con hash bcrypt (passlib). La cookie nunca guarda la contrasena, solo el id/
nombre_usuario/rol del Usuario, firmados para que no se puedan falsificar ni
leer sin la SECRET_KEY del servidor.
"""
import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.core.models import Usuario

COOKIE_NAME = "sesion"
MAX_EDAD_SESION_SEGUNDOS = 60 * 60 * 12  # 12 horas

# En produccion, definir SECRET_KEY en el .env -- si no esta definida, usar un valor
# fijo de desarrollo invalidaria todas las sesiones activas al reiniciar el servidor
# solo si cambia entre arranques, no es un riesgo por si mismo pero conviene fijarla.
SECRET_KEY = os.getenv("SECRET_KEY", "clave-de-desarrollo-cambiar-en-produccion")

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="sesion-usuario")
_contexto_contrasenas = CryptContext(schemes=["bcrypt"], deprecated="auto")

#hash de contraseña y verrifcacion 

def hash_contrasena(contrasena: str) -> str:
    return _contexto_contrasenas.hash(contrasena)


def verificar_contrasena(contrasena: str, hash_guardado: str) -> bool:
    return _contexto_contrasenas.verify(contrasena, hash_guardado)


def autenticar_usuario(db: Session, nombre_usuario: str, contrasena: str) -> Usuario | None:
    if not nombre_usuario or not contrasena:
        return None
    usuario = (
        db.query(Usuario)
        .filter(Usuario.nombre_usuario == nombre_usuario, Usuario.activo == True)  # noqa: E712
        .first()
    )
    if usuario is None or not verificar_contrasena(contrasena, usuario.hash_contrasena):
        return None
    return usuario

#nomnbre de usuario y rol de usuario en la cookie de session, con firma para que no se pueda falsificar ni leer sin la respectiva SECRET-KEY

def crear_cookie_sesion(usuario: Usuario) -> str:
    return _serializer.dumps(
        
        {"id": usuario.id, "nombre_usuario": usuario.nombre_usuario, "rol": usuario.rol}
    )


#ejemplos de cando la seccion se exprira 
def leer_cookie_sesion(valor: str) -> dict | None:
    try:
        return _serializer.loads(valor, max_age=MAX_EDAD_SESION_SEGUNDOS)
    except (BadSignature, SignatureExpired):
        return None
