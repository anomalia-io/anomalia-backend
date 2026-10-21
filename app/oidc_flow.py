"""Helpers OIDC puros (spec 020).

Funciones sin acceso a BD ni red, para poder testearlas de forma aislada.
"""
import base64
import hashlib
import os
from typing import Optional
from urllib.parse import urlencode, urljoin


def build_pkce_pair() -> tuple:
    """Genera un par (code_verifier, code_challenge) S256 para PKCE (RFC 7636)."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def discovery_url_for(issuer_url: str) -> str:
    """Construye la URL de discovery OIDC a partir del issuer (spec 020)."""
    return urljoin(issuer_url.rstrip("/") + "/", ".well-known/openid-configuration")


def build_auth_url(
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    scopes: str,
    code_challenge: Optional[str] = None,
    extra_params: Optional[dict] = None,
) -> str:
    """Construye la URL de autorización OIDC con los parámetros estándar."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
        "nonce": nonce,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if extra_params:
        params.update(extra_params)
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


def extract_identity(claims: dict, username_claim: str = "email") -> dict:
    """Extrae la identidad de un ID token con fallbacks (spec 020).

    Prioridad: claim configurado → email → preferred_username → sub.
    """
    email = claims.get("email")
    preferred = claims.get("preferred_username")
    sub = claims.get("sub")

    primary = claims.get(username_claim) if username_claim else None
    if not primary:
        primary = email or preferred or sub

    return {
        "username": primary or "",
        "email": email or "",
        "sub": sub or "",
        "email_verified": claims.get("email_verified"),
    }


def extract_roles_tenants(claims: dict, client_id: str = "") -> tuple:
    """Extrae roles y tenants enviados por el IdP (spec 020).

    Busca claims típicos de Keycloak/Auth0/etc. Devuelve (roles, tenants).
    Si el IdP no envía nada, devuelve listas vacías (sin roles por defecto).
    """
    roles: list = []
    tenants: list = []

    for key in ("roles", "groups"):
        val = claims.get(key)
        if isinstance(val, list):
            roles.extend(str(x) for x in val)
        elif isinstance(val, str) and val:
            roles.append(val)

    realm = claims.get("realm_access")
    if isinstance(realm, dict):
        r = realm.get("roles")
        if isinstance(r, list):
            roles.extend(str(x) for x in r)

    if client_id:
        resource = claims.get("resource_access")
        if isinstance(resource, dict):
            client = resource.get(client_id)
            if isinstance(client, dict):
                r = client.get("roles")
                if isinstance(r, list):
                    roles.extend(str(x) for x in r)

    t = claims.get("tenants")
    if isinstance(t, list):
        tenants.extend(str(x) for x in t)
    elif isinstance(t, str) and t:
        tenants.append(t)

    # Dedup preservando orden.
    roles = list(dict.fromkeys(roles))
    tenants = list(dict.fromkeys(tenants))
    return roles, tenants


def domain_allowed(email: str, allowed_domains: Optional[str]) -> bool:
    """Valida si el email pertenece a un dominio permitido (CSV, spec 020)."""
    if not allowed_domains or not str(allowed_domains).strip():
        return True
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower()
    allowed = [d.strip().lower() for d in str(allowed_domains).split(",") if d.strip()]
    return domain in allowed
