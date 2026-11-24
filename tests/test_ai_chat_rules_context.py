import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import ai_router, auth
from app.vmalert_validation import validate_vmalert_yaml

VERIFY_ANY_USER_TOKEN = auth.verify_any_user_token

VALID_YAML = """groups:
  - name: ejemplo
    rules:
      - alert: Ejemplo
        expr: up == 0
"""

INVALID_YAML = """groups:
  - name: prueba
   rules: []
"""

SAVED_YAML = """groups:
  - name: guardado
    rules:
      - alert: Guardado
        expr: up == 1
"""


class FakeCursor:
    def __init__(self):
        self.rows = []

    def execute(self, sql, params=()):
        statement = " ".join(sql.split())
        if "FROM tenants WHERE id" in statement:
            self.rows = [("Tenant A", "tenant-a", "metrics", 1, 1)]
        elif "FROM tenant_vmalert_instances WHERE tenant_id" in statement:
            self.rows = [(SAVED_YAML, "healthy", "ok")]
        else:
            self.rows = []
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class FakeConnection:
    def cursor(self):
        return FakeCursor()

    def close(self):
        pass


@pytest.fixture
def chat_api(monkeypatch):
    token = {"sub": "user-a", "role": "alerts_manager", "roles": []}
    captured = {"prompt": None}

    class FakeProvider:
        def generate(self, prompt):
            captured["prompt"] = prompt
            return "Explicación del asistente."

    monkeypatch.setattr(auth, "verify_tenant_access", lambda tenant_id: lambda user_payload: None)
    monkeypatch.setattr(ai_router, "get_provider", lambda: FakeProvider())
    monkeypatch.setattr(ai_router, "get_db_connection", lambda: FakeConnection())

    app = FastAPI()
    app.include_router(ai_router.router)
    app.dependency_overrides[VERIFY_ANY_USER_TOKEN] = lambda: token
    client = TestClient(app)
    yield client, captured, app
    app.dependency_overrides.clear()


def test_chat_includes_editor_draft_and_reports_indentation_errors(chat_api):
    client, captured, _ = chat_api

    response = client.post(
        "/api/v1/ai/chat",
        json={
            "context": "vmalert_rules",
            "tenant_id": 1,
            "messages": [{"role": "user", "content": "¿Qué está mal?"}],
            "rules_yaml": INVALID_YAML,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "Explicación del asistente."
    validation = body["yaml_validation"]
    assert validation["valid"] is False
    assert validation["issues"]
    assert any(issue["line"] == 3 for issue in validation["issues"])
    assert "BORRADOR ACTUAL DEL EDITOR" in captured["prompt"]
    assert INVALID_YAML.strip() in captured["prompt"]
    assert "ERRORES DETECTADOS" in captured["prompt"]
    assert "Explicá cada error" in captured["prompt"]


def test_chat_valid_draft_takes_precedence_over_saved_yaml(chat_api):
    client, captured, _ = chat_api

    response = client.post(
        "/api/v1/ai/chat",
        json={
            "context": "vmalert_rules",
            "tenant_id": 1,
            "messages": [{"role": "user", "content": "¿Está bien?"}],
            "rules_yaml": VALID_YAML,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["yaml_validation"]["valid"] is True
    assert body["yaml_validation"]["issues"] == []
    assert "BORRADOR ACTUAL DEL EDITOR" in captured["prompt"]
    assert "name: ejemplo" in captured["prompt"]
    assert "name: guardado" not in captured["prompt"]
    assert "sin errores de sintaxis" in captured["prompt"]


def test_chat_without_draft_keeps_saved_yaml_behavior(chat_api):
    client, captured, _ = chat_api

    response = client.post(
        "/api/v1/ai/chat",
        json={
            "context": "vmalert_rules",
            "tenant_id": 1,
            "messages": [{"role": "user", "content": "Resumí las reglas"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["yaml_validation"] is None
    assert "Reglas Actuales YAML" in captured["prompt"]
    assert SAVED_YAML.strip() in captured["prompt"]


def test_validate_vmalert_yaml_detects_schema_errors():
    validation = validate_vmalert_yaml("groups:\n  - name: sin reglas\n    rules: []\n")

    assert validation.valid is False
    assert any("rules" in issue.message for issue in validation.issues)
    assert validate_vmalert_yaml("   ").valid is True


def test_chat_still_enforces_tenant_access(chat_api, monkeypatch):
    client, _, _ = chat_api

    def deny(tenant_id):
        def dependency(user_payload):
            raise HTTPException(status_code=403, detail="sin acceso al tenant %s" % tenant_id)
        return dependency

    monkeypatch.setattr(auth, "verify_tenant_access", deny)

    response = client.post(
        "/api/v1/ai/chat",
        json={
            "context": "vmalert_rules",
            "tenant_id": 2,
            "messages": [{"role": "user", "content": "Hola"}],
            "rules_yaml": VALID_YAML,
        },
    )

    assert response.status_code == 403
