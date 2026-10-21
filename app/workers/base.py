"""Base worker class for background job processing."""
import asyncio
import json
import logging
import uuid
from abc import ABC, abstractmethod
from typing import Optional

from psycopg2.extras import Json

from ..database import get_db_connection
from ..notifications import notify_job_outcome

logger = logging.getLogger("anomalia.workers")

# Canal LISTEN/NOTIFY de encolado de jobs (spec 018). Ver workers/notifier.py.
JOBS_CHANNEL = "anomalia_jobs"

# Cadencia del heartbeat de un job en ejecución y umbral para considerar
# huérfano (proceso muerto) un job 'running' con heartbeat viejo (spec 016 §2.4).
HEARTBEAT_SECONDS = 15
ORPHAN_TIMEOUT_SECONDS = 120


def enqueue_job_tx(
    cursor,
    job_type: str,
    ref_id: int,
    payload: Optional[dict] = None,
    created_by: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
) -> str:
    """Encola un job nuevo en ``job_state`` usando un cursor ya abierto.

    Helper compartido (spec 011 §4.11): lo usan los workers (fases en thread) y
    la reconciliación post-import del router de sistema. ``payload`` viaja como
    extra-vars de entrada del job y se serializa a JSONB. ``created_by`` enlaza el
    job con su dueño para notificar el desenlace (spec 017); ``timeout_seconds``
    fija ``timeout_at`` (spec 016 §2.4).
    """
    job_id = str(uuid.uuid4())
    if timeout_seconds is not None:
        cursor.execute(
            """INSERT INTO job_state
                   (id, type, ref_id, status, phase, progress_pct, created_by, payload, timeout_at)
               VALUES (%s, %s, %s, 'queued', 'init', 0, %s, %s, NOW() + make_interval(secs => %s))""",
            (job_id, job_type, ref_id, created_by, Json(payload) if payload else None, timeout_seconds),
        )
    else:
        cursor.execute(
            """INSERT INTO job_state (id, type, ref_id, status, phase, progress_pct, created_by, payload)
               VALUES (%s, %s, %s, 'queued', 'init', 0, %s, %s)""",
            (job_id, job_type, ref_id, created_by, Json(payload) if payload else None),
        )
    # Despierta a los workers vía LISTEN/NOTIFY. Dentro de la transacción, Postgres
    # entrega el aviso solo al commit (no notifica jobs que terminan en rollback).
    cursor.execute(f"NOTIFY {JOBS_CHANNEL}")
    return job_id


def enqueue_job(
    job_type: str,
    ref_id: int,
    payload: Optional[dict] = None,
    created_by: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
) -> str:
    """Variante autónoma de :func:`enqueue_job_tx` con conexión corta + commit."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        job_id = enqueue_job_tx(cursor, job_type, ref_id, payload, created_by, timeout_seconds)
        conn.commit()
        return job_id
    finally:
        cursor.close()
        conn.close()


class BaseWorker(ABC):
    """Base class for background workers that poll job_state table."""

    def __init__(self, interval: int = 30, job_types: list[str] = None,
                 max_concurrency: int = 1, notifier=None):
        self.interval = interval
        self.job_types = job_types or []
        self.max_concurrency = max(1, max_concurrency)
        self.notifier = notifier
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the worker polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Worker %s started (interval=%ss, types=%s)",
                    self.__class__.__name__, self.interval, self.job_types)

    async def recover_orphans(self) -> None:
        """Recupera jobs 'running' huérfanos de un proceso muerto (spec 016 §2.4).

        Lo invoca el supervisor al convertirse en ejecutor, antes de arrancar los
        workers. La subclase puede sobreescribir ``_handle_orphan`` para revertir
        efectos (p. ej. el rollback del import).
        """
        await asyncio.to_thread(self._recover_orphans_sync)

    def _recover_orphans_sync(self) -> None:
        orphans = self._find_orphans()
        for job_id, job_type, ref_id, payload in orphans:
            try:
                self._handle_orphan(job_id, job_type, ref_id, payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Recuperación del job huérfano %s falló: %s", job_id, exc, exc_info=True)
            self._mark_orphan_failed(job_id)
            logger.warning("Job huérfano %s (%s) marcado como failed/orphaned", job_id, job_type)

    def _find_orphans(self) -> list:
        if not self.job_types:
            return []
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            placeholders = ','.join(['%s'] * len(self.job_types))
            cursor.execute(
                """SELECT id, type, ref_id, payload
                   FROM job_state
                   WHERE type IN (""" + placeholders + """)
                     AND status = 'running'
                     AND (heartbeat_at IS NULL
                          OR heartbeat_at < NOW() - make_interval(secs => %s))""",
                tuple(self.job_types) + (ORPHAN_TIMEOUT_SECONDS,),
            )
            rows = cursor.fetchall()
            return [(r[0], r[1], r[2], self._parse_payload(r[3])) for r in rows]
        finally:
            cursor.close()
            conn.close()

    def _handle_orphan(self, job_id: str, job_type: str, ref_id: Optional[int], payload: dict) -> None:
        """Hook de recuperación (por defecto no-op; lo usa TenantWorker)."""

    @staticmethod
    def _mark_orphan_failed(job_id: str) -> None:
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """UPDATE job_state
                       SET status='failed', finished_at=NOW(), phase='error', error_code='orphaned'
                       WHERE id=%s AND status='running'""",
                    (job_id,),
                )
            conn.commit()
        finally:
            conn.close()

    async def stop(self):
        """Stop the worker."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Worker %s stopped", self.__class__.__name__)

    async def _run_loop(self):
        """Main polling loop."""
        while self._running:
            try:
                await self._process_pending_jobs()
            except Exception as e:
                logger.error("Error in worker %s: %s", self.__class__.__name__, e, exc_info=True)
            if self.notifier is not None:
                # Despierta al encolarse un job (LISTEN/NOTIFY); el timeout del
                # intervalo queda como red de seguridad si el aviso se pierde.
                await self.notifier.wait(timeout=self.interval)
            else:
                await asyncio.sleep(self.interval)

    async def _process_pending_jobs(self):
        """Fetch and process pending jobs (con concurrencia acotada opcional)."""
        jobs = await asyncio.to_thread(self._claim_jobs)
        if not jobs:
            return
        if self.max_concurrency <= 1:
            for job_id, job_type, ref_id, payload, created_by in jobs:
                await self._execute_job(job_id, job_type, ref_id, payload or {}, created_by)
            return
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def _run(job):
            async with semaphore:
                await self._execute_job(job[0], job[1], job[2], job[3] or {}, job[4])

        await asyncio.gather(*(_run(job) for job in jobs))

    def _claim_jobs(self) -> list:
        """Reclama jobs queued en una transaccion corta (spec 011 4.2).

        Corre en thread (asyncio.to_thread): una espera de lock aqui no debe
        congelar el event loop de uvicorn.
        """
        if not self.job_types:
            return []
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            placeholders = ','.join(['%s'] * len(self.job_types))
            cursor.execute(
                """
                SELECT id, type, ref_id, payload, created_by
                FROM job_state
                WHERE type IN (""" + placeholders + """)
                  AND status = 'queued'
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 10
                """,
                tuple(self.job_types)
            )
            jobs = cursor.fetchall()
            if jobs:
                cursor.execute(
                    """UPDATE job_state
                       SET status='running', started_at=NOW(), heartbeat_at=NOW(), phase='processing', progress_pct=10
                       WHERE id = ANY(%s)""",
                    ([j[0] for j in jobs],)
                )
                conn.commit()
            return [
                (j[0], j[1], j[2], self._parse_payload(j[3]), j[4]) for j in jobs
            ]
        finally:
            cursor.close()
            conn.close()

    @staticmethod
    def _parse_payload(raw) -> dict:
        """Normaliza job_state.payload (JSONB) a dict (spec 011 §4.11).

        psycopg2 puede entregar JSONB como dict ya decodificado o como str.
        """
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}


    async def _execute_job(self, job_id: str, job_type: str, ref_id: int, payload: dict,
                           created_by: Optional[int] = None):
        """Execute a single job with status updates, heartbeat y notificación."""
        await asyncio.to_thread(self._set_job_running, job_id)
        heartbeat = asyncio.create_task(self._heartbeat_loop(job_id))
        status = "succeeded"
        error: Optional[Exception] = None
        try:
            await self.process_job(job_id, job_type, ref_id, payload)
            await asyncio.to_thread(self._set_job_succeeded, job_id)
            logger.info("Job %s (%s) completed for ref_id=%s", job_id, job_type, ref_id)
        except Exception as e:
            status = "failed"
            error = e
            logger.error("Job %s (%s) failed for ref_id=%s: %s", job_id, job_type, ref_id, e, exc_info=True)
            try:
                await asyncio.to_thread(self._set_job_failed, job_id, e)
            except Exception:
                pass
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

        if created_by is not None:
            try:
                await notify_job_outcome(
                    created_by, job_id, job_type, ref_id, status,
                    type(error).__name__[:50] if error else None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo notificar el job %s al user %s: %s", job_id, created_by, exc)

    async def _heartbeat_loop(self, job_id: str) -> None:
        """Mantiene ``heartbeat_at`` fresco mientras el job corre (spec 016 §2.4)."""
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                await asyncio.to_thread(self._touch_heartbeat, job_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Heartbeat del job %s falló: %s", job_id, exc)

    @staticmethod
    def _touch_heartbeat(job_id: str) -> None:
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE job_state SET heartbeat_at=NOW() WHERE id=%s", (job_id,))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _set_job_running(job_id: str) -> None:
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE job_state SET status='running', started_at=NOW(), heartbeat_at=NOW(), phase='processing', progress_pct=10 WHERE id=%s",
                    (job_id,)
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _set_job_succeeded(job_id: str) -> None:
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE job_state SET status='succeeded', finished_at=NOW(), phase='done', progress_pct=100 WHERE id=%s",
                    (job_id,)
                )
            conn.commit()
        finally:
            conn.close()

    # job_state.logs_ref es TEXT (spec 011 §4.11); se acota a 8000 para no
    # almacenar un stdout de playbook desmedido, pero sin truncar el diagnóstico.
    LOGS_REF_MAX = 8000

    @staticmethod
    def _set_job_failed(job_id: str, error: Exception) -> None:
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE job_state SET status='failed', finished_at=NOW(), phase='error', error_code=%s, logs_ref=%s WHERE id=%s",
                    (type(error).__name__[:50], str(error)[:BaseWorker.LOGS_REF_MAX], job_id)
                )
            conn.commit()
        finally:
            conn.close()

    @abstractmethod
    async def process_job(self, job_id: str, job_type: str, ref_id: int, payload: dict):
        """Process a specific job type. Must be implemented by subclass."""
        pass