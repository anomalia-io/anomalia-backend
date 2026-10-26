"""Credenciales FCM y detalle del despacho push (spec 018 §6).

Estos tests no tocan la BD ni la red: validan el parser de la service account y
las ramas de `_dispatch` con dobles de `_get_push_config`, `_get_access_token` y
`httpx.post`.
"""
import json

import pytest

from app import push
from app.routers import system_router

# Se arma en runtime para no dejar un encabezado PEM literal en el repo: los
# escáneres de secretos (gitleaks) lo marcarían aunque sea una clave de prueba.
_FAKE_PEM = "".join(
    [
        "-----BEGIN ",
        "PRIVATE KEY-----\n",
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ==\n",
        "-----END ",
        "PRIVATE KEY-----\n",
    ]
)


def _service_account(**overrides):
    data = {
        "type": "service_account",
        "project_id": "anomalia-b1f5f",
        "private_key_id": "abc123",
        "private_key": _FAKE_PEM,
        "client_email": "firebase-adminsdk-abc@anomalia-b1f5f.iam.gserviceaccount.com",
        "client_id": "123456789",
    }
    data.update(overrides)
    return data


def test_validate_service_account_ok():
    parsed = push.validate_service_account(json.dumps(_service_account()))
    assert parsed["client_email"].endswith("@anomalia-b1f5f.iam.gserviceaccount.com")
    assert parsed["project_id"] == "anomalia-b1f5f"


def test_validate_service_account_derives_project_id_from_email():
    raw = _service_account()
    raw.pop("project_id")
    parsed = push.validate_service_account(json.dumps(raw))
    assert parsed["project_id"] == "anomalia-b1f5f"


@pytest.mark.parametrize(
    "raw, message",
    [
        pytest.param("", "vacía", id="empty"),
        pytest.param("   ", "vacía", id="blank"),
        pytest.param("{no-json", "inválido", id="malformed"),
        pytest.param("[]", "objeto JSON", id="not-object"),
        pytest.param(
            json.dumps(_service_account(client_email="")),
            "client_email",
            id="missing-client-email",
        ),
        pytest.param(
            json.dumps(_service_account(private_key="no-pem")),
            "private_key",
            id="invalid-private-key",
        ),
    ],
)
def test_validate_service_account_rejects_invalid(raw, message):
    with pytest.raises(ValueError) as exc:
        push.validate_service_account(raw)
    assert message in str(exc.value)


def _config(enabled=True, project_id="anomalia-b1f5f", service_account=None):
    return {
        "enabled": enabled,
        "project_id": project_id,
        "service_account": service_account or json.dumps(_service_account()),
    }


def test_dispatch_skips_when_push_disabled(monkeypatch):
    monkeypatch.setattr(push, "_get_push_config", lambda: _config(enabled=False))
    result = push.send_push_to_tokens(["token-123"], "t", "b")
    assert result["skipped"] == "push_disabled"
    assert result["delivered"] == 0


def test_dispatch_skips_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(
        push, "_get_push_config", lambda: {"enabled": True, "project_id": "", "service_account": ""}
    )
    result = push.send_push_to_tokens(["token-123"], "t", "b")
    assert result["skipped"] == "not_configured"


def test_dispatch_skips_when_no_tokens(monkeypatch):
    monkeypatch.setattr(push, "_get_push_config", lambda: _config())
    result = push.send_push_to_tokens([], "t", "b")
    assert result["skipped"] == "no_tokens"
    assert result["attempted"] == 0


class _Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def test_dispatch_reports_delivered_and_errors(monkeypatch):
    monkeypatch.setattr(push, "_get_push_config", lambda: _config())
    monkeypatch.setattr(push, "_get_access_token", lambda sa: "access-token")

    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if json["message"]["token"] == "bad-token":
            return _Response(404, '{"error":"UNREGISTERED"}')
        return _Response(200)

    monkeypatch.setattr(push.httpx, "post", fake_post)

    result = push.send_push_to_tokens(["good-token", "bad-token"], "t", "b")

    assert calls["n"] == 2
    assert result["attempted"] == 2
    assert result["delivered"] == 1
    assert result["skipped"] is None
    assert result["errors"][0]["token"] == "bad-token"
    assert result["errors"][0]["status"] == 404


def test_dispatch_reports_auth_error(monkeypatch):
    monkeypatch.setattr(push, "_get_push_config", lambda: _config())

    def fail(_sa):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(push, "_get_access_token", fail)

    result = push.send_push_to_tokens(["token-123"], "t", "b")
    assert result["skipped"] == "auth_error"
    assert "invalid_grant" in result["errors"][0]["error"]


def test_push_routes_registered():
    paths = {route.path for route in system_router.router.routes}
    assert "/api/v1/system/push" in paths
    assert "/api/v1/system/push-test" in paths


def test_public_push_settings_hides_private_key():
    config = {
        "push_enabled": True,
        "fcm_project_id": "",
        "fcm_service_account_json": json.dumps(_service_account()),
    }
    public = system_router._public_push_settings(config)
    assert public["has_service_account"] is True
    assert public["service_account_valid"] is True
    assert public["fcm_project_id"] == "anomalia-b1f5f"
    assert public["service_account_client_email"].endswith(
        "@anomalia-b1f5f.iam.gserviceaccount.com"
    )
    assert "fcm_service_account_json" not in public
    assert "PRIVATE KEY" not in json.dumps(public)


def test_public_push_settings_flags_invalid_credential():
    config = {
        "push_enabled": True,
        "fcm_project_id": "anomalia-b1f5f",
        "fcm_service_account_json": "no-es-json",
    }
    public = system_router._public_push_settings(config)
    assert public["has_service_account"] is True
    assert public["service_account_valid"] is False
    assert public["service_account_client_email"] == ""
