from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from fastapi.responses import JSONResponse
from typing import List, Optional
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
import psycopg2
import socket
import re
import yaml
from ..database import get_db_connection
from ..models import (
    UserCreateRequest, 
    UserUpdateRequest, 
    RoleCreateUpdatePayload, 
    ProfileCreateUpdatePayload, 
    TenantPayload,
    AuthNodePayload,
    DeleteTenantConfirm1,
    DeleteTenantConfirm2,
    VMAlerterRulesPut,
    JobStateOut,
    AlertRecipientCreate,
    AlertRecipientUpdate
)
from ..auth import verify_admin_token, hash_password, get_current_user_profiles, verify_profile_access, is_valid_email_format

router = APIRouter(prefix="/api/v1/admin", tags=["Administration"])

logger = logging.getLogger(__name__)


def slugify_name(name: str) -> str:
    sname = re.sub(r'[^a-zA-Z0-9]+', '-', name.lower())
    sname = re.sub(r'-+', '-', sname).strip('-')
    return sname or 'tenant'

def org_id_upper_from_slug(slug: str) -> str:
    # UPPER, sin espacios/caracteres especiales (solo A-Z0-9_-)
    su = re.sub(r'[^A-Z0-9_-]+', '', slug.upper())
    return su or 'TENANT'

def ensure_slug_unique(cursor, base_slug: str) -> str:
    slug = base_slug
    i = 1
    while True:
        cursor.execute("SELECT 1 FROM tenants WHERE slug = %s;", (slug,))
        if not cursor.fetchone():
            return slug
        i += 1
        slug = f"{base_slug}-{i}"

# Componentes que ansible-infra/install-binaries.yml sabe actualizar. Cada valor
# debe coincidir con el 'servicio' aceptado por el playbook; 'parses' y 'backend'
# quedan fuera a propósito porque se gestionan solo con el playbook de despliegue.
ANSIBLE_UPDATABLE_COMPONENTS = frozenset({
    "vlstorage", "vlinsert", "vlselect",
    "vmstorage", "vminsert", "vmselect",
    "vtstorage", "vtinsert", "vtselect",
    "vmagent", "vmauth", "vmalert",
    "pyroscope", "alertmanager",
})

@router.get("/catalogs", dependencies=[Depends(verify_any_user_token := verify_profile_access("users_manager"))])
def get_catalogs():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT name, description FROM roles ORDER BY id ASC;")
        roles = [{"name": r[0], "description": r[1]} for r in cursor.fetchall()]

        cursor.execute("SELECT id, name, slug, type, environment, port, description, org_id_upper, status, instance_id, has_alerts, placement_mode, is_internal FROM tenants ORDER BY id ASC;")
        tenants = [{
            "id": t[0], "name": t[1], "slug": t[2], "type": t[3],
            "environment": t[4], "port": t[5], "description": t[6],
            "org_id_upper": t[7], "status": t[8], "instance_id": t[9],
            "has_alerts": t[10], "placement_mode": t[11], "is_internal": t[12]
        } for t in cursor.fetchall()]

        cursor.execute("SELECT id, code, name, description FROM profiles ORDER BY id ASC;")
        profiles = [{
            "id": p[0], "code": p[1], "name": p[2], "description": p[3]
        } for p in cursor.fetchall()]

        return {"roles": roles, "tenants": tenants, "profiles": profiles}
    finally:
        cursor.close()
        conn.close()

# --- USERS (users_manager) ---
@router.get("/users", dependencies=[Depends(verify_profile_access("users_manager"))])
def get_users():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, username, is_active FROM users ORDER BY id ASC;")
        user_rows = cursor.fetchall()
        users = []
        for ur in user_rows:
            uid, uname, is_act = ur[0], ur[1], ur[2]
            
            cursor.execute("SELECT r.name FROM user_roles sub JOIN roles r ON sub.role_id = r.id WHERE sub.user_id = %s;", (uid,))
            uroles = [row[0] for row in cursor.fetchall()]

            cursor.execute("SELECT t.name FROM user_tenants sub JOIN tenants t ON sub.tenant_id = t.id WHERE sub.user_id = %s;", (uid,))
            utenants = [row[0] for row in cursor.fetchall()]

            users.append({
                "id": uid,
                "username": uname,
                "is_active": bool(is_act),
                "roles": uroles,
                "tenants": utenants
            })
        return {"users": users}
    finally:
        cursor.close()
        conn.close()

@router.post("/users", dependencies=[Depends(verify_profile_access("users_manager"))])
def create_user(payload: UserCreateRequest):
    if not is_valid_email_format(payload.username):
        raise HTTPException(status_code=400, detail="Formato de email invalido.")
    if payload.password_confirm and payload.password != payload.password_confirm:
        raise HTTPException(status_code=400, detail="Las contraseñas no coinciden.")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        hashed_pw = hash_password(payload.password)
        cursor.execute(
            "INSERT INTO users (username, password_hash, is_active) VALUES (%s, %s, %s) RETURNING id;",
            (payload.username, hashed_pw, True if payload.is_active else False)
        )
        user_id = cursor.fetchone()[0]

        if payload.roles:
            for r_name in payload.roles:
                cursor.execute("SELECT id FROM roles WHERE name = %s;", (r_name,))
                role_row = cursor.fetchone()
                if role_row:
                    cursor.execute("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s);", (user_id, role_row[0]))

        if payload.tenants:
            for t_name in payload.tenants:
                cursor.execute("SELECT id FROM tenants WHERE name = %s;", (t_name,))
                tenant_row = cursor.fetchone()
                if tenant_row:
                    cursor.execute("INSERT INTO user_tenants (user_id, tenant_id) VALUES (%s, %s);", (user_id, tenant_row[0]))

        conn.commit()
        return {"status": "success", "message": "Usuario creado correctamente", "id": user_id}
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=400, detail="El nombre de usuario ya existe.")
    finally:
        cursor.close()
        conn.close()

@router.put("/users/{user_id}", dependencies=[Depends(verify_profile_access("users_manager"))])
def update_user(user_id: int, payload: UserUpdateRequest):
    if payload.username is not None and not is_valid_email_format(payload.username):
        raise HTTPException(status_code=400, detail="Formato de email invalido.")
    if payload.password and payload.password_confirm and payload.password != payload.password_confirm:
        raise HTTPException(status_code=400, detail="Las contraseñas no coinciden.")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM users WHERE id = %s;", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Usuario no encontrado.")
        
        if payload.username is not None:
            cursor.execute("UPDATE users SET username = %s WHERE id = %s;", (payload.username, user_id))
        if payload.is_active is not None:
            cursor.execute("UPDATE users SET is_active = %s WHERE id = %s;", (True if payload.is_active else False, user_id))
        if payload.password:
            hashed_pw = hash_password(payload.password)
            cursor.execute("UPDATE users SET password_hash = %s WHERE id = %s;", (hashed_pw, user_id))

        if payload.roles is not None:
            cursor.execute("DELETE FROM user_roles WHERE user_id = %s;", (user_id,))
            for r_name in payload.roles:
                cursor.execute("SELECT id FROM roles WHERE name = %s;", (r_name,))
                role_row = cursor.fetchone()
                if role_row:
                    cursor.execute("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s);", (user_id, role_row[0]))

        if payload.tenants is not None:
            cursor.execute("DELETE FROM user_tenants WHERE user_id = %s;", (user_id,))
            for t_name in payload.tenants:
                cursor.execute("SELECT id FROM tenants WHERE name = %s;", (t_name,))
                tenant_row = cursor.fetchone()
                if tenant_row:
                    cursor.execute("INSERT INTO user_tenants (user_id, tenant_id) VALUES (%s, %s);", (user_id, tenant_row[0]))

        conn.commit()
        return {"status": "success", "message": "Usuario actualizado correctamente"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@router.delete("/users/{user_id}", dependencies=[Depends(verify_profile_access("users_manager"))])
def delete_user(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM user_roles WHERE user_id = %s;", (user_id,))
        cursor.execute("DELETE FROM user_tenants WHERE user_id = %s;", (user_id,))
        cursor.execute("DELETE FROM users WHERE id = %s;", (user_id,))
        conn.commit()
        return {"status": "success", "message": "Usuario eliminado correctamente"}
    finally:
        cursor.close()
        conn.close()

# --- TENANTS (tenants_manager) ---
@router.get("/tenants", dependencies=[Depends(verify_profile_access("tenants_manager"))])
def get_tenants_admin():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # spec 011: la UI necesita slug (paso 2 del hard-delete) y el estado de
        # la instancia vmalert para mostrar la consola del tenant.
        cursor.execute(
            """SELECT id, name, type, account_id, project_id, environment, port, description,
                      slug, display_name, org_id_upper, status, instance_id, vmalert_node_id,
                      vmalert_port, has_alerts, placement_mode, is_internal, vmauth_node_id, vmauth_placement_mode
               FROM tenants ORDER BY id ASC;"""
        )
        rows = cursor.fetchall()

        cursor.execute(
            """SELECT id, service_ip, ip_address, port
               FROM infrastructure_nodes
               WHERE component_type = 'vmauth'
                 AND is_active = TRUE
                 AND status = 'operational'
               ORDER BY tenants_count_active ASC, id ASC"""
        )
        vmauth_nodes = cursor.fetchall()

        def resolve_vmauth_node(tenant_vmauth_node_id):
            if tenant_vmauth_node_id:
                for n in vmauth_nodes:
                    if n[0] == tenant_vmauth_node_id:
                        return n
            return vmauth_nodes[0] if vmauth_nodes else None

        def build_ingest_url(node, t_type, slug):
            if not node or not slug:
                return None
            ip = node[1] or node[2] or '127.0.0.1'
            port = node[3] or 8427
            base = f"http://{ip}:{port}"
            if t_type == 'profiles':
                return f"{base}/ingest?tenant={slug}"
            if t_type == 'logs':
                return f"{base}/insert/loki/api/v1/push?tenant={slug}"
            elif t_type == 'traces':
                return f"{base}/insert/opentelemetry/v1/traces?tenant={slug}"
            else:
                return f"{base}/insert/opentelemetry/v1/metrics?tenant={slug}"

        tenants = []
        for r in rows:
            node = resolve_vmauth_node(r[18])
            ingest_url = build_ingest_url(node, r[2], r[8])
            tenants.append({
                "id": r[0], "name": r[1], "type": r[2],
                "account_id": r[3], "project_id": r[4],
                "environment": r[5], "port": r[6], "description": r[7],
                "slug": r[8], "display_name": r[9], "org_id_upper": r[10],
                "status": r[11], "instance_id": r[12], "vmalert_node_id": r[13],
                "vmalert_port": r[14], "has_alerts": r[15], "placement_mode": r[16],
                "is_internal": r[17], "vmauth_node_id": r[18], "vmauth_placement_mode": r[19],
                "vmauth_ingest_url": ingest_url
            })
        return {"tenants": tenants}
    finally:
        cursor.close()
        conn.close()


@router.get("/infra/vmalert-nodes", dependencies=[Depends(verify_profile_access("tenants_manager"))])
def get_vmalert_nodes():
    """Obtiene nodos de infraestructura con component_type='vmalert' para selector en alta de tenant."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """SELECT id, hostname, ip_address, service_ip, port, capacity_slots, tenants_count_active
               FROM infrastructure_nodes
               WHERE component_type = 'vmalert'
                 AND is_active = TRUE
                 AND status = 'operational'
               ORDER BY tenants_count_active ASC, id ASC"""
        )
        rows = cursor.fetchall()
        nodes = []
        for r in rows:
            nodes.append({
                "id": r[0],
                "hostname": r[1],
                "ip_address": r[2],
                "service_ip": r[3],
                "port": r[4],
                "capacity_slots": r[5],
                "tenants_count_active": r[6],
                "available_slots": max(0, r[5] - r[6]),
            })
        return {"nodes": nodes}
    finally:
        cursor.close()
        conn.close()


@router.get("/infra/vmauth-nodes", dependencies=[Depends(verify_profile_access("tenants_manager"))])
def get_vmauth_nodes():
    """Obtiene nodos de infraestructura con component_type='vmauth' para selector en alta de tenant."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """SELECT id, hostname, ip_address, service_ip, port, capacity_slots, tenants_count_active
               FROM infrastructure_nodes
               WHERE component_type = 'vmauth'
                 AND is_active = TRUE
                 AND status = 'operational'
               ORDER BY tenants_count_active ASC, id ASC"""
        )
        rows = cursor.fetchall()
        nodes = []
        for r in rows:
            nodes.append({
                "id": r[0],
                "hostname": r[1],
                "ip_address": r[2],
                "service_ip": r[3],
                "port": r[4],
                "capacity_slots": r[5],
                "tenants_count_active": r[6],
                "available_slots": max(0, r[5] - r[6]),
            })
        return {"nodes": nodes}
    finally:
        cursor.close()
        conn.close()


@router.get("/alerts/tenants", dependencies=[Depends(verify_profile_access("alerts_manager"))])
def get_alerts_tenants(mine: bool = False, wait_deployed: bool = False, payload: dict = Depends(verify_any_user_token)):
    """Listado minimo para la tarjeta de consolas de alertas.

    Solo lectura y con el perfil `alerts_manager`, que no tiene por que tener
    `tenants_manager`. No expone account_id ni project_id.

    Excluye el tenant interno `profiles` (Pyroscope), que nunca tiene vmalert
    (spec 011 §4.6/§4.9) pero nace con has_alerts=true por el seed interno.

    Si mine=True, filtra por tenants asignados al usuario actual (user_tenants + role_tenants).
    Si wait_deployed=True, espera hasta que todos los tenants activos con alertas
    esten deployed o hasta que expire el timeout (600s).
    """
    import time

    username = payload.get("sub") or payload.get("username")
    where_clause = "WHERE t.deleted_at IS NULL AND t.type <> 'profiles'"
    params = []

    if mine and username:
        where_clause += """
            AND t.id IN (
                SELECT ut.tenant_id FROM user_tenants ut JOIN users u ON u.id = ut.user_id WHERE u.username = %s
                UNION
                SELECT rt.tenant_id FROM role_tenants rt
                JOIN user_roles ur ON ur.role_id = rt.role_id
                JOIN users ru ON ru.id = ur.user_id
                WHERE ru.username = %s
            )
        """
        params.extend([username, username])

    query_sql = (
        f"""SELECT t.id, t.name, t.slug, t.org_id_upper, t.status, t.instance_id,
                   t.vmalert_port, t.has_alerts,
                   EXISTS (SELECT 1 FROM tenant_vmalert_instances i
                           WHERE i.tenant_id = t.id AND i.status = 'deployed') AS deployed,
                   j.status, j.phase, j.progress_pct
            FROM tenants t
            LEFT JOIN LATERAL (
                SELECT status, phase, progress_pct
                FROM job_state
                WHERE ref_id = t.id
                  AND type IN ('vmalert_deploy', 'vmalert_redeploy', 'vmalert_undeploy')
                ORDER BY created_at DESC
                LIMIT 1
            ) j ON TRUE
            {where_clause}
            ORDER BY t.id ASC;"""
    )

    def _fetch_rows():
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(query_sql, tuple(params))
            return cursor.fetchall()
        finally:
            cursor.close()
            conn.close()

    def _has_pending(rows):
        for r in rows:
            is_active = r[4] == 'active'
            has_alerts = r[7]
            deployed = r[8]
            if is_active and has_alerts and not deployed:
                return True
        return False

    timeout_sec = 600
    deadline = time.monotonic() + timeout_sec
    rows = _fetch_rows()
    while wait_deployed and _has_pending(rows) and time.monotonic() < deadline:
        time.sleep(3)
        rows = _fetch_rows()

    return {"tenants": [
        {
            "id": r[0], "name": r[1], "slug": r[2], "org_id_upper": r[3],
            "status": r[4], "instance_id": r[5], "vmalert_port": r[6],
            "has_alerts": r[7], "vmalert_deployed": r[8],
            "vmalert_job_status": r[9], "vmalert_job_phase": r[10],
            "vmalert_job_progress": r[11]
        } for r in rows
    ]}

# --- DESTINATARIOS DE ALERTAS POR TENANT (spec 018, alerts_manager) ---

def _assert_tenant_exists(cursor, tenant_id: int) -> None:
    cursor.execute("SELECT 1 FROM tenants WHERE id = %s AND deleted_at IS NULL", (tenant_id,))
    if not cursor.fetchone():
        raise HTTPException(status_code=404, detail="Tenant no encontrado.")


@router.get("/alerts/tenants/{tenant_id}/recipients",
            dependencies=[Depends(verify_profile_access("alerts_manager"))])
def list_alert_recipients(tenant_id: int):
    """Lista los correos destinatarios de alertas del tenant (spec 018)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        _assert_tenant_exists(cursor, tenant_id)
        cursor.execute(
            """SELECT id, email, label, enabled, created_at
               FROM tenant_alert_recipients
               WHERE tenant_id = %s ORDER BY id""",
            (tenant_id,),
        )
        return {"recipients": [
            {
                "id": r[0], "email": r[1], "label": r[2] or "",
                "enabled": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
            }
            for r in cursor.fetchall()
        ]}
    finally:
        cursor.close()
        conn.close()


@router.post("/alerts/tenants/{tenant_id}/recipients",
             dependencies=[Depends(verify_profile_access("alerts_manager"))])
def add_alert_recipient(tenant_id: int, body: AlertRecipientCreate):
    """Añade un correo destinatario de alertas al tenant (spec 018)."""
    email = (body.email or "").strip()
    if not is_valid_email_format(email):
        raise HTTPException(status_code=400, detail="Formato de correo inválido.")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        _assert_tenant_exists(cursor, tenant_id)
        cursor.execute(
            """INSERT INTO tenant_alert_recipients (tenant_id, email, label, enabled)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (tenant_id, email) DO UPDATE SET
                   label = EXCLUDED.label,
                   enabled = EXCLUDED.enabled
               RETURNING id""",
            (tenant_id, email, (body.label or "").strip(), bool(body.enabled)),
        )
        recipient_id = cursor.fetchone()[0]
        conn.commit()
        return {"ok": True, "id": recipient_id}
    finally:
        cursor.close()
        conn.close()


@router.put("/alerts/tenants/{tenant_id}/recipients/{recipient_id}",
            dependencies=[Depends(verify_profile_access("alerts_manager"))])
def update_alert_recipient(tenant_id: int, recipient_id: int, body: AlertRecipientUpdate):
    """Actualiza correo/etiqueta/estado de un destinatario (spec 018)."""
    email = None
    if body.email is not None:
        email = body.email.strip()
        if not is_valid_email_format(email):
            raise HTTPException(status_code=400, detail="Formato de correo inválido.")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT 1 FROM tenant_alert_recipients WHERE id = %s AND tenant_id = %s",
            (recipient_id, tenant_id),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Destinatario no encontrado.")

        updates = []
        params: List = []
        if email is not None:
            updates.append("email = %s")
            params.append(email)
        if body.label is not None:
            updates.append("label = %s")
            params.append(body.label.strip())
        if body.enabled is not None:
            updates.append("enabled = %s")
            params.append(bool(body.enabled))
        if updates:
            params.extend([recipient_id, tenant_id])
            cursor.execute(
                f"UPDATE tenant_alert_recipients SET {', '.join(updates)} "
                "WHERE id = %s AND tenant_id = %s",
                params,
            )
        conn.commit()
        return {"ok": True}
    finally:
        cursor.close()
        conn.close()


@router.delete("/alerts/tenants/{tenant_id}/recipients/{recipient_id}",
               dependencies=[Depends(verify_profile_access("alerts_manager"))])
def delete_alert_recipient(tenant_id: int, recipient_id: int):
    """Elimina un destinatario de alertas del tenant (spec 018)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM tenant_alert_recipients WHERE id = %s AND tenant_id = %s",
            (recipient_id, tenant_id),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Destinatario no encontrado.")
        conn.commit()
        return {"ok": True}
    finally:
        cursor.close()
        conn.close()


@router.post("/tenants", dependencies=[Depends(verify_profile_access("tenants_manager"))])
def create_tenant(payload: TenantPayload):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        base_slug = slugify_name(payload.display_name or payload.name)
        slug = ensure_slug_unique(cursor, base_slug)
        org_id_upper = org_id_upper_from_slug(slug)
        display_name = payload.display_name or payload.name
        placement_mode = payload.placement_mode or "manual"
        vmauth_placement_mode = payload.vmauth_placement_mode or "manual"

        # Normalizar has_alerts según tipo
        tenant_type = payload.type or "metrics"
        if tenant_type == "profiles":
            has_alerts = False
        else:
            has_alerts = payload.has_alerts if payload.has_alerts is not None else True

        # Si placement_mode=auto y no se proporciona vmalert_node_id,
        # el worker lo asignará automáticamente (dejar NULL por ahora)
        vmalert_node_id = payload.vmalert_node_id
        if placement_mode != "manual":
            vmalert_node_id = None
        if tenant_type == "profiles":
            vmalert_node_id = None

        vmauth_node_id = payload.vmauth_node_id
        if vmauth_placement_mode != "manual":
            vmauth_node_id = None

        cursor.execute(
            """INSERT INTO tenants (name, slug, display_name, type, account_id, project_id, environment, port, description, has_alerts, placement_mode, vmalert_node_id, vmalert_port, org_id_upper, status, vmauth_node_id, vmauth_placement_mode) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;""",
            (payload.name, slug, display_name, tenant_type, payload.account_id, payload.project_id, payload.environment, payload.port, payload.description, has_alerts, placement_mode, vmalert_node_id, payload.vmalert_port, org_id_upper, "provisioning", vmauth_node_id, vmauth_placement_mode)
        )
        tenant_id = cursor.fetchone()[0]
        conn.commit()

        # Auto-grant access to the newly created tenant to the admin role (and tenant_manager if applicable)
        # This keeps parity with the seed logic (role_tenants) and prevents permission issues for admins.
        try:
            cursor.execute(
                "INSERT INTO role_tenants (role_id, tenant_id) SELECT r.id, %s FROM roles r WHERE r.name = 'admin' AND NOT EXISTS (SELECT 1 FROM role_tenants rt WHERE rt.role_id = r.id AND rt.tenant_id = %s);",
                (tenant_id, tenant_id)
            )
            env = payload.environment or 'default'
            cursor.execute(
                "INSERT INTO role_tenants (role_id, tenant_id) SELECT r.id, %s FROM roles r WHERE r.name = 'tenant_manager' AND (%s IN ('work','default')) AND NOT EXISTS (SELECT 1 FROM role_tenants rt WHERE rt.role_id = r.id AND rt.tenant_id = %s);",
                (tenant_id, env, tenant_id)
            )
            conn.commit()
        except Exception:
            # Granting is best-effort; don't block tenant creation if it fails
            conn.rollback()
            # Re-fetch connection is not necessary as we just committed - continue

        job_id = str(uuid.uuid4())
        cursor.execute("""INSERT INTO job_state (id, type, ref_id, status, phase, progress_pct, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s);""", (job_id, "tenant_create", tenant_id, "queued", "init", 0, None))
        conn.commit()
        return {"status": "accepted", "message": "Creación de tenant en curso", "id": tenant_id, "job_id": job_id}
    except HTTPException:
        # Los HTTPException de este bloque ya llevan su status y su detalle.
        # Sin esta rama el 'except Exception' generico los reenvuelve en un 400
        # y duplica el mensaje ('400: 404: Tenant no encontrado').
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@router.put("/tenants/{tenant_id}", dependencies=[Depends(verify_profile_access("tenants_manager"))])
def update_tenant(tenant_id: int, payload: TenantPayload):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        _guard_internal_tenant(cursor, tenant_id, "Actualizar el tenant")
        tenant_type = payload.type or "metrics"
        if tenant_type == "profiles":
            has_alerts = False
        else:
            has_alerts = payload.has_alerts if payload.has_alerts is not None else True
        placement_mode = payload.placement_mode or "manual"
        vmauth_placement_mode = payload.vmauth_placement_mode or "manual"
        vmalert_node_id = payload.vmalert_node_id
        if placement_mode != "manual":
            vmalert_node_id = None
        if tenant_type == "profiles":
            vmalert_node_id = None
        vmauth_node_id = payload.vmauth_node_id
        if vmauth_placement_mode != "manual":
            vmauth_node_id = None
        cursor.execute(
            "UPDATE tenants SET name = %s, display_name = %s, type = %s, account_id = %s, project_id = %s, environment = %s, port = %s, description = %s, has_alerts = %s, placement_mode = %s, vmalert_node_id = %s, vmalert_port = %s, vmauth_node_id = %s, vmauth_placement_mode = %s WHERE id = %s;",
            (payload.name, payload.display_name or payload.name, tenant_type, payload.account_id, payload.project_id, payload.environment, payload.port, payload.description, has_alerts, placement_mode, vmalert_node_id, payload.vmalert_port, vmauth_node_id, vmauth_placement_mode, tenant_id)
        )
        conn.commit()
        return {"status": "success", "message": "Tenant actualizado correctamente"}
    except HTTPException:
        # Los HTTPException de este bloque ya llevan su status y su detalle.
        # Sin esta rama el 'except Exception' generico los reenvuelve en un 400
        # y duplica el mensaje ('400: 404: Tenant no encontrado').
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@router.delete("/tenants/{tenant_id}", dependencies=[Depends(verify_profile_access("tenants_manager"))])
def delete_tenant(tenant_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        _guard_internal_tenant(cursor, tenant_id, "Eliminar el tenant")
        cursor.execute("DELETE FROM user_tenants WHERE tenant_id = %s;", (tenant_id,))
        cursor.execute("DELETE FROM role_tenants WHERE tenant_id = %s;", (tenant_id,))
        cursor.execute("DELETE FROM tenants WHERE id = %s;", (tenant_id,))
        conn.commit()
        return {"status": "success", "message": "Tenant eliminado correctamente"}
    finally:
        cursor.close()
        conn.close()

def _guard_internal_tenant(cursor, tenant_id: int, action: str):
    """Bloquea operaciones destructivas sobre tenants de control interno
    (spec 011 §4.7). Las reglas internas son inmutables por diseño."""
    cursor.execute("SELECT is_internal FROM tenants WHERE id = %s;", (tenant_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    if row[0]:
        raise HTTPException(
            status_code=409,
            detail=f"{action}: el tenant es de control interno y no admite modificaciones.",
        )

def _guard_non_profiles_tenant(cursor, tenant_id: int, action: str):
    """Bloquea operaciones vmalert sobre tenants pyroscope (type='profiles').

    Los perfiles no son series PromQL consultables por vmalert (spec 011 §4.6):
    estos tenants no tienen instancia vmalert, por lo que deploy/redeploy no
    tienen sentido y se rechazan con 400 en lugar de encolar un job que falla.
    """
    cursor.execute("SELECT type FROM tenants WHERE id = %s;", (tenant_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    if row[0] == 'profiles':
        raise HTTPException(
            status_code=400,
            detail=f"{action}: el tenant es tipo 'profiles' (Pyroscope) y no lleva "
                   "instancia vmalert (los perfiles no son consultables por PromQL).",
        )

# --- ROLES (roles_manager) ---
@router.get("/roles", dependencies=[Depends(verify_profile_access("roles_manager"))])
def get_roles_admin():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, name, description FROM roles ORDER BY id ASC;")
        rows = cursor.fetchall()
        roles = []
        for r in rows:
            cursor.execute("""
                SELECT t.name FROM role_tenants rt JOIN tenants t ON rt.tenant_id = t.id WHERE rt.role_id = %s;
            """, (r[0],))
            r_tenants = [t[0] for t in cursor.fetchall()]

            cursor.execute("""
                SELECT p.code FROM role_profiles rp JOIN profiles p ON rp.profile_id = p.id WHERE rp.role_id = %s;
            """, (r[0],))
            r_profiles = [p[0] for p in cursor.fetchall()]

            roles.append({
                "id": r[0], "name": r[1], "description": r[2],
                "tenants": r_tenants, "profiles": r_profiles
            })
        return {"roles": roles}
    finally:
        cursor.close()
        conn.close()

@router.post("/roles", dependencies=[Depends(verify_profile_access("roles_manager"))])
def create_role(payload: RoleCreateUpdatePayload):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO roles (name, description) VALUES (%s, %s) RETURNING id;", (payload.name, payload.description))
        role_id = cursor.fetchone()[0]

        if payload.tenants:
            for t_name in payload.tenants:
                cursor.execute("SELECT id FROM tenants WHERE name = %s;", (t_name,))
                t_row = cursor.fetchone()
                if t_row:
                    cursor.execute("INSERT INTO role_tenants (role_id, tenant_id) VALUES (%s, %s);", (role_id, t_row[0]))

        if payload.profiles:
            for p_code in payload.profiles:
                cursor.execute("SELECT id FROM profiles WHERE code = %s;", (p_code,))
                p_row = cursor.fetchone()
                if p_row:
                    cursor.execute("INSERT INTO role_profiles (role_id, profile_id) VALUES (%s, %s);", (role_id, p_row[0]))

        conn.commit()
        return {"status": "success", "message": "Rol creado correctamente", "id": role_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@router.put("/roles/{role_id}", dependencies=[Depends(verify_profile_access("roles_manager"))])
def update_role(role_id: int, payload: RoleCreateUpdatePayload):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE roles SET name = %s, description = %s WHERE id = %s;", (payload.name, payload.description, role_id))
        
        cursor.execute("DELETE FROM role_tenants WHERE role_id = %s;", (role_id,))
        if payload.tenants:
            for t_name in payload.tenants:
                cursor.execute("SELECT id FROM tenants WHERE name = %s;", (t_name,))
                t_row = cursor.fetchone()
                if t_row:
                    cursor.execute("INSERT INTO role_tenants (role_id, tenant_id) VALUES (%s, %s);", (role_id, t_row[0]))

        cursor.execute("DELETE FROM role_profiles WHERE role_id = %s;", (role_id,))
        if payload.profiles:
            for p_code in payload.profiles:
                cursor.execute("SELECT id FROM profiles WHERE code = %s;", (p_code,))
                p_row = cursor.fetchone()
                if p_row:
                    cursor.execute("INSERT INTO role_profiles (role_id, profile_id) VALUES (%s, %s);", (role_id, p_row[0]))

        conn.commit()
        return {"status": "success", "message": "Rol actualizado correctamente"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@router.delete("/roles/{role_id}", dependencies=[Depends(verify_profile_access("roles_manager"))])
def delete_role(role_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM user_roles WHERE role_id = %s;", (role_id,))
        cursor.execute("DELETE FROM role_tenants WHERE role_id = %s;", (role_id,))
        cursor.execute("DELETE FROM role_profiles WHERE role_id = %s;", (role_id,))
        cursor.execute("DELETE FROM roles WHERE id = %s;", (role_id,))
        conn.commit()
        return {"status": "success", "message": "Rol eliminado correctamente"}
    finally:
        cursor.close()
        conn.close()

# --- PROFILES (profile_manager) ---
@router.get("/profiles", dependencies=[Depends(verify_profile_access("profile_manager"))])
def get_profiles_admin():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, code, name, description FROM profiles ORDER BY id ASC;")
        rows = cursor.fetchall()
        profiles = [{"id": p[0], "code": p[1], "name": p[2], "description": p[3]} for p in rows]
        return {"profiles": profiles}
    finally:
        cursor.close()
        conn.close()

@router.post("/profiles", dependencies=[Depends(verify_profile_access("profile_manager"))])
def create_profile(payload: ProfileCreateUpdatePayload):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        code = payload.code if payload.code else payload.name.lower().replace(" ", "_")
        cursor.execute("INSERT INTO profiles (code, name, description) VALUES (%s, %s, %s) RETURNING id;", (code, payload.name, payload.description))
        profile_id = cursor.fetchone()[0]
        conn.commit()
        return {"status": "success", "message": "Perfil creado correctamente", "id": profile_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@router.put("/profiles/{profile_id}", dependencies=[Depends(verify_profile_access("profile_manager"))])
def update_profile(profile_id: int, payload: ProfileCreateUpdatePayload):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        code = payload.code if payload.code else payload.name.lower().replace(" ", "_")
        cursor.execute("UPDATE profiles SET code = %s, name = %s, description = %s WHERE id = %s;", (code, payload.name, payload.description, profile_id))
        conn.commit()
        return {"status": "success", "message": "Perfil actualizado correctamente"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@router.delete("/profiles/{profile_id}", dependencies=[Depends(verify_profile_access("profile_manager"))])
def delete_profile(profile_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM role_profiles WHERE profile_id = %s;", (profile_id,))
        cursor.execute("DELETE FROM profiles WHERE id = %s;", (profile_id,))
        conn.commit()
        return {"status": "success", "message": "Perfil eliminado correctamente"}
    finally:
        cursor.close()
        conn.close()

# --- INFRASTRUCTURE (infra_manager) ---
@router.get("/infra/component-types", dependencies=[Depends(verify_profile_access("infra_manager"))])
def get_component_types():
    """Catálogo de tipos de nodo de infraestructura.

    'value' es el identificador que se persiste en infrastructure_nodes.component_type
    y debe coincidir con los 'type' que siembra roles/backend/templates/init.sql.j2 y
    con los 'servicio' que acepta ansible-infra/install-binaries.yml. 'updatable'
    indica si existe una sección del playbook capaz de reinstalar el componente.
    """
    catalog = [
        ("vlstorage", "VictoriaLogs Storage"),
        ("vlinsert", "VictoriaLogs Insert"),
        ("vlselect", "VictoriaLogs Select"),
        ("vmstorage", "VictoriaMetrics Storage"),
        ("vminsert", "VictoriaMetrics Insert"),
        ("vmselect", "VictoriaMetrics Select"),
        ("vtstorage", "VictoriaTraces Storage"),
        ("vtinsert", "VictoriaTraces Insert"),
        ("vtselect", "VictoriaTraces Select"),
        ("vmagent", "VMAgent"),
        ("vmauth", "VMAuth"),
        ("vmalert", "VMAlert"),
        ("pyroscope", "Pyroscope"),
        ("alertmanager", "Alertmanager"),
        ("parses", "Parses"),
        ("backend", "Backend"),
    ]
    return {
        "component_types": [
            {
                "value": value,
                "label": label,
                "updatable": value in ANSIBLE_UPDATABLE_COMPONENTS,
            }
            for value, label in catalog
        ]
    }

@router.get("/infra", dependencies=[Depends(verify_profile_access("infra_manager"))])
def get_infra_nodes():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, hostname, ip_address, service_ip, component_type, port, status, description, updated_at FROM infrastructure_nodes ORDER BY id ASC;")
        rows = cursor.fetchall()
        nodes = []
        for r in rows:
            nodes.append({
                "id": r[0],
                "hostname": r[1],
                "ip_address": r[2],       
                "service_ip": r[3] if r[3] else r[2],  
                "component_type": r[4],
                "updatable": r[4] in ANSIBLE_UPDATABLE_COMPONENTS,
                "port": r[5],
                "status": r[6],
                "description": r[7],
                "updated_at": str(r[8]) if r[8] else None
            })
        return {"nodes": nodes}
    finally:
        cursor.close()
        conn.close()

@router.put("/infra/{node_id}", dependencies=[Depends(verify_profile_access("infra_manager"))])
def update_infra_node(node_id: int, payload: AuthNodePayload):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM infrastructure_nodes WHERE id = %s;", (node_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Nodo no encontrado.")
        
        svc_ip = payload.service_ip if payload.service_ip else payload.ip_address
        cursor.execute(
            "UPDATE infrastructure_nodes SET hostname = %s, ip_address = %s, service_ip = %s, component_type = %s, port = %s, status = %s, description = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s;",
            (payload.hostname, payload.ip_address, svc_ip, payload.component_type, payload.port, payload.status, payload.description, node_id)
        )
        conn.commit()
        return {"status": "success", "message": "Nodo actualizado correctamente"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@router.delete("/infra/{node_id}", dependencies=[Depends(verify_profile_access("infra_manager"))])
def delete_infra_node(node_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM infrastructure_nodes WHERE id = %s;", (node_id,))
        conn.commit()
        return {"status": "success", "message": "Nodo eliminado correctamente"}
    finally:
        cursor.close()
        conn.close()

@router.post("/infra/{node_id}/update", dependencies=[Depends(verify_profile_access("infra_manager"))])
def run_infra_ansible_update(node_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id, hostname, ip_address, service_ip, component_type, port, status, description, admin_username, admin_password FROM infrastructure_nodes WHERE id = %s;", 
            (node_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Nodo no encontrado en la base de datos.")
        
        node = {
            "id": row[0],
            "hostname": row[1],
            "ip_address": row[2],
            "service_ip": row[3] if row[3] else row[2],
            "component_type": row[4],
            "port": row[5],
            "status": row[6],
            "description": row[7],
            "admin_username": row[8] if row[8] else "anomalia",
            "admin_password": row[9] if row[9] else ""
        }
    finally:
        cursor.close()
        conn.close()

    playbook_path = "/app/playbooks/install-binaries.yml"
    target_ip = node["ip_address"]
    admin_user = node["admin_username"]
    admin_pass = node["admin_password"]
    component = node["component_type"]

    if not os.path.isfile(playbook_path):
        raise HTTPException(
            status_code=500,
            detail=f"Playbook no encontrado dentro del contenedor: {playbook_path}",
        )

    if shutil.which("ansible-playbook") is None:
        raise HTTPException(
            status_code=500,
            detail="ansible-playbook no está instalado en la imagen del gateway.",
        )

    if component not in ANSIBLE_UPDATABLE_COMPONENTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"El componente '{component}' no se actualiza con install-binaries.yml. "
                f"Componentes soportados: {', '.join(sorted(ANSIBLE_UPDATABLE_COMPONENTS))}."
            ),
        )

    extra_vars: dict[str, object] = {
        "ansible_user": admin_user,
        "servicio": component,
        "gestionar_servicios": True,
    }
    if admin_pass:
        extra_vars["ansible_password"] = admin_pass
        extra_vars["ansible_become_password"] = admin_pass

    # Las credenciales van en un archivo 0600 dentro de un directorio privado en
    # lugar de argv: con -e key=valor la contraseña queda legible en 'ps' para
    # cualquier proceso del contenedor.
    secret_dir = tempfile.mkdtemp(prefix="anomalia-ansible-")
    extra_vars_path = os.path.join(secret_dir, "extra-vars.json")
    try:
        fd = os.open(extra_vars_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(extra_vars, handle)

        cmd = [
            "ansible-playbook",
            "-i", f"{target_ip},",
            playbook_path,
            "--extra-vars", f"@{extra_vars_path}",
        ]
        logger.info(
            "Ejecutando install-binaries.yml nodo=%s componente=%s host=%s usuario=%s",
            node_id, component, target_ip, admin_user,
        )

        try:
            result = subprocess.run(
                cmd,
                cwd="/app/playbooks",
                capture_output=True,
                text=True,
                timeout=300
            )
        except subprocess.TimeoutExpired:
            logger.error(
                "Timeout de install-binaries.yml nodo=%s componente=%s", node_id, component
            )
            raise HTTPException(status_code=504, detail="El playbook excedió el tiempo límite de ejecución (Timeout).")

        if result.returncode == 0:
            logger.info("Nodo %s (%s) actualizado correctamente", node_id, component)
            return {"status": "success", "message": "Playbook ejecutado exitosamente OK.", "stdout": result.stdout}

        error_details = f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        # El detalle va en el body y también al log del contenedor: sin esto el
        # fallo quedaba invisible salvo por un 500 genérico en el frontend.
        logger.error(
            "install-binaries.yml fallo nodo=%s componente=%s rc=%s\n%s",
            node_id, component, result.returncode, error_details,
        )
        raise HTTPException(status_code=500, detail=error_details)
    finally:
        shutil.rmtree(secret_dir, ignore_errors=True)

@router.post("/tenants/{tenant_id}/delete/confirm1", dependencies=[Depends(verify_profile_access("tenants_manager"))])
def delete_tenant_confirm1(tenant_id: int, payload: DeleteTenantConfirm1):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        _guard_internal_tenant(cursor, tenant_id, "Eliminar el tenant")
        cursor.execute("SELECT slug FROM tenants WHERE id = %s;", (tenant_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")
        tenant_slug = row[0] or ""
        challenge_id = str(uuid.uuid4())
        cursor.execute(
            """INSERT INTO audit_deletions (tenant_id, tenant_slug, actor, reason, challenge_id, confirm_step1_at, outcome)
               VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s);""",
            (tenant_id, tenant_slug, None, payload.reason, challenge_id, "pending")
        )
        conn.commit()
        return {"challenge_id": challenge_id, "expires_at": None, "tenant_slug": tenant_slug}
    except HTTPException:
        # Los HTTPException de este bloque ya llevan su status y su detalle.
        # Sin esta rama el 'except Exception' generico los reenvuelve en un 400
        # y duplica el mensaje ('400: 404: Tenant no encontrado').
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@router.post("/tenants/{tenant_id}/delete/confirm2", dependencies=[Depends(verify_profile_access("tenants_manager"))])
def delete_tenant_confirm2(tenant_id: int, payload: DeleteTenantConfirm2):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        _guard_internal_tenant(cursor, tenant_id, "Eliminar el tenant")
        cursor.execute("SELECT slug FROM tenants WHERE id = %s;", (tenant_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")
        tenant_slug = row[0] or ""
        if payload.confirm_text != tenant_slug:
            raise HTTPException(status_code=400, detail="confirm_text debe coincidir con tenant.slug")
        cursor.execute("SELECT 1 FROM audit_deletions WHERE tenant_id = %s AND challenge_id = %s AND outcome='pending';", (tenant_id, payload.challenge_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=400, detail="challenge_id inválido o usado")
        job_id = str(uuid.uuid4())
        cursor.execute("UPDATE audit_deletions SET confirm_step2_at = CURRENT_TIMESTAMP, outcome = 'completed' WHERE tenant_id = %s AND challenge_id = %s;", (tenant_id, payload.challenge_id))
        cursor.execute("UPDATE tenants SET status = 'deleting' WHERE id = %s;", (tenant_id,))
        cursor.execute("""INSERT INTO job_state (id, type, ref_id, status, phase, progress_pct, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s);""", (job_id, "tenant_delete", tenant_id, "queued", "init", 0, None))
        conn.commit()
        return {"status": "accepted", "message": "Hard-delete en curso", "job_id": job_id}
    except HTTPException:
        # Los HTTPException de este bloque ya llevan su status y su detalle.
        # Sin esta rama el 'except Exception' generico los reenvuelve en un 400
        # y duplica el mensaje ('400: 404: Tenant no encontrado').
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@router.get("/jobs")
def list_jobs(
    type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
):
    """Lista jobs recientes para reanudar el seguimiento tras recargar la UI (spec 016 §2.6)."""
    limit = max(1, min(limit, 200))
    clauses = []
    params: list = []
    if type:
        clauses.append("type = %s")
        params.append(type)
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"""SELECT id, type, ref_id, status, phase, progress_pct, error_code,
                       started_at, finished_at, created_at
                FROM job_state
                {where}
                ORDER BY created_at DESC
                LIMIT %s""",
            params + [limit],
        )
        jobs = [
            {
                "id": r[0], "type": r[1], "ref_id": r[2], "status": r[3],
                "phase": r[4], "progress_pct": r[5], "error_code": r[6],
                "started_at": r[7], "finished_at": r[8], "created_at": r[9],
            }
            for r in cursor.fetchall()
        ]
        return {"jobs": jobs}
    finally:
        cursor.close()
        conn.close()


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, type, ref_id, status, phase, progress_pct, logs_ref, error_code, result, started_at, finished_at, timeout_at, steps, heartbeat_at FROM job_state WHERE id = %s;", (job_id,))
        r = cursor.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Job no encontrado")
        return {"id": r[0], "type": r[1], "ref_id": r[2], "status": r[3], "phase": r[4], "progress_pct": r[5], "logs_ref": r[6], "error_code": r[7], "result": r[8], "started_at": r[9], "finished_at": r[10], "timeout_at": r[11], "steps": r[12], "heartbeat_at": r[13]}
    finally:
        cursor.close()
        conn.close()

@router.get("/tenants/{tenant_id}/vmalert/rules", dependencies=[Depends(verify_profile_access("alerts_manager"))])
def get_vmalert_rules(tenant_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT slug FROM tenants WHERE id = %s;", (tenant_id,))
        row = cursor.fetchone()
        if not row or not row[0]:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")
        # La BD es la fuente de verdad de las reglas (spec 011/012); el nodo la
        # materializa en rules_path vía el job vmalert_sync_rules.
        cursor.execute(
            "SELECT rules_yaml FROM tenant_vmalert_instances WHERE tenant_id = %s;",
            (tenant_id,)
        )
        inst = cursor.fetchone()
        return {"yaml": (inst[0] if inst and inst[0] else "")}
    finally:
        cursor.close()
        conn.close()

@router.put("/tenants/{tenant_id}/vmalert/rules", dependencies=[Depends(verify_profile_access("alerts_manager"))])
def put_vmalert_rules(tenant_id: int, payload: VMAlerterRulesPut):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT slug FROM tenants WHERE id = %s;", (tenant_id,))
        row = cursor.fetchone()
        if not row or not row[0]:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")
        _guard_internal_tenant(cursor, tenant_id, "Modificar las reglas")

        # Validar YAML antes de persistir (el nodo recarga esto tal cual).
        if payload.yaml and payload.yaml.strip():
            try:
                yaml.safe_load(payload.yaml)
            except yaml.YAMLError as exc:
                raise HTTPException(status_code=400, detail=f"YAML inválido: {exc}")

        cursor.execute(
            "UPDATE tenant_vmalert_instances SET rules_yaml = %s, updated_at = CURRENT_TIMESTAMP WHERE tenant_id = %s;",
            (payload.yaml or "", tenant_id)
        )
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=409,
                detail="El tenant no tiene una instancia vmalert desplegada. Desplegue vmalert antes de guardar reglas.",
            )
        conn.commit()
        return {"status": "ok"}
    finally:
        cursor.close()
        conn.close()

@router.post("/tenants/{tenant_id}/vmalert/sync_rules", dependencies=[Depends(verify_profile_access("alerts_manager"))])
def sync_vmalert_rules(tenant_id: int):
    """Materializa las reglas guardadas en la BD hacia el nodo y recarga vmalert (job)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT rules_yaml FROM tenant_vmalert_instances WHERE tenant_id = %s AND status = 'deployed';",
            (tenant_id,)
        )
        inst = cursor.fetchone()
        if not inst:
            raise HTTPException(
                status_code=409,
                detail="El tenant no tiene una instancia vmalert desplegada.",
            )
        job_id = str(uuid.uuid4())
        cursor.execute("""INSERT INTO job_state (id, type, ref_id, status, phase, progress_pct, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s);""", (job_id, "vmalert_sync_rules", tenant_id, "queued", "init", 0, None))
        conn.commit()
        return {"status": "accepted", "job_id": job_id}
    finally:
        cursor.close()
        conn.close()

@router.post("/tenants/{tenant_id}/vmalert/reload", dependencies=[Depends(verify_profile_access("alerts_manager"))])
def reload_vmalert(tenant_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        job_id = str(uuid.uuid4())
        cursor.execute("""INSERT INTO job_state (id, type, ref_id, status, phase, progress_pct, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s);""", (job_id, "vmalert_reload", tenant_id, "queued", "init", 0, None))
        conn.commit()
        return {"status": "accepted", "job_id": job_id}
    finally:
        cursor.close()
        conn.close()

@router.post("/tenants/{tenant_id}/parses/provision", dependencies=[Depends(verify_profile_access("tenants_manager"))])
def provision_parses(tenant_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        job_id = str(uuid.uuid4())
        cursor.execute("""INSERT INTO job_state (id, type, ref_id, status, phase, progress_pct, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s);""", (job_id, "parses_provision", tenant_id, "queued", "init", 0, None))
        conn.commit()
        return {"status": "accepted", "job_id": job_id}
    finally:
        cursor.close()
        conn.close()

@router.post("/tenants/{tenant_id}/vmalert/deploy", dependencies=[Depends(verify_profile_access("alerts_manager"))])
def deploy_vmalert(tenant_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        _guard_non_profiles_tenant(cursor, tenant_id, "Desplegar instancia vmalert")
        job_id = str(uuid.uuid4())
        cursor.execute("""INSERT INTO job_state (id, type, ref_id, status, phase, progress_pct, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s);""", (job_id, "vmalert_deploy", tenant_id, "queued", "init", 0, None))
        conn.commit()
        return {"status": "accepted", "job_id": job_id}
    finally:
        cursor.close()
        conn.close()

@router.post("/tenants/{tenant_id}/vmalert/undeploy", dependencies=[Depends(verify_profile_access("alerts_manager"))])
def undeploy_vmalert(tenant_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        _guard_internal_tenant(cursor, tenant_id, "Desplegar instancia vmalert")
        job_id = str(uuid.uuid4())
        cursor.execute("""INSERT INTO job_state (id, type, ref_id, status, phase, progress_pct, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s);""", (job_id, "vmalert_undeploy", tenant_id, "queued", "init", 0, None))
        conn.commit()
        return {"status": "accepted", "job_id": job_id}
    finally:
        cursor.close()
        conn.close()

@router.post("/tenants/{tenant_id}/vmalert/redeploy", dependencies=[Depends(verify_profile_access("alerts_manager"))])
def redeploy_vmalert(tenant_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        _guard_non_profiles_tenant(cursor, tenant_id, "Redesplegar instancia vmalert")
        job_id = str(uuid.uuid4())
        cursor.execute("""INSERT INTO job_state (id, type, ref_id, status, phase, progress_pct, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s);""", (job_id, "vmalert_redeploy", tenant_id, "queued", "init", 0, None))
        conn.commit()
        return {"status": "accepted", "job_id": job_id}
    finally:
        cursor.close()
        conn.close()
