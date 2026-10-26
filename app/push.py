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


def validate_service_account(raw_input: str) -> Dict[str, Any]:
    """Valida y normaliza el JSON de una service account de Firebase o una clave privada PEM cruda.

    Permite ingresar tanto el JSON completo de la service account como la clave
    privada sola (PEM con ``-----BEGIN PRIVATE KEY-----``). Si se ingresa solo la
    clave, reutiliza el ``client_email`` y ``project_id`` vigentes en la base de datos.
    Lanza ``ValueError`` con un mensaje apto para el administrador.
    """
    if not raw_input or not raw_input.strip():
        raise ValueError("La credencial está vacía.")
    raw_input = raw_input.strip()

    # Intentar parsear como JSON primero
    try:
        data = json.loads(raw_input)
        if not isinstance(data, dict):
            raise ValueError("La service account debe ser un objeto JSON.")
        client_email = data.get("client_email")
        if not isinstance(client_email, str) or not client_email.strip():
            raise ValueError("La service account no contiene 'client_email'.")
        data["client_email"] = client_email.strip()

        private_key = data.get("private_key")
        if not isinstance(private_key, str) or "BEGIN PRIVATE KEY" not in private_key:
            raise ValueError("La service account no contiene 'private_key' (PEM RSA).")

        if not data.get("project_id"):
            domain = data["client_email"].rsplit("@", 1)[-1]
            data["project_id"] = domain.split(".", 1)[0]
        return data
    except json.JSONDecodeError:
        pass  # No es JSON válido, verificar si es clave PEM cruda
    except ValueError as exc:
        if "objeto JSON" in str(exc):
            raise
        raise

    # Soporte para clave privada PEM cruda
    if "BEGIN PRIVATE KEY" in raw_input or raw_input.startswith("-----BEGIN"):
        config = _get_push_config()
        existing_sa = {}
        if config["service_account"]:
            try:
                existing_sa = json.loads(config["service_account"])
            except Exception:  # noqa: BLE001
                pass

        client_email = existing_sa.get("client_email") or f"firebase-adminsdk@{config['project_id'] or 'anomalia'}.iam.gserviceaccount.com"
        project_id = config["project_id"] or existing_sa.get("project_id") or "anomalia"

        return {
            "type": "service_account",
            "project_id": project_id,
            "private_key": raw_input,
            "client_email": client_email,
        }

    raise ValueError("Service account JSON inválido o clave PEM no reconocida.")


def load_device_tokens(tenant_id: Optional[int]) -> List[str]:
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


def _dispatch(
    tokens: List[str],
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Envía a cada token y devuelve el detalle del intento.

    El resultado incluye ``attempted``, ``delivered``, ``errors`` y, si no se
    envió nada, un ``skipped`` con el motivo. Se usa tanto para los envíos del
    worker (que solo toma ``delivered``) como para el endpoint de prueba.
    """
    result: Dict[str, Any] = {
        "attempted": len(tokens),
        "delivered": 0,
        "errors": [],
        "skipped": None,
    }
    config = _get_push_config()
    if not config["enabled"]:
        logger.info("Push deshabilitado (push_enabled=false); se omite el envío.")
        result["skipped"] = "push_disabled"
        return result
    if not config["project_id"] or not config["service_account"]:
        logger.info("Push sin credenciales FCM configuradas; se omite el envío.")
        result["skipped"] = "not_configured"
        return result
    if not tokens:
        result["skipped"] = "no_tokens"
        return result

    try:
        service_account = validate_service_account(config["service_account"])
        access_token = _get_access_token(service_account)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo obtener el token de FCM: %s", exc)
        result["skipped"] = "auth_error"
        result["errors"].append({"token": None, "status": None, "error": str(exc)[:300]})
        return result

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
                result["errors"].append(
                    {
                        "token": token[:12],
                        "status": response.status_code,
                        "error": response.text[:300],
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("Envío push falló para un token: %s", exc)
            result["errors"].append(
                {"token": token[:12], "status": None, "error": str(exc)[:300]}
            )
    result["delivered"] = delivered
    return result


def _send_to_tokens(
    tokens: List[str],
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
) -> int:
    """Envía el mensaje a cada token. Devuelve cuántos se entregaron con éxito."""
    return _dispatch(tokens, title, body, data)["delivered"]


def send_push_to_tokens(
    tokens: List[str],
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Despacha a tokens explícitos y devuelve el detalle (endpoint de prueba)."""
    return _dispatch(tokens, title, body, data)


def send_push_to_tenant(
    tenant_id: Optional[int],
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
) -> int:
    """Carga los tokens con acceso al tenant y despacha el push (best-effort)."""
    try:
        tokens = load_device_tokens(tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudieron cargar los dispositivos para push: %s", exc)
        return 0
    return _send_to_tokens(tokens, title, body, data)
