"""Ejecutor unico de jobs en background (spec 016).

El gateway se despliega en dos contenedores (`anomalia_gw` en :8000 y
`anomalia_gw_tls` en :443) que ejecutan la **misma** app FastAPI. Sin
coordinacion, ambos arrancarian `TenantWorker` y `VmalertWorker`, reclamarian
jobs del mismo `job_state` (`FOR UPDATE SKIP LOCKED`) y correrian
`ansible-playbook` en paralelo contra el mismo `tmp_dir` compartido, pisandose
(p. ej. el `Cleanup` de una corrida borra el directorio mientras la otra copia
los binarios).

`WorkerSupervisor` serializa la ejecucion con un advisory lock de PostgreSQL:
solo el poseedor del lock arranca los workers. El lock es de sesion, ligado a
una conexion dedicada; un watchdog verifica su vitalidad y, si la conexion cae,
intenta readquirir el lock (failover) o cede el turno si otro proceso lo tomo.
"""
import asyncio
import logging
from typing import Optional

from ..database import get_db_connection
from .base import BaseWorker
from .notifier import JobNotifier

logger = logging.getLogger("anomalia.workers")

# Clave arbitraria pero constante que identifica el lock global del ejecutor.
# Debe ser DISTINTA de `database._INIT_DB_ADVISORY_LOCK` ('ANOM'): el ejecutor
# retiene su lock toda la vida del proceso, de modo que reusar la clave de
# `init_db` bloquearia su `pg_advisory_lock` en el otro contenedor y abortaria el
# arranque (crash-loop del gateway TLS).
DEFAULT_LOCK_KEY = 0x414E4F53  # "ANOS" (executor lock, no colisionar con 'ANOM')

# Cada cuanto se revisa la vitalidad del lock / se reintenta si aun no es nuestro.
SUPERVISOR_CHECK_SECONDS = 15


class WorkerSupervisor:
    """Posee los workers y garantiza que solo un proceso los ejecute."""

    def __init__(
        self,
        interval: int = 30,
        lock_key: int = DEFAULT_LOCK_KEY,
        workers: Optional[list[BaseWorker]] = None,
    ):
        self.interval = interval
        self.lock_key = lock_key
        self._notifier = JobNotifier()
        if workers is None:
            from .tenant_worker import TenantWorker
            from .vmalert_worker import VmalertWorker
            from .alerts_worker import AlertWorker

            workers = [
                TenantWorker(interval=interval, notifier=self._notifier),
                VmalertWorker(interval=interval, notifier=self._notifier),
                AlertWorker(notifier=self._notifier),
            ]
        self._workers = workers
        self._lock_conn = None
        self._is_owner = False
        self._workers_started = False
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._recovery_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Arranca el bucle supervisor (no garantiza aun ser el ejecutor)."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._supervise_loop())
        logger.info(
            "WorkerSupervisor started (lock_key=%s, workers=%s)",
            self.lock_key,
            [w.__class__.__name__ for w in self._workers],
        )

    async def stop(self) -> None:
        """Detiene los workers (si se poseen) y libera el advisory lock."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._stop_workers()
        self._release_lock()

    @property
    def is_owner(self) -> bool:
        return self._is_owner

    async def _supervise_loop(self) -> None:
        while self._running:
            try:
                await self._ensure_ownership()
            except Exception as exc:  # noqa: BLE001
                logger.error("WorkerSupervisor error: %s", exc, exc_info=True)
            await asyncio.sleep(SUPERVISOR_CHECK_SECONDS)

    async def _ensure_ownership(self) -> None:
        # Si teniamos el lock pero la conexion murio, el servidor ya lo libero.
        if self._lock_conn is not None and not await asyncio.to_thread(self._check_lock_alive):
            logger.warning("WorkerSupervisor: conexion del advisory lock perdida; re-adquiriendo.")
            self._release_lock()

        if not self._is_owner:
            await asyncio.to_thread(self._try_acquire_lock)

        if self._is_owner and not self._workers_started:
            await self._start_workers()
        elif not self._is_owner and self._workers_started:
            # Otro proceso tomo el relevo: cedemos los workers.
            logger.warning("WorkerSupervisor: se perdio la propiedad; deteniendo workers")
            await self._stop_workers()

    async def _start_workers(self) -> None:
        self._notifier.start(asyncio.get_running_loop())
        for worker in self._workers:
            await worker.start()
        self._workers_started = True
        logger.info("WorkerSupervisor: ejecutor activo; workers en marcha")
        # Recuperar jobs 'running' huérfanos de un proceso muerto (spec 016 §2.4).
        # En background: el rollback de un import huérfano encola jobs de vmalert
        # que los workers (ya arrancados) deben procesar.
        self._recovery_task = asyncio.create_task(self._recover_orphans())

    async def _recover_orphans(self) -> None:
        for worker in self._workers:
            try:
                await worker.recover_orphans()
            except Exception as exc:  # noqa: BLE001
                logger.error("WorkerSupervisor: fallo recuperando huérfanos de %s: %s", worker.__class__.__name__, exc)

    async def _stop_workers(self) -> None:
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except asyncio.CancelledError:
                pass
            self._recovery_task = None
        self._notifier.stop()
        if not self._workers_started:
            return
        for worker in self._workers:
            try:
                await worker.stop()
            except Exception as exc:  # noqa: BLE001
                logger.error("WorkerSupervisor: fallo deteniendo worker: %s", exc)
        self._workers_started = False

    def _try_acquire_lock(self) -> bool:
        """Abre una conexion dedicada e intenta el advisory lock (no bloqueante)."""
        try:
            conn = get_db_connection()
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", (self.lock_key,))
                acquired = bool(cursor.fetchone()[0])
            if acquired:
                self._lock_conn = conn
                self._is_owner = True
                logger.info("WorkerSupervisor: advisory lock %s adquirido", self.lock_key)
                return True
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WorkerSupervisor: fallo al intentar el advisory lock: %s", exc)
        self._is_owner = False
        return False

    def _check_lock_alive(self) -> bool:
        if self._lock_conn is None or self._lock_conn.closed:
            return False
        try:
            with self._lock_conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _release_lock(self) -> None:
        conn = self._lock_conn
        self._lock_conn = None
        self._is_owner = False
        if conn is None:
            return
        try:
            if not conn.closed:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", (self.lock_key,))
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WorkerSupervisor: fallo al liberar el advisory lock: %s", exc)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
