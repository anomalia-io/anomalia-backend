"""job_state.logs_ref como TEXT (spec 011 §4.11).

Antes era VARCHAR(255), lo que truncaba el diagnóstico de fallos (p. ej. el
``UNREACHABLE!`` del SSH en la reconciliación post-import). Ahora es TEXT con un
tope de aplicación de 8000 chars para no guardar un stdout desmedido.
"""
import pytest

from app.workers.base import BaseWorker


def test_logs_ref_max_is_8000():
    assert BaseWorker.LOGS_REF_MAX == 8000


def _postgres_available() -> bool:
    try:
        from app.database import get_db_connection

        get_db_connection().close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _postgres_available(), reason="Postgres no accesible")
def test_logs_ref_column_accepts_more_than_255_chars():
    """Aplica el ALTER de la migración y valida TEXT; revierte (no persiste)."""
    from app.database import get_db_connection

    payload = "x" * 1000
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE job_state ALTER COLUMN logs_ref TYPE TEXT;")
        cursor.execute(
            "INSERT INTO job_state (id, type, status, logs_ref) VALUES (%s, %s, %s, %s);",
            ("test-logs-ref", "test", "failed", payload),
        )
        cursor.execute("SELECT logs_ref FROM job_state WHERE id = %s;", ("test-logs-ref",))
        stored = cursor.fetchone()[0]
        assert stored == payload
        assert len(stored) == 1000
    finally:
        conn.rollback()
        conn.close()