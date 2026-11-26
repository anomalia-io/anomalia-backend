import pytest

from app.workers import vmalert_worker as vw


class _FakeCursor:
    def __init__(self, ports_pool=None, tenant_row=None, node_row=None):
        self.ports_pool = ports_pool
        self.tenant_row = tenant_row
        self.node_row = node_row
        self.last_sql = ""

    def execute(self, sql, params=None):
        self.last_sql = " ".join(sql.split())

    def fetchone(self):
        if "ports_pool" in self.last_sql:
            return (self.ports_pool,)
        if "FROM tenants t" in self.last_sql:
            return self.tenant_row
        if "FROM infrastructure_nodes" in self.last_sql:
            return self.node_row
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass

    def commit(self):
        pass


@pytest.fixture
def worker():
    return vw.VmalertWorker()


def test_high_tenant_id_uses_pool_instead_of_invalid_port(worker, monkeypatch):
    monkeypatch.setattr(worker, "_used_vmalert_ports", lambda *args, **kwargs: {8881})
    cursor = _FakeCursor(ports_pool=[8881, 8882, 8883])

    candidates = worker._vmalert_port_candidates(cursor, 14, 990102)

    assert candidates[:3] == [8882, 8883, 8884]
    assert 998982 not in candidates
    assert all(8881 <= port <= 65535 for port in candidates)


def test_preferred_port_wins_when_it_is_free(worker, monkeypatch):
    monkeypatch.setattr(worker, "_used_vmalert_ports", lambda *args, **kwargs: {8881})
    cursor = _FakeCursor(ports_pool=[8881, 8882])

    candidates = worker._vmalert_port_candidates(
        cursor, 14, 2, preferred_port=8899, current_port=8899
    )

    assert candidates[0] == 8899


def test_parse_allocated_port_accepts_ansible_debug_output(worker):
    stdout = '''ok: [host] => {
    "msg": "VMALERT_FREE_PORT=8883"
}
'''

    assert worker._parse_allocated_port(stdout) == 8883


def test_parse_allocated_port_rejects_missing_marker(worker):
    with pytest.raises(RuntimeError):
        worker._parse_allocated_port("PLAY RECAP **********************")


def test_undeploy_load_builds_descriptor_without_deployed_instance(worker, monkeypatch):
    cursor = _FakeCursor(
        tenant_row=(990102, "test-logs", "test-logs", 14, 998982, None, None),
        node_row=("vmalert-1", "192.168.1.14", "192.168.1.27", "anomalia", None),
    )
    monkeypatch.setattr(vw, "get_db_connection", lambda: _FakeConn(cursor))
    monkeypatch.setattr(worker, "_update_progress_sync", lambda *args, **kwargs: None)

    data = worker._undeploy_load("job-1", 990102)

    assert data is not None
    assert data["target_ip"] == "192.168.1.27"
    assert data["vmalert_instances"][0]["tenant_slug"] == "test-logs"
    assert data["vmalert_instances"][0]["rules_path"] == "/etc/anomalia/vmalert/test-logs"
    assert data["vmalert_instances"][0]["port"] == 998982
