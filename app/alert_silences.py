import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator, Optional

import httpx
from psycopg2.extras import Json

from .database import get_db_connection

logger = logging.getLogger("anomalia.alerts")

VMALERT_DOWN_ALERT = "VmalertTenantDown"
DEFAULT_SILENCE_TTL_SECONDS = 600
ALERTMANAGER_FALLBACK_URL = "http://127.0.0.1:9093"
SILENCE_CREATED_BY = "anomalia"


@dataclass(frozen=True)
class PlannedSilence:
    silence_id: str
    tenant_id: int
    matchers: list[dict]
    starts_at: str
    ends_at: str


def resolve_alertmanager_url(cursor=None) -> str:
    own_connection = cursor is None
    if own_connection:
        conn = get_db_connection()
        cursor = conn.cursor()
    try:
        cursor.execute(
            """SELECT service_ip, ip_address, port
               FROM infrastructure_nodes
               WHERE component_type = 'alertmanager'
                 AND is_active = TRUE
                 AND status = 'operational'
               LIMIT 1"""
        )
        row = cursor.fetchone()
        if row:
            service_ip, ip_address, port = row
            return f"http://{service_ip or ip_address}:{port or 9093}"
        return ALERTMANAGER_FALLBACK_URL
    finally:
        if own_connection:
            cursor.close()
            conn.close()


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def create_vmalert_silence(
    tenant_id: int,
    job_ref: str,
    ttl_seconds: int = DEFAULT_SILENCE_TTL_SECONDS,
    base_url: Optional[str] = None,
) -> Optional[PlannedSilence]:
    url = base_url or resolve_alertmanager_url()
    starts = datetime.now(timezone.utc)
    ends = starts + timedelta(seconds=max(30, ttl_seconds))
    matchers = [
        {
            "name": "alertname",
            "value": VMALERT_DOWN_ALERT,
            "isRegex": False,
            "isEqual": True,
        },
        {
            "name": "tenant_id",
            "value": str(tenant_id),
            "isRegex": False,
            "isEqual": True,
        },
    ]
    payload = {
        "matchers": matchers,
        "startsAt": _iso_utc(starts),
        "endsAt": _iso_utc(ends),
        "createdBy": SILENCE_CREATED_BY,
        "comment": f"planned vmalert downtime job={job_ref} tenant={tenant_id}",
    }
    try:
        response = httpx.post(f"{url.rstrip('/')}/api/v2/silences", json=payload, timeout=5.0)
        response.raise_for_status()
        body = response.json()
        silence_id = body.get("silenceID") or body.get("silenceId")
        if not silence_id:
            raise ValueError("respuesta de Alertmanager sin silenceID")
    except Exception as exc:
        logger.warning(
            "No se pudo crear silence de mantenimiento para tenant %s (job %s): %s",
            tenant_id,
            job_ref,
            exc,
        )
        return None
    return PlannedSilence(
        silence_id=str(silence_id),
        tenant_id=tenant_id,
        matchers=matchers,
        starts_at=payload["startsAt"],
        ends_at=payload["endsAt"],
    )


def delete_vmalert_silence(
    silence: PlannedSilence,
    base_url: Optional[str] = None,
) -> bool:
    url = base_url or resolve_alertmanager_url()
    try:
        response = httpx.delete(
            f"{url.rstrip('/')}/api/v2/silence/{silence.silence_id}", timeout=5.0
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        logger.warning(
            "No se pudo levantar silence %s del tenant %s: %s",
            silence.silence_id,
            silence.tenant_id,
            exc,
        )
        return False


def vmalert_alert_active(tenant_id: int, base_url: Optional[str] = None) -> bool:
    url = base_url or resolve_alertmanager_url()
    try:
        response = httpx.get(f"{url.rstrip('/')}/api/v2/alerts", timeout=5.0)
        response.raise_for_status()
        for alert in response.json():
            labels = alert.get("labels") or {}
            if labels.get("alertname") != VMALERT_DOWN_ALERT:
                continue
            if str(labels.get("tenant_id") or "").strip() != str(tenant_id):
                continue
            state = ((alert.get("status") or {}).get("state") or "active").lower()
            if state != "resolved":
                return True
    except Exception as exc:
        logger.warning(
            "No se pudo consultar alertas activas de vmalert para tenant %s: %s",
            tenant_id,
            exc,
        )
    return False


def wait_for_vmalert_alert_clear(
    tenant_id: int,
    timeout_seconds: int = 180,
    base_url: Optional[str] = None,
    poll_seconds: float = 5.0,
) -> bool:
    deadline = time.monotonic() + max(0, timeout_seconds)
    while True:
        if not vmalert_alert_active(tenant_id, base_url):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(max(poll_seconds, 0.1), remaining))


def record_job_step(job_id: str, name: str, status: str, detail: dict) -> None:
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT steps FROM job_state WHERE id = %s", (job_id,))
            row = cursor.fetchone()
            steps = row[0] if row and isinstance(row[0], list) else []
            steps.append(
                {
                    "name": name,
                    "status": status,
                    "detail": detail,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            cursor.execute(
                "UPDATE job_state SET steps = %s WHERE id = %s", (Json(steps), job_id)
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    except Exception as exc:
        logger.warning("No se pudo registrar el paso '%s' del job %s: %s", name, job_id, exc)


@contextmanager
def planned_vmalert_silences(
    targets: Iterable[int],
    job_ref: str,
    job_id: Optional[str] = None,
    ttl_seconds: int = DEFAULT_SILENCE_TTL_SECONDS,
    base_url: Optional[str] = None,
    settle_tenant_id: Optional[int] = None,
    settle_timeout_seconds: int = 180,
) -> Iterator[dict[int, PlannedSilence]]:
    silences: dict[int, PlannedSilence] = {}
    try:
        for tenant_id in targets:
            silence = create_vmalert_silence(tenant_id, job_ref, ttl_seconds, base_url)
            if silence is None:
                continue
            silences[tenant_id] = silence
            detail = {
                "tenant_id": tenant_id,
                "silence_id": silence.silence_id,
                "ends_at": silence.ends_at,
                "matchers": silence.matchers,
            }
            if job_id:
                record_job_step(job_id, "Mantenimiento: silence VmalertTenantDown", "running", detail)
        yield silences
    finally:
        for silence in silences.values():
            if settle_tenant_id is not None and silence.tenant_id == settle_tenant_id:
                cleared = wait_for_vmalert_alert_clear(
                    silence.tenant_id, settle_timeout_seconds, base_url
                )
                if not cleared:
                    if job_id:
                        record_job_step(
                            job_id,
                            "Mantenimiento: levantar silence",
                            "pending",
                            {
                                "tenant_id": silence.tenant_id,
                                "silence_id": silence.silence_id,
                                "kept_until_ttl": True,
                            },
                        )
                    continue
            removed = delete_vmalert_silence(silence, base_url)
            detail = {
                "tenant_id": silence.tenant_id,
                "silence_id": silence.silence_id,
                "removed": removed,
            }
            if job_id:
                record_job_step(job_id, "Mantenimiento: levantar silence", "done", detail)
