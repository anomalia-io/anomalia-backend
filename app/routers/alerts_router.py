"""Endpoints REST de alertas enriquecidas por IA (spec 018).

Alimentan la tarjeta expandible del index web y la pantalla de alertas de la app
Android. El scope es siempre el del usuario: intersección de los tenants que
puede ver (user_tenants ∪ role_tenants) con las alertas persistidas; el rol
`admin` ve todo.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..ai_providers import ALERT_SEVERITIES
from ..auth import verify_any_user_token, verify_profile_access
from ..database import get_db_connection

logger = logging.getLogger("anomalia.alerts")

router = APIRouter(prefix="/api/v1/alerts", tags=["Alerts"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class SeverityOverride(BaseModel):
    severity: str


def _roles(payload: dict) -> List[str]:
    roles = payload.get("roles", []) or []
    primary = payload.get("role", "")
    return ([primary] if primary else []) + list(roles)


def _is_admin(payload: dict) -> bool:
    return "admin" in _roles(payload)


def _ilike_contains(value: str) -> str:
    """Patrón ILIKE ``%value%`` escapando los comodines del usuario.

    Sin esto, un ``%`` o ``_`` de la búsqueda actuaría como comodín de SQL.
    El ``ESCAPE '\\'`` de la condición marca ``\\`` como carácter de escape.
    """
    escaped = value.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# Columnas que cubre la búsqueda de texto del historial (spec 018).
SEARCH_COLUMNS: tuple = ("a.alertname", "a.summary", "a.description", "a.ai_summary")


def _text_search_condition() -> str:
    """Condición ILIKE para la búsqueda de texto del historial (spec 018).

    Cada operando lleva su propio ``ESCAPE '\\'``: un único ``ESCAPE`` al final de
    la expresión agrupada con ``OR`` es **sintaxis inválida** en PostgreSQL
    (``(...) ESCAPE '\\'``). ``_ilike_contains`` ya escapa ``\\``, ``%`` y ``_``.
    """
    return "(" + " OR ".join(
        f"{col} ILIKE %s ESCAPE '\\'" for col in SEARCH_COLUMNS
    ) + ")"


def _accessible_tenant_ids(cursor, payload: dict) -> Optional[List[int]]:
    """Devuelve los tenant_id visibles o None si el usuario es admin (todos)."""
    if _is_admin(payload):
        return None
    username = payload.get("sub") or payload.get("username")
    if not username:
        return []
    cursor.execute(
        """
        SELECT ut.tenant_id
        FROM user_tenants ut
        JOIN users u ON u.id = ut.user_id
        WHERE u.username = %s
        UNION
        SELECT rt.tenant_id
        FROM role_tenants rt
        JOIN user_roles ur ON ur.role_id = rt.role_id
        JOIN users ru ON ru.id = ur.user_id
        WHERE ru.username = %s
        """,
        (username, username),
    )
    return [r[0] for r in cursor.fetchall()]


def _serialize(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "tenant_id": row[1],
        "tenant_name": row[2],
        "alertname": row[3],
        "severity": row[4],
        "severity_source": row[5],
        "summary": row[6],
        "description": row[7],
        "ai_summary": row[8],
        "ai_root_cause": row[9],
        "ai_checks": row[10] or [],
        "ai_error": row[21],
        "email_status": row[22],
        "email_error": row[23],
        "status": row[11],
        "labels": row[12] or {},
        "annotations": row[13] or {},
        "generator_url": row[14],
        "severity_override_by": row[15],
        "severity_override_at": row[16].isoformat() if row[16] else None,
        "starts_at": row[17].isoformat() if row[17] else None,
        "ends_at": row[18].isoformat() if row[18] else None,
        "notified_at": row[19].isoformat() if row[19] else None,
        "updated_at": row[20].isoformat() if row[20] else None,
    }


_SELECT_COLUMNS = """
    a.id, a.tenant_id, t.name, a.alertname, a.severity, a.severity_source,
    a.summary, a.description, a.ai_summary, a.ai_root_cause, a.ai_checks,
    a.status, a.labels, a.annotations, a.generator_url,
    a.severity_override_by, a.severity_override_at, a.starts_at, a.ends_at,
    a.notified_at, a.updated_at, a.ai_error, a.email_status, a.email_error
"""


@router.get("/recent")
async def recent_alerts(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    payload: dict = Depends(verify_profile_access("access_alerts")),
):
    """Alertas activas (status='firing') visibles para el usuario (spec 018)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        tenant_ids = _accessible_tenant_ids(cursor, payload)
        conditions = ["a.status = 'firing'"]
        params: List[Any] = []
        if tenant_ids is not None:
            if not tenant_ids:
                return {"items": []}
            conditions.append("a.tenant_id = ANY(%s)")
            params.append(tenant_ids)
        where = "WHERE " + " AND ".join(conditions)
        cursor.execute(
            f"""SELECT {_SELECT_COLUMNS}
                FROM alert_history a
                LEFT JOIN tenants t ON t.id = a.tenant_id
                {where}
                ORDER BY a.updated_at DESC, a.id DESC
                LIMIT %s""",
            params + [limit],
        )
        return {"items": [_serialize(r) for r in cursor.fetchall()]}
    finally:
        cursor.close()
        conn.close()


@router.get("/history")
async def alert_history(
    tenant_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    q: Optional[str] = Query(None, description="Búsqueda case-insensitive"),
    payload: dict = Depends(verify_profile_access("access_alerts")),
):
    """Historial de alertas con filtros y paginación (spec 018)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        tenant_ids = _accessible_tenant_ids(cursor, payload)
        if tenant_ids is not None and not tenant_ids:
            return {"items": [], "total": 0}

        conditions: List[str] = []
        params: List[Any] = []
        if tenant_ids is not None:
            conditions.append("a.tenant_id = ANY(%s)")
            params.append(tenant_ids)
        if tenant_id is not None:
            if tenant_ids is not None and tenant_id not in tenant_ids:
                raise HTTPException(status_code=403, detail="Acceso denegado al tenant.")
            conditions.append("a.tenant_id = %s")
            params.append(tenant_id)
        if status:
            conditions.append("a.status = %s")
            params.append(status)
        if q and q.strip():
            like = _ilike_contains(q)
            conditions.append(_text_search_condition())
            params.extend([like] * len(SEARCH_COLUMNS))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        cursor.execute(
            f"SELECT COUNT(*) FROM alert_history a {where}",
            params,
        )
        total = cursor.fetchone()[0]

        cursor.execute(
            f"""SELECT {_SELECT_COLUMNS}
                FROM alert_history a
                LEFT JOIN tenants t ON t.id = a.tenant_id
                {where}
                ORDER BY a.updated_at DESC, a.id DESC
                LIMIT %s OFFSET %s""",
            params + [limit, offset],
        )
        return {"items": [_serialize(r) for r in cursor.fetchall()], "total": total}
    finally:
        cursor.close()
        conn.close()


@router.patch("/{alert_id}/severity")
async def override_severity(
    alert_id: int,
    body: SeverityOverride,
    payload: dict = Depends(verify_profile_access("alerts_manager")),
):
    """Fija manualmente la criticidad de una alerta (spec 018).

    Requiere el perfil `alerts_manager`; registra quién y cuándo. La decisión
    manual prevalece sobre el veredicto de la IA en reanálisis posteriores.
    """
    severity = (body.severity or "").strip().lower()
    if severity not in ALERT_SEVERITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Severidad inválida. Valores: {', '.join(ALERT_SEVERITIES)}",
        )
    username = payload.get("sub") or payload.get("username") or "unknown"

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT tenant_id FROM alert_history WHERE id = %s", (alert_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Alerta no encontrada.")

        tenant_id = row[0]
        if not _is_admin(payload):
            tenant_ids = _accessible_tenant_ids(cursor, payload)
            if tenant_ids is not None and tenant_id not in tenant_ids:
                raise HTTPException(status_code=403, detail="Acceso denegado al tenant.")

        cursor.execute(
            """UPDATE alert_history
                  SET severity = %s,
                      severity_source = 'manual',
                      severity_override_by = %s,
                      severity_override_at = NOW(),
                      updated_at = NOW()
                WHERE id = %s""",
            (severity, username, alert_id),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    return {"ok": True, "id": alert_id, "severity": severity, "severity_source": "manual"}
