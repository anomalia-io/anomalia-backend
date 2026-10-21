"""Webhook endpoints for Alertmanager and external integrations."""
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from datetime import datetime
import logging

from psycopg2.extras import Json

from ..auth import verify_webhook_token, verify_admin_token
from ..database import get_db_connection
from ..workers.base import enqueue_job_tx

router = APIRouter(prefix="/api/v1/webhook", tags=["Webhooks"])

logger = logging.getLogger(__name__)

# spec 018: el worker `alert_analyze` hace IA + despacho (email/push/in-app).
ALERT_ANALYZE_JOB_TYPE = "alert_analyze"
# Budget del análisis IA (una llamada al proveedor). El worker es el único que lo
# ejecuta; el webhook solo persiste y encola para no bloquear a Alertmanager.
ALERT_ANALYZE_TIMEOUT_SECONDS = 600
# spec 018: notificación ligera de regularización (sin IA). Se encola solo cuando
# el UPDATE de cierre afecta exactamente una fila activa (idempotente ante
# resoluciones repetidas de AM).
ALERT_RESOLVED_JOB_TYPE = "alert_resolved"
ALERT_RESOLVED_TIMEOUT_SECONDS = 300


class AlertmanagerAlert(BaseModel):
    status: str
    labels: Dict[str, str]
    annotations: Dict[str, str]
    startsAt: str
    endsAt: str
    generatorURL: str
    fingerprint: str


class AlertmanagerPayload(BaseModel):
    version: str
    groupKey: str
    status: str
    receiver: str
    groupLabels: Dict[str, str]
    commonLabels: Dict[str, str]
    commonAnnotations: Dict[str, str]
    externalURL: str
    alerts: List[Dict[str, Any]]


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Convierte un timestamp ISO de Alertmanager a datetime (o None)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _resolve_tenant_id(cursor, labels: Dict[str, str]) -> Optional[int]:
    """Resuelve el tenant desde el label externo de vmalert (spec 018).

    Cada vmalert per-tenant inyecta `-external.label=tenant_slug=<slug>`; como
    fallback se acepta el `org_id` (X-Scope-OrgID) en mayúsculas.
    """
    slug = (labels.get("tenant_slug") or "").strip()
    if slug:
        cursor.execute(
            "SELECT id FROM tenants WHERE slug = %s AND deleted_at IS NULL LIMIT 1",
            (slug,),
        )
        row = cursor.fetchone()
        if row:
            return row[0]
    org = (labels.get("org_id") or labels.get("org_id_upper") or "").strip()
    if org:
        cursor.execute(
            "SELECT id FROM tenants WHERE UPPER(COALESCE(org_id_upper, '')) = UPPER(%s) "
            "AND deleted_at IS NULL LIMIT 1",
            (org,),
        )
        row = cursor.fetchone()
        if row:
            return row[0]
    return None


def _persist_alerts(alerts: List[Dict[str, Any]], status: str) -> List[Dict[str, Any]]:
    """Persiste cada alerta y encola su análisis IA o su notificación de
    resolución. Transacción corta; se ejecuta vía asyncio.to_thread para no
    congelar el event loop (spec 003 §1, spec 011 §4.2, spec 018).

    - ``firing``: upsert ``ON CONFLICT (tenant_id, fingerprint) WHERE status='firing'``
      (índice parcial): mantiene **una sola fila activa** por evento. Si la fila
      resuelta previa no colisiona con el índice, el re-disparo abre un **nuevo**
      episodio (nueva fila).
    - ``resolved``: cierra la fila activa (UPDATE a ``resolved``). Nunca inserta
      una fila nueva: de ahí que la tarjeta se cierre en vez de duplicarse.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        processed: List[Dict[str, Any]] = []
        for alert in alerts:
            labels = alert.get("labels", {}) or {}
            annotations = alert.get("annotations", {}) or {}
            fingerprint = (alert.get("fingerprint") or "").strip() or None
            tenant_id = _resolve_tenant_id(cursor, labels)
            alertname = labels.get("alertname", "unknown") or "unknown"
            severity_label = (labels.get("severity") or "").strip() or None
            summary = annotations.get("summary", "") or ""
            description = annotations.get("description", "") or ""
            generator_url = alert.get("generatorURL", "") or ""
            starts_at = _parse_ts(alert.get("startsAt"))
            ends_at = _parse_ts(alert.get("endsAt"))

            alert_id: Optional[int] = None
            queued: Optional[str] = None

            if status == "firing":
                cursor.execute(
                    """INSERT INTO alert_history
                           (tenant_id, fingerprint, alertname, severity, severity_source,
                            summary, description, labels, annotations, generator_url,
                            status, starts_at, ends_at, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, 'label', %s, %s, %s, %s, %s,
                               %s, %s, %s, NOW(), NOW())
                       ON CONFLICT (tenant_id, fingerprint) WHERE status = 'firing'
                       DO UPDATE SET
                           alertname = EXCLUDED.alertname,
                           severity = CASE WHEN alert_history.severity_source = 'manual'
                                           THEN alert_history.severity
                                           ELSE EXCLUDED.severity END,
                           severity_source = CASE WHEN alert_history.severity_source = 'manual'
                                                   THEN 'manual' ELSE 'label' END,
                           summary = EXCLUDED.summary,
                           description = EXCLUDED.description,
                           labels = EXCLUDED.labels,
                           annotations = EXCLUDED.annotations,
                           generator_url = EXCLUDED.generator_url,
                           starts_at = EXCLUDED.starts_at,
                           ends_at = EXCLUDED.ends_at,
                           updated_at = NOW()
                       RETURNING id, (xmax = 0) AS inserted, ai_summary""",
                    (
                        tenant_id, fingerprint, alertname, severity_label,
                        summary, description, Json(labels), Json(annotations),
                        generator_url, status, starts_at, ends_at,
                    ),
                )
                row = cursor.fetchone()
                alert_id, inserted, existing_summary = row[0], row[1], row[2]

                # Solo las alertas en firing nuevas (o cuyo análisis previo no
                # completó) se envían a la IA. Un re-disparo tras resolver inserta
                # una fila nueva y vuelve a analizarse.
                if inserted or existing_summary is None:
                    queued = ALERT_ANALYZE_JOB_TYPE
                    enqueue_job_tx(
                        cursor,
                        ALERT_ANALYZE_JOB_TYPE,
                        alert_id,
                        payload={"tenant_id": tenant_id},
                        timeout_seconds=ALERT_ANALYZE_TIMEOUT_SECONDS,
                    )
            else:
                # resolved: cerrar la fila activa, no duplicar.
                cursor.execute(
                    """UPDATE alert_history
                          SET status = 'resolved',
                              ends_at = %s,
                              updated_at = NOW()
                        WHERE tenant_id = %s AND fingerprint = %s
                          AND status = 'firing'""",
                    (ends_at, tenant_id, fingerprint),
                )
                if cursor.rowcount == 1:
                    cursor.execute(
                        """SELECT id FROM alert_history
                            WHERE tenant_id = %s AND fingerprint = %s
                              AND status = 'resolved'
                            ORDER BY updated_at DESC LIMIT 1""",
                        (tenant_id, fingerprint),
                    )
                    alert_id = cursor.fetchone()[0]
                    queued = ALERT_RESOLVED_JOB_TYPE
                    enqueue_job_tx(
                        cursor,
                        ALERT_RESOLVED_JOB_TYPE,
                        alert_id,
                        payload={"tenant_id": tenant_id},
                        timeout_seconds=ALERT_RESOLVED_TIMEOUT_SECONDS,
                    )

            processed.append({
                "fingerprint": fingerprint,
                "status": status,
                "alert_id": alert_id,
                "tenant_id": tenant_id,
                "queued": queued,
            })

        conn.commit()
        return processed
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


@router.post("/alertmanager")
async def alertmanager_webhook(
    payload: AlertmanagerPayload,
    _: None = Depends(verify_webhook_token),
):
    """Recibe alertas de Alertmanager, las persiste y encola su análisis con IA.

    Autenticación por token compartido (spec 018): Alertmanager no puede enviar
    un JWT de usuario. El enriquecimiento con IA corre en background (worker
    `alert_analyze`) para responder a AM sin bloquear su notificador.
    """
    try:
        processed = await asyncio.to_thread(_persist_alerts, payload.alerts, payload.status)
        return {
            "status": "received",
            "alerts_processed": len(processed),
            "queued": sum(1 for p in processed if p.get("queued")),
            "alerts": processed,
        }
    except Exception as e:
        logger.error(f"Alertmanager webhook error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/alertmanager/test")
def test_alertmanager_webhook(admin_payload: dict = Depends(verify_admin_token)):
    """Endpoint de prueba para verificar conectividad."""
    return {"status": "ok", "message": "Alertmanager webhook endpoint is reachable"}
