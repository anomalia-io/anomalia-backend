"""Worker de análisis IA de alertas y su entrega (spec 018).

El webhook de Alertmanager solo persiste la alerta y encola un job
(`alert_analyze` o `alert_resolved`); este worker (ejecutor único, spec 016) hace
el trabajo pesado: llamada al proveedor de IA, parseo del JSON estructurado,
persistencia del diagnóstico y despacho (email a la lista del tenant +
notificación in-app a los usuarios con perfil `access_alerts` y acceso al tenant +
push a sus dispositivos). El job `alert_resolved` solo notifica la regularización
(sin IA).
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json

from ..ai_providers import (
    AI_MAX_ATTEMPTS,
    AI_RETRY_BACKOFF_SECONDS,
    AI_ERROR_INVALID,
    AI_ERROR_QUOTA,
    AIProviderError,
    DEFAULT_AI_PROVIDER,
    DEFAULT_ALERT_SEVERITY,
    build_alert_analysis_prompt,
    get_ai_settings,
    get_provider,
    parse_alert_analysis,
)
from ..ai_status import record_ai_failure, record_ai_success
from ..database import get_db_connection
from ..mailer import (
    EmailNotConfiguredError,
    is_email_configured,
    send_email,
)
from ..notifications import notify_user
from ..push import send_push_to_tenant
from .base import BaseWorker

logger = logging.getLogger("anomalia.workers")

ALERT_ANALYZE_JOB_TYPE = "alert_analyze"
ALERT_RESOLVED_JOB_TYPE = "alert_resolved"
ACCESS_ALERTS_PROFILE = "access_alerts"
# Tipo de notificación in-app a los admins cuando falta configurar el SMTP
# (deduplicada por ventana temporal para no generar spam).
SMTP_MISSING_NOTIFICATION = "smtp_not_configured"
SMTP_MISSING_NOTIFY_WINDOW_HOURS = 12
# Tipo de notificación in-app a los admins cuando el envío de email falla
# (SMTP configurado, pero el servidor no aceptó/el envío expiró). Deduplicada.
EMAIL_FAILED_NOTIFICATION = "smtp_send_failed"
EMAIL_FAILED_NOTIFY_WINDOW_HOURS = 12
# Resultados de envío persistidos en alert_history.email_status (spec 018).
EMAIL_STATUS_SENT = "sent"
EMAIL_STATUS_FAILED = "failed"
EMAIL_STATUS_NOT_CONFIGURED = "not_configured"
EMAIL_STATUS_SKIPPED = "skipped"
# Tipo de notificación in-app a los admins por cambios de estado del proveedor IA
# (cuota agotada / recuperado). Deduplicada por ventana.
AI_STATUS_NOTIFICATION = "ai_status"
AI_STATUS_NOTIFY_WINDOW_HOURS = 1
# Intervalo de polling propio del AlertWorker (spec 018): más corto que el de los
# workers de Ansible para reducir la latencia alerta → análisis.
ALERT_WORKER_INTERVAL_SECONDS = 5
# Alertas analizadas en paralelo por el ejecutor (spec 018). Acotado para no
# saturar al proveedor de IA ni la cuota de tokens.
ALERT_WORKER_MAX_CONCURRENCY = 4


class AlertWorker(BaseWorker):
    """Analiza alertas con IA y las entrega (email + in-app + push)."""

    def __init__(
        self,
        interval: int = ALERT_WORKER_INTERVAL_SECONDS,
        max_concurrency: int = ALERT_WORKER_MAX_CONCURRENCY,
        notifier=None,
    ):
        super().__init__(
            interval=interval,
            job_types=[ALERT_ANALYZE_JOB_TYPE, ALERT_RESOLVED_JOB_TYPE],
            max_concurrency=max_concurrency,
            notifier=notifier,
        )

    async def process_job(self, job_id: str, job_type: str, ref_id: int, payload: dict):
        alert = await asyncio.to_thread(self._load_alert, ref_id)
        if alert is None:
            logger.warning("Alerta %s no encontrada (job %s); se omita", ref_id, job_id)
            return

        if job_type == ALERT_RESOLVED_JOB_TYPE:
            await self._dispatch_resolved(alert)
            return

        settings = await asyncio.to_thread(get_ai_settings)
        language = settings.get("analysis_language") or "en"
        provider_id = settings.get("selected_provider") or DEFAULT_AI_PROVIDER

        analysis, error_kind, error_msg = await self._analyze_with_retry(alert, language)

        await asyncio.to_thread(
            self._persist_analysis, ref_id, analysis, error_msg, error_kind
        )
        if error_kind:
            logger.warning(
                "Análisis IA de la alerta %s incompleto (%s: %s); se entrega con "
                "los datos de Alertmanager",
                ref_id,
                error_kind,
                error_msg,
            )
            await self._handle_ai_failure(provider_id, error_kind, error_msg or "")
        else:
            await self._handle_ai_success(provider_id)

        await self._dispatch(alert, analysis)

    # --- Carga ---

    @staticmethod
    def _load_alert(alert_id: int) -> Optional[Dict[str, Any]]:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT id, tenant_id, fingerprint, alertname, severity, summary,
                          description, labels, annotations, generator_url, status
                   FROM alert_history WHERE id = %s""",
                (alert_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "tenant_id": row[1],
                "fingerprint": row[2],
                "alertname": row[3],
                "severity": row[4],
                "summary": row[5],
                "description": row[6],
                "labels": row[7] or {},
                "annotations": row[8] or {},
                "generator_url": row[9],
                "status": row[10],
            }
        finally:
            cursor.close()
            conn.close()

    # --- Análisis IA ---

    @staticmethod
    def _fallback_analysis(alert: Dict[str, Any]) -> Dict[str, Any]:
        """Diagnóstico de relleno con los datos que envió Alertmanager (spec 018).

        La tarjeta nunca queda vacía: se conserva el resumen/descripción de AM y se
        marca el fallo con ``ai_error``.
        """
        return {
            "summary": (
                alert.get("summary")
                or alert.get("description")
                or alert.get("alertname")
                or ""
            ),
            "root_cause": "",
            "severity": (alert.get("severity") or DEFAULT_ALERT_SEVERITY),
            "checks": [],
        }

    @staticmethod
    def _analyze_once(alert: Dict[str, Any], language: str) -> Dict[str, Any]:
        """Una llamada al proveedor IA. Lanza ``AIProviderError`` si falla."""
        prompt = build_alert_analysis_prompt(
            {"labels": alert.get("labels", {}), "annotations": alert.get("annotations", {})},
            language,
        )
        provider = get_provider()
        raw = provider.generate(prompt)
        return parse_alert_analysis(raw)

    async def _analyze_with_retry(
        self, alert: Dict[str, Any], language: str
    ) -> tuple[Dict[str, Any], Optional[str], Optional[str]]:
        """Analiza con reintentos inline acotados (spec 018).

        Reintenta solo errores transitorios (timeout, conexión, 5xx, 429 temporal).
        ``auth``/``invalid``/``quota`` son definitivos: no se reintentan. Ante
        agotamiento devuelve el fallback con los datos de Alertmanager y el
        ``kind`` del error.
        """
        fallback = self._fallback_analysis(alert)
        last_kind: Optional[str] = None
        last_msg: Optional[str] = None
        for attempt in range(1, AI_MAX_ATTEMPTS + 1):
            try:
                analysis = await asyncio.to_thread(self._analyze_once, alert, language)
                return analysis, None, None
            except AIProviderError as exc:
                last_kind, last_msg = exc.kind, str(exc)
                if not exc.retryable or attempt >= AI_MAX_ATTEMPTS:
                    break
                delay = AI_RETRY_BACKOFF_SECONDS[attempt - 1]
                logger.info(
                    "Reintento IA %d/%d en %ss (%s): %s",
                    attempt,
                    AI_MAX_ATTEMPTS,
                    delay,
                    exc.kind,
                    exc,
                )
                await asyncio.sleep(delay)
            except Exception as exc:  # noqa: BLE001
                logger.error("Fallo inesperado analizando la alerta: %s", exc, exc_info=True)
                last_kind, last_msg = AI_ERROR_INVALID, str(exc)
                break
        return fallback, last_kind, last_msg

    @staticmethod
    def _persist_analysis(
        alert_id: int,
        analysis: Dict[str, Any],
        error: Optional[str],
        error_kind: Optional[str] = None,
    ) -> None:
        """Persiste el diagnóstico. Nunca pisa una severidad fijada a mano
        (severity_source='manual'), que es la decisión del operador (spec 018)."""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """UPDATE alert_history
                      SET ai_summary = %s,
                          ai_root_cause = %s,
                          ai_checks = %s,
                          severity = CASE WHEN severity_source = 'manual'
                                          THEN severity ELSE %s END,
                          severity_source = CASE WHEN severity_source = 'manual'
                                                 THEN 'manual' ELSE 'ai' END,
                          ai_error = %s,
                          updated_at = NOW()
                    WHERE id = %s""",
                (
                    analysis.get("summary") or "",
                    analysis.get("root_cause") or "",
                    Json(analysis.get("checks") or []),
                    analysis.get("severity") or DEFAULT_ALERT_SEVERITY,
                    error_kind,
                    alert_id,
                ),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    # --- Entrega ---

    async def _dispatch(self, alert: Dict[str, Any], analysis: Dict[str, Any]) -> None:
        tenant_id = alert.get("tenant_id")
        severity = (analysis.get("severity") or DEFAULT_ALERT_SEVERITY).upper()
        title = f"[{severity}] {alert.get('alertname') or 'Alerta'}"
        body = self._build_body(alert, analysis)

        recipients = await asyncio.to_thread(self._load_recipients, tenant_id)
        if recipients and not await asyncio.to_thread(is_email_configured):
            # Falla de configuración (no de envío): se avisa una sola vez y no se
            # intenta enviar. El resto de canales (in-app/push) siguen.
            logger.warning(
                "SMTP sin configurar: %d destinatario(s) del tenant %s no recibirán email.",
                len(recipients),
                tenant_id,
            )
            await self._notify_admins_smtp_missing()
            email_status = EMAIL_STATUS_NOT_CONFIGURED
            email_error = "smtp_no_configured"
            recipients = []
        elif not recipients:
            email_status = EMAIL_STATUS_SKIPPED
            email_error = None
        else:
            email_status = EMAIL_STATUS_SENT
            email_error = None

        for email in recipients:
            try:
                await send_email(email, title, body)
            except EmailNotConfiguredError as exc:
                logger.warning("SMTP sin configurar: %s", exc)
                email_status = EMAIL_STATUS_NOT_CONFIGURED
                email_error = "smtp_no_configured"
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("Email de alerta no enviado a %s: %s", email, exc)
                email_status = EMAIL_STATUS_FAILED
                email_error = f"{type(exc).__name__}: {exc}"[:200]
                await self._notify_admins_email_failed(tenant_id, email, str(exc))

        users = await asyncio.to_thread(self._load_alert_users, tenant_id)
        link = "/?tab=alerts"
        for user_id in users:
            try:
                await notify_user(
                    user_id,
                    "alert",
                    title,
                    body,
                    link=link,
                    send_email_notice=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("Notificación in-app no enviada al user %s: %s", user_id, exc)

        try:
            await asyncio.to_thread(
                send_push_to_tenant,
                tenant_id,
                title,
                (analysis.get("summary") or alert.get("alertname") or "")[:200],
                {"alert_id": str(alert.get("id")), "severity": analysis.get("severity", "")},
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("Push de alerta no enviado: %s", exc)

        await asyncio.to_thread(
            self._mark_notified, alert.get("id"), email_status, email_error
        )

    @staticmethod
    def _build_body(alert: Dict[str, Any], analysis: Dict[str, Any]) -> str:
        lines: List[str] = []
        if analysis.get("summary"):
            lines.append(str(analysis["summary"]))
        if analysis.get("root_cause"):
            lines.append(f"\nCausa raíz: {analysis['root_cause']}")
        checks = analysis.get("checks") or []
        if checks:
            lines.append("\nQué revisar:")
            lines.extend(f"  {i}. {c}" for i, c in enumerate(checks, start=1))
        if alert.get("generator_url"):
            lines.append(f"\nFuente: {alert['generator_url']}")
        return "\n".join(lines) or (alert.get("summary") or "Alerta sin detalle.")

    @staticmethod
    def _dispatch_resolved(alert: Dict[str, Any]) -> None:
        """Notifica la regularización de la alerta sin IA (spec 018)."""
        tenant_id = alert.get("tenant_id")
        title = f"[RESUELTA] {alert.get('alertname') or 'Alerta'}"
        body = "La alerta se ha regularizado."
        link = "/?tab=alerts"
        users = AlertWorker._load_alert_users(tenant_id)
        for user_id in users:
            try:
                notify_user(
                    user_id,
                    "alert_resolved",
                    title,
                    body,
                    link=link,
                    send_email_notice=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "Notificación de regularización no enviada al user %s: %s",
                    user_id,
                    exc,
                )

        try:
            send_push_to_tenant(
                tenant_id,
                title,
                body,
                {"alert_id": str(alert.get("id")), "resolved": "true"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("Push de regularización no enviado: %s", exc)

    async def _notify_admins_smtp_missing(self) -> None:
        """Avisa a los admins (una vez por ventana) que falta configurar el SMTP."""
        try:
            admin_ids = await asyncio.to_thread(self._load_admin_user_ids)
        except Exception as exc:  # noqa: BLE001
            logger.info("No se pudieron cargar los admins para avisar de SMTP: %s", exc)
            return
        for user_id in admin_ids:
            try:
                already = await asyncio.to_thread(
                    self._recent_notification_exists,
                    user_id,
                    SMTP_MISSING_NOTIFICATION,
                    SMTP_MISSING_NOTIFY_WINDOW_HOURS,
                )
                if already:
                    continue
                await notify_user(
                    user_id,
                    SMTP_MISSING_NOTIFICATION,
                    "SMTP sin configurar",
                    "Las alertas no pueden enviarse por email a los destinatarios del "
                    "tenant porque falta configurar el servidor SMTP. Configúralo en "
                    "Ajustes del sistema.",
                    link="/?tab=bkp",
                    send_email_notice=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("No se pudo avisar de SMTP al admin %s: %s", user_id, exc)

    async def _notify_admins_email_failed(
        self, tenant_id: Optional[int], recipient: str, error: str
    ) -> None:
        """Avisa a los admins (una vez por ventana) que un envío de email falló."""
        try:
            admin_ids = await asyncio.to_thread(self._load_admin_user_ids)
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "No se pudieron cargar los admins para avisar del fallo SMTP: %s", exc
            )
            return
        for user_id in admin_ids:
            try:
                already = await asyncio.to_thread(
                    self._recent_notification_exists,
                    user_id,
                    EMAIL_FAILED_NOTIFICATION,
                    EMAIL_FAILED_NOTIFY_WINDOW_HOURS,
                )
                if already:
                    continue
                await notify_user(
                    user_id,
                    EMAIL_FAILED_NOTIFICATION,
                    "Fallo al enviar email de alerta",
                    f"No se pudo enviar el aviso de alerta del tenant "
                    f"{tenant_id or '-'} a {recipient}: {error[:150]}. Revisá la "
                    "configuración SMTP en Ajustes del sistema.",
                    link="/?tab=bkp",
                    send_email_notice=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "No se pudo avisar del fallo SMTP al admin %s: %s", user_id, exc
                )

    async def _notify_admins_ai(self, title: str, body: str) -> None:
        """Avisa a los admins del cambio de estado del proveedor IA (deduplicado)."""
        try:
            admin_ids = await asyncio.to_thread(self._load_admin_user_ids)
        except Exception as exc:  # noqa: BLE001
            logger.info("No se pudieron cargar los admins para avisar de IA: %s", exc)
            return
        for user_id in admin_ids:
            try:
                already = await asyncio.to_thread(
                    self._recent_notification_exists,
                    user_id,
                    AI_STATUS_NOTIFICATION,
                    AI_STATUS_NOTIFY_WINDOW_HOURS,
                )
                if already:
                    continue
                await notify_user(
                    user_id,
                    AI_STATUS_NOTIFICATION,
                    title,
                    body,
                    link="/?tab=bkp",
                    send_email_notice=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("No se pudo avisar de IA al admin %s: %s", user_id, exc)

    async def _handle_ai_failure(self, provider_id: str, kind: str, message: str) -> None:
        """Registra el fallo y notifica a admins solo si es agotamiento de cuota."""
        try:
            result = await asyncio.to_thread(record_ai_failure, provider_id, kind, message)
        except Exception as exc:  # noqa: BLE001
            logger.info("No se pudo registrar el estado del proveedor IA: %s", exc)
            return
        if not result.get("transitioned"):
            return
        if kind == AI_ERROR_QUOTA:
            await self._notify_admins_ai(
                "Cuota de IA agotada",
                f"El proveedor de IA '{provider_id}' agotó su cuota de tokens. Las "
                "alertas se seguirán mostrando con los datos de Alertmanager, pero sin "
                "análisis IA. Repón la cuota o cambia de proveedor en Configuración de IA.",
            )

    async def _handle_ai_success(self, provider_id: str) -> None:
        """Registra el éxito y avisa a los admins si hubo recuperación."""
        try:
            recovered = await asyncio.to_thread(record_ai_success, provider_id)
        except Exception as exc:  # noqa: BLE001
            logger.info("No se pudo registrar el estado del proveedor IA: %s", exc)
            return
        if recovered:
            await self._notify_admins_ai(
                "IA recuperada",
                f"El proveedor de IA '{provider_id}' vuelve a responder normalmente.",
            )

    @staticmethod
    def _load_admin_user_ids() -> List[int]:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT DISTINCT u.id FROM users u
                   JOIN user_roles ur ON ur.user_id = u.id
                   JOIN role_profiles rp ON rp.role_id = ur.role_id
                   JOIN profiles p ON p.id = rp.profile_id
                   WHERE p.code = 'admin' AND u.is_active = TRUE"""
            )
            return [r[0] for r in cursor.fetchall()]
        finally:
            cursor.close()
            conn.close()

    @staticmethod
    def _recent_notification_exists(user_id: int, ntype: str, hours: int) -> bool:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT 1 FROM notifications
                   WHERE user_id = %s AND type = %s
                     AND created_at > NOW() - make_interval(hours => %s)
                   LIMIT 1""",
                (user_id, ntype, hours),
            )
            return cursor.fetchone() is not None
        finally:
            cursor.close()
            conn.close()

    @staticmethod
    def _load_recipients(tenant_id: Optional[int]) -> List[str]:
        if tenant_id is None:
            return []
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT email FROM tenant_alert_recipients "
                "WHERE tenant_id = %s AND enabled = TRUE ORDER BY id",
                (tenant_id,),
            )
            return [r[0] for r in cursor.fetchall() if r[0]]
        finally:
            cursor.close()
            conn.close()

    @staticmethod
    def _load_alert_users(tenant_id: Optional[int]) -> List[int]:
        """Usuarios activos con perfil access_alerts y acceso al tenant."""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            if tenant_id is None:
                cursor.execute(
                    """SELECT DISTINCT u.id FROM users u
                       JOIN user_roles ur ON ur.user_id = u.id
                       JOIN role_profiles rp ON rp.role_id = ur.role_id
                       JOIN profiles p ON p.id = rp.profile_id
                       WHERE p.code = %s AND u.is_active = TRUE""",
                    (ACCESS_ALERTS_PROFILE,),
                )
            else:
                cursor.execute(
                    """SELECT DISTINCT u.id FROM users u
                       JOIN user_roles ur ON ur.user_id = u.id
                       JOIN role_profiles rp ON rp.role_id = ur.role_id
                       JOIN profiles p ON p.id = rp.profile_id
                       WHERE p.code = %s AND u.is_active = TRUE
                         AND u.id IN (
                             SELECT ut.user_id FROM user_tenants ut WHERE ut.tenant_id = %s
                             UNION
                             SELECT ur2.user_id FROM role_tenants rt
                               JOIN user_roles ur2 ON ur2.role_id = rt.role_id
                              WHERE rt.tenant_id = %s
                         )""",
                    (ACCESS_ALERTS_PROFILE, tenant_id, tenant_id),
                )
            return [r[0] for r in cursor.fetchall()]
        finally:
            cursor.close()
            conn.close()

    @staticmethod
    def _mark_notified(
        alert_id: Optional[int],
        email_status: Optional[str] = None,
        email_error: Optional[str] = None,
    ) -> None:
        if alert_id is None:
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """UPDATE alert_history
                      SET notified_at = NOW(),
                          email_status = COALESCE(%s, email_status),
                          email_error = %s
                    WHERE id = %s""",
                (email_status, email_error, alert_id),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
