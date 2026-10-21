"""Unit tests del flujo de aprobaciones (spec 019).

Validan funciones puras del router y el registro de rutas, sin requerir BD.
"""
from app.routers.approvals_router import (
    APPROVER_PROFILE,
    NON_REQUESTABLE_ROLES,
    router,
    _clean_names,
)


def test_clean_names_trims_and_dedupes_preserving_order():
    assert _clean_names(["  METRICS ", "METRICS", "", "TRACES", None]) == ["METRICS", "TRACES"]


def test_clean_names_handles_empty_and_none():
    assert _clean_names([]) == []
    assert _clean_names(None) == []


def test_admin_role_is_not_requestable():
    assert "admin" in NON_REQUESTABLE_ROLES


def test_approver_profile_code():
    assert APPROVER_PROFILE == "approvers"


def test_expected_routes_registered():
    paths = {r.path for r in router.routes}
    assert "/api/v1/approvals/catalog" in paths
    assert "/api/v1/approvals/requests" in paths
    assert "/api/v1/approvals/requests/{request_id}/approve" in paths
    assert "/api/v1/approvals/requests/{request_id}/reject" in paths
    assert "/api/v1/approvals/requests/{request_id}/cancel" in paths
