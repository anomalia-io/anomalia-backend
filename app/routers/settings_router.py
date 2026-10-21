from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import verify_admin_token
from ..database import get_db_connection

router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])

# Modos de autenticación admitidos (spec 020).
AUTH_MODES = ("local", "local_only", "hybrid", "oidc_only")


class SsoConfigRequest(BaseModel):
    auth_mode: Optional[str] = Field(default=None)
    oidc_enabled: Optional[bool] = None
    oidc_require_email_verified: Optional[bool] = None
    oidc_auto_provision: Optional[bool] = None
    name: Optional[str] = None
    issuer_url: Optional[str] = None
    client_id: Optional[str] = Field(default=None)
    client_secret: Optional[str] = Field(default=None, repr=False)
    redirect_uri: Optional[str] = None
    scopes: Optional[str] = None
    use_discovery: Optional[bool] = None
    client_auth_method: Optional[str] = None
    pkce_enabled: Optional[bool] = None
    allowed_domains: Optional[str] = None
    is_active: Optional[bool] = None


def _get_system_settings() -> dict:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id, auth_mode, oidc_enabled, default_oidc_provider_id,
                   oidc_auto_provision, oidc_require_email_verified,
                   oidc_username_claim, oidc_groups_claim
            FROM system_settings ORDER BY id LIMIT 1;
            """
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=500, detail="system_settings no inicializado.")
        return {
            "id": row[0],
            "auth_mode": row[1] or "hybrid",
            "oidc_enabled": bool(row[2]),
            "default_oidc_provider_id": row[3],
            "oidc_auto_provision": bool(row[4]),
            "oidc_require_email_verified": bool(row[5]),
            "oidc_username_claim": row[6] or "email",
            "oidc_groups_claim": row[7] or "groups",
        }
    finally:
        cursor.close()
        conn.close()


def _get_provider(provider_id: int) -> Optional[dict]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id, name, issuer_url, discovery_url, client_id, client_secret,
                   redirect_uri, scopes, use_discovery, client_auth_method,
                   pkce_enabled, allowed_domains, is_active
            FROM oidc_providers WHERE id = %s;
            """,
            (provider_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "name": row[1],
            "issuer_url": row[2],
            "discovery_url": row[3],
            "client_id": row[4],
            "has_client_secret": bool(row[5]),
            "redirect_uri": row[6],
            "scopes": row[7] or "openid profile email",
            "use_discovery": bool(row[8]),
            "client_auth_method": row[9] or "client_secret_basic",
            "pkce_enabled": bool(row[10]),
            "allowed_domains": row[11],
            "is_active": bool(row[12]),
        }
    finally:
        cursor.close()
        conn.close()


@router.get("/auth")
def get_auth_settings():
    """Config pública de autenticación: usada por web y móvil (spec 020)."""
    settings = _get_system_settings()
    mode = settings["auth_mode"]
    return {
        "auth_mode": mode,
        "oidc_enabled": settings["oidc_enabled"],
        "local_enabled": mode in ("local", "local_only", "hybrid"),
    }


@router.get("/sso", dependencies=[Depends(verify_admin_token)])
def get_sso_config():
    settings = _get_system_settings()
    provider = None
    if settings["default_oidc_provider_id"]:
        provider = _get_provider(settings["default_oidc_provider_id"])
    return {
        "auth_mode": settings["auth_mode"],
        "oidc_enabled": settings["oidc_enabled"],
        "oidc_require_email_verified": settings["oidc_require_email_verified"],
        "oidc_auto_provision": settings["oidc_auto_provision"],
        "default_oidc_provider_id": settings["default_oidc_provider_id"],
        "provider": provider,
    }


@router.put("/sso", dependencies=[Depends(verify_admin_token)])
def update_sso_config(payload: SsoConfigRequest):
    settings = _get_system_settings()
    provider_id = settings["default_oidc_provider_id"]

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Upsert del proveedor OIDC (un solo proveedor activo por diseño).
        provider_fields = {
            "name": payload.name,
            "issuer_url": payload.issuer_url,
            "client_id": payload.client_id,
            "client_secret": payload.client_secret,
            "redirect_uri": payload.redirect_uri,
            "scopes": payload.scopes,
            "use_discovery": payload.use_discovery,
            "client_auth_method": payload.client_auth_method,
            "pkce_enabled": payload.pkce_enabled,
            "allowed_domains": payload.allowed_domains,
            "is_active": payload.is_active,
        }
        provider_fields = {k: v for k, v in provider_fields.items() if v is not None}

        if provider_id and provider_fields:
            set_clause = ", ".join(f"{k} = %s" for k in provider_fields)
            values = list(provider_fields.values()) + [provider_id]
            cursor.execute(
                f"UPDATE oidc_providers SET {set_clause} WHERE id = %s;", values
            )
        elif provider_fields and payload.issuer_url and payload.client_id:
            cols = ", ".join(provider_fields.keys())
            placeholders = ", ".join(["%s"] * len(provider_fields))
            cursor.execute(
                f"INSERT INTO oidc_providers ({cols}) VALUES ({placeholders}) RETURNING id;",
                list(provider_fields.values()),
            )
            provider_id = cursor.fetchone()[0]

        # Guarda la configuración general de autenticación.
        if payload.auth_mode is not None and payload.auth_mode not in AUTH_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"auth_mode inválido: {payload.auth_mode}",
            )
        updates = {}
        if payload.auth_mode is not None:
            updates["auth_mode"] = payload.auth_mode
        if payload.oidc_enabled is not None:
            updates["oidc_enabled"] = payload.oidc_enabled
        if payload.oidc_require_email_verified is not None:
            updates["oidc_require_email_verified"] = payload.oidc_require_email_verified
        if payload.oidc_auto_provision is not None:
            updates["oidc_auto_provision"] = payload.oidc_auto_provision
        if provider_id is not None:
            updates["default_oidc_provider_id"] = provider_id

        if updates:
            set_clause = ", ".join(f"{k} = %s" for k in updates)
            values = list(updates.values()) + [datetime.utcnow(), settings["id"]]
            cursor.execute(
                f"UPDATE system_settings SET {set_clause}, updated_at = %s WHERE id = %s;",
                values,
            )

        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"Error guardando config SSO: {str(e)}")
    finally:
        cursor.close()
        conn.close()

    return get_sso_config()
