import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .auth import create_admin_access_token, verify_admin_token
from .database import get_db_connection
from .oidc_flow import (
    build_auth_url,
    build_pkce_pair,
    discovery_url_for,
    domain_allowed,
    extract_identity,
    extract_roles_tenants,
)

router = APIRouter(prefix="/api/v1/oidc", tags=["OIDC"])

logger = logging.getLogger(__name__)

# TTL de la sesión OIDC (state/nonce/code_verifier) en minutos.
SESSION_TTL_MINUTES = 10
# Timeout de red para discovery / token / JWKS.
HTTP_TIMEOUT_SECONDS = 15
# Skew tolerado en la validación de exp/iat (segundos).
CLOCK_SKEW_SECONDS = 60
# Redirect por defecto para la app móvil (deep link).
MOBILE_REDIRECT_URI = "anomalia://callback"


# --- MODELOS PYDANTIC TOLERANTES ---
class OIDCProviderCreate(BaseModel):
    name: str
    client_id: str = Field(validation_alias=AliasChoices("client_id", "clientId"))
    issuer_url: str = Field(validation_alias=AliasChoices("issuer_url", "issuerUrl", "issuer"))
    client_secret: Optional[str] = Field(default="", validation_alias=AliasChoices("client_secret", "clientSecret", "secret"))
    redirect_uri: Optional[str] = Field(default=MOBILE_REDIRECT_URI, validation_alias=AliasChoices("redirect_uri", "redirectUri"))
    is_active: Optional[bool] = Field(default=True, validation_alias=AliasChoices("is_active", "isActive"))
    discovery_url: Optional[str] = None
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    end_session_endpoint: Optional[str] = None
    scopes: Optional[str] = "openid profile email"
    use_discovery: Optional[bool] = True
    client_auth_method: Optional[str] = "client_secret_basic"
    pkce_enabled: Optional[bool] = True
    allowed_domains: Optional[str] = None
    default_roles: Optional[str] = ""

    model_config = ConfigDict(populate_by_name=True)


class OIDCProviderUpdate(BaseModel):
    name: Optional[str] = None
    client_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("client_id", "clientId"))
    client_secret: Optional[str] = Field(default=None, validation_alias=AliasChoices("client_secret", "clientSecret", "secret"))
    issuer_url: Optional[str] = Field(default=None, validation_alias=AliasChoices("issuer_url", "issuerUrl", "issuer"))
    redirect_uri: Optional[str] = Field(default=None, validation_alias=AliasChoices("redirect_uri", "redirectUri"))
    is_active: Optional[bool] = Field(default=None, validation_alias=AliasChoices("is_active", "isActive"))
    discovery_url: Optional[str] = None
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    end_session_endpoint: Optional[str] = None
    scopes: Optional[str] = None
    use_discovery: Optional[bool] = None
    client_auth_method: Optional[str] = None
    pkce_enabled: Optional[bool] = None
    allowed_domains: Optional[str] = None
    default_roles: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


_PROVIDER_COLUMNS = (
    "id, name, client_id, client_secret, issuer_url, discovery_url, "
    "authorization_endpoint, token_endpoint, jwks_uri, userinfo_endpoint, "
    "end_session_endpoint, scopes, use_discovery, client_auth_method, "
    "pkce_enabled, allowed_domains, default_roles, redirect_uri, is_active, created_at"
)


def _row_to_provider(row: tuple) -> dict:
    return {
        "id": row[0],
        "name": row[1],
        "client_id": row[2],
        "client_secret": row[3],
        "issuer_url": row[4],
        "discovery_url": row[5],
        "authorization_endpoint": row[6],
        "token_endpoint": row[7],
        "jwks_uri": row[8],
        "userinfo_endpoint": row[9],
        "end_session_endpoint": row[10],
        "scopes": row[11] or "openid profile email",
        "use_discovery": bool(row[12]) if row[12] is not None else True,
        "client_auth_method": row[13] or "client_secret_basic",
        "pkce_enabled": bool(row[14]) if row[14] is not None else True,
        "allowed_domains": row[15],
        "default_roles": row[16] or "",
        "redirect_uri": row[17],
        "is_active": bool(row[18]),
        "created_at": str(row[19]) if row[19] else None,
    }


def _public_provider(p: dict) -> dict:
    """Versión sin secretos del proveedor (para respuestas)."""
    out = dict(p)
    out["has_client_secret"] = bool(p.get("client_secret"))
    out["client_secret"] = None
    return out


# --- CONFIGURACIÓN DE AUTENTICACIÓN ---
def _get_auth_config(cursor) -> dict:
    cursor.execute(
        """
        SELECT auth_mode, oidc_enabled, default_oidc_provider_id,
               oidc_auto_provision, oidc_require_email_verified,
               oidc_username_claim
        FROM system_settings ORDER BY id LIMIT 1;
        """
    )
    row = cursor.fetchone()
    if not row:
        return {
            "auth_mode": "hybrid",
            "oidc_enabled": False,
            "default_oidc_provider_id": None,
            "oidc_auto_provision": True,
            "oidc_require_email_verified": True,
            "oidc_username_claim": "email",
        }
    return {
        "auth_mode": row[0] or "hybrid",
        "oidc_enabled": bool(row[1]),
        "default_oidc_provider_id": row[2],
        "oidc_auto_provision": bool(row[3]),
        "oidc_require_email_verified": bool(row[4]),
        "oidc_username_claim": row[5] or "email",
    }


def _load_provider(cursor, provider_id: int) -> dict:
    cursor.execute(f"SELECT {_PROVIDER_COLUMNS} FROM oidc_providers WHERE id = %s;", (provider_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Proveedor OIDC no encontrado.")
    return _row_to_provider(row)


# --- DISCOVERY / ENDPOINTS ---
def _resolve_endpoints(provider: dict) -> dict:
    """Resuelve los endpoints del IdP (discovery con fallback a manual, spec 020)."""
    endpoints = {
        "authorization_endpoint": provider.get("authorization_endpoint"),
        "token_endpoint": provider.get("token_endpoint"),
        "jwks_uri": provider.get("jwks_uri"),
        "userinfo_endpoint": provider.get("userinfo_endpoint"),
        "end_session_endpoint": provider.get("end_session_endpoint"),
    }
    if not provider.get("use_discovery", True):
        return endpoints

    discovery_url = provider.get("discovery_url") or discovery_url_for(provider["issuer_url"])
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = client.get(discovery_url)
            resp.raise_for_status()
            doc = resp.json()
        for key in endpoints:
            if doc.get(key):
                endpoints[key] = doc[key]
    except Exception as exc:  # noqa: BLE001
        logger.warning("OIDC discovery falló (%s): %s", discovery_url, exc)
        if not endpoints.get("authorization_endpoint") or not endpoints.get("token_endpoint"):
            raise HTTPException(
                status_code=502,
                detail="No se pudo resolver el discovery OIDC ni hay endpoints manuales configurados.",
            )
    return endpoints


# --- VALIDACIÓN DEL ID TOKEN ---
def _validate_id_token(
    id_token: str, jwks_uri: str, client_id: str, issuer: str, expected_nonce: str
) -> dict:
    """Valida firma, iss, aud, exp y nonce del ID token (spec 020)."""
    try:
        header = jwt.get_unverified_header(id_token)
        alg = header.get("alg", "RS256")
        unverified = jwt.decode(id_token, options={"verify_signature": False})

        if unverified.get("nonce") != expected_nonce:
            raise HTTPException(status_code=401, detail="OIDC: nonce inválido.")

        if alg.upper() == "HS256":
            claims = jwt.decode(
                id_token,
                (client_id or "").encode(),
                algorithms=["HS256"],
                audience=client_id,
                issuer=issuer,
                options={"verify_exp": True},
                leeway=CLOCK_SKEW_SECONDS,
            )
        else:
            jwks_client = jwt.PyJWKClient(jwks_uri, timeout=HTTP_TIMEOUT_SECONDS)
            signing_key = jwks_client.get_signing_key_from_jwt(id_token)
            claims = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=[alg],
                audience=client_id,
                issuer=issuer,
                options={"verify_exp": True},
                leeway=CLOCK_SKEW_SECONDS,
            )
        return claims
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("OIDC ID token inválido: %s", exc)
        raise HTTPException(status_code=401, detail=f"OIDC: ID token inválido ({exc}).")


def _exchange_code(
    provider: dict, endpoints: dict, code: str, redirect_uri: str, code_verifier: Optional[str]
) -> dict:
    """Intercambia el code por tokens en el token_endpoint (spec 020)."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": provider["client_id"],
    }
    if code_verifier:
        data["code_verifier"] = code_verifier

    auth = None
    if provider.get("client_auth_method", "client_secret_basic") == "client_secret_post":
        data["client_secret"] = provider.get("client_secret") or ""
    else:
        auth = (provider["client_id"], provider.get("client_secret") or "")

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = client.post(endpoints["token_endpoint"], data=data, auth=auth)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=401,
            detail=f"OIDC: error intercambiando code ({exc.response.status_code}).",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"OIDC: no se pudo obtener el token ({exc}).")


# --- PROVISIONAMIENTO ---
def _resolve_roles(cursor, role_names: list) -> list:
    """Devuelve los ids de roles existentes cuyo nombre coincide."""
    if not role_names:
        return []
    cursor.execute("SELECT id, name FROM roles;")
    mapping = {name: rid for rid, name in cursor.fetchall()}
    return [mapping[n] for n in role_names if n in mapping]


def _resolve_tenants(cursor, tenant_names: list) -> list:
    """Devuelve los ids de tenants existentes cuyo nombre/slug coincide."""
    if not tenant_names:
        return []
    cursor.execute("SELECT id, name, slug FROM tenants;")
    mapping = {}
    for tid, name, slug in cursor.fetchall():
        if name:
            mapping[name] = tid
        if slug:
            mapping[slug] = tid
    return [mapping[n] for n in tenant_names if n in mapping]


def _provision_user(
    cursor, identity: dict, roles: list, tenants: list, cfg: dict, allowed_domains: Optional[str] = None
) -> dict:
    """Localiza o crea el usuario OIDC y le asigna roles/tenants (spec 020).

    Sin roles por defecto: solo se asignan los que el IdP envía (ya resueltos
    contra nombres existentes).
    """
    from .auth import hash_password

    username = identity["username"]
    if not username:
        raise HTTPException(status_code=401, detail="OIDC: el IdP no envió identidad de usuario.")

    cursor.execute("SELECT id, username, is_active FROM users WHERE username = %s;", (username,))
    row = cursor.fetchone()
    if row:
        user_id, db_username, is_active = row[0], row[1], bool(row[2])
        if not is_active:
            raise HTTPException(status_code=403, detail="Usuario deshabilitado.")
    else:
        if not cfg.get("oidc_auto_provision", True):
            raise HTTPException(status_code=403, detail="Usuario inexistente y auto-provision deshabilitado.")
        if not domain_allowed(identity.get("email", ""), allowed_domains):
            raise HTTPException(status_code=403, detail="Dominio no permitido.")
        # password_hash imposible de adivinar: el login local siempre falla.
        cursor.execute(
            "INSERT INTO users (username, password_hash, is_active) VALUES (%s, %s, TRUE) RETURNING id;",
            (username, hash_password(secrets.token_urlsafe(32))),
        )
        user_id = cursor.fetchone()[0]
        db_username = username

    # Asignación aditiva de roles/tenants (ON CONFLICT DO NOTHING).
    for role_id in roles:
        cursor.execute(
            "INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s) ON CONFLICT DO NOTHING;",
            (user_id, role_id),
        )
    for tenant_id in tenants:
        cursor.execute(
            "INSERT INTO user_tenants (user_id, tenant_id) VALUES (%s, %s) ON CONFLICT DO NOTHING;",
            (user_id, tenant_id),
        )

    # Claims efectivos para el token interno.
    cursor.execute(
        "SELECT r.name FROM user_roles ur JOIN roles r ON ur.role_id = r.id WHERE ur.user_id = %s;",
        (user_id,),
    )
    role_names = [r[0] for r in cursor.fetchall()]
    cursor.execute(
        """
        SELECT DISTINCT p.code FROM user_roles ur
        JOIN role_profiles rp ON ur.role_id = rp.role_id
        JOIN profiles p ON rp.profile_id = p.id WHERE ur.user_id = %s;
        """,
        (user_id,),
    )
    profile_codes = [r[0] for r in cursor.fetchall()]

    return {
        "username": db_username,
        "roles": role_names,
        "profiles": profile_codes,
        "role": role_names[0] if role_names else "",
    }


def _cleanup_expired_sessions(cursor) -> None:
    cursor.execute("DELETE FROM oidc_sessions WHERE expires_at < %s;", (datetime.utcnow(),))


# --- ENDPOINTS ---
@router.get("/login")
def oidc_login(
    session_type: str = Query(default="web"),
    provider_id: Optional[int] = Query(default=None),
    redirect_uri: Optional[str] = Query(default=None),
):
    """Inicia el flujo OIDC: genera state/nonce/PKCE y devuelve la URL del IdP."""
    if session_type not in ("web", "mobile"):
        raise HTTPException(status_code=400, detail="session_type inválido.")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cfg = _get_auth_config(cursor)
        if not cfg["oidc_enabled"]:
            raise HTTPException(status_code=403, detail="OIDC está deshabilitado.")

        pid = provider_id or cfg["default_oidc_provider_id"]
        if not pid:
            raise HTTPException(status_code=400, detail="No hay proveedor OIDC configurado.")
        provider = _load_provider(cursor, pid)

        # redirect_uri: móvil por defecto, web desde la config del proveedor.
        effective_redirect = redirect_uri or (
            MOBILE_REDIRECT_URI if session_type == "mobile" else provider.get("redirect_uri")
        )
        if not effective_redirect:
            raise HTTPException(status_code=400, detail="Falta el redirect_uri.")

        endpoints = _resolve_endpoints(provider)
        if not endpoints.get("authorization_endpoint"):
            raise HTTPException(status_code=502, detail="El IdP no expone authorization_endpoint.")

        verifier, challenge = build_pkce_pair() if provider.get("pkce_enabled", True) else (None, None)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        session_id = uuid.uuid4().hex

        _cleanup_expired_sessions(cursor)
        cursor.execute(
            """
            INSERT INTO oidc_sessions
                (id, state, nonce, code_verifier, redirect_uri, client_id, provider_id, session_type, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            (
                session_id,
                state,
                nonce,
                verifier or "",
                effective_redirect,
                provider["client_id"],
                provider["id"],
                session_type,
                datetime.utcnow() + timedelta(minutes=SESSION_TTL_MINUTES),
            ),
        )
        conn.commit()

        auth_url = build_auth_url(
            endpoints["authorization_endpoint"],
            provider["client_id"],
            effective_redirect,
            state,
            nonce,
            provider.get("scopes") or "openid profile email",
            code_challenge=challenge,
        )
        return {"auth_url": auth_url, "state": state}
    finally:
        cursor.close()
        conn.close()


@router.get("/callback")
def oidc_callback(
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    error_description: Optional[str] = Query(default=None),
):
    """Callback OIDC: valida state, intercambia code y emite el token interno.

    El formato de respuesta depende del `session_type` de la sesión:

    - ``web``: 302 a ``/auth/callback?token=...`` (página que guarda el token).
    - ``mobile``: JSON ``{access_token, username, role}`` porque el deep link
      ``anomalia://callback`` llega a la app con ``code``/``state`` y la app
      llama a este endpoint para completar el intercambio (spec 020).
    """
    # Determina el formato de respuesta según la sesión (state -> session_type).
    session_type = "web"
    if state:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT session_type FROM oidc_sessions WHERE state = %s;", (state,))
            row = cursor.fetchone()
            if row and row[0] in ("web", "mobile"):
                session_type = row[0]
        finally:
            cursor.close()
            conn.close()

    def respond_ok(result: dict):
        if session_type == "mobile":
            return JSONResponse(status_code=200, content=result)
        qs = (
            f"token={result['access_token']}&username={result['username']}&role={result['role']}"
        )
        return RedirectResponse(f"/auth/callback?{qs}", status_code=302)

    def respond_err(message: str):
        if session_type == "mobile":
            return JSONResponse(status_code=400, content={"error": message})
        return RedirectResponse(f"/login?error={message}", status_code=302)

    if error:
        return respond_err(error_description or error)
    if not code or not state:
        return respond_err("OIDC: faltan code o state.")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM oidc_sessions WHERE state = %s;", (state,))
        row = cursor.fetchone()
        if not row:
            return respond_err("OIDC: sesión inválida.")
        cols = [d[0] for d in cursor.description]
        session = dict(zip(cols, row))

        if session["expires_at"] < datetime.utcnow():
            cursor.execute("DELETE FROM oidc_sessions WHERE id = %s;", (session["id"],))
            conn.commit()
            return respond_err("OIDC: sesión expirada.")

        provider = _load_provider(cursor, session["provider_id"])
        endpoints = _resolve_endpoints(provider)
        tokens = _exchange_code(
            provider, endpoints, code, session["redirect_uri"], session.get("code_verifier") or None
        )
        id_token = tokens.get("id_token")
        if not id_token:
            return respond_err("OIDC: el IdP no devolvió id_token.")

        claims = _validate_id_token(
            id_token, endpoints["jwks_uri"], provider["client_id"], provider["issuer_url"], session["nonce"]
        )

        cfg = _get_auth_config(cursor)
        identity = extract_identity(claims, cfg.get("oidc_username_claim", "email"))
        if cfg["oidc_require_email_verified"] and identity.get("email_verified") is not True:
            return respond_err("OIDC: el email no está verificado en el IdP.")

        role_names, tenant_names = extract_roles_tenants(claims, provider["client_id"])
        role_ids = _resolve_roles(cursor, role_names)
        tenant_ids = _resolve_tenants(cursor, tenant_names)

        try:
            user = _provision_user(cursor, identity, role_ids, tenant_ids, cfg, provider.get("allowed_domains"))
        except HTTPException as exc:
            return respond_err(str(exc.detail))

        token = create_admin_access_token(
            {"sub": user["username"], "roles": user["roles"], "profiles": user["profiles"], "role": user["role"]}
        )

        cursor.execute("DELETE FROM oidc_sessions WHERE id = %s;", (session["id"],))
        conn.commit()

        return respond_ok(
            {"access_token": token, "token_type": "bearer", "username": user["username"], "role": user["role"]}
        )
    except HTTPException as exc:
        return respond_err(str(exc.detail))
    finally:
        cursor.close()
        conn.close()


# --- ENDPOINTS ABM: OIDC PROVIDERS ---
@router.get("/providers")
@router.get("/")
def list_oidc_providers(admin_payload: dict = Depends(verify_admin_token)):
    """Lista todos los proveedores OIDC configurados (devuelve lista directa)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT {_PROVIDER_COLUMNS} FROM oidc_providers ORDER BY id ASC;")
        return [_public_provider(_row_to_provider(r)) for r in cursor.fetchall()]
    finally:
        cursor.close()
        conn.close()


@router.post("/", status_code=status.HTTP_201_CREATED)
@router.post("/providers", status_code=status.HTTP_201_CREATED)
def create_oidc_provider(payload: OIDCProviderCreate, admin_payload: dict = Depends(verify_admin_token)):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO oidc_providers
                (name, client_id, client_secret, issuer_url, redirect_uri, is_active,
                 discovery_url, authorization_endpoint, token_endpoint, jwks_uri,
                 userinfo_endpoint, end_session_endpoint, scopes, use_discovery,
                 client_auth_method, pkce_enabled, allowed_domains, default_roles)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id, name, client_id, issuer_url, redirect_uri, is_active;
            """,
            (
                payload.name, payload.client_id, payload.client_secret, payload.issuer_url,
                payload.redirect_uri, payload.is_active, payload.discovery_url,
                payload.authorization_endpoint, payload.token_endpoint, payload.jwks_uri,
                payload.userinfo_endpoint, payload.end_session_endpoint, payload.scopes,
                payload.use_discovery, payload.client_auth_method, payload.pkce_enabled,
                payload.allowed_domains, payload.default_roles,
            ),
        )
        new_provider = cursor.fetchone()
        conn.commit()
        return {
            "status": "success",
            "message": f"Proveedor OIDC '{payload.name}' creado correctamente.",
            "provider": {
                "id": new_provider[0],
                "name": new_provider[1],
                "client_id": new_provider[2],
                "issuer_url": new_provider[3],
                "redirect_uri": new_provider[4],
                "is_active": new_provider[5],
            },
        }
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"Error al crear proveedor: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.put("/{provider_id}")
@router.put("/providers/{provider_id}")
def update_oidc_provider(provider_id: int, payload: OIDCProviderUpdate, admin_payload: dict = Depends(verify_admin_token)):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM oidc_providers WHERE id = %s", (provider_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Proveedor OIDC no encontrado.")

        fields, values = [], []
        for name in (
            "name", "client_id", "client_secret", "issuer_url", "redirect_uri", "is_active",
            "discovery_url", "authorization_endpoint", "token_endpoint", "jwks_uri",
            "userinfo_endpoint", "end_session_endpoint", "scopes", "use_discovery",
            "client_auth_method", "pkce_enabled", "allowed_domains", "default_roles",
        ):
            val = getattr(payload, name, None)
            if val is not None:
                fields.append(f"{name} = %s")
                values.append(val)

        if fields:
            values.append(provider_id)
            cursor.execute(f"UPDATE oidc_providers SET {', '.join(fields)} WHERE id = %s", tuple(values))
            conn.commit()

        return {"status": "success", "message": f"Proveedor OIDC {provider_id} actualizado correctamente."}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"Error al actualizar proveedor: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.delete("/{provider_id}")
@router.delete("/providers/{provider_id}")
def delete_oidc_provider(provider_id: int, admin_payload: dict = Depends(verify_admin_token)):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM oidc_providers WHERE id = %s RETURNING id", (provider_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Proveedor OIDC no encontrado.")
        conn.commit()
        return {"status": "success", "message": f"Proveedor OIDC {provider_id} eliminado correctamente."}
    finally:
        cursor.close()
        conn.close()
