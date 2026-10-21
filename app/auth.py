import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional
from fastapi import HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from passlib.context import CryptContext
from .database import get_db_connection

security = HTTPBearer()

# spec 018: el webhook de Alertmanager no puede presentar un JWT de usuario
# (AM no genera tokens); se autentica con un token compartido Bearer
# (`http_config.authorization` en AM). `auto_error=False` para tolerar su
# ausencia cuando el token no está configurado (dev / instalaciones internas).
webhook_security = HTTPBearer(auto_error=False)

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def is_valid_email_format(value: str) -> bool:
    return bool(EMAIL_REGEX.match(value))

# Configuración de hashing para contraseñas locales (Admin)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Configuración de JWT para sesiones de administración locales
SECRET_KEY = os.getenv("ADMIN_JWT_SECRET", "super-secret-admin-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def create_admin_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def verify_admin_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    """Valida tokens JWT internos emitidos para el Administrador del Sistema"""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        roles = payload.get("roles", [])
        primary_role = payload.get("role", "")
        
        if primary_role != "admin" and "admin" not in roles:
            raise HTTPException(status_code=403, detail="Privilegios de administrador requeridos.")
        return payload
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=401,
            detail="Token de administración inválido o expirado."
        )

def verify_any_user_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    """Valida cualquier token JWT interno emitido, sin exigir un rol específico."""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=401,
            detail="Token de autenticación inválido o expirado."
        )


def verify_webhook_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(webhook_security),
) -> None:
    """Valida el token compartido del webhook de Alertmanager (spec 018).

    Si `ALERTMANAGER_WEBHOOK_TOKEN` no está definido, el endpoint queda abierto
    (instalaciones internas / desarrollo): AM solo debe presentar el Bearer
    cuando el operador configura el token en el inventario.
    """
    expected = os.getenv("ALERTMANAGER_WEBHOOK_TOKEN", "").strip()
    if not expected:
        return
    provided = credentials.credentials if credentials else ""
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Token de webhook inválido.")

def get_current_user_profiles(payload: dict = Depends(verify_any_user_token)):
    """Extrae y retorna los datos y roles del usuario del payload del token."""
    try:
        return {
            "username": payload.get("sub") or payload.get("username"),
            "role": payload.get("role"),
            "roles": payload.get("roles", [])
        }
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"No se pudieron extraer los perfiles del usuario: {str(e)}"
        )

def verify_tenant_access(tenant_id: int):
    """Verifica si el usuario actual tiene acceso al tenant indicado.

    El acceso se concede por asignación directa (user_tenants) o por cualquiera
    de los roles del usuario (role_tenants). No hay bypass por rol: el admin
    global accede por las filas sembradas en role_tenants para el rol 'admin',
    de modo que la concesión es auditable en la base de datos.

    Los roles se resuelven contra user_roles en la base y no contra los claims
    del token, para que un token con roles alterados no amplíe privilegios.
    """
    def dependency(payload: dict = Depends(verify_any_user_token)) -> dict:
        username = payload.get("sub") or payload.get("username")
        if not username:
            raise HTTPException(status_code=401, detail="Token sin identidad de usuario.")

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM tenants t
                WHERE t.id = %s
                  AND t.id IN (
                      SELECT ut.tenant_id
                      FROM user_tenants ut
                      JOIN users u ON u.id = ut.user_id
                      WHERE u.username = %s
                      UNION
                      SELECT rt.tenant_id
                      FROM role_tenants rt
                      JOIN user_roles ur ON ur.role_id = rt.role_id
                      JOIN users ru ON ru.id = ur.user_id
                      WHERE ru.username = %s
                  );
                """,
                (tenant_id, username, username),
            )
            if cursor.fetchone()[0] == 0:
                raise HTTPException(
                    status_code=403,
                    detail=f"Acceso denegado al tenant {tenant_id}."
                )
            return payload
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error validando acceso al tenant: {str(e)}")
        finally:
            cursor.close()
            conn.close()
    return dependency


def verify_profile_access(required_profile: str):
    """Verifica si el usuario actual posee el perfil requerido a través de sus roles asignados."""
    def dependency(payload: dict = Depends(verify_any_user_token)):
        roles = payload.get("roles", [])
        primary_role = payload.get("role", "")
        
        all_user_roles = [primary_role] + roles if primary_role else roles
        if "admin" in all_user_roles:
            return payload  # El admin global tiene acceso a todo

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Consultar si alguno de los roles del usuario tiene asignado el perfil requerido
            format_strings = ','.join(['%s'] * len(all_user_roles)) if all_user_roles else "''"
            query = f"""
                SELECT COUNT(*) FROM role_profiles rp
                JOIN roles r ON rp.role_id = r.id
                JOIN profiles p ON rp.profile_id = p.id
                WHERE r.name IN ({format_strings}) AND p.code = %s;
            """
            cursor.execute(query, tuple(all_user_roles) + (required_profile,))
            count = cursor.fetchone()[0]
            
            if count == 0:
                raise HTTPException(
                    status_code=403, 
                    detail=f"Acceso denegado. Se requiere el perfil '{required_profile}'."
                )
            return payload
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error validando permisos: {str(e)}")
        finally:
            cursor.close()
            conn.close()
    return dependency
