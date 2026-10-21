"""Tenant worker: processes tenant_create, tenant_delete, parses_provision and backup_import jobs."""
import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from psycopg2.extras import Json

from .base import BaseWorker, enqueue_job_tx
from ..database import get_db_connection
from ..parses_provisioner import provision_tenant_parses, delete_tenant_parses

logger = logging.getLogger("anomalia.workers.tenant")

# Tiempo maximo de espera del job dependiente de vmalert (Ansible en el nodo).
VMALERT_DEPLOY_WAIT_SECONDS = 900
# Tiempo maximo de espera del regen de scrapes + rutas vmauth.
REGEN_WAIT_SECONDS = 300
# Gracia para que un job dependiente salga de la cola antes de rendirse; el
# budget real se mide desde que arranca (spec 016 §2.5).
QUEUE_GRACE_SECONDS = 600
# Intervalo de sondeo del estado de un job dependiente.
JOB_POLL_SECONDS = 3


class TenantWorker(BaseWorker):
    """Worker that handles tenant provisioning jobs."""

    def __init__(self, interval: int = 30, notifier=None):
        super().__init__(interval, job_types=[
            'tenant_create', 'tenant_delete', 'parses_provision', 'backup_import'
        ], notifier=notifier)

    async def process_job(self, job_id: str, job_type: str, ref_id: int, payload: dict):
        if job_type == 'tenant_create':
            await self._process_tenant_create(job_id, ref_id, payload)
        elif job_type == 'tenant_delete':
            await self._process_tenant_delete(job_id, ref_id, payload)
        elif job_type == 'parses_provision':
            await self._process_parses_provision(job_id, ref_id, payload)
        elif job_type == 'backup_import':
            await self._process_backup_import(job_id, payload)
        else:
            raise ValueError(f"Unknown job type: {job_type}")

    async def _process_tenant_create(self, job_id: str, tenant_id: int, payload: dict):
        """Process tenant creation as a single orchestrated pipeline (spec 011 §4.2).

        Fases secuenciales: BD -> auto-placement -> vmalert (si aplica) -> rutas
        (vmauth/vmagent) -> Parses -> active. El job no termina hasta que todas
        las fases estan listas; ante cualquier fallo se ejecuta rollback total.
        Corre completo en thread: sin DB síncrona en el event loop de uvicorn.
        """
        await asyncio.to_thread(self._tenant_create_sync, job_id, tenant_id)

    def _tenant_create_sync(self, job_id: str, tenant_id: int) -> None:
        try:
            self._tenant_create_pipeline(job_id, tenant_id)
        except Exception as exc:
            logger.error("tenant_create %s falló, iniciando rollback: %s", tenant_id, exc, exc_info=True)
            try:
                self._rollback_tenant_create(tenant_id, exc)
            except Exception as rb_exc:  # noqa: BLE001
                logger.error("Rollback del tenant %s falló: %s", tenant_id, rb_exc, exc_info=True)
            raise

    def _tenant_create_pipeline(self, job_id: str, tenant_id: int) -> None:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Get tenant details
            cursor.execute(
                """SELECT id, name, slug, type, account_id, project_id, has_alerts,
                          placement_mode, vmalert_node_id, vmalert_port, environment, org_id_upper,
                          vmauth_node_id, vmauth_placement_mode
                   FROM tenants WHERE id=%s""",
                (tenant_id,)
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"Tenant {tenant_id} not found")

            t_id, name, slug, t_type, account_id, project_id, has_alerts, placement_mode, vmalert_node_id, vmalert_port, environment, org_id_upper, vmauth_node_id, vmauth_placement_mode = row

            is_profiles = (t_type == 'profiles')

            # Update progress
            self._update_progress_sync(job_id, 15, "assigning_ids")

            # 1. Auto-generate account_id / project_id using sequences. Los tenants
            #    pyroscope (type='profiles') quedan NULL: sus perfiles no viven en
            #    un proyecto de VM cluster y su aislamiento es por header
            #    X-Scope-OrgID (spec 008), igual que el seed interno.
            if not is_profiles:
                if account_id is None:
                    cursor.execute("SELECT nextval('tenant_account_id_seq')")
                    account_id = cursor.fetchone()[0]
                if project_id is None:
                    cursor.execute("SELECT nextval('tenant_project_id_seq')")
                    project_id = cursor.fetchone()[0]

            if account_id is not None or project_id is not None:
                cursor.execute(
                    "UPDATE tenants SET account_id=%s, project_id=%s WHERE id=%s",
                    (account_id, project_id, tenant_id)
                )
                conn.commit()

            self._update_progress_sync(job_id, 30, "seeding_datasources")

            # 2. Seed tenant_datasources (idempotent)
            if is_profiles:
                self._seed_pyroscope_datasource(cursor, tenant_id, org_id_upper)
            else:
                self._seed_tenant_datasources(cursor, tenant_id, t_type, account_id, project_id)
            conn.commit()

            self._update_progress_sync(job_id, 45, "auto_placement")

            # 3. Auto placement for vmauth if needed (punto 4)
            if vmauth_placement_mode == 'auto' and vmauth_node_id is None:
                vmauth_node_id = self._select_vmauth_node_auto(cursor)
                if vmauth_node_id:
                    cursor.execute(
                        "UPDATE tenants SET vmauth_node_id=%s WHERE id=%s",
                        (vmauth_node_id, tenant_id)
                    )
                    conn.commit()

            # 4. Auto placement for vmalert if needed (no aplica a pyroscope)
            if not is_profiles and placement_mode == 'auto' and vmalert_node_id is None:
                vmalert_node_id = self._select_vmalert_node_auto(cursor, environment)
                if vmalert_node_id:
                    cursor.execute(
                        "UPDATE tenants SET vmalert_node_id=%s WHERE id=%s",
                        (vmalert_node_id, tenant_id)
                    )
                    conn.commit()

            # 4b. Contabilizar el tenant en el nodo vmauth asignado (manual o auto).
            #     Se revierte en la baja y en el rollback (spec 011 §4.4).
            if vmauth_node_id is not None:
                cursor.execute(
                    "UPDATE infrastructure_nodes SET tenants_count_active = tenants_count_active + 1 WHERE id=%s",
                    (vmauth_node_id,)
                )
                conn.commit()

            cursor.close()
            conn.close()
            conn = cursor = None

            # 5. Marcar 'active' antes del deploy: asi el regen que encola el propio
            #    vmalert ya incluye la ruta del tenant (un solo regen, spec 011 §4.4).
            self._set_tenant_status(tenant_id, 'active')

            needs_vmalert = has_alerts and not is_profiles
            if needs_vmalert:
                self._update_progress_sync(job_id, 60, "deploying_vmalert")
                dep_job = self._enqueue_standalone_job('vmalert_deploy', tenant_id)
                status = self._wait_for_job(dep_job, VMALERT_DEPLOY_WAIT_SECONDS)
                if status != 'succeeded':
                    raise RuntimeError(f"vmalert_deploy terminó en estado '{status}'")
                self._update_progress_sync(job_id, 80, "provisioning_parses")
            else:
                # Sin vmalert: publicar igual la ruta del tenant (vmauth) + scrape.
                self._update_progress_sync(job_id, 65, "publishing_routes")
                dep_job = self._enqueue_standalone_regen(tenant_id)
                status = self._wait_for_job(dep_job, REGEN_WAIT_SECONDS)
                if status != 'succeeded':
                    raise RuntimeError(f"vmalert_regen_scrapes terminó en estado '{status}'")
                self._update_progress_sync(job_id, 80, "provisioning_parses")

            # 6. Parses per-tenant (project + datasource + dashboards). Propaga errores.
            provision_tenant_parses(tenant_id)

            self._update_progress_sync(job_id, 100, "done")
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def _set_tenant_status(self, tenant_id: int, status: str) -> None:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE tenants SET status=%s WHERE id=%s", (status, tenant_id))
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def _enqueue_standalone_job(self, job_type: str, ref_id: int, payload: Optional[dict] = None) -> str:
        """Encola un job en una conexion corta y devuelve su id."""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            job_id = self._enqueue_job(cursor, job_type, ref_id, payload)
            conn.commit()
            return job_id
        finally:
            cursor.close()
            conn.close()

    def _enqueue_standalone_regen(self, ref_id: int, components: Optional[list] = None) -> str:
        return self._enqueue_standalone_job(
            'vmalert_regen_scrapes', ref_id, {'components': components} if components else None
        )

    def _wait_for_job(self, job_id: str, timeout_seconds: int,
                      queue_grace_seconds: int = QUEUE_GRACE_SECONDS) -> str:
        """Espera (sondeo, conexion corta) a que un job llegue a estado terminal.

        Devuelve 'succeeded', 'failed' o 'timeout'. El budget ``timeout_seconds``
        se mide desde que el job **arranca** (``started_at``), no desde el enqueue
        (spec 016 §2.5): con el ejecutor serial y colas largas, medir desde el
        enqueue abortaba trabajos que ni habían empezado. ``queue_grace_seconds``
        acota por separado el tiempo máximo en cola.

        No bloquea el event loop: corre dentro de ``asyncio.to_thread`` del job
        orquestador, y el worker que procesa el job dependiente vive en otra tarea.
        """
        enqueue_started = time.monotonic()
        run_started: Optional[float] = None
        while True:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT status, started_at FROM job_state WHERE id=%s", (job_id,))
                row = cursor.fetchone()
            finally:
                cursor.close()
                conn.close()
            if row is None:
                return 'failed'
            status = row[0]
            if status == 'succeeded':
                return 'succeeded'
            if status in ('failed', 'cancelled'):
                return 'failed'

            now = time.monotonic()
            if status == 'running':
                if run_started is None:
                    run_started = now
                if now - run_started > timeout_seconds:
                    return 'timeout'
            elif now - enqueue_started > queue_grace_seconds:
                return 'timeout'
            time.sleep(JOB_POLL_SECONDS)

    def _job_deadline(self, job_id: str) -> Optional[float]:
        """Deadline monotónico del job a partir de ``job_state.timeout_at`` (None si no aplica)."""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT EXTRACT(EPOCH FROM (timeout_at - NOW())) FROM job_state WHERE id=%s",
                (job_id,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()
        if not row or row[0] is None:
            return None
        return time.monotonic() + float(row[0])

    def _handle_orphan(self, job_id: str, job_type: str, ref_id: Optional[int], payload: dict) -> None:
        """Recupera un job huérfano de este worker (spec 016 §2.4).

        Un ``backup_import`` huérfano (proceso muerto a mitad) ejecuta su rollback
        total antes de marcarse fallido, para no dejar la BD a medias.
        """
        if job_type == 'backup_import':
            logger.warning("backup_import huérfano %s: ejecutando rollback total", job_id)
            self._rollback_backup_import(job_id, payload or {}, RuntimeError('orphaned'))

    def _rollback_tenant_create(self, tenant_id: int, error: Exception) -> None:
        """Retira todo lo creado por el alta fallida y audita el rollback (spec 011 §4.9).

        Deja la fila del tenant en status='failed' para trazabilidad (la baja
        explicita es la que la purga, spec 011 §4.11) y limpia datasources,
        vmalert y el project de Parses.
        """
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT type, has_alerts, slug, vmauth_node_id FROM tenants WHERE id=%s", (tenant_id,))
            row = cursor.fetchone()
            t_type = row[0] if row else None
            has_alerts = row[1] if row else False
            vmauth_node_id = row[3] if row else None

            # Liberar el slot del nodo vmauth asignado (si llegó a contabilizarse).
            if vmauth_node_id is not None:
                cursor.execute(
                    "UPDATE infrastructure_nodes SET tenants_count_active = GREATEST(tenants_count_active - 1, 0) WHERE id=%s",
                    (vmauth_node_id,)
                )

            # Retirar vmalert (best-effort, sincronico via job + espera acotada)
            if t_type != 'profiles' and has_alerts:
                try:
                    dep = self._enqueue_job(cursor, 'vmalert_undeploy', tenant_id)
                    conn.commit()
                    self._wait_for_job(dep, VMALERT_DEPLOY_WAIT_SECONDS)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Rollback: undeploy vmalert del tenant %s falló: %s", tenant_id, exc)

            # Retirar el project de Parses (best-effort)
            try:
                delete_tenant_parses(tenant_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Rollback: delete Parses del tenant %s falló: %s", tenant_id, exc)

            cursor.execute("DELETE FROM tenant_datasources WHERE tenant_id=%s", (tenant_id,))
            cursor.execute("DELETE FROM user_tenants WHERE tenant_id=%s", (tenant_id,))
            cursor.execute("DELETE FROM role_tenants WHERE tenant_id=%s", (tenant_id,))
            cursor.execute("UPDATE tenants SET status='failed' WHERE id=%s", (tenant_id,))
            cursor.execute(
                """INSERT INTO audit_deletions (tenant_id, actor, reason, challenge_id, confirm_step1_at, outcome)
                   VALUES (%s, NULL, %s, %s, NOW(), 'rolled_back')
                   ON CONFLICT (challenge_id) DO NOTHING""",
                (tenant_id, f"rollback de alta: {type(error).__name__}: {str(error)[:180]}", f"rollback-{tenant_id}-{uuid.uuid4().hex[:16]}"),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    async def _process_tenant_delete(self, job_id: str, tenant_id: int, payload: dict):
        """Process tenant deletion: cleanup resources.

        Corre completo en thread (spec 011 §4.2).
        """
        await asyncio.to_thread(self._tenant_delete_sync, job_id, tenant_id)

    def _tenant_delete_sync(self, job_id: str, tenant_id: int) -> None:
        """Baja secuencial de un tenant (spec 011 §4.11).

        Orden estricto: undeploy vmalert (si aplica) -> retirar scrape vmagent ->
        borrar Parses -> retirar ruta vmauth -> limpiar BD -> purgar la fila del
        tenant. Cada paso largo espera a su job dependiente; al final no queda
        ningun registro operativo del tenant y la evidencia de auditoria
        sobrevive de-identificada (audit_deletions.tenant_id = NULL).
        """
        # Fase 1: marcar 'deleting' y leer el estado (transaccion corta).
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            self._update_progress_sync(job_id, 10, "cleanup_start")
            cursor.execute("UPDATE tenants SET status='deleting' WHERE id=%s", (tenant_id,))
            conn.commit()
            cursor.execute(
                "SELECT has_alerts, type, vmauth_node_id FROM tenants WHERE id=%s", (tenant_id,)
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()
        if row is None:
            logger.info("tenant_delete %s: el tenant ya no existe", tenant_id)
            return

        has_alerts, t_type, vmauth_node_id = row
        needs_vmalert = bool(has_alerts) and t_type != 'profiles'

        # Fase 2: undeploy vmalert (no aplica a pyroscope: no tiene vmalert).
        # skip_regen=True: el pipeline controla el regen por separado (vmauth se
        # retira despues de Parses).
        if needs_vmalert:
            self._update_progress_sync(job_id, 25, "undeploying_vmalert")
            dep_job = self._enqueue_standalone_job('vmalert_undeploy', tenant_id, {'skip_regen': True})
            if self._wait_for_job(dep_job, VMALERT_DEPLOY_WAIT_SECONDS) != 'succeeded':
                raise RuntimeError("vmalert_undeploy no terminó correctamente")

        # Fase 3: retirar el scraping del tenant (vmagent).
        self._update_progress_sync(job_id, 45, "removing_scrapes")
        dep_job = self._enqueue_standalone_regen(tenant_id, components=['vmagent'])
        if self._wait_for_job(dep_job, REGEN_WAIT_SECONDS) != 'succeeded':
            raise RuntimeError("regen de vmagent no terminó correctamente")

        # Fase 4: retirar el project de Parses del tenant (spec 008). El slug se
        # resuelve desde `tenants`; es idempotente. Propaga errores: no se purga
        # el tenant si queda un project huerfano.
        self._update_progress_sync(job_id, 65, "deleting_parses")
        delete_tenant_parses(tenant_id)

        # Fase 5: retirar la ruta del tenant en vmauth.
        self._update_progress_sync(job_id, 80, "removing_vmauth")
        dep_job = self._enqueue_standalone_regen(tenant_id, components=['vmauth'])
        if self._wait_for_job(dep_job, REGEN_WAIT_SECONDS) != 'succeeded':
            raise RuntimeError("regen de vmauth no terminó correctamente")

        # Fase 6: limpieza de datos y purga total de la fila del tenant.
        self._update_progress_sync(job_id, 92, "cleaning_db")
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Liberar el slot en el nodo vmauth asignado (spec 011 §4.4).
            if vmauth_node_id is not None:
                cursor.execute(
                    "UPDATE infrastructure_nodes SET tenants_count_active = GREATEST(tenants_count_active - 1, 0) WHERE id=%s",
                    (vmauth_node_id,)
                )
            cursor.execute("DELETE FROM tenant_datasources WHERE tenant_id=%s", (tenant_id,))
            cursor.execute("DELETE FROM user_tenants WHERE tenant_id=%s", (tenant_id,))
            cursor.execute("DELETE FROM role_tenants WHERE tenant_id=%s", (tenant_id,))
            # Liberar los puertos host (spec 011 §4.7): el FK es SET NULL, asi que
            # la fila quedaria sin tenant pero seguiria ocupando el host_port.
            cursor.execute("DELETE FROM port_mapping WHERE tenant_id=%s", (tenant_id,))
            # De-identificar el resto de jobs del tenant (el orchestrator conserva
            # su ref_id para que el frontend siga el polling).
            cursor.execute(
                "UPDATE job_state SET ref_id=NULL WHERE ref_id=%s AND id<>%s", (tenant_id, job_id)
            )
            # Purga total (spec 011 §4.11): CASCADE retira tenant_vmalert_instances y
            # ui_tickets; port_mapping ya se limpio; audit_deletions queda con
            # tenant_id=NULL (SET NULL) preservando la evidencia de auditoria.
            cursor.execute("DELETE FROM tenants WHERE id=%s", (tenant_id,))
            conn.commit()
        finally:
            cursor.close()
            conn.close()

        self._update_progress_sync(job_id, 100, "done")

    async def _process_parses_provision(self, job_id: str, tenant_id: int, payload: dict):
        """Provision Parses para el tenant (project + datasource + dashboards, spec 008).

        Idempotente y reutilizable desde la UI/admin. La via normal de alta es el
        pipeline de `tenant_create`, que ya incluye esta fase.
        """
        await self._update_progress(job_id, 40, "provisioning_parses")
        try:
            await asyncio.to_thread(provision_tenant_parses, tenant_id)
        except Exception as e:
            logger.error("parses_provision failed for tenant %s: %s", tenant_id, e)
            raise
        await self._update_progress(job_id, 100, "done")

    # ------------------------------------------------------------------
    # Import de backup como job único con rollback total (spec 016)
    # ------------------------------------------------------------------
    async def _process_backup_import(self, job_id: str, payload: dict):
        """Reconcilia (y revierte si falla) un import de backup, en un solo job.

        Corre completo en thread: el pipeline encola jobs de ``VmalertWorker``
        (vmalert_redeploy/regen/undeploy) y los espera; Parses se invoca directo
        (sería deadlock esperar un job del propio TenantWorker). Ante cualquier
        fallo, ``_rollback_backup_import`` revierte BD + infraestructura remota.
        """
        await asyncio.to_thread(self._backup_import_sync, job_id, payload)

    def _backup_import_sync(self, job_id: str, payload: dict) -> None:
        try:
            self._backup_import_pipeline(job_id, payload)
        except Exception as exc:
            logger.error("backup_import %s falló, iniciando rollback total: %s", job_id, exc, exc_info=True)
            try:
                self._rollback_backup_import(job_id, payload, exc)
            except Exception as rb_exc:  # noqa: BLE001
                logger.error("Rollback del import %s falló: %s", job_id, rb_exc, exc_info=True)
            raise

    def _backup_import_pipeline(self, job_id: str, payload: dict) -> None:
        from ..routers.system_router import delete_import_state

        plan = payload.get("plan") or []
        steps: list = []
        total = sum(len(item.get("steps") or []) for item in plan)
        deadline = self._job_deadline(job_id)
        self._write_steps(job_id, steps, pct=10, phase="reconciling")
        if total <= 0:
            self._write_steps(job_id, steps, pct=100, phase="done")
            delete_import_state(job_id)
            return

        done = 0
        for item in plan:
            tenant_id = item.get("tenant_id")
            for step in item.get("steps") or []:
                if deadline is not None and time.monotonic() > deadline:
                    raise TimeoutError("backup_import excedió su timeout configurado")
                stype = step.get("type")
                label = f"Tenant {tenant_id}: {stype}"
                self._log_step(job_id, steps, label, "running", pct=10 + int(85 * done / total))
                if stype == "parses_provision":
                    provision_tenant_parses(tenant_id)
                else:
                    wait = (
                        VMALERT_DEPLOY_WAIT_SECONDS
                        if stype == "vmalert_redeploy"
                        else REGEN_WAIT_SECONDS
                    )
                    dep = self._enqueue_standalone_job(stype, tenant_id, step.get("payload"))
                    status = self._wait_for_job(dep, wait)
                    if status != "succeeded":
                        raise RuntimeError(
                            f"{stype} del tenant {tenant_id} terminó en estado '{status}'"
                        )
                done += 1
                self._log_step(job_id, steps, label, "done", pct=10 + int(85 * done / total))

        self._write_steps(job_id, steps, pct=100, phase="done")
        delete_import_state(job_id)

    def _rollback_backup_import(self, job_id: str, payload: dict, error: Exception) -> None:
        """Revierte el import: infraestructura remota de tenants nuevos + BD.

        Orden: (1) bajar lo remoto de los tenants ausentes del snapshot —mientras
        la fila aún existe para que undeploy la lea—, (2) restaurar la BD al
        snapshot, (3) regenerar rutas globales para retirar los tenants nuevos.
        Es best-effort en la infra; la BD queda exacta o no se aplica nada.
        """
        from ..routers.system_router import restore_import_snapshot

        steps = self._load_steps(job_id)
        self._write_steps(job_id, steps, phase="rollback")

        preexisting = self._load_preexisting_tenant_ids(job_id)
        plan = payload.get("plan") or []

        # (1) Infraestructura remota de los tenants nuevos (ausentes del snapshot).
        self._log_step(job_id, steps, "Rollback: revertir infraestructura remota", "running")
        for item in plan:
            tenant_id = item.get("tenant_id")
            if tenant_id in preexisting:
                continue
            types = {s.get("type") for s in (item.get("steps") or [])}
            if "vmalert_redeploy" in types:
                try:
                    dep = self._enqueue_standalone_job("vmalert_undeploy", tenant_id)
                    self._wait_for_job(dep, VMALERT_DEPLOY_WAIT_SECONDS)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Rollback import: undeploy vmalert de %s falló: %s", tenant_id, exc)
            try:
                delete_tenant_parses(tenant_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Rollback import: delete Parses de %s falló: %s", tenant_id, exc)
        self._log_step(
            job_id, steps, "Rollback: revertir infraestructura remota", "warning",
            detail="Best-effort; revisar logs del nodo si algo falló.",
        )

        # (2) Restaurar la BD exactamente al snapshot pre-import.
        self._log_step(job_id, steps, "Rollback: restaurar base de datos", "running")
        restore_import_snapshot(job_id)
        self._log_step(job_id, steps, "Rollback: restaurar base de datos", "done")

        # (3) Regen global: los tenants nuevos ya no existen en la BD.
        try:
            dep = self._enqueue_standalone_job("vmalert_regen_scrapes", 0)
            self._wait_for_job(dep, REGEN_WAIT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rollback import: regen global falló: %s", exc)

        self._log_step(
            job_id, steps, "Rollback completado", "warning",
            detail=str(error)[:300],
        )

    def _load_steps(self, job_id: str) -> list:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT steps FROM job_state WHERE id = %s", (job_id,))
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()
        if not row or not row[0]:
            return []
        steps = row[0]
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except json.JSONDecodeError:
                return []
        return steps if isinstance(steps, list) else []

    def _load_preexisting_tenant_ids(self, job_id: str) -> set:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT preexisting_tenant_ids FROM backup_import_state WHERE job_id = %s",
                (job_id,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()
        return set(row[0]) if row and row[0] else set()

    def _write_steps(self, job_id: str, steps: list, pct: Optional[int] = None,
                     phase: Optional[str] = None) -> None:
        sets = ["steps = %s"]
        params: list = [Json(steps)]
        if pct is not None:
            sets.append("progress_pct = %s")
            params.append(pct)
        if phase is not None:
            sets.append("phase = %s")
            params.append(phase)
        params.append(job_id)
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"UPDATE job_state SET {', '.join(sets)} WHERE id = %s", params
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def _log_step(self, job_id: str, steps: list, name: str, status: str,
                  pct: Optional[int] = None, detail: Optional[str] = None) -> list:
        for entry in steps:
            if entry.get("name") == name:
                entry["status"] = status
                if detail is not None:
                    entry["detail"] = detail
                break
        else:
            entry = {"name": name, "status": status}
            if detail is not None:
                entry["detail"] = detail
            steps.append(entry)
        self._write_steps(job_id, steps, pct=pct)
        return steps

    def _seed_tenant_datasources(self, cursor, tenant_id: int, t_type: str, account_id: int, project_id: int):
        """Seed tenant_datasources based on infrastructure select nodes."""
        tmap = {
            'metrics': ('vmselect', 'VictoriaMetrics', 'path'),
            'logs': ('vlselect', 'VictoriaLogs', 'header'),
            'traces': ('vtselect', 'VictoriaTraces', 'header'),
        }
        comp_type_sel, kind_sel, scoping_sel = tmap.get((t_type or '').lower(), ('vmselect', 'VictoriaMetrics', 'path'))

        cursor.execute(
            """SELECT service_ip, ip_address, port
               FROM infrastructure_nodes
               WHERE component_type = %s
                 AND is_active = TRUE
                 AND status = 'operational'
               LIMIT 1""",
            (comp_type_sel,)
        )
        row = cursor.fetchone()
        if not row:
            logger.warning("No se encontró nodo %s operativo para tenant %s (type=%s)", comp_type_sel, tenant_id, t_type)
            return

        service_ip, ip_address, port = row
        read_url = f"http://{service_ip or ip_address}:{port}"
        scoping = scoping_sel

        cursor.execute(
            """INSERT INTO tenant_datasources (tenant_id, kind, read_url, scoping, account_id, project_id, enabled)
               VALUES (%s, %s, %s, %s, %s, %s, TRUE)
               ON CONFLICT (tenant_id) DO UPDATE SET
                   kind = EXCLUDED.kind,
                   read_url = EXCLUDED.read_url,
                   scoping = EXCLUDED.scoping,
                   account_id = EXCLUDED.account_id,
                   project_id = EXCLUDED.project_id,
                   enabled = EXCLUDED.enabled""",
            (tenant_id, kind_sel, read_url, scoping, account_id, project_id)
        )

    def _component_to_kind(self, comp_type: str) -> str:
        mapping = {
            'vmselect': 'VictoriaMetrics',
            'vlselect': 'VictoriaLogs',
            'vtselect': 'VictoriaTraces',
        }
        return mapping.get(comp_type, 'VictoriaMetrics')

    def _seed_pyroscope_datasource(self, cursor, tenant_id: int, org_id_upper: Optional[str]):
        """Seed datasource Pyroscope para tenants type='profiles' (spec 008 §2).

        El read_url apunta a la base del nodo Pyroscope y el aislamiento es por
        header `X-Scope-OrgID` (org_id), sin account/project (los perfiles no son
        series PromQL consultables vía vmselect). Mismo shape que el seed interno
        de init.sql.j2: kind='Pyroscope', scoping='header', org_id=<org upper>.
        """
        cursor.execute(
            """SELECT service_ip, ip_address, port
               FROM infrastructure_nodes
               WHERE component_type = 'pyroscope'
                 AND is_active = TRUE
                 AND status = 'operational'
               LIMIT 1"""
        )
        row = cursor.fetchone()
        if row:
            service_ip, ip_address, port = row
            base_url = f"http://{service_ip or ip_address}:{port or 4040}"
        else:
            base_url = "http://127.0.0.1:4040"
        cursor.execute(
            """INSERT INTO tenant_datasources (tenant_id, kind, read_url, scoping, account_id, project_id, org_id, enabled)
               VALUES (%s, 'Pyroscope', %s, 'header', NULL, NULL, %s, TRUE)
               ON CONFLICT (tenant_id) DO UPDATE SET
                   kind = EXCLUDED.kind,
                   read_url = EXCLUDED.read_url,
                   scoping = EXCLUDED.scoping,
                   account_id = EXCLUDED.account_id,
                   project_id = EXCLUDED.project_id,
                   org_id = EXCLUDED.org_id,
                   enabled = EXCLUDED.enabled""",
            (tenant_id, base_url, org_id_upper)
        )

    def _select_vmauth_node_auto(self, cursor) -> Optional[int]:
        """Select vmauth node with least tenants (auto placement)."""
        cursor.execute(
            """SELECT id, tenants_count_active, capacity_slots
               FROM infrastructure_nodes
               WHERE component_type = 'vmauth'
                 AND is_active = TRUE
                 AND status = 'operational'
                 AND tenants_count_active < capacity_slots
               ORDER BY tenants_count_active ASC, id ASC
               LIMIT 1"""
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def _select_vmalert_node_auto(self, cursor, environment: Optional[str] = None) -> Optional[int]:
        """Select vmalert node with least tenants (auto placement)."""
        cursor.execute(
            """SELECT id, tenants_count_active, capacity_slots
               FROM infrastructure_nodes
               WHERE component_type = 'vmalert'
                 AND is_active = TRUE
                 AND status = 'operational'
                 AND tenants_count_active < capacity_slots
               ORDER BY tenants_count_active ASC, id ASC
               LIMIT 1"""
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def _enqueue_job(self, cursor, job_type: str, ref_id: int, payload: Optional[dict] = None) -> str:
        """Enqueue a new job in job_state (sync: fases en thread, spec 011 §4.2).

        ``payload`` viaja como extra-vars de entrada del job (spec 011 §4.11).
        Delega en el helper compartido de ``workers.base``.
        """
        return enqueue_job_tx(cursor, job_type, ref_id, payload)

    async def _update_progress(self, job_id: str, progress: int, phase: str):
        """Update job progress (en thread: nunca bloquea el event loop)."""
        await asyncio.to_thread(self._update_progress_sync, job_id, progress, phase)

    @staticmethod
    def _update_progress_sync(job_id: str, progress: int, phase: str):
        """UPDATE de progreso en transacción corta (spec 011 §4.2)."""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE job_state SET progress_pct=%s, phase=%s WHERE id=%s",
                (progress, phase, job_id)
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()