import base64
import hashlib

from app.oidc_flow import (
    build_auth_url,
    build_pkce_pair,
    discovery_url_for,
    domain_allowed,
    extract_identity,
    extract_roles_tenants,
)


def test_pkce_pair_is_s256_compatible():
    verifier, challenge = build_pkce_pair()
    assert isinstance(verifier, str) and len(verifier) >= 43
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_discovery_url_for_appends_well_known():
    assert discovery_url_for("https://idp.example.com") == (
        "https://idp.example.com/.well-known/openid-configuration"
    )
    assert discovery_url_for("https://idp.example.com/realms/x/") == (
        "https://idp.example.com/realms/x/.well-known/openid-configuration"
    )


def test_build_auth_url_includes_pkce_and_params():
    url = build_auth_url(
        "https://idp.example.com/auth",
        "cid",
        "https://app/auth/callback",
        "state123",
        "nonce123",
        "openid profile email",
        code_challenge="challenge123",
    )
    assert "response_type=code" in url
    assert "client_id=cid" in url
    assert "state=state123" in url
    assert "nonce=nonce123" in url
    assert "code_challenge=challenge123" in url
    assert "code_challenge_method=S256" in url


def test_build_auth_url_without_pkce():
    url = build_auth_url(
        "https://idp.example.com/auth", "cid", "r", "s", "n", "openid"
    )
    assert "code_challenge" not in url


def test_extract_identity_fallback_chain():
    claims = {"email": "a@b.com", "preferred_username": "ab", "sub": "123"}
    ident = extract_identity(claims, "email")
    assert ident["username"] == "a@b.com"
    assert ident["email_verified"] is None

    ident = extract_identity({"sub": "123"}, "email")
    assert ident["username"] == "123"

    ident = extract_identity({"preferred_username": "ab"}, "nonexistent")
    assert ident["username"] == "ab"


def test_extract_roles_tenants_from_keycloak_claims():
    claims = {
        "roles": ["ops"],
        "groups": ["admins"],
        "realm_access": {"roles": ["realm-role"]},
        "resource_access": {"cid": {"roles": ["client-role"]}},
        "tenants": ["prod"],
    }
    roles, tenants = extract_roles_tenants(claims, "cid")
    assert "ops" in roles
    assert "admins" in roles
    assert "realm-role" in roles
    assert "client-role" in roles
    assert tenants == ["prod"]


def test_extract_roles_tenants_empty_when_no_claims():
    roles, tenants = extract_roles_tenants({}, "cid")
    assert roles == []
    assert tenants == []


def test_domain_allowed():
    assert domain_allowed("a@empresa.com", "empresa.com, otra.com")
    assert domain_allowed("a@otra.com", "empresa.com, otra.com")
    assert not domain_allowed("a@gmail.com", "empresa.com")
    assert domain_allowed("a@gmail.com", "")
    assert domain_allowed("a@gmail.com", None)
    assert not domain_allowed("sin-email", "empresa.com")
