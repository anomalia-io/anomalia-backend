"""Solicitudes de acceso (roles y/o tenants) y su flujo de aprobación (spec 019).

Cualquier usuario autenticado puede pedir roles/tenants indicando un motivo. Un
usuario con perfil ``approvers`` puede aprobar o rechazar; al aprobar se da de
alta de forma aditiva (unión) en ``user_roles`` y ``user_tenants``. El
solicitante nunca puede aprobar su propia solicitud.
"""
import json
import logging
from typing import List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import verify_any_user_token, verify_profile_access
from ..database import get_db_connection
from ..notifications import notify_user

logger = logging.getLogger("anomalia.approvals")

router = APIRouter(prefix="/api/v1/approvals", tags=["Approvals"])

APPROVER_PROFILE = "approvers"
APPROVALS_LINK = "/?tab=approvals"

# El rol 'admin' no es solicitable: un usuario no debe poder autonivelarse a
# administrador global por la vía de una solicitud (spec 019 §2).
NON_REQUESTABLE_ROLES = frozenset({"admin"})


class AccessRequestCreate(BaseModel):
    roles: List[str] = Field(default_factory=list)
    tenants: List[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=2000)


class AccessRequestReject(BaseModel):
    review_note: Optional[str] = Field(default=None, max_length=2000)


def _resolve_user_id(payload: dict) -> int:
    """Resuelve el id del usuario autenticado a partir del token."""
    username = payload.get("sub") or payload.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Token sin identidad de usuario.")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    return row[0]


def _user_is_approver(payload: dict) -> bool:
    """Indica si el usuario posee el perfil 'approvers' (el admin siempre puede)."""
    roles = [payload.get("role")] if payload.get("role") else []
    roles += payload.get("roles", [])
    roles = [r for r in roles if r]
    if "admin" in roles:
        return True
    if not roles:
        return False
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        placeholders = ",".join(["%s"] * len(roles))
        cursor.execute(
            f"""SELECT COUNT(*)
                FROM role_profiles rp
                JOIN roles r ON rp.role_id = r.id
                JOIN profiles p ON rp.profile_id = p.id
                WHERE r.name IN ({placeholders}) AND p.code = %s""",
            tuple(roles) + (APPROVER_PROFILE,),
        )
        return cursor.fetchone()[0] > 0
    finally:
        cursor.close()
        conn.close()


def _clean_names(names: List[str]) -> List[str]:
    """Normaliza: recorta, descarta vacíos y deduplica preservando el orden."""
    seen: List[str] = []
    for raw in names or []:
        name = (raw or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _existing_names(cursor, table: str, names: List[str], extra_where: str = "") -> Set[str]:
    if not names:
        return set()
    placeholders = ",".join(["%s"] * len(names))
    cursor.execute(
        f"SELECT name FROM {table} WHERE name IN ({placeholders}) {extra_where}",
        tuple(names),
    )
    return {r[0] for r in cursor.fetchall()}


async def _notify_approvers(title: str, body: str) -> None:
    """Notifica (in-app + email) a todos los usuarios activos con perfil approvers."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """SELECT DISTINCT u.id
               FROM users u
               JOIN user_roles ur ON ur.user_id = u.id
               JOIN role_profiles rp ON rp.role_id = ur.role_id
               JOIN profiles p ON p.id = rp.profile_id
               WHERE p.code = %s AND u.is_active = TRUE""",
            (APPROVER_PROFILE,),
        )
        approver_ids = [r[0] for r in cursor.fetchall()]
    finally:
        cursor.close()
        conn.close()
    for uid in approver_ids:
        await notify_user(uid, "access_request", title, body, link=APPROVALS_LINK)


@router.get("/catalog")
def get_access_catalog(payload: dict = Depends(verify_any_user_token)):
    """Roles y tenants que un usuario puede solicitar (spec 019 §3)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, name, description FROM roles ORDER BY id ASC;")
        roles = [
            {"id": r[0], "name": r[1], "description": r[2]}
            for r in cursor.fetchall()
            if r[1] not in NON_REQUESTABLE_ROLES
        ]
        cursor.execute(
            """SELECT id, name, type, environment, description
               FROM tenants
               WHERE deleted_at IS NULL AND status = 'active'
               ORDER BY id ASC;"""
        )
        tenants = [
            {"id": t[0], "name": t[1], "type": t[2], "environment": t[3], "description": t[4]}
            for t in cursor.fetchall()
        ]
        return {"roles": roles, "tenants": tenants}
    finally:
        cursor.close()
        conn.close()


@router.post("/requests")
async def create_access_request(
    payload: AccessRequestCreate,
    identity: dict = Depends(verify_any_user_token),
):
    """Crea una solicitud de acceso del usuario autenticado (spec 019 §4)."""
    user_id = _resolve_user_id(identity)
    reason = (payload.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="El motivo es obligatorio.")

    roles = [r for r in _clean_names(payload.roles) if r not in NON_REQUESTABLE_ROLES]
    tenants = _clean_names(payload.tenants)
    if not roles and not tenants:
        raise HTTPException(
            status_code=400,
            detail="La solicitud debe incluir al menos un rol o un tenant válido.",
        )

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        valid_roles = _existing_names(cursor, "roles", roles)
        valid_tenants = _existing_names(
            cursor, "tenants", tenants, extra_where="AND deleted_at IS NULL"
        )
        final_roles = [r for r in roles if r in valid_roles]
        final_tenants = [t for t in tenants if t in valid_tenants]
        if not final_roles and not final_tenants:
            raise HTTPException(
                status_code=400,
                detail="Los roles o tenants solicitados no existen.",
            )

        cursor.execute(
            """INSERT INTO access_requests
                   (user_id, requested_roles, requested_tenants, reason)
               VALUES (%s, %s::jsonb, %s::jsonb, %s)
               RETURNING id, created_at;""",
            (user_id, json.dumps(final_roles), json.dumps(final_tenants), reason),
        )
        new_id, created_at = cursor.fetchone()
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        logger.exception("No se pudo crear la solicitud de acceso: %s", exc)
        raise HTTPException(status_code=500, detail="No se pudo crear la solicitud.")
    finally:
        cursor.close()
        conn.close()

    await _notify_approvers(
        "Nueva solicitud de acceso",
        f"El usuario {identity.get('sub') or identity.get('username')} solicitó "
        f"{len(final_roles)} rol(es) y {len(final_tenants)} tenant(s).",
    )
    return {
        "id": new_id,
        "status": "pending",
        "requested_roles": final_roles,
        "requested_tenants": final_tenants,
        "created_at": created_at.isoformat() if created_at else None,
    }


@router.get("/requests")
def list_access_requests(
    status_filter: Optional[str] = None,
    payload: dict = Depends(verify_any_user_token),
):
    """Lista solicitudes: todas para approvers, solo las propias para el resto."""
    user_id = _resolve_user_id(payload)
    is_approver = _user_is_approver(payload)

    where = []
    params: list = []
    if not is_approver:
        where.append("ar.user_id = %s")
        params.append(user_id)
    if status_filter in ("pending", "approved", "rejected", "cancelled"):
        where.append("ar.status = %s")
        params.append(status_filter)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"""SELECT ar.id, ar.user_id, u.username, ar.requested_roles,
                       ar.requested_tenants, ar.reason, ar.status, ar.review_note,
                       ar.created_at, ar.reviewed_at, ru.username
                FROM access_requests ar
                JOIN users u ON u.id = ar.user_id
                LEFT JOIN users ru ON ru.id = ar.reviewer_id
                {where_sql}
                ORDER BY
                    CASE ar.status WHEN 'pending' THEN 0 ELSE 1 END,
                    ar.created_at DESC, ar.id DESC""",
            tuple(params),
        )
        items = [
            {
                "id": r[0],
                "user_id": r[1],
                "username": r[2],
                "requested_roles": r[3] or [],
                "requested_tenants": r[4] or [],
                "reason": r[5],
                "status": r[6],
                "review_note": r[7],
                "created_at": r[8].isoformat() if r[8] else None,
                "reviewed_at": r[9].isoformat() if r[9] else None,
                "reviewer": r[10],
                "can_cancel": (r[1] == user_id and r[6] == "pending"),
                "can_review": (is_approver and r[6] == "pending" and r[1] != user_id),
            }
            for r in cursor.fetchall()
        ]
    finally:
        cursor.close()
        conn.close()
    return {"items": items, "is_approver": is_approver}


def _apply_access(cursor, user_id: int, roles: List[str], tenants: List[str]) -> None:
    """Da de alta (unión) los roles y tenants aprobados, sin quitar los previos."""
    for name in roles:
        cursor.execute("SELECT id FROM roles WHERE name = %s", (name,))
        row = cursor.fetchone()
        if row:
            cursor.execute(
                "INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (user_id, row[0]),
            )
    for name in tenants:
        cursor.execute(
            "SELECT id FROM tenants WHERE name = %s AND deleted_at IS NULL", (name,)
        )
        row = cursor.fetchone()
        if row:
            cursor.execute(
                "INSERT INTO user_tenants (user_id, tenant_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (user_id, row[0]),
            )


def _load_pending_request(cursor, request_id: int) -> dict:
    cursor.execute(
        """SELECT id, user_id, requested_roles, requested_tenants, reason, status
           FROM access_requests WHERE id = %s FOR UPDATE""",
        (request_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada.")
    return {
        "id": row[0],
        "user_id": row[1],
        "requested_roles": row[2] or [],
        "requested_tenants": row[3] or [],
        "reason": row[4],
        "status": row[5],
    }


@router.post("/requests/{request_id}/approve")
async def approve_access_request(
    request_id: int,
    identity: dict = Depends(verify_profile_access(APPROVER_PROFILE)),
):
    """Aprueba una solicitud y aplica roles/tenants de forma aditiva (spec 019 §5)."""
    reviewer_id = _resolve_user_id(identity)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        req = _load_pending_request(cursor, request_id)
        if req["status"] != "pending":
            raise HTTPException(status_code=409, detail="La solicitud ya fue resuelta.")
        if req["user_id"] == reviewer_id:
            raise HTTPException(
                status_code=403, detail="No puedes aprobar tu propia solicitud."
            )

        _apply_access(cursor, req["user_id"], req["requested_roles"], req["requested_tenants"])
        cursor.execute(
            """UPDATE access_requests
               SET status = 'approved', reviewer_id = %s, reviewed_at = NOW()
               WHERE id = %s AND status = 'pending'""",
            (reviewer_id, request_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=409, detail="La solicitud ya fue resuelta.")
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        logger.exception("No se pudo aprobar la solicitud %s: %s", request_id, exc)
        raise HTTPException(status_code=500, detail="No se pudo aprobar la solicitud.")
    finally:
        cursor.close()
        conn.close()

    await notify_user(
        req["user_id"],
        "access_request",
        "Solicitud de acceso aprobada",
        "Tu solicitud de acceso fue aprobada. Los roles y tenants solicitados ya están activos.",
        link="/",
    )
    return {"ok": True, "status": "approved"}


@router.post("/requests/{request_id}/reject")
async def reject_access_request(
    request_id: int,
    payload: AccessRequestReject,
    identity: dict = Depends(verify_profile_access(APPROVER_PROFILE)),
):
    """Rechaza una solicitud de acceso (spec 019 §5)."""
    reviewer_id = _resolve_user_id(identity)
    note = (payload.review_note or "").strip() or None
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        req = _load_pending_request(cursor, request_id)
        if req["status"] != "pending":
            raise HTTPException(status_code=409, detail="La solicitud ya fue resuelta.")
        cursor.execute(
            """UPDATE access_requests
               SET status = 'rejected', reviewer_id = %s, review_note = %s, reviewed_at = NOW()
               WHERE id = %s AND status = 'pending'""",
            (reviewer_id, note, request_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=409, detail="La solicitud ya fue resuelta.")
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        logger.exception("No se pudo rechazar la solicitud %s: %s", request_id, exc)
        raise HTTPException(status_code=500, detail="No se pudo rechazar la solicitud.")
    finally:
        cursor.close()
        conn.close()

    body = "Tu solicitud de acceso fue rechazada."
    if note:
        body += f" Motivo: {note}"
    await notify_user(req["user_id"], "access_request", "Solicitud de acceso rechazada", body, link="/")
    return {"ok": True, "status": "rejected"}


@router.post("/requests/{request_id}/cancel")
async def cancel_access_request(
    request_id: int,
    identity: dict = Depends(verify_any_user_token),
):
    """El solicitante cancela su propia solicitud mientras siga pendiente (spec 019 §5)."""
    user_id = _resolve_user_id(identity)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        req = _load_pending_request(cursor, request_id)
        if req["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="No puedes cancelar esta solicitud.")
        if req["status"] != "pending":
            raise HTTPException(status_code=409, detail="La solicitud ya fue resuelta.")
        cursor.execute(
            "UPDATE access_requests SET status = 'cancelled', reviewed_at = NOW() "
            "WHERE id = %s AND status = 'pending' AND user_id = %s",
            (request_id, user_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=409, detail="La solicitud ya fue resuelta.")
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        logger.exception("No se pudo cancelar la solicitud %s: %s", request_id, exc)
        raise HTTPException(status_code=500, detail="No se pudo cancelar la solicitud.")
    finally:
        cursor.close()
        conn.close()
    return {"ok": True, "status": "cancelled"}
