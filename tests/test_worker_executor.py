"""Ejecutor único de jobs en background (spec 016).

Dos contenedores gateway comparten la app; sin coordinación ambos correrían
``ansible-playbook`` en paralelo. ``WorkerSupervisor`` serializa con un advisory
lock de PostgreSQL y solo el dueño arranca los workers. Estos tests usan una
conexión falsa: no requieren Postgres.
"""
import app.workers.supervisor as sup
from app.workers import TenantWorker
from app.workers.supervisor import WorkerSupervisor


class _FakeCursor:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, *args, **kwargs):
        return None

    def fetchone(self):
        return (self._value,)


class _FakeConn:
    def __init__(self, acquired: bool):
        self._acquired = acquired
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._acquired)

    def commit(self):
        return None

    def close(self):
        self.closed = True


def test_tenant_worker_handles_backup_import():
    assert "backup_import" in TenantWorker(interval=1).job_types


def test_supervisor_acquires_lock(monkeypatch):
    monkeypatch.setattr(sup, "get_db_connection", lambda: _FakeConn(True))
    supervisor = WorkerSupervisor(interval=1, workers=[])

    assert supervisor._try_acquire_lock() is True
    assert supervisor.is_owner is True
    assert supervisor._lock_conn is not None


def test_supervisor_does_not_acquire_when_taken(monkeypatch):
    monkeypatch.setattr(sup, "get_db_connection", lambda: _FakeConn(False))
    supervisor = WorkerSupervisor(interval=1, workers=[])

    assert supervisor._try_acquire_lock() is False
    assert supervisor.is_owner is False
    assert supervisor._lock_conn is None


def test_supervisor_desists_on_db_error(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(sup, "get_db_connection", _boom)
    supervisor = WorkerSupervisor(interval=1, workers=[])

    assert supervisor._try_acquire_lock() is False
    assert supervisor.is_owner is False


def test_supervisor_releases_lock(monkeypatch):
    monkeypatch.setattr(sup, "get_db_connection", lambda: _FakeConn(True))
    supervisor = WorkerSupervisor(interval=1, workers=[])
    supervisor._try_acquire_lock()

    supervisor._release_lock()

    assert supervisor.is_owner is False
    assert supervisor._lock_conn is None


def test_supervisor_check_lock_alive(monkeypatch):
    monkeypatch.setattr(sup, "get_db_connection", lambda: _FakeConn(True))
    supervisor = WorkerSupervisor(interval=1, workers=[])
    supervisor._try_acquire_lock()

    assert supervisor._check_lock_alive() is True
    supervisor._lock_conn.closed = True
    assert supervisor._check_lock_alive() is False
