"""Wake-up de workers vía PostgreSQL LISTEN/NOTIFY (spec 018).

En vez de esperar *siempre* el intervalo completo de polling, los workers que lo
soportan se despiertan cuando se encola un job nuevo. Esto reduce la latencia del
circuito alerta → análisis (Alertmanager → webhook → job → IA → tarjeta) sin
disminuir el intervalo de respaldo: si una notificación se pierde, el timeout del
``wait`` actúa como red de seguridad.

Diseño:
- Una conexión dedicada (autocommit) ejecuta ``LISTEN anomalia_jobs``.
- Un hilo bloqueante hace ``select()`` sobre el socket de la conexión y, al
  recibir una notificación, marca un ``asyncio.Event`` vía
  ``loop.call_soon_threadsafe``.
- ``wait(timeout)`` espera el evento o expira.

El ``NOTIFY`` lo emite ``enqueue_job_tx`` dentro de la misma transacción del
INSERT, de modo que PostgreSQL lo entrega **solo al hacer commit** (nunca notifica
jobs que terminaron en rollback).
"""
import asyncio
import logging
import select
import threading
import time
from typing import Optional

from psycopg2 import extensions

from ..database import get_db_connection

logger = logging.getLogger("anomalia.workers")

# Canal lógico de PostgreSQL para avisos de encolado. Debe coincidir con el
# `NOTIFY` de `enqueue_job_tx`.
JOBS_CHANNEL = "anomalia_jobs"


class JobNotifier:
    """Escucha ``anomalia_jobs`` y expone ``wait()`` para despertar a los workers."""

    def __init__(self, channel: str = JOBS_CHANNEL):
        self.channel = channel
        self._event = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Arranca el hilo de escucha ligado al event loop del proceso."""
        if self._thread is not None:
            return
        self._loop = loop
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._listen_forever, name="anomalia-job-notifier", daemon=True
        )
        self._thread.start()
        logger.info("JobNotifier escuchando el canal '%s'", self.channel)

    def stop(self) -> None:
        self._stop_flag.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=3)

    async def wait(self, timeout: float) -> None:
        """Espera un aviso o expira tras ``timeout`` segundos (red de seguridad)."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._event.clear()

    def _listen_forever(self) -> None:
        while not self._stop_flag.is_set():
            conn = None
            try:
                conn = get_db_connection()
                conn.set_isolation_level(extensions.ISOLATION_LEVEL_AUTOCOMMIT)
                with conn.cursor() as cursor:
                    cursor.execute(f"LISTEN {self.channel}")
                while not self._stop_flag.is_set():
                    if select.select([conn], [], [], 1.0) == ([], [], []):
                        continue
                    conn.poll()
                    while conn.notifies:
                        conn.notifies.pop()
                        if self._loop is not None:
                            self._loop.call_soon_threadsafe(self._event.set)
            except Exception as exc:  # noqa: BLE001
                logger.warning("JobNotifier: reconectando tras error: %s", exc)
                time.sleep(1.0)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
