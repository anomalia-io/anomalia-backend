import asyncio
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.alert_silences import (
    PlannedSilence,
    create_vmalert_silence,
    planned_vmalert_silences,
    vmalert_alert_active,
    wait_for_vmalert_alert_clear,
)
from app.routers.webhook_router import (
    _persist_alerts,
    _resolve_tenant_id,
    _tenant_alert_key,
)
from app.workers.tenant_worker import TenantWorker
from app.workers.vmalert_worker import VmalertWorker


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []
        self.rowcount = 0

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def close(self):
        return None


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        return None

    def close(self):
        return None


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_resolve_tenant_id_prefers_explicit_tenant_label():
    cursor = FakeCursor([(990202,)])

    result = _resolve_tenant_id(
        cursor,
        {
            "tenant_id": "990202",
            "tenant_slug": "metrics",
            "exported_tenant_slug": "test-logs",
        },
    )

    assert result == 990202
    assert "WHERE id = %s" in cursor.calls[0][0]


def test_resolve_tenant_id_does_not_fall_back_when_explicit_id_is_missing():
    cursor = FakeCursor([None])

    result = _resolve_tenant_id(
        cursor,
        {"tenant_id": "999999", "tenant_slug": "metrics"},
    )

    assert result is None
    assert len(cursor.calls) == 1


def test_resolve_tenant_id_uses_exported_slug_before_internal_slug():
    cursor = FakeCursor([None, (990202,)])

    result = _resolve_tenant_id(
        cursor,
        {"tenant_slug": "metrics", "exported_tenant_slug": "test-logs"},
    )

    assert result == 990202
    assert len(cursor.calls) == 2


def test_tenant_alert_key_prefers_explicit_tenant_id():
    assert _tenant_alert_key({"tenant_id": "990205", "exported_tenant_slug": "test-logs"}) == "990205"
    assert _tenant_alert_key({"exported_tenant_slug": "test-logs"}) == "test-logs"
    assert _tenant_alert_key({"tenant_slug": "metrics"}) is None


def test_persist_alerts_skips_firing_from_deleted_tenant(monkeypatch):
    cursor = FakeCursor([None])
    cursor.rowcount = 1

    monkeypatch.setattr(
        "app.routers.webhook_router.get_db_connection",
        lambda: FakeConn(cursor),
    )

    processed = _persist_alerts(
        [{
            "fingerprint": "fp-orphan",
            "labels": {
                "alertname": "VmalertTenantDown",
                "tenant_id": "990205",
                "tenant_slug": "metrics",
            },
            "annotations": {"summary": "sin tenant"},
            "startsAt": "2026-01-01T00:00:00Z",
        }],
        "firing",
    )

    assert processed[0]["status"] == "resolved"
    assert processed[0]["queued"] is None
    assert any("UPDATE alert_history" in query for query, _ in cursor.calls)
    assert not any("INSERT INTO alert_history" in query for query, _ in cursor.calls)


def test_create_silence_uses_exact_tenant_matcher(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse({"silenceID": "sil-1"})

    monkeypatch.setattr("app.alert_silences.httpx.post", fake_post)

    silence = create_vmalert_silence(990202, "job-1", base_url="http://alertmanager:9093")

    assert silence.silence_id == "sil-1"
    assert captured["url"] == "http://alertmanager:9093/api/v2/silences"
    assert captured["json"]["matchers"] == [
        {
            "name": "alertname",
            "value": "VmalertTenantDown",
            "isRegex": False,
            "isEqual": True,
        },
        {
            "name": "tenant_id",
            "value": "990202",
            "isRegex": False,
            "isEqual": True,
        },
    ]
    assert "tenant=990202" in captured["json"]["comment"]


def test_create_silence_fails_open(monkeypatch):
    def fake_post(*args, **kwargs):
        raise RuntimeError("alertmanager down")

    monkeypatch.setattr("app.alert_silences.httpx.post", fake_post)

    assert create_vmalert_silence(990202, "job-1", base_url="http://am:9093") is None


def test_create_silence_accepts_legacy_silence_id(monkeypatch):
    monkeypatch.setattr(
        "app.alert_silences.httpx.post",
        lambda *args, **kwargs: FakeResponse({"silenceId": "sil-legacy"}),
    )

    silence = create_vmalert_silence(990202, "job-1", base_url="http://am:9093")

    assert silence.silence_id == "sil-legacy"


def test_planned_silences_are_removed_on_success_and_failure(monkeypatch):
    created = []
    removed = []

    def fake_create(tenant_id, job_ref, ttl_seconds, base_url=None):
        silence = PlannedSilence(
            silence_id=f"sil-{tenant_id}",
            tenant_id=tenant_id,
            matchers=[],
            starts_at="start",
            ends_at="end",
        )
        created.append(silence)
        return silence

    def fake_delete(silence, base_url=None):
        removed.append(silence.silence_id)
        return True

    monkeypatch.setattr("app.alert_silences.create_vmalert_silence", fake_create)
    monkeypatch.setattr("app.alert_silences.delete_vmalert_silence", fake_delete)

    with planned_vmalert_silences([990202], "job-1"):
        assert len(created) == 1

    with pytest.raises(RuntimeError):
        with planned_vmalert_silences([990203], "job-2"):
            raise RuntimeError("boom")

    assert [silence.silence_id for silence in created] == ["sil-990202", "sil-990203"]
    assert removed == ["sil-990202", "sil-990203"]


def test_planned_silences_wait_for_alert_clear_before_release(monkeypatch):
    removed = []
    waited = []
    silence = PlannedSilence(
        silence_id="sil-990202",
        tenant_id=990202,
        matchers=[],
        starts_at="start",
        ends_at="end",
    )

    monkeypatch.setattr(
        "app.alert_silences.create_vmalert_silence",
        lambda *args, **kwargs: silence,
    )
    monkeypatch.setattr(
        "app.alert_silences.delete_vmalert_silence",
        lambda current, base_url=None: removed.append(current.silence_id) or True,
    )
    monkeypatch.setattr(
        "app.alert_silences.wait_for_vmalert_alert_clear",
        lambda *args, **kwargs: waited.append(args) or True,
    )

    with planned_vmalert_silences(
        [990202], "job-1", settle_tenant_id=990202, settle_timeout_seconds=42
    ):
        pass

    assert waited == [(990202, 42, None)]
    assert removed == ["sil-990202"]


def test_planned_silences_keep_silence_when_alert_is_still_active(monkeypatch):
    removed = []
    silence = PlannedSilence(
        silence_id="sil-990202",
        tenant_id=990202,
        matchers=[],
        starts_at="start",
        ends_at="end",
    )

    monkeypatch.setattr(
        "app.alert_silences.create_vmalert_silence",
        lambda *args, **kwargs: silence,
    )
    monkeypatch.setattr(
        "app.alert_silences.delete_vmalert_silence",
        lambda current, base_url=None: removed.append(current.silence_id) or True,
    )
    monkeypatch.setattr(
        "app.alert_silences.wait_for_vmalert_alert_clear",
        lambda *args, **kwargs: False,
    )

    with planned_vmalert_silences([990202], "job-1", settle_tenant_id=990202):
        pass

    assert removed == []


def test_vmalert_alert_active_matches_only_target_tenant(monkeypatch):
    payload = [
        {
            "labels": {"alertname": "VmalertTenantDown", "tenant_id": "990202"},
            "status": {"state": "suppressed"},
        },
        {
            "labels": {"alertname": "VmalertTenantDown", "tenant_id": "990203"},
            "status": {"state": "active"},
        },
    ]
    monkeypatch.setattr(
        "app.alert_silences.httpx.get",
        lambda *args, **kwargs: FakeResponse(payload),
    )

    assert vmalert_alert_active(990202, "http://am:9093") is True
    assert vmalert_alert_active(990204, "http://am:9093") is False


def test_wait_for_vmalert_alert_clear_polls_until_absent(monkeypatch):
    states = [True, False]
    monkeypatch.setattr(
        "app.alert_silences.vmalert_alert_active",
        lambda *args, **kwargs: states.pop(0),
    )
    monkeypatch.setattr("app.alert_silences.time.sleep", lambda _: None)

    assert wait_for_vmalert_alert_clear(990202, 30, "http://am:9093") is True


def test_tenant_delete_opens_silence_before_undeploy_and_closes_after_regen(monkeypatch):
    events = []

    @contextmanager
    def fake_silences(targets, job_ref, **kwargs):
        events.append(("silence_open", tuple(targets), kwargs.get("settle_tenant_id")))
        yield {}
        events.append(("silence_close",))

    cursor = FakeCursor([(True, "metrics", None, "test-logs")])
    connections = []

    def fake_get_db_connection():
        connection = FakeConn(cursor if not connections else FakeCursor([]))
        connections.append(connection)
        return connection

    def fake_enqueue(job_type, ref_id, payload=None):
        events.append(("enqueue", job_type))
        return f"{job_type}-job"

    def fake_wait(job_id, timeout_seconds, queue_grace_seconds=None):
        events.append(("wait", job_id))
        return "succeeded"

    monkeypatch.setattr("app.workers.tenant_worker.planned_vmalert_silences", fake_silences)
    monkeypatch.setattr("app.workers.tenant_worker.get_db_connection", fake_get_db_connection)
    monkeypatch.setattr("app.workers.tenant_worker.delete_tenant_parses", lambda tenant_id: None)

    worker = TenantWorker()
    monkeypatch.setattr(worker, "_update_progress_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_enqueue_standalone_job", fake_enqueue)
    monkeypatch.setattr(worker, "_wait_for_job", fake_wait)

    worker._tenant_delete_sync("job-1", 990202)

    assert events[0] == ("silence_open", (990202,), 990202)
    assert events[1] == ("enqueue", "vmalert_undeploy")
    assert ("wait", "vmalert_regen_scrapes-job") in events
    assert events.index(("wait", "vmalert_regen_scrapes-job")) < events.index(("silence_close",))
    assert any(
        "UPDATE alert_history" in query for query, _ in connections[1]._cursor.calls
    )


def test_redeploy_uses_single_silence_window(monkeypatch):
    events = []

    @contextmanager
    def fake_silences(targets, job_ref, **kwargs):
        events.append(("silence_open", tuple(targets)))
        yield {}
        events.append(("silence_close",))

    async def fake_undeploy(job_id, tenant_id, payload):
        events.append(("undeploy", payload.get("skip_silence")))

    async def fake_deploy(job_id, tenant_id, payload):
        events.append(("deploy",))

    monkeypatch.setattr("app.workers.vmalert_worker.planned_vmalert_silences", fake_silences)

    worker = VmalertWorker()
    monkeypatch.setattr(worker, "_process_vmalert_undeploy", fake_undeploy)
    monkeypatch.setattr(worker, "_process_vmalert_deploy", fake_deploy)

    asyncio.run(worker._process_vmalert_redeploy("job-1", 990202, {}))

    assert events == [
        ("silence_open", (990202,)),
        ("undeploy", True),
        ("deploy",),
        ("silence_close",),
    ]


def test_vmagent_template_publishes_tenant_label():
    template = Path(
        "/home/joel/git/anomalia/anomalia-infra/roles/vmagent/templates/vmagent.yaml.j2"
    ).read_text(encoding="utf-8")

    assert "target_label: tenant" in template
    assert "replacement: '{{ vi.tenant_slug | default(\"\") }}'" in template


def test_install_binaries_restarts_all_per_tenant_vmalert_units():
    playbook = Path(
        "/home/joel/git/anomalia/anomalia-infra/install-binaries.yml"
    ).read_text(encoding="utf-8")

    assert "Descubrir unidades vmalert por tenant en el nodo" in playbook
    assert "Reiniciar unidades vmalert por tenant al actualizar el binario" in playbook
    assert "vmalert_pt_units_update.files | default([])" in playbook
