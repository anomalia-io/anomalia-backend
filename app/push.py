"""Despacho de notificaciones push a la app móvil vía FCM HTTP v1 (spec 018).

Best-effort y desacoplado: la configuración (project_id + service account JSON)
vive SOLO en `system_settings` (nunca en el repositorio, constitución §4). Si
`push_enabled` es falso o faltan credenciales, no se envía nada y se registra un
aviso; el resto del pipeline (email + in-app) sigue funcionando.

FCM HTTP v1 exige un OAuth2 access token derivado de un JWT RS256 firmado con la
private key del service account. Se firma con PyJWT (RS256 disponible gracias a
`python-jose[cryptography]`) y se canjea en el endpoint de Google.
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx
import jwt

from .database import get_db_connection

logger = logging.getLogger("anomalia.push")

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
FCM_TIMEOUT_SECONDS = 15


def _get_push_config() -> Dict[str, Any]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT push_enabled, fcm_project_id, fcm_service_account_json "
            "FROM system_settings ORDER BY id LIMIT 1"
        )
        row = cursor.fetchone()
        if not row:
            return {"enabled": False, "project_id": "", "service_account": None}
        return {
            "enabled": bool(row[0]),
            "project_id": row[1] or "",
            "service_account": row[2] or "",
        }
    finally:
        cursor.close()
        conn.close()


def _load_device_tokens(tenant_id: Optional[int]) -> List[str]:
    """Tokens FCM de usuarios con acceso al tenant (o todos si tenant_id es NULL)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if tenant_id is None:
            cursor.execute("SELECT fcm_token FROM user_devices")
        else:
            cursor.execute(
                """
                SELECT DISTINCT d.fcm_token
                FROM user_devices d
                WHERE d.user_id IN (
                    SELECT ut.user_id FROM user_tenants ut WHERE ut.tenant_id = %s
                    UNION
                    SELECT ur.user_id FROM role_tenants rt
                      JOIN user_roles ur ON ur.role_id = rt.role_id
                     WHERE rt.tenant_id = %s
                )
                """,
                (tenant_id, tenant_id),
            )
        return [r[0] for r in cursor.fetchall() if r[0]]
    finally:
        cursor.close()
        conn.close()


def _get_access_token(service_account: Dict[str, Any]) -> str:
    now = int(time.time())
    claims = {
        "iss": service_account["client_email"],
        "scope": FCM_SCOPE,
        "aud": GOOGLE_TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    assertion = jwt.encode(claims, service_account["private_key"], algorithm="RS256")
    response = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=FCM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def _send_to_tokens(
    tokens: List[str],
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
) -> int:
    """Envía el mensaje a cada token. Devuelve cuántos se entregaron con éxito."""
    config = _get_push_config()
    if not config["enabled"]:
        logger.info("Push deshabilitado (push_enabled=false); se omite el envío.")
        return 0
    if not config["project_id"] or not config["service_account"]:
        logger.info("Push sin credenciales FCM configuradas; se omite el envío.")
        return 0
    if not tokens:
        return 0

    try:
        service_account = json.loads(config["service_account"])
        access_token = _get_access_token(service_account)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo obtener el token de FCM: %s", exc)
        return 0

    url = FCM_SEND_URL.format(project_id=config["project_id"])
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    delivered = 0
    for token in tokens:
        message = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": {k: str(v) for k, v in (data or {}).items()},
            }
        }
        try:
            response = httpx.post(url, headers=headers, json=message, timeout=FCM_TIMEOUT_SECONDS)
            if response.status_code == 200:
                delivered += 1
            else:
                logger.info(
                    "FCM rechazó el token %s...: %s %s",
                    token[:12],
                    response.status_code,
                    response.text[:200],
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("Envío push falló para un token: %s", exc)
    return delivered


def send_push_to_tenant(
    tenant_id: Optional[int],
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
) -> int:
    """Carga los tokens con acceso al tenant y despacha el push (best-effort)."""
    try:
        tokens = _load_device_tokens(tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudieron cargar los dispositivos para push: %s", exc)
        return 0
    return _send_to_tokens(tokens, title, body, data)
