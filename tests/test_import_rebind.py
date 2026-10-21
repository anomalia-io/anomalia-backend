"""Import de backup entre clústeres (spec 015).

El backup **no incluye** la infraestructura (`infrastructure_nodes`,
`tenant_vmalert_instances`); al importar, cada tenant se **desacopla** de los
nodos del clúster origen (FK a ``NULL`` + placement ``auto``) y la reconciliación
post-import le asigna un nodo local. Sin nodo del tipo, no se crea nada.
"""
import pytest

from app.routers import system_router as sr


def test_infra_no_viaja_en_el_backup():
    assert "infrastructure_nodes" not in sr.BACKUP_TABLES
    assert "tenant_vmalert_instances" not in sr.BACKUP_TABLES
    assert "infrastructure_nodes" not in sr.BACKUP_PK
    assert "tenant_vmalert_instances" not in sr.BACKUP_PK


def test_detach_tenant_node_refs_nulls_and_sets_auto():
    row = {
        "id": 7,
        "vmalert_node_id": 11,
        "vmauth_node_id": 12,
        "instance_id": 11,
        "vmalert_port": 8887,
        "placement_mode": "manual",
        "vmauth_placement_mode": "manual",
    }

    detached = sr._detach_tenant_node_refs(row)

    assert detached["vmalert_node_id"] is None
    assert detached["vmauth_node_id"] is None
    assert detached["instance_id"] is None
    assert detached["vmalert_port"] is None
    assert detached["placement_mode"] == "auto"
    assert detached["vmauth_placement_mode"] == "auto"


def test_detach_tenant_node_refs_is_noop_without_ref_columns():
    row = {"id": 7, "name": "tenant"}

    assert sr._detach_tenant_node_refs(row) == {"id": 7, "name": "tenant"}


def test_collect_imported_pks_uses_pk_columns():
    tables = {
        "tenants": [{"id": 1}, {"id": 2}, {"name": "sin_id"}],
        "user_roles": [{"user_id": 1, "role_id": 2}],
    }

    pks = sr._collect_imported_pks(tables)

    assert pks["tenants"] == [[1], [2]]
    assert pks["user_roles"] == [[1, 2]]
    assert pks["users"] == []


def test_pk_tuple_normalizes_scalar_and_list():
    assert sr._pk_tuple(5) == (5,)
    assert sr._pk_tuple([5]) == (5,)
    assert sr._pk_tuple([5, 6]) == (5, 6)


def test_import_backup_no_longer_reconcilia_con_jobs():
    """El import pasó a un job único ``backup_import`` (spec 016)."""
    assert sr.IMPORT_JOB_TYPE == "backup_import"
    assert not hasattr(sr, "_reconcile_imported_tenants")


def test_pick_vmalert_node_respects_existing_load_and_handles_empty():
    assert sr._pick_vmalert_node([], {}) is None
    nodes = [1, 2]
    assert sr._pick_vmalert_node(nodes, {1: 0, 2: 0}) == 1
    assert sr._pick_vmalert_node(nodes, {1: 5, 2: 1}) == 2
    assert sr._pick_vmalert_node(nodes, {1: 2, 2: 2}) == 1


def _postgres_available() -> bool:
    try:
        from app.database import get_db_connection

        get_db_connection().close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _postgres_available(), reason="Postgres no accesible")
def test_import_persists_detached_tenant_refs():
    """Emula el upsert del import y revierte (no muta datos)."""
    from app.database import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        row = {
            "id": 990001,
            "name": "TEST_DETACH",
            "slug": "test-detach",
            "vmalert_node_id": 9001,
            "vmauth_node_id": 9002,
            "vmalert_port": 8899,
            "placement_mode": "manual",
            "vmauth_placement_mode": "manual",
        }
        detached = sr._detach_tenant_node_refs(row)
        sr._upsert_row(cursor, "tenants", detached)

        cursor.execute(
            """SELECT vmalert_node_id, vmauth_node_id, vmalert_port,
                      placement_mode, vmauth_placement_mode
               FROM tenants WHERE slug = %s;""",
            ("test-detach",),
        )
        stored = cursor.fetchone()
        assert stored == (None, None, None, "auto", "auto")
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.skipif(not _postgres_available(), reason="Postgres no accesible")
def test_restore_import_snapshot_reverts_bd():
    """El rollback borra filas nuevas y restaura las sobrescritas (spec 016)."""
    from psycopg2.extras import Json

    from app.database import get_db_connection

    job_id = "test-restore-import-016"
    orig_id, new_id = 990101, 990102

    def _cleanup(cursor):
        cursor.execute(
            "DELETE FROM tenants WHERE id IN (%s, %s);", (orig_id, new_id)
        )
        cursor.execute(
            "DELETE FROM backup_import_state WHERE job_id = %s;", (job_id,)
        )

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # El volumen del contenedor puede ser anterior a la migración spec 016.
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS backup_import_state (
                   job_id VARCHAR(36) PRIMARY KEY,
                   snapshot JSONB NOT NULL,
                   preexisting_tenant_ids INTEGER[] NOT NULL DEFAULT '{}',
                   imported_pks JSONB,
                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
               );"""
        )
        conn.commit()
        _cleanup(cursor)
        conn.commit()

        cursor.execute(
            """INSERT INTO tenants
                   (id, name, slug, type, status, placement_mode, vmauth_placement_mode)
               VALUES (%s, 'ORIG', 'test-restore-orig', 'metrics', 'active', 'auto', 'auto');""",
            (orig_id,),
        )
        snapshot = {
            "tenants": [{
                "id": orig_id, "name": "ORIG", "slug": "test-restore-orig",
                "type": "metrics", "status": "active",
                "placement_mode": "auto", "vmauth_placement_mode": "auto",
            }]
        }
        cursor.execute(
            """INSERT INTO backup_import_state
                   (job_id, snapshot, preexisting_tenant_ids, imported_pks)
               VALUES (%s, %s, %s, %s);""",
            (job_id, Json(snapshot), [orig_id], Json({"tenants": [[orig_id], [new_id]]})),
        )
        # Simular el import: introduce un tenant nuevo y muta el existente.
        cursor.execute(
            """INSERT INTO tenants
                   (id, name, slug, type, status, placement_mode, vmauth_placement_mode)
               VALUES (%s, 'NEW', 'test-restore-new', 'metrics', 'active', 'auto', 'auto');""",
            (new_id,),
        )
        cursor.execute("UPDATE tenants SET name = 'MUTATED' WHERE id = %s;", (orig_id,))
        conn.commit()

        sr.restore_import_snapshot(job_id)
        conn.commit()

        cursor.execute("SELECT COUNT(*) FROM tenants WHERE id = %s;", (new_id,))
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT name FROM tenants WHERE id = %s;", (orig_id,))
        assert cursor.fetchone()[0] == "ORIG"
    finally:
        conn.rollback()
        cursor = conn.cursor()
        _cleanup(cursor)
        conn.commit()
        conn.close()
