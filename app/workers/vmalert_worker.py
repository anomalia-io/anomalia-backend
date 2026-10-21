"""Vmalert worker: processes vmalert_deploy, vmalert_undeploy, vmalert_redeploy, vmalert_reload, vmalert_sync_rules, vmalert_regen_scrapes jobs."""
import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
import uuid
from typing import Optional

import httpx
from psycopg2.extras import Json

from .base import BaseWorker, enqueue_job_tx
from ..database import get_db_connection
from ..internal_rules import INTERNAL_RULES

logger = logging.getLogger("anomalia.workers.vmalert")

RELOAD_ATTEMPTS = 3
RELOAD_TIMEOUT_SECONDS = 10.0

# Timeout del playbook Ansible por job (spec 011 §4.6). El primer deploy en frío
# tarda ~5 min; un 300s hardcodeado mató el deploy de METRICS antes de crear la
# unit. Se parametriza por env para ajustarlo sin tocar código.
VMALERT_PLAYBOOK_TIMEOUT_SECONDS = int(os.getenv("VMALERT_PLAYBOOK_TIMEOUT", "600"))

# vmalert evalúa contra vmselect/vminsert CLUSTER (spec 008): el plugin espera
# rutas con formato cluster de un solo segmento `<account>:<project>` (p. ej.
# /select/0:0/prometheus), no las URLs desnudas de los nodos ni la forma con
# barra /select/0/0/prometheus (vmselect responde 400 'unsupported path
# requested'). La resolución por-tenant está en _resolve_tenant_datasources.

# Puertos por defecto por componente (spec 011 §4.3/§4.4): usados por el regen
# de scrapes cuando infrastructure_nodes no fija port para un componente.
PORT_DEFAULTS = {
    'vlstorage': 8482, 'vlinsert': 8480, 'vlselect': 8481,
    'vmstorage': 8402, 'vminsert': 8400, 'vmselect': 8401,
    'vtstorage': 8492, 'vtinsert': 8490, 'vtselect': 8491,
    'pyroscope': 4040, 'vmauth': 8427, 'vmagent': 8429,
    'alertmanager': 9093, 'vmalert': 8880,
    'backend': 8000, 'parses': 8090,
}


class VmalertWorker(BaseWorker):
    """Worker that handles vmalert deployment jobs."""

    def __init__(self, interval: int = 30, notifier=None):
        super().__init__(
            interval,
            job_types=[
                'vmalert_deploy', 'vmalert_undeploy', 'vmalert_redeploy',
                'vmalert_reload', 'vmalert_sync_rules', 'vmalert_regen_scrapes',
            ],
            notifier=notifier,
        )
        self._reconcile_task: Optional[asyncio.Task] = None

    async def process_job(self, job_id: str, job_type: str, ref_id: int, payload: dict):
        if job_type == 'vmalert_deploy':
            await self._process_vmalert_deploy(job_id, ref_id, payload)
        elif job_type == 'vmalert_undeploy':
            await self._process_vmalert_undeploy(job_id, ref_id, payload)
        elif job_type == 'vmalert_redeploy':
            await self._process_vmalert_redeploy(job_id, ref_id, payload)
        elif job_type == 'vmalert_reload':
            await self._process_vmalert_reload(job_id, ref_id, payload)
        elif job_type == 'vmalert_sync_rules':
            await self._process_vmalert_sync_rules(job_id, ref_id, payload)
        elif job_type == 'vmalert_regen_scrapes':
            await self._process_vmalert_regen_scrapes(job_id, ref_id, payload)
        else:
            raise ValueError(f"Unknown job type: {job_type}")

    async def start(self):
        """Arranca el loop de polling y garantiza vmalert always-on para los
        tenants de control interno (spec 011 §4.7)."""
        await super().start()
        try:
            await self._bootstrap_internal()
        except Exception as e:
            logger.error("Bootstrap de tenants internos falló: %s", e, exc_info=True)
        # Reconciliación periódica (spec 011 §4.7): el bootstrap inicial corre antes
        # de que fallen los jobs (el deploy de METRICS murió por timeout tras 300s
        # hardcodeados y nadie re-encolaba -> tenant roto para siempre). Este task
        # repite el always-on cada interval, de modo que un deploy fallido o una
        # deriva de puerto se auto-corrige en <60s. Idempotente por los guards de
        # _bootstrap_internal (instancia 'deployed' o job queued/running).
        if self._reconcile_task is None or self._reconcile_task.done():
            self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    async def stop(self):
        await super().stop()
        if self._reconcile_task:
            self._reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconcile_task
            self._reconcile_task = None

    async def _reconcile_loop(self):
        """Repite _bootstrap_internal con la cadencia del worker (spec 011 §4.7)."""
        while self._running:
            try:
                await self._bootstrap_internal()
            except Exception as e:
                logger.error("Reconciliación interna de vmalert falló: %s", e, exc_info=True)
            await asyncio.sleep(self.interval)

    async def _bootstrap_internal(self):
        """Asegura que cada tenant de control interno tenga su vmalert desplegado
        (siempre-on). Idempotente: no re-encola si ya hay instancia 'deployed' o
        un job vmalert_deploy queued/running. El auto-placement usa la misma regla
        de menor carga que tenant_worker (spec 011 §4.2)."""
        await asyncio.to_thread(self._bootstrap_internal_sync)

    def _bootstrap_internal_sync(self):
        """Cuerpo sincrónico de _bootstrap_internal: corre en thread para que una
        espera de lock nunca congele el event loop de uvicorn (spec 011 §4.2)."""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Reconciliación de puertos determinísticos (spec 011 §4.5): el
            # puerto esperado de cada tenant es 8880 + tenant_id. Si la BD registra
            # otro (colisión histórica 8881/8881, puertos heredados del pool), se
            # corrige tenants.vmalert_port y, si hay instancia desplegada, se
            # re-encola un redeploy para confluir la unit en el nodo.
            cursor.execute(
                """SELECT id, vmalert_port FROM tenants
                   WHERE status = 'active'
                     AND has_alerts = TRUE
                     AND type <> 'profiles'
                     AND COALESCE(vmalert_node_id, 0) != 0"""
            )
            for t_id, cur_port in cursor.fetchall():
                expected_port = 8880 + t_id
                if cur_port is not None and cur_port == expected_port:
                    continue
                cursor.execute(
                    "UPDATE tenants SET vmalert_port=%s WHERE id=%s",
                    (expected_port, t_id)
                )
                logger.info("Reconciliación de puerto: tenant %s ahora usa %s", t_id, expected_port)
                cursor.execute(
                    """SELECT 1 FROM tenant_vmalert_instances
                       WHERE tenant_id = %s AND status = 'deployed'""",
                    (t_id,)
                )
                if not cursor.fetchone():
                    continue
                cursor.execute(
                    """SELECT 1 FROM job_state
                       WHERE type = 'vmalert_redeploy' AND ref_id = %s
                         AND status IN ('queued', 'running')""",
                    (t_id,)
                )
                if cursor.fetchone():
                    continue
                self._enqueue_job(cursor, 'vmalert_redeploy', t_id)
                logger.info("Reconciliación de puerto: encolado vmalert_redeploy para tenant %s", t_id)

            cursor.execute(
                """SELECT id FROM tenants
                   WHERE is_internal = TRUE
                     AND status = 'active'
                     AND type <> 'profiles'"""
            )
            internal_ids = [r[0] for r in cursor.fetchall()]
            for tenant_id in internal_ids:
                cursor.execute(
                    """SELECT 1 FROM tenant_vmalert_instances
                       WHERE tenant_id = %s AND status = 'deployed'""",
                    (tenant_id,)
                )
                if cursor.fetchone():
                    continue
                cursor.execute(
                    """SELECT 1 FROM job_state
                       WHERE type = 'vmalert_deploy' AND ref_id = %s
                         AND status IN ('queued', 'running')""",
                    (tenant_id,)
                )
                if cursor.fetchone():
                    continue
                cursor.execute(
                    """SELECT id FROM infrastructure_nodes
                       WHERE component_type = 'vmalert'
                         AND is_active = TRUE AND status = 'operational'
                         AND tenants_count_active < capacity_slots
                       ORDER BY tenants_count_active ASC, id ASC
                       LIMIT 1"""
                )
                node = cursor.fetchone()
                node_id = node[0] if node else None
                cursor.execute(
                    """UPDATE tenants
                       SET has_alerts = TRUE, placement_mode = 'auto',
                           vmalert_node_id = COALESCE(vmalert_node_id, %s)
                       WHERE id = %s""",
                    (node_id, tenant_id)
                )
                self._enqueue_job(cursor, 'vmalert_deploy', tenant_id)
                logger.info("Bootstrap interno: encolado vmalert_deploy para tenant %s", tenant_id)

            # Pyroscope (type='profiles') NO lleva instancia vmalert: los perfiles
            # no son series PromQL consultables por vmalert (spec 011 §4.6). Si una
            # instalación previa dejó una instancia o un nodo asignado, se retira la
            # unidad y se limpia el placement. Idempotente: solo encola el undeploy
            # mientras exista instancia 'deployed' (nodo intacto para el undeploy) y
            # limpia vmalert_node_id/port una vez que desaparece.
            cursor.execute(
                """SELECT t.id,
                          EXISTS (SELECT 1 FROM tenant_vmalert_instances tvi
                                  WHERE tvi.tenant_id = t.id AND tvi.status = 'deployed')
                   FROM tenants t
                   WHERE t.type = 'profiles' AND t.status = 'active'"""
            )
            for profiles_id, has_instance in cursor.fetchall():
                if has_instance:
                    cursor.execute(
                        """SELECT 1 FROM job_state
                           WHERE type = 'vmalert_undeploy' AND ref_id = %s
                             AND status IN ('queued', 'running')""",
                        (profiles_id,)
                    )
                    if cursor.fetchone():
                        continue
                    self._enqueue_job(cursor, 'vmalert_undeploy', profiles_id)
                    logger.info(
                        "Bootstrap interno: encolado vmalert_undeploy para tenant pyroscope %s",
                        profiles_id,
                    )
                else:
                    cursor.execute(
                        """UPDATE tenants
                           SET vmalert_node_id = NULL, vmalert_port = NULL
                           WHERE id = %s AND vmalert_node_id IS NOT NULL""",
                        (profiles_id,)
                    )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def _enqueue_job(self, cursor, job_type: str, ref_id: int, payload: Optional[dict] = None) -> str:
        """Encola un job nuevo en job_state (sync: se usa dentro de fases que
        corren en thread, spec 011 §4.2). ``payload`` viaja como extra-vars de
        entrada del job (spec 011 §4.11). Delega en el helper compartido."""
        return enqueue_job_tx(cursor, job_type, ref_id, payload)

    async def _process_vmalert_deploy(self, job_id: str, tenant_id: int, payload: dict):
        """Deploy vmalert instance for tenant.

        Sin transacción abierta durante Ansible (spec 011 §4.2): la fase de
        lectura corre en thread con conn propia y corta; la escritura abre otra
        conn recién terminado el playbook. Antes la txn abierta desde el SELECT
        inicial retenía locks minutos enteros (descargas) y provocaba el
        deadlock con init_db del segundo gateway.
        """
        await self._update_progress(job_id, 10, "preparing")
        data = await asyncio.to_thread(self._deploy_load, job_id, tenant_id)
        if data is None:
            return
        await self._run_ansible_vmalert(
            job_id, data['target_ip'], data['admin_user'], data['admin_pass'],
            data['vmalert_instances'], vmalert_rules=data['vmalert_rules'],
        )
        await self._update_progress(job_id, 90, "registering_instance")
        await asyncio.to_thread(self._deploy_persist, tenant_id, data)
        await self._update_progress(job_id, 100, "done")

    def _deploy_load(self, job_id: str, tenant_id: int) -> Optional[dict]:
        """Fase de lectura del deploy: transacción corta en thread (spec 011 §4.2).

        Devuelve None si el tenant no aplica (type='profiles' o has_alerts=false).
        """
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Get tenant details
            cursor.execute(
                """SELECT id, name, slug, vmalert_node_id, vmalert_port, has_alerts,
                          org_id_upper, is_internal, account_id, project_id, type
                   FROM tenants WHERE id=%s""",
                (tenant_id,)
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"Tenant {tenant_id} not found")

            t_id, name, slug, vmalert_node_id, vmalert_port, has_alerts, org_id_upper, is_internal, account_id, project_id, t_type = row

            if t_type == 'profiles':
                logger.info(
                    "Tenant %s es tipo 'profiles' (Pyroscope): no se despliega vmalert "
                    "(los perfiles no son PromQL consultables por vmalert, spec 011 §4.6)",
                    tenant_id
                )
                return None

            if not has_alerts:
                logger.info("Tenant %s has has_alerts=false, skipping vmalert deploy", tenant_id)
                return None

            if not vmalert_node_id:
                raise ValueError(f"Tenant {tenant_id} has no vmalert_node_id assigned")

            self._update_progress_sync(job_id, 20, "allocating_port")

            # spec 011 §4.5: puerto determinístico por tenant (8880 + tenant_id).
            # El 8880 queda reservado para la unit single anomalia-vmalert.service.
            # La asignación determinística elimina la carrera entre workers
            # concurrentes (spec 013) y auto-corrige colisiones históricas
            # (p. ej. 8881/8881). Se fuerza SIEMPRE en el deploy para que la BD
            # confluya; si otro tenant ya ocupa el puerto en el mismo nodo, se
            # cae al pool por-tenant como fallback defensivo.
            expected_port = 8880 + tenant_id
            cursor.execute(
                """SELECT 1
                   FROM tenant_vmalert_instances tvi
                   JOIN tenants t ON t.id = tvi.tenant_id AND t.id != %s
                   WHERE tvi.instance_id = %s
                     AND tvi.status = 'deployed'
                     AND tvi.port = %s""",
                (tenant_id, vmalert_node_id, expected_port)
            )
            if cursor.fetchone():
                expected_port = self._allocate_vmalert_port(cursor, vmalert_node_id)
            if vmalert_port != expected_port:
                vmalert_port = expected_port
                cursor.execute(
                    "UPDATE tenants SET vmalert_port=%s WHERE id=%s",
                    (vmalert_port, tenant_id)
                )
                conn.commit()

            self._update_progress_sync(job_id, 40, "preparing_ansible")

            # Get node details
            cursor.execute(
                """SELECT hostname, ip_address, service_ip, admin_username, admin_password
                   FROM infrastructure_nodes WHERE id=%s""",
                (vmalert_node_id,)
            )
            node_row = cursor.fetchone()
            if not node_row:
                raise ValueError(f"Node {vmalert_node_id} not found")

            hostname, ip_address, service_ip, admin_user, admin_pass = node_row
            target_ip = service_ip or ip_address

            # Get Alertmanager URL (from alertmanager node)
            alertmanager_url = self._get_alertmanager_url(cursor)
            # Datasource/remoteWrite cluster URL por-tenant (spec 011 §4.6):
            # internos -> store METRICS 0:0; no internos -> su account:project.
            datasource_url, remote_write_url = self._resolve_tenant_datasources(
                cursor, {
                    'is_internal': is_internal,
                    'account_id': account_id,
                    'project_id': project_id,
                }
            )

            self._update_progress_sync(job_id, 60, "running_ansible")

            # La BD es la fuente de verdad de las reglas: si ya existe una versión
            # del tenant (redeploy), se conserva y se materializa al nodo en el
            # playbook; si es el primer deploy, se usa la plantilla por defecto.
            cursor.execute(
                "SELECT rules_yaml FROM tenant_vmalert_instances WHERE tenant_id = %s;",
                (tenant_id,)
            )
            prev = cursor.fetchone()
            existing_rules = prev[0] if prev else None

            # spec 011 §4.9: para tenants de control interno la regla canónica
            # (internal_rules/<slug>.yaml) se sobre-escribe en cada deploy; como
            # el PUT de reglas está bloqueado, ese archivo es el único origen
            # admisible -> las reglas internas son inmutables por construcción.
            rules_yaml = existing_rules
            if is_internal:
                rules_yaml = INTERNAL_RULES.get(slug) or existing_rules

            # Prepare vmalert_instances for Ansible. El rules_yaml NO viaja dentro
            # de esta lista: Ansible templa recursivamente la variable en los
            # `when` del playbook (ej. install-binaries.yml) y un {{ $labels.job }}
            # anidado reventaba con 'unexpected char $' (spec 011 §4.2). Las
            # reglas van por el canal aparte vmalert_rules y se materializan a
            # archivos temporales (rutas en vmalert_rules_files).
            vmalert_instance = {
                'tenant_slug': slug,
                'tenant_name': name,
                'tenant_id': t_id,
                'org_id_upper': org_id_upper,
                'is_internal': is_internal,
                'port': vmalert_port,
                'rules_path': f'/etc/anomalia/vmalert/{slug}',
                'alertmanager_url': alertmanager_url,
                'datasource_url': datasource_url,
                'remote_write_url': remote_write_url,
            }
            vmalert_instances = [vmalert_instance]

            vmalert_rules = []
            if rules_yaml is not None:
                vmalert_rules = [{
                    'tenant_slug': slug,
                    'rules_yaml': rules_yaml,
                    'rules_path': f'/etc/anomalia/vmalert/{slug}',
                }]

            conn.commit()
            return {
                'target_ip': target_ip,
                'admin_user': admin_user,
                'admin_pass': admin_pass,
                'vmalert_node_id': vmalert_node_id,
                'vmalert_port': vmalert_port,
                'slug': slug,
                'rules_yaml': rules_yaml,
                'vmalert_instances': vmalert_instances,
                'vmalert_rules': vmalert_rules,
            }
        finally:
            cursor.close()
            conn.close()

    def _deploy_persist(self, tenant_id: int, data: dict) -> None:
        """Fase de escritura del deploy: conn propia, commit corto (spec 011 §4.2)."""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Register instance
            cursor.execute(
                """INSERT INTO tenant_vmalert_instances
                       (tenant_id, instance_id, port, status, unit_name, rules_path, rules_yaml)
                   VALUES (%s, %s, %s, 'deployed', %s, %s, %s)
                   ON CONFLICT (tenant_id) DO UPDATE SET
                       instance_id = EXCLUDED.instance_id,
                       port = EXCLUDED.port,
                       status = 'deployed',
                       unit_name = EXCLUDED.unit_name,
                       rules_path = EXCLUDED.rules_path,
                       rules_yaml = EXCLUDED.rules_yaml,
                       enabled = TRUE,
                       updated_at = CURRENT_TIMESTAMP""",
                (
                    tenant_id,
                    data['vmalert_node_id'],
                    data['vmalert_port'],
                    f"anomalia-vmalert-{data['slug']}.service",
                    f"/etc/anomalia/vmalert/{data['slug']}",
                    data['rules_yaml'],
                )
            )

            # Increment node tenant count
            cursor.execute(
                "UPDATE infrastructure_nodes SET tenants_count_active = tenants_count_active + 1 WHERE id=%s",
                (data['vmalert_node_id'],)
            )

            # Regen incremental de scrapes vmagent + rutas vmauth (spec 011 §4.3/§4.4)
            self._enqueue_regenerate_scrapes(cursor, tenant_id)

            conn.commit()
        finally:
            cursor.close()
            conn.close()

    async def _process_vmalert_undeploy(self, job_id: str, tenant_id: int, payload: dict):
        """Undeploy vmalert instance for tenant (sin txn durante Ansible, spec 011 §4.2)."""
        await self._update_progress(job_id, 10, "preparing")
        data = await asyncio.to_thread(self._undeploy_load, job_id, tenant_id)
        if data is None:
            return
        await self._update_progress(job_id, 60, "running_ansible")

        # Run Ansible playbook with state=absent
        await self._run_ansible_vmalert(
            job_id, data['target_ip'], data['admin_user'], data['admin_pass'],
            data['vmalert_instances'], extra_vars={'vmalert_state': 'absent'}
        )

        await self._update_progress(job_id, 90, "cleanup_db")
        await asyncio.to_thread(self._undeploy_persist, tenant_id, data, bool((payload or {}).get('skip_regen')))
        await self._update_progress(job_id, 100, "done")

    def _undeploy_load(self, job_id: str, tenant_id: int) -> Optional[dict]:
        """Fase de lectura del undeploy: transacción corta en thread (spec 011 §4.2).

        Devuelve None si no hay nada que retirar (tenant sin instancia).
        """
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Get tenant and instance details
            cursor.execute(
                """SELECT t.id, t.name, t.slug, t.vmalert_node_id, t.vmalert_port,
                          tvi.instance_id, tvi.port
                   FROM tenants t
                   LEFT JOIN tenant_vmalert_instances tvi ON tvi.tenant_id = t.id AND tvi.status = 'deployed'
                   WHERE t.id = %s""",
                (tenant_id,)
            )
            row = cursor.fetchone()
            if not row:
                logger.info("Tenant %s not found for undeploy", tenant_id)
                return None

            t_id, name, slug, vmalert_node_id, vmalert_port, instance_id, instance_port = row

            if not vmalert_node_id or not instance_id:
                logger.info("No vmalert instance to undeploy for tenant %s", tenant_id)
                return None

            self._update_progress_sync(job_id, 20, "preparing_ansible")

            # Get node details
            cursor.execute(
                """SELECT hostname, ip_address, service_ip, admin_username, admin_password
                   FROM infrastructure_nodes WHERE id=%s""",
                (vmalert_node_id,)
            )
            node_row = cursor.fetchone()
            if not node_row:
                raise ValueError(f"Node {vmalert_node_id} not found")

            hostname, ip_address, service_ip, admin_user, admin_pass = node_row
            target_ip = service_ip or ip_address
            port = instance_port or vmalert_port

            # Prepare vmalert_instances for undeploy (empty list to trigger cleanup)
            vmalert_instances = [{
                'tenant_slug': slug,
                'tenant_name': name,
                'tenant_id': t_id,
                'port': port,
                'rules_path': f'/etc/anomalia/vmalert/{slug}',
                'state': 'absent',  # Signal to remove
            }]

            conn.commit()
            return {
                'target_ip': target_ip,
                'admin_user': admin_user,
                'admin_pass': admin_pass,
                'vmalert_node_id': vmalert_node_id,
                'instance_id': instance_id,
                'vmalert_instances': vmalert_instances,
            }
        finally:
            cursor.close()
            conn.close()

    def _undeploy_persist(self, tenant_id: int, data: dict, skip_regen: bool = False) -> None:
        """Fase de escritura del undeploy: conn propia, commit corto (spec 011 §4.2).

        ``skip_regen`` lo usa el pipeline de baja (spec 011 §4.11): el undeploy no
        encola el regen para que el orquestador lo lance por separado (vmagent
        antes de Parses, vmauth despues).
        """
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Update instance status
            cursor.execute(
                "UPDATE tenant_vmalert_instances SET status='undeployed' WHERE tenant_id=%s AND instance_id=%s",
                (tenant_id, data['instance_id'])
            )

            # Decrement node tenant count
            cursor.execute(
                "UPDATE infrastructure_nodes SET tenants_count_active = GREATEST(tenants_count_active - 1, 0) WHERE id=%s",
                (data['vmalert_node_id'],)
            )

            # Regen incremental de scrapes vmagent + rutas vmauth (spec 011 §4.3/§4.4).
            # Se omite cuando el pipeline de baja lo controla explicitamente.
            if not skip_regen:
                self._enqueue_regenerate_scrapes(cursor, tenant_id)

            conn.commit()
        finally:
            cursor.close()
            conn.close()

    async def _process_vmalert_redeploy(self, job_id: str, tenant_id: int, payload: dict):
        """Redeploy: undeploy then deploy.

        El undeploy interno corre con ``skip_regen=True`` (Perf B, spec 016 §2.7):
        el regen se dispara una sola vez, desde ``_deploy_persist``, en lugar de
        dos veces (una por el undeploy y otra por el deploy).
        """
        undeploy_payload = dict(payload or {})
        undeploy_payload['skip_regen'] = True
        await self._process_vmalert_undeploy(job_id, tenant_id, undeploy_payload)
        await self._process_vmalert_deploy(job_id, tenant_id, payload)

    async def _process_vmalert_reload(self, job_id: str, tenant_id: int, payload: dict):
        """Reload vmalert instance via HTTP /-/reload (spec 011: timeout + healthcheck + retry)."""
        await self._update_progress(job_id, 20, "resolving_instance")
        base_url = await asyncio.to_thread(self._reload_load, tenant_id)
        await self._update_progress(job_id, 60, "reloading")
        await self._http_reload_vmalert(base_url)
        await self._update_progress(job_id, 100, "done")

    @staticmethod
    def _reload_load(tenant_id: int) -> str:
        """Fase de lectura del reload: transacción corta en thread (spec 011 §4.2)."""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT tvi.port, n.service_ip, n.ip_address
                   FROM tenant_vmalert_instances tvi
                   JOIN infrastructure_nodes n ON n.id = tvi.instance_id
                   WHERE tvi.tenant_id = %s AND tvi.status = 'deployed'""",
                (tenant_id,)
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"No deployed vmalert instance for tenant {tenant_id}")

            port, service_ip, ip_address = row
            return f"http://{service_ip or ip_address}:{port}"
        finally:
            cursor.close()
            conn.close()

    async def _process_vmalert_sync_rules(self, job_id: str, tenant_id: int, payload: dict):
        """Materializa rules_yaml (BD = fuente de verdad) hacia el nodo y recarga vmalert."""
        await self._update_progress(job_id, 10, "preparing")
        data = await asyncio.to_thread(self._sync_rules_load, tenant_id)
        if data is None:
            return
        await self._update_progress(job_id, 30, "running_ansible")

        await self._run_ansible_vmalert(
            job_id, data['target_ip'], data['admin_user'], data['admin_pass'],
            data['instances'], vmalert_rules=data['vmalert_rules'],
            playbook="sync-vmalert-rules.yml"
        )

        await self._update_progress(job_id, 70, "reloading")
        await self._http_reload_vmalert(f"http://{data['target_ip']}:{data['port']}")
        await self._update_progress(job_id, 100, "done")

    def _sync_rules_load(self, tenant_id: int) -> Optional[dict]:
        """Fase de lectura del sync_rules: transacción corta en thread (spec 011 §4.2).

        Devuelve None si el tenant es type='profiles' (sin vmalert que sincronizar).
        """
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT t.slug, t.name, tvi.rules_yaml, tvi.port,
                          n.service_ip, n.ip_address, n.admin_username, n.admin_password,
                          t.type, t.is_internal, t.account_id, t.project_id
                   FROM tenants t
                   JOIN tenant_vmalert_instances tvi ON tvi.tenant_id = t.id AND tvi.status = 'deployed'
                   JOIN infrastructure_nodes n ON n.id = tvi.instance_id
                   WHERE t.id = %s""",
                (tenant_id,)
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"No deployed vmalert instance for tenant {tenant_id}")
            if row[2] is None:
                raise ValueError(
                    f"El tenant {tenant_id} no tiene reglas guardadas en la BD. "
                    "Guarde las reglas antes de sincronizar."
                )

            slug, name, rules_yaml, port, service_ip, ip_address, admin_user, admin_pass, t_type, is_internal, account_id, project_id = row

            if t_type == 'profiles':
                logger.info(
                    "Tenant %s es tipo 'profiles' (Pyroscope): no tiene vmalert que sincronizar",
                    tenant_id
                )
                return None
            target_ip = service_ip or ip_address

            datasource_url, remote_write_url = self._resolve_tenant_datasources(
                cursor, {
                    'is_internal': is_internal,
                    'account_id': account_id,
                    'project_id': project_id,
                }
            )

            instances = [{
                'tenant_slug': slug,
                'tenant_name': name,
                'tenant_id': tenant_id,
                'port': port,
                'rules_path': f'/etc/anomalia/vmalert/{slug}',
                'alertmanager_url': self._get_alertmanager_url(cursor),
                'datasource_url': datasource_url,
                'remote_write_url': remote_write_url,
            }]

            # Mismo canal separado que el deploy: el rules_yaml no se serializa
            # dentro de vmalert_instances (spec 011 §4.2).
            vmalert_rules = [{
                'tenant_slug': slug,
                'rules_yaml': rules_yaml,
                'rules_path': f'/etc/anomalia/vmalert/{slug}',
            }]

            conn.commit()
            return {
                'target_ip': target_ip,
                'port': port,
                'admin_user': admin_user,
                'admin_pass': admin_pass,
                'instances': instances,
                'vmalert_rules': vmalert_rules,
            }
        finally:
            cursor.close()
            conn.close()

    def _enqueue_regenerate_scrapes(self, cursor, ref_id: int, components: Optional[list] = None) -> None:
        """Encola un job vmalert_regen_scrapes (spec 011 §4.3/§4.4).

        Sync: se usa dentro de fases de DB que corren en thread (spec 011 §4.2).
        La regeneración es global (refleja el conjunto completo de instancias
        vmalert por tenant), por eso ref_id solo identifica al actor que la
        disparó. ``components`` acota los componentes a regenerar (spec 011
        §4.11): p.ej. ``['vmagent']`` o ``['vmauth']``; None = ambos. El job no
        re-encola nada: la cadena termina naturalmente.
        """
        metadata = Json({'components': components}) if components else None
        cursor.execute(
            """INSERT INTO job_state (id, type, ref_id, status, phase, progress_pct, created_by, payload)
               VALUES (%s, %s, %s, 'queued', 'init', 0, %s, %s);""",
            (str(uuid.uuid4()), 'vmalert_regen_scrapes', ref_id, None, metadata)
        )

    async def _process_vmalert_regen_scrapes(self, job_id: str, ref_id: int, payload: dict):
        """Regenera scrapes de vmagent y rutas de vmauth tras deploy/undeploy por tenant.

        La BD es la fuente de verdad (spec 005): los targets base y los jobs
        por-tenant se calculan a partir de infrastructure_nodes y
        tenant_vmalert_instances, y se inyectan al playbook sync-scrapes.yml
        como extra-vars (modo dirigido, sin inventario con grupos). Sin txn
        abierta durante Ansible (spec 011 §4.2). ``payload['components']`` acota
        qué componentes se regeneran (spec 011 §4.11).
        """
        components = (payload or {}).get('components') or ['vmagent', 'vmauth']
        await self._update_progress(job_id, 10, "collecting_infra")
        data = await asyncio.to_thread(self._regen_scrapes_load, job_id)
        base_ev = data['base_ev']
        vmagent_node = data['vmagent_node']
        vmauth_nodes = data['vmauth_nodes']

        await self._update_progress(job_id, 50, "running_ansible")

        if 'vmagent' in components and vmagent_node is not None:
            ev = dict(base_ev)
            ev.update({
                'regen_components': ['vmagent'],
                'vmagent_remote_write_url': (
                    f"{data['vmauth_url'].rstrip('/')}/insert/prometheus/api/v1/write?tenant=METRICS"
                ),
                'vmagent_ip': vmagent_node['ip'],
                'vmagent_port': vmagent_node['port'],
            })
            await self._run_ansible_vmalert(
                job_id, vmagent_node['target_ip'], vmagent_node['admin_user'],
                vmagent_node['admin_pass'], [], extra_vars=ev,
                playbook="sync-scrapes.yml",
            )

        # Rutas vmauth: cada nodo recibe SOLO los tenants que tiene asignados
        # (`tenants_routing` filtrado por `vmauth_node_id`, spec 011 §4.4). Los
        # tenants sin nodo asignado se publican en todos como fallback defensivo.
        if 'vmauth' in components:
            for vmauth_node in vmauth_nodes:
                ev = dict(base_ev)
                ev.update({
                    'regen_components': ['vmauth'],
                    'vmauth_ip': vmauth_node['ip'],
                    'vmauth_server_port': vmauth_node['port'],
                    'tenants_routing': [
                        tr for tr in base_ev['tenants_routing']
                        if tr.get('vmauth_node_id') in (None, vmauth_node['id'])
                    ],
                })
                await self._run_ansible_vmalert(
                    job_id, vmauth_node['target_ip'], vmauth_node['admin_user'],
                    vmauth_node['admin_pass'], [], extra_vars=ev,
                    playbook="sync-scrapes.yml",
                )

        await self._update_progress(job_id, 100, "done")

    def _regen_scrapes_load(self, job_id: str) -> dict:
        """Fase de lectura del regen_scrapes: transacción corta en thread (spec 011 §4.2)."""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT id, component_type, service_ip, ip_address, port,
                          admin_username, admin_password
                   FROM infrastructure_nodes
                   WHERE is_active = TRUE AND status = 'operational'
                   ORDER BY id ASC"""
            )
            infra_components = []
            vmagent_node = None
            vmauth_nodes = []
            for node_id, comp_type, service_ip, ip_address, port, admin_user, admin_pass in cursor.fetchall():
                component = {
                    'id': node_id,
                    'component': comp_type,
                    'ip': service_ip or ip_address,
                    'port': port or PORT_DEFAULTS.get(comp_type, 8427),
                    'admin_user': admin_user,
                    'admin_pass': admin_pass,
                    'target_ip': service_ip or ip_address,
                }
                infra_components.append(component)
                if comp_type == 'vmagent':
                    vmagent_node = component
                elif comp_type == 'vmauth':
                    vmauth_nodes.append(component)

            if not infra_components:
                raise ValueError("No operational infra components found to regenerate scrapes")

            self._update_progress_sync(job_id, 30, "collecting_tenants")

            cursor.execute(
                """SELECT t.slug, t.name, t.org_id_upper, tvi.port,
                          n.service_ip, n.ip_address, tvi.instance_id, t.id
                   FROM tenant_vmalert_instances tvi
                   JOIN tenants t ON t.id = tvi.tenant_id AND t.status = 'active'
                   JOIN infrastructure_nodes n ON n.id = tvi.instance_id
                   WHERE tvi.status = 'deployed'"""
            )
            vmalert_instances = [
                {
                    'tenant_slug': slug,
                    'tenant_name': name,
                    'org_id_upper': org_id_upper,
                    'port': port,
                    'node_ip': service_ip or ip_address,
                    'instance_id': instance_id,
                    'tenant_id': tenant_id,
                }
                for slug, name, org_id_upper, port, service_ip, ip_address, instance_id, tenant_id in cursor.fetchall()
            ]

            vmauth_url = self._get_component_url(cursor, 'vmauth', PORT_DEFAULTS['vmauth'])

            # Routing por-tenant para vmauth (spec 011 §4.4): tenants activos con
            # su estrategia de aislamiento. metrics/logs/traces por AccountID/
            # ProjectID; profiles por X-Scope-OrgID. Se excluyen los tenants de
            # control interno: sus mapeos fijos ya están en la plantilla de vmauth
            # (tenant=METRICS/TRACES/AUDIT_LOGS y PROFILES) y duplicarlos rompe el
            # primer-match del url_map.
            cursor.execute(
                """SELECT slug, type, org_id_upper, account_id, project_id, vmauth_node_id
                   FROM tenants
                   WHERE deleted_at IS NULL AND status = 'active'
                     AND is_internal = FALSE
                     AND slug IS NOT NULL
                     AND (type = 'profiles' OR (account_id IS NOT NULL AND project_id IS NOT NULL))
                   ORDER BY id ASC"""
            )
            tenants_routing = [
                {
                    'slug': slug,
                    'type': t_type or 'metrics',
                    'org_id_upper': org_id_upper,
                    'account_id': account_id,
                    'project_id': project_id,
                    'vmauth_node_id': vmauth_node_id,
                }
                for slug, t_type, org_id_upper, account_id, project_id, vmauth_node_id in cursor.fetchall()
            ]

            conn.commit()
            return {
                'base_ev': {
                    'infra_components': infra_components,
                    'vmalert_instances': vmalert_instances,
                    'tenants_routing': tenants_routing,
                },
                'vmagent_node': vmagent_node,
                'vmauth_nodes': vmauth_nodes,
                'vmauth_url': vmauth_url,
            }
        finally:
            cursor.close()
            conn.close()

    async def _http_reload_vmalert(self, base_url: str):
        """Healthcheck + POST /-/reload con reintentos y backoff (spec 011).

        El healthcheck usa GET /health: vmalert responde 400 a /-/health
        ("unsupported path requested"); /-/health lo soportan vmauth y
        vmagent, no vmalert.
        """
        last_error: Optional[Exception] = None
        for attempt in range(1, RELOAD_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=RELOAD_TIMEOUT_SECONDS) as client:
                    health = await client.get(f"{base_url}/health")
                    health.raise_for_status()
                    response = await client.post(f"{base_url}/-/reload")
                    response.raise_for_status()
                last_error = None
                break
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                last_error = e
                logger.warning(
                    "vmalert reload attempt %s/%s failed for %s: %s",
                    attempt, RELOAD_ATTEMPTS, base_url, e
                )
                if attempt < RELOAD_ATTEMPTS:
                    await asyncio.sleep(2 * attempt)

        if last_error is not None:
            raise RuntimeError(f"vmalert reload failed after {RELOAD_ATTEMPTS} attempts: {last_error}")

    def _allocate_vmalert_port(self, cursor, node_id: int) -> int:
        """Allocate next available port from node's ports_pool.

        El pool por-tenant arranca en 8881 (spec 011 §4.5): el 8880 está
        reservado para la unit single `anomalia-vmalert.service`.
        """
        cursor.execute(
            "SELECT ports_pool FROM infrastructure_nodes WHERE id=%s",
            (node_id,)
        )
        row = cursor.fetchone()
        ports_pool = row[0] if row and row[0] else list(range(8881, 8980))

        # Find first unused port
        cursor.execute(
            "SELECT port FROM tenant_vmalert_instances WHERE instance_id=%s AND status='deployed'",
            (node_id,)
        )
        used_ports = {r[0] for r in cursor.fetchall()}

        for port in ports_pool:
            if port not in used_ports:
                return port

        # Fallback: max used + 1 (siempre >= 8882 porque 8881 ya está usado)
        return max(used_ports) + 1

    def _get_alertmanager_url(self, cursor) -> str:
        """Get Alertmanager URL from infrastructure."""
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
        return "http://127.0.0.1:9093"

    def _get_component_url(self, cursor, component_type: str, default_port: int) -> str:
        """Get base URL for an infra component from infrastructure_nodes (spec 005).

        Fuente de verdad = BD: devuelve la URL del primer nodo operativo de
        'component_type'; si no existe, cae a http://127.0.0.1:{default_port}.
        """
        cursor.execute(
            """SELECT service_ip, ip_address, port
               FROM infrastructure_nodes
               WHERE component_type = %s
                 AND is_active = TRUE
                 AND status = 'operational'
               LIMIT 1""",
            (component_type,)
        )
        row = cursor.fetchone()
        if row:
            service_ip, ip_address, port = row
            return f"http://{service_ip or ip_address}:{port or default_port}"
        return f"http://127.0.0.1:{default_port}"

    def _resolve_tenant_datasources(self, cursor, tenant: dict) -> tuple[str, str]:
        """Resuelve datasource (vmselect) y remoteWrite (vminsert) cluster URL por-tenant.

        vmselect/vminsert CLUSTER solo aceptan un segmento `<account>:<project>`
        (p. ej. /select/0:0/prometheus); la forma con barra /select/0/0/prometheus
        responde 400 ('unsupported path requested') y las raíces desnudas también
        (spec 008 §2, validado contra el binario):

        - tenants internos (`is_internal`): sus reglas canónicas consultan el store
          METRICS (0:0), donde vmagent deposita las métricas de TODO el stack.
          Aunque TRACES/AUDIT_LOGS tengan project propio (1/3), evalúan contra 0:0.
        - no internos con account_id/project_id: cada uno lee SU proyecto.
        - type='profiles' (Pyroscope): no aplica (sin vmalert); guard aguas arriba.
        - fallback defensivo: 0:0 con warning.

        Devuelve (datasource_url, remote_write_url).
        """
        vmselect = self._get_component_url(cursor, 'vmselect', 8401)
        vminsert = self._get_component_url(cursor, 'vminsert', 8400)
        if tenant.get('is_internal'):
            return (
                vmselect + '/select/0:0/prometheus',
                vminsert + '/insert/0:0/prometheus',
            )
        account_id = tenant.get('account_id')
        project_id = tenant.get('project_id')
        if account_id is not None and project_id is not None:
            return (
                vmselect + f'/select/{account_id}:{project_id}/prometheus',
                vminsert + f'/insert/{account_id}:{project_id}/prometheus',
            )
        logger.warning(
            "Tenant sin account/project (no pyroscope): datasource cae a 0:0. tenant=%s",
            tenant,
        )
        return (
            vmselect + '/select/0:0/prometheus',
            vminsert + '/insert/0:0/prometheus',
        )

    async def _run_ansible_vmalert(self, job_id: str, target_ip: str, admin_user: str, admin_pass: Optional[str],
                                    vmalert_instances: list, extra_vars: dict = None,
                                    vmalert_rules: Optional[list] = None,
                                    playbook: str = "deploy-vmalert.yml"):
        """Run Ansible playbook to deploy/undeploy/sync vmalert.

        Por defecto usa ``deploy-vmalert.yml`` (Perf C, spec 016 §2.7): un playbook
        liviano que solo asegura el binario ``vmalert`` (descarga short-circuit si
        ya existe) y ejecuta el role ``vmalert``, en lugar de ``install-binaries.yml``
        que descarga el stack completo aunque ``servicio=vmalert``. Las reglas y el
        regen de scrapes siguen usando sus playbooks dedicados.

        ``vmalert_instances`` es lo que viaja en los extra-vars al playbook. Las
        reglas (``vmalert_rules``) NO van ahí: contiene ``{{ ... }}`` propio de
        PromQL/vmalert y Ansible las templaria al evaluar cualquier ``when`` que
        referencie la variable (spec 011 §4.2). Se materializan a archivos
        temporales y los extra-vars solo llevan las rutas.
        """
        playbook_path = f"/app/playbooks/{playbook}"
        if not os.path.isfile(playbook_path):
            raise FileNotFoundError(f"Playbook not found: {playbook_path}")

        # Prepare extra vars
        ev = {
            'ansible_user': admin_user,
            'servicio': 'vmalert',
            'gestionar_servicios': True,
            'vmalert_instances': vmalert_instances,
        }
        if extra_vars:
            ev.update(extra_vars)
        if admin_pass:
            ev['ansible_password'] = admin_pass
            ev['ansible_become_password'] = admin_pass

        # Write extra vars to temp file (secure)
        secret_dir = tempfile.mkdtemp(prefix="anomalia-ansible-vmalert-")
        extra_vars_path = os.path.join(secret_dir, "extra-vars.json")
        try:
            # Materializar reglas guardadas en la BD a archivos temporales.
            # NO se usa copy.content en los playbooks: prometheus templating con
            # {{ ... }} (labels de alerta) sería evaluado por Jinja y corrupto.
            rule_files = []
            for rule in (vmalert_rules or []):
                rules_yaml = rule.get('rules_yaml')
                if rules_yaml is not None:
                    rule_src = os.path.join(secret_dir, f"rules-{rule.get('tenant_slug', 'tenant')}.yml")
                    fd = os.open(rule_src, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as rf:
                        rf.write(rules_yaml)
                    rule_files.append({
                        'src': rule_src,
                        'dest': os.path.join(rule['rules_path'], 'alert_rules.yml'),
                        'rules_path': rule['rules_path'],
                    })
            if rule_files:
                ev['vmalert_rules_files'] = rule_files

            fd = os.open(extra_vars_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(ev, f)

            cmd = [
                "ansible-playbook",
                "-i", f"{target_ip},",
                playbook_path,
                "--extra-vars", f"@{extra_vars_path}",
            ]

            logger.info("Running vmalert playbook for job %s on %s", job_id, target_ip)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd="/app/playbooks",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=VMALERT_PLAYBOOK_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise TimeoutError(
                    f"vmalert playbook timeout ({VMALERT_PLAYBOOK_TIMEOUT_SECONDS}s)"
                )

            if proc.returncode != 0:
                error_msg = f"STDOUT:\n{stdout.decode()}\n\nSTDERR:\n{stderr.decode()}"
                raise RuntimeError(f"vmalert playbook failed: {error_msg}")

            logger.info("vmalert playbook completed for job %s", job_id)

        finally:
            # Cleanup temp files (extra vars + reglas materializadas)
            try:
                shutil.rmtree(secret_dir, ignore_errors=True)
            except Exception:
                pass

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
