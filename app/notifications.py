"""Notificaciones genéricas para el usuario (spec 017).

Persiste el desenlace de un job iniciado por un usuario y, si hay SMTP
configurado, envía además un email. Es reutilizable por cualquier evento futuro
(no solo el import de backup).
"""
import asyncio
import logging
from typing import Optional

from .database import get_db_connection
from .mailer import send_email

logger = logging.getLogger("anomalia.notifications")

# Retención: máximo de notificaciones conservadas por usuario (spec 017 §2).
MAX_NOTIFICATIONS_PER_USER = 200


def _insert_notification(
    user_id: int,
    ntype: Optional[str],
    title: str,
    body: Optional[str],
    job_id: Optional[str],
    link: Optional[str],
) -> int:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT INTO notifications (user_id, type, title, body, job_id, link)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (user_id, ntype, title, body, job_id, link),
        )
        new_id = cursor.fetchone()[0]
        # Cap de retención: elimina las más antiguas por encima del límite.
        cursor.execute(
            """DELETE FROM notifications
               WHERE id IN (
                   SELECT id FROM notifications
                   WHERE user_id = %s
                   ORDER BY created_at DESC, id DESC
                   OFFSET %s
               )""",
            (user_id, MAX_NOTIFICATIONS_PER_USER),
        )
        conn.commit()
        return new_id
    finally:
        cursor.close()
        conn.close()


def _get_username(user_id: int) -> Optional[str]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT username FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    return row[0] if row else None


async def notify_user(
    user_id: int,
    ntype: Optional[str],
    title: str,
    body: Optional[str] = None,
    job_id: Optional[str] = None,
    link: Optional[str] = None,
    send_email_notice: bool = True,
) -> None:
    """Inserta la notificación in-app y, best-effort, envía el email (spec 017 §3).

    ``send_email_notice=False`` inserta solo la notificación in-app (spec 018):
    el despacho de email de alertas va a la lista de destinatarios del tenant,
    no al buzón personal del usuario, para no duplicar avisos.
    """
    try:
        await asyncio.to_thread(
            _insert_notification, user_id, ntype, title, body, job_id, link
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo insertar la notificación para user=%s: %s", user_id, exc)
        return

    if not send_email_notice:
        return

    # El email es best-effort y requiere SMTP configurado (spec 014).
    to_addr = await asyncio.to_thread(_get_username, user_id)
    if not to_addr:
        return
    try:
        await send_email(to_addr, title, body or title)
    except Exception as exc:  # noqa: BLE001
        logger.info("Email de notificación no enviado a %s: %s", to_addr, exc)


async def notify_job_outcome(
    user_id: int,
    job_id: str,
    job_type: str,
    ref_id: Optional[int],
    status: str,
    error_code: Optional[str] = None,
) -> None:
    """Notifica el desenlace de un job al usuario que lo inició (spec 017 §3)."""
    if status == "succeeded":
        title = f"Trabajo completado: {job_type}"
        body = f"El trabajo {job_type} (ref {ref_id}) finalizó correctamente."
    else:
        title = f"Trabajo fallido: {job_type}"
        body = f"El trabajo {job_type} (ref {ref_id}) falló"
        if error_code:
            body += f" ({error_code})"
        body += "."
        if job_type == "backup_import":
            body += " Se revirtió por completo la importación (base de datos e infraestructura)."
    link = "/?tab=bkp" if job_type == "backup_import" else None
    await notify_user(user_id, job_type, title, body, job_id=job_id, link=link)
