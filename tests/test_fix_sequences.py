"""Regresión de ``_fix_sequences`` (import de backup, spec 014 §6.3).

El bug original pasaba el nombre de la secuencia como literal en el ``FROM``
(``SELECT last_value FROM 'public.users_id_seq'``), que PostgreSQL rechaza con
``syntax error at or near "'public.users_id_seq'"``. El arreglo referencia la
secuencia con ``%s::regclass`` y lee su último valor con
``pg_sequence_last_value``.
"""
import pytest

from app.routers import system_router as sr


class _FakeCursor:
    def __init__(self, max_id):
        self._max_id = max_id
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        sql = self.calls[-1][0]
        if "COALESCE(MAX" in sql:
            return (self._max_id,)
        if "pg_get_serial_sequence" in sql:
            return ("public.users_id_seq",)
        return None


def test_fix_sequences_uses_regclass_not_literal(monkeypatch):
    monkeypatch.setattr(sr, "BACKUP_PK", {"users": ["id"]})
    cursor = _FakeCursor(max_id=5)

    sr._fix_sequences(cursor, conn=None)

    setval_calls = [c for c in cursor.calls if "setval" in c[0]]
    assert len(setval_calls) == 1
    sql, params = setval_calls[0]
    assert "FROM %s" not in sql
    assert "::regclass" in sql
    assert "pg_sequence_last_value" in sql
    assert params == ("public.users_id_seq", 5, "public.users_id_seq")


def test_fix_sequences_skips_empty_table(monkeypatch):
    monkeypatch.setattr(sr, "BACKUP_PK", {"users": ["id"]})
    cursor = _FakeCursor(max_id=0)

    sr._fix_sequences(cursor, conn=None)

    assert not [c for c in cursor.calls if "setval" in c[0]]


def _postgres_available() -> bool:
    try:
        from app.database import get_db_connection

        get_db_connection().close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _postgres_available(), reason="Postgres no accesible")
def test_fix_sequences_against_postgres():
    """Ejecuta el SQL real contra Postgres y revierte (no muta el esquema)."""
    from app.database import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        sr._fix_sequences(cursor, conn)
        conn.rollback()
        cursor.close()
    finally:
        conn.close()
