import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.routers import admin_router

VERIFY_ANY_USER_TOKEN = auth.verify_any_user_token

TENANT_ENDPOINTS = [
    ("get", "/api/v1/admin/tenants/{tenant_id}/vmalert/rules", None),
    ("put", "/api/v1/admin/tenants/{tenant_id}/vmalert/rules", {"yaml": "groups: []"}),
    ("post", "/api/v1/admin/tenants/{tenant_id}/vmalert/sync_rules", None),
    ("post", "/api/v1/admin/tenants/{tenant_id}/vmalert/reload", None),
    ("post", "/api/v1/admin/tenants/{tenant_id}/vmalert/deploy", None),
    ("post", "/api/v1/admin/tenants/{tenant_id}/vmalert/undeploy", None),
    ("post", "/api/v1/admin/tenants/{tenant_id}/vmalert/redeploy", None),
    ("get", "/api/v1/admin/alerts/tenants/{tenant_id}/recipients", None),
    ("post", "/api/v1/admin/alerts/tenants/{tenant_id}/recipients", {"email": "ops@example.com"}),
    ("put", "/api/v1/admin/alerts/tenants/{tenant_id}/recipients/1", {"label": "ops"}),
    ("delete", "/api/v1/admin/alerts/tenants/{tenant_id}/recipients/1", None),
]


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rows = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        statement = " ".join(sql.split())
        self.db.statements.append((statement, tuple(params)))
        if "SELECT COUNT(*)" in statement and "FROM tenants t" in statement:
            tenant_id, username, _ = params
            self.rows = [(1,)] if tenant_id in self.db.allowed_tenants(username) else [(0,)]
        elif "FROM role_profiles rp" in statement:
            profile = params[-1]
            roles = params[:-1]
            self.rows = [(1,)] if any(
                profile in self.db.role_profiles.get(role, set()) for role in roles
            ) else [(0,)]
        elif "SELECT id, code, name, description FROM profiles" in statement:
            self.rows = list(self.db.profiles)
        elif "SELECT t.id, t.name, t.slug, t.org_id_upper" in statement:
            username = params[0] if params else None
            allowed = self.db.allowed_tenants(username) if username else set()
            self.rows = [row for row in self.db.tenant_rows if row[0] in allowed]
            if username is None:
                self.rows = list(self.db.tenant_rows)
        elif "SELECT 1 FROM tenants WHERE id = %s AND deleted_at IS NULL" in statement:
            self.rows = [(1,)] if params[0] in self.db.tenant_ids else []
        elif "SELECT 1 FROM tenant_alert_recipients" in statement:
            self.rows = [(1,)]
        elif statement.startswith("INSERT INTO tenant_alert_recipients"):
            self.rows = [(1,)]
        elif statement.startswith("DELETE FROM tenant_alert_recipients"):
            self.rowcount = 1
        elif statement.startswith("SELECT slug FROM tenants WHERE id = %s"):
            self.rows = [("tenant-%s" % params[0],)] if params[0] in self.db.tenant_ids else []
        elif statement.startswith("SELECT is_internal FROM tenants WHERE id = %s"):
            self.rows = [(False,)]
        elif statement.startswith("SELECT type FROM tenants WHERE id = %s"):
            self.rows = [("metrics",)]
        elif "FROM tenant_vmalert_instances" in statement and "rules_yaml" in statement:
            self.rows = [("groups: []",)]
        elif statement.startswith("UPDATE tenant_vmalert_instances"):
            self.rowcount = 1
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
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.statements = []
        self.profiles = [(1, "users_manager", "Users Manager", "gestion de usuarios")]
        self.role_profiles = {
            "admin": {"users_manager", "alerts_manager", "profile_manager"},
            "alerts_manager": {"users_manager", "alerts_manager"},
            "tenant_manager": {"users_manager"},
            "viewer": {"users_manager"},
        }
        self.roles = {
            "user-a": {"alerts_manager"},
            "user-b": {"viewer"},
            "admin": {"admin"},
        }
        self.user_tenants = {"user-a": {1}, "user-b": {1}, "admin": set()}
        self.role_tenants = {
            "alerts_manager": {1},
            "viewer": {1},
            "admin": {1, 2},
        }
        self.tenant_rows = [
            (1, "Tenant A", "tenant-a", "TENANT_A", "active", "i-1", 8429, True,
             True, None, None, None),
            (2, "Tenant B", "tenant-b", "TENANT_B", "active", "i-2", 8430, True,
             True, None, None, None),
        ]

    @property
    def tenant_ids(self):
        return {row[0] for row in self.tenant_rows}

    def allowed_tenants(self, username):
        roles = self.roles.get(username, set())
        from_roles = set().union(*(self.role_tenants.get(role, set()) for role in roles))
        return self.user_tenants.get(username, set()) | from_roles


@pytest.fixture
def admin_api(monkeypatch):
    db = FakeDB()
    token = {"sub": "user-a", "role": "alerts_manager", "roles": []}

    def fake_token():
        return token

    app = FastAPI()
    app.include_router(admin_router.router)
    app.dependency_overrides[VERIFY_ANY_USER_TOKEN] = fake_token
    monkeypatch.setattr(auth, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.setattr(admin_router, "get_db_connection", lambda: FakeConnection(db))

    client = TestClient(app)
    yield client, db, token, app
    app.dependency_overrides.clear()


def test_profiles_catalog_get_is_read_only_and_keeps_shape(admin_api):
    client, db, token, _ = admin_api
    token["sub"] = "admin"
    token["role"] = "admin"

    response = client.get("/api/v1/admin/profiles")

    assert response.status_code == 200
    assert response.json() == {
        "profiles": [{"id": 1, "code": "users_manager", "name": "Users Manager",
                      "description": "gestion de usuarios"}]
    }


def test_profile_write_routes_are_gone(admin_api):
    client, _, token, _ = admin_api
    token["sub"] = "admin"
    token["role"] = "admin"

    responses = [
        client.post("/api/v1/admin/profiles", json={"name": "new", "code": "new"}).status_code,
        client.put("/api/v1/admin/profiles/1", json={"name": "new", "code": "new"}).status_code,
        client.delete("/api/v1/admin/profiles/1").status_code,
    ]

    assert responses == [405, 404, 404]
    profile_routes = [
        (route.path, route.methods)
        for route in admin_router.router.routes
        if "/profiles" in route.path
    ]
    assert profile_routes == [("/api/v1/admin/profiles", {"GET"})]


@pytest.mark.parametrize("method,path,body", TENANT_ENDPOINTS)
def test_alerts_manager_cannot_use_other_tenant_endpoints(admin_api, method, path, body):
    client, _, _, _ = admin_api
    url = path.format(tenant_id=2)

    response = client.request(method, url, json=body) if body else client.request(method, url)

    assert response.status_code == 403
    assert "tenant 2" in response.json()["detail"]


@pytest.mark.parametrize("method,path,body", TENANT_ENDPOINTS)
def test_user_without_alerts_manager_is_forbidden_even_with_tenant_access(admin_api, method, path, body):
    client, _, token, _ = admin_api
    token["sub"] = "user-b"
    token["role"] = "viewer"
    url = path.format(tenant_id=1)

    response = client.request(method, url, json=body) if body else client.request(method, url)

    assert response.status_code == 403
    assert "alerts_manager" in response.json()["detail"]


@pytest.mark.parametrize("method,path,body", TENANT_ENDPOINTS)
def test_admin_with_seeded_role_tenant_access_succeeds(admin_api, method, path, body):
    client, _, token, _ = admin_api
    token["sub"] = "admin"
    token["role"] = "admin"
    url = path.format(tenant_id=2)

    response = client.request(method, url, json=body) if body else client.request(method, url)

    assert response.status_code in (200, 202)


def test_alerts_tenants_filters_non_admin_even_without_mine(admin_api):
    client, db, _, _ = admin_api

    response = client.get("/api/v1/admin/alerts/tenants?mine=false")

    assert response.status_code == 200
    assert [tenant["id"] for tenant in response.json()["tenants"]] == [1]
    query, params = next(
        (statement, values) for statement, values in db.statements
        if "SELECT t.id, t.name, t.slug" in statement
    )
    assert "SELECT ut.tenant_id FROM user_tenants ut" in query
    assert "SELECT rt.tenant_id FROM role_tenants rt" in query
    assert params == ("user-a", "user-a")


def test_alerts_tenants_admin_sees_all_without_mine(admin_api):
    client, db, token, _ = admin_api
    token["sub"] = "admin"
    token["role"] = "admin"

    response = client.get("/api/v1/admin/alerts/tenants?mine=false")

    assert response.status_code == 200
    assert [tenant["id"] for tenant in response.json()["tenants"]] == [1, 2]
    query, params = next(
        (statement, values) for statement, values in db.statements
        if "SELECT t.id, t.name, t.slug" in statement
    )
    assert "user_tenants" not in query
    assert params == ()
