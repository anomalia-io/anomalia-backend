"""Endpoints REST de notificaciones por usuario (spec 017 §4)."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import verify_any_user_token
from ..database import get_db_connection

logger = logging.getLogger("anomalia.notifications")

router = APIRouter(prefix="/api/v1/notifications", tags=["Notifications"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class DeviceRegistration(BaseModel):
    fcm_token: str = Field(min_length=8, max_length=4096)
    platform: str = Field(default="android", max_length=50)


def _resolve_user_id(payload: dict) -> int:
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


@router.get("")
async def list_notifications(
    unread: bool = Query(False),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    payload: dict = Depends(verify_any_user_token),
):
    """Lista las notificaciones del usuario y el contador de no leídas (spec 017 §4)."""
    user_id = _resolve_user_id(payload)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        where = "WHERE user_id = %s"
        params: list = [user_id]
        if unread:
            where += " AND is_read = FALSE"
        cursor.execute(
            f"""SELECT id, type, title, body, job_id, link, is_read, created_at
                FROM notifications
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT %s""",
            params + [limit],
        )
        items = [
            {
                "id": r[0],
                "type": r[1],
                "title": r[2],
                "body": r[3],
                "job_id": r[4],
                "link": r[5],
                "is_read": r[6],
                "created_at": r[7].isoformat() if r[7] else None,
            }
            for r in cursor.fetchall()
        ]
        cursor.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = %s AND is_read = FALSE",
            (user_id,),
        )
        unread_count = cursor.fetchone()[0]
    finally:
        cursor.close()
        conn.close()
    return {"items": items, "unread_count": unread_count}


@router.post("/{notification_id}/read")
async def mark_read(notification_id: int, payload: dict = Depends(verify_any_user_token)):
    """Marca una notificación del usuario como leída (spec 017 §4)."""
    user_id = _resolve_user_id(payload)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE notifications SET is_read = TRUE WHERE id = %s AND user_id = %s",
            (notification_id, user_id),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Notificación no encontrada.")
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    return {"ok": True}


@router.post("/read-all")
async def mark_all_read(payload: dict = Depends(verify_any_user_token)):
    """Marca todas las notificaciones del usuario como leídas (spec 017 §4)."""
    user_id = _resolve_user_id(payload)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE notifications SET is_read = TRUE WHERE user_id = %s AND is_read = FALSE",
            (user_id,),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    return {"ok": True}


@router.post("/devices")
async def register_device(
    device: DeviceRegistration,
    payload: dict = Depends(verify_any_user_token),
):
    """Registra (upsert) el token FCM de un dispositivo para push (spec 018).

    Un token identifica un dispositivo físico; si otro usuario inició sesión en
    él, el token se reasigna al usuario actual (la app reenvía el token al login).
    """
    user_id = _resolve_user_id(payload)
    token = device.fcm_token.strip()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT INTO user_devices (user_id, fcm_token, platform, updated_at, last_seen_at)
               VALUES (%s, %s, %s, NOW(), NOW())
               ON CONFLICT (fcm_token) DO UPDATE SET
                   user_id = EXCLUDED.user_id,
                   platform = EXCLUDED.platform,
                   updated_at = NOW(),
                   last_seen_at = NOW()""",
            (user_id, token, device.platform or "android"),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    return {"ok": True}


@router.delete("/devices")
async def unregister_device(
    fcm_token: str = Query(..., min_length=8),
    payload: dict = Depends(verify_any_user_token),
):
    """Elimina el token FCM del usuario (p. ej. al cerrar sesión)."""
    user_id = _resolve_user_id(payload)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM user_devices WHERE fcm_token = %s AND user_id = %s",
            (fcm_token.strip(), user_id),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    return {"ok": True}
