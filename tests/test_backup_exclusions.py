"""Alcance del backup (spec 014 §6.2, spec 015).

La infraestructura la gestiona cada clúster vía deploy; no es config portable.
Por eso `infrastructure_nodes` (y sus artefactos `tenant_vmalert_instances`) no
se exportan ni se importan. El resto de tablas de configuración sí.
"""
from app.routers import system_router as sr


def test_infra_tables_excluded_from_backup():
    assert "infrastructure_nodes" not in sr.BACKUP_TABLES
    assert "tenant_vmalert_instances" not in sr.BACKUP_TABLES


def test_config_tables_present_in_backup():
    for table in (
        "users",
        "roles",
        "profiles",
        "tenants",
        "tenant_datasources",
        "ai_settings",
        "oidc_providers",
        "system_settings",
    ):
        assert table in sr.BACKUP_TABLES


def test_backup_pk_has_no_infra_tables():
    assert "infrastructure_nodes" not in sr.BACKUP_PK
    assert "tenant_vmalert_instances" not in sr.BACKUP_PK
