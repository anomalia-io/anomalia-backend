import asyncio
import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from .database import get_db_connection

logger = logging.getLogger("anomalia.mailer")

MAIL_TIMEOUT_SECONDS = int(os.getenv("MAIL_TIMEOUT_SECONDS", "15"))


class EmailNotConfiguredError(RuntimeError):
    """SMTP no configurado (falta ``smtp_host`` en ``system_settings``).

    Se distingue de un fallo de envío real para poder avisar de forma explícita
    (log WARNING + notificación a administradores) en lugar de un error genérico.
    """


def _get_smtp_config() -> dict:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT smtp_host, smtp_port, smtp_username, smtp_password,
                   smtp_use_tls, smtp_from
            FROM system_settings ORDER BY id LIMIT 1;
            """
        )
        row = cursor.fetchone()
        if not row:
            return {}
        return {
            "host": row[0],
            "port": row[1],
            "username": row[2],
            "password": row[3],
            "use_tls": row[4],
            "from_addr": row[5],
        }
    finally:
        cursor.close()
        conn.close()


def _send_email_sync(
    to_addr: str,
    subject: str,
    body: str,
    config: dict,
    timeout: int,
) -> None:
    host = config.get("host", "")
    port = int(config.get("port") or 587)
    username = config.get("username", "")
    password = config.get("password", "")
    use_tls = config.get("use_tls", True)
    from_addr = config.get("from_addr", "") or username

    if not host:
        raise EmailNotConfiguredError(
            "SMTP no configurado: falta smtp_host en system_settings."
        )

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if use_tls:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            if username and password:
                server.login(username, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            if username and password:
                server.login(username, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())


async def send_email(
    to_addr: str,
    subject: str,
    body: str,
    timeout: Optional[int] = None,
) -> dict:
    config = await asyncio.to_thread(_get_smtp_config)
    effective_timeout = timeout if timeout is not None else MAIL_TIMEOUT_SECONDS
    try:
        await asyncio.to_thread(
            _send_email_sync, to_addr, subject, body, config, effective_timeout
        )
    except Exception:
        # WARNING para que sea visible en `docker logs` (raíz en WARNING); el
        # error se propaga y el caller lo persiste en alert_history.email_error.
        logger.warning("Fallo al enviar email a %s: %s", to_addr, subject)
        raise
    logger.info("Email enviado a %s (%s)", to_addr, subject)
    return {"ok": True, "to": to_addr}


def is_email_configured() -> bool:
    """Indica si hay ``smtp_host`` configurado (sin abrir conexión SMTP)."""
    return bool(_get_smtp_config().get("host"))
