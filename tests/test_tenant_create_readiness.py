import asyncio
from contextlib import nullcontext

import pytest

from app.workers import tenant_worker as tw
from app.workers import vmalert_worker as vw


class _FakeCursor:
    def __init__(self, rows=None, one=None):
        self.rows = rows or []
        self.one = one
        self.last_sql = ""
        self.calls = []

    def execute(self, sql, params=None):
        self.last_sql = " ".join(sql.split())
        self.calls.append((self.last_sql, params))

    def fetchone(self):
        if "nextval" in self.last_sql:
            return (1004,)
        return self.one

    def fetchall(self):
        return self.rows

    def close(self):
        pass


@pytest.fixture
def worker():
    return vw.VmalertWorker()


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass

    def commit(self):
        pass


def test_tenant_create_waits_for_regen_after_vmalert_deploy(monkeypatch):
    worker = tw.TenantWorker()
    cursor = _FakeCursor(one=(
        990202, "test-ready", "test-ready", "logs", None, None, True,
        "auto", 14, None, "TEST", "TEST-READY", None, "auto",
    ))
    monkeypatch.setattr(tw, "get_db_connection", lambda: _FakeConn(cursor))
    monkeypatch.setattr(worker, "_seed_tenant_datasources", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_select_vmauth_node_auto", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_select_vmalert_node_auto", lambda *args, **kwargs: 14)
    monkeypatch.setattr(worker, "_set_tenant_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_job_deadline", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_check_deadline", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker, "_tenant_create_step",
        lambda job_id, steps, name, pct, status=None: steps,
    )
    monkeypatch.setattr(tw, "provision_tenant_parses", lambda *args, **kwargs: None)

    enqueued = []
    waited = []
    monkeypatch.setattr(
        worker, "_enqueue_standalone_job",
        lambda job_type, ref_id, payload=None: enqueued.append((job_type, payload)) or "deploy-job",
    )
    monkeypatch.setattr(
        worker, "_enqueue_standalone_regen",
        lambda ref_id, components=None: enqueued.append(("vmalert_regen_scrapes", None)) or "regen-job",
    )
    monkeypatch.setattr(
        worker, "_wait_for_job",
        lambda job_id, timeout: waited.append(job_id) or "succeeded",
    )

    worker._tenant_create_pipeline("job-1", 990202)

    assert enqueued == [
        ("vmalert_deploy", {"skip_regen": True}),
        ("vmalert_regen_scrapes", None),
    ]
    assert waited == ["deploy-job", "regen-job"]


def test_readiness_adds_tenant_route_probe(monkeypatch):
    worker = tw.TenantWorker()
    cursor = _FakeCursor(one=(
        "test-ready", "logs", True, "192.168.1.27", 8427, 8883, "192.168.1.27",
    ))
    monkeypatch.setattr(tw, "get_db_connection", lambda: _FakeConn(cursor))

    endpoints = worker._readiness_endpoints(990202)

    assert endpoints["ingest"] == "http://192.168.1.27:8427/health"
    assert endpoints["ingest_route"] == (
        "http://192.168.1.27:8427/insert/loki/api/v1/push?tenant=test-ready"
    )
    assert endpoints["vmalert"] == "http://192.168.1.27:8883/health"


def test_route_probe_rejects_missing_route_but_accepts_backend_error():
    class _Response:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text

    assert tw.TenantWorker._route_probe_failed(_Response(
        400, "user unauthorized missing route for /insert/loki/api/v1/push"
    ))
    assert not tw.TenantWorker._route_probe_failed(_Response(
        400, "cannot read Loki protobuf data"
    ))


def test_deploy_persist_honors_skip_regen(worker, monkeypatch):
    called = []
    cursor = _FakeCursor()
    monkeypatch.setattr(vw, "get_db_connection", lambda: _FakeConn(cursor))
    monkeypatch.setattr(
        worker, "_enqueue_regenerate_scrapes",
        lambda *args, **kwargs: called.append(args),
    )
    data = {
        "vmalert_node_id": 14,
        "vmalert_port": 8883,
        "slug": "test-ready",
        "rules_yaml": "groups: []\n",
    }

    worker._deploy_persist(990202, data, skip_regen=True)

    assert called == []


def test_vmalert_deploy_propagates_skip_regen(monkeypatch):
    worker = vw.VmalertWorker()
    captured = {}

    async def noop(*args, **kwargs):
        return None

    data = {
        "target_ip": "192.168.1.27",
        "admin_user": "anomalia",
        "admin_pass": "unused",
        "vmalert_instances": [],
        "vmalert_rules": [],
        "vmalert_port": 8883,
        "vmalert_node_id": 14,
        "slug": "test-ready",
        "rules_yaml": "groups: []\n",
    }
    monkeypatch.setattr(worker, "_update_progress", noop)
    monkeypatch.setattr(worker, "_deploy_load", lambda *args, **kwargs: data)
    monkeypatch.setattr(worker, "_allocate_remote_port", noop)
    monkeypatch.setattr(worker, "_run_ansible_vmalert", noop)
    monkeypatch.setattr(worker, "_http_wait_ready", noop)
    monkeypatch.setattr(
        worker, "_deploy_persist",
        lambda tenant_id, payload, skip_regen: captured.update(skip_regen=skip_regen),
    )

    asyncio.run(worker._process_vmalert_deploy("job-1", 990202, {"skip_regen": True}))

    assert captured["skip_regen"] is True


def test_regen_load_includes_trigger_tenant_while_provisioning(worker, monkeypatch):
    cursor = _FakeCursor(rows=[])
    node_row = (14, "vmauth", "192.168.1.27", "192.168.1.27", 8427, "anomalia", "unused")

    def fetchall():
        if "FROM infrastructure_nodes" in cursor.last_sql:
            return [node_row]
        if "FROM tenant_vmalert_instances" in cursor.last_sql:
            return [(
                "test-ready", "Test", "TEST-READY", 8883,
                "192.168.1.27", "192.168.1.27", 14, 990202,
            )]
        if "FROM tenants" in cursor.last_sql:
            return [("test-ready", "logs", "TEST-READY", 1004, 1004, None)]
        return []

    monkeypatch.setattr(cursor, "fetchall", fetchall)
    monkeypatch.setattr(vw, "get_db_connection", lambda: _FakeConn(cursor))
    monkeypatch.setattr(worker, "_update_progress_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker, "_get_component_url",
        lambda *args, **kwargs: "http://192.168.1.27:8427",
    )

    data = worker._regen_scrapes_load("job-1", 990202)

    assert any(
        "FROM tenant_vmalert_instances" in sql
        and "t.status = 'provisioning' AND t.id = %s" in sql
        and params == (990202,)
        for sql, params in cursor.calls
    )
    assert any(
        "FROM tenants" in sql
        and "status = 'provisioning' AND id = %s" in sql
        and params == (990202,)
        for sql, params in cursor.calls
    )
    assert data["base_ev"]["tenants_routing"][0]["slug"] == "test-ready"
    assert data["base_ev"]["vmalert_instances"][0]["tenant_id"] == 990202


def test_rollback_removes_routes_after_marking_failed(monkeypatch):
    worker = tw.TenantWorker()
    cursor = _FakeCursor(one=("logs", True, "test-ready", 14))
    monkeypatch.setattr(tw, "get_db_connection", lambda: _FakeConn(cursor))
    monkeypatch.setattr(tw, "delete_tenant_parses", lambda *args, **kwargs: None)
    monkeypatch.setattr(tw, "planned_vmalert_silences", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(worker, "_wait_for_job", lambda *args, **kwargs: "succeeded")

    enqueued = []
    monkeypatch.setattr(
        worker, "_enqueue_job",
        lambda cur, job_type, ref_id, payload=None: enqueued.append((job_type, payload)) or job_type,
    )

    worker._rollback_tenant_create(990202, RuntimeError("boom"))

    assert enqueued == [
        ("vmalert_undeploy", {"skip_regen": True, "skip_silence": True}),
        ("vmalert_regen_scrapes", None),
    ]
    failed_at = next(
        index for index, (sql, _) in enumerate(cursor.calls)
        if "UPDATE tenants SET status='failed'" in sql
    )
    regen_at = next(
        index for index, (_, payload) in enumerate(enqueued)
        if payload is None
    )
    assert failed_at >= 0 and regen_at == 1
