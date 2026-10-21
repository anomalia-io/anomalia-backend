import asyncio
import base64
import io
import json
import os
import uuid
from datetime import datetime
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from psycopg2.extras import Json
from pydantic import BaseModel, Field

from ..auth import verify_admin_token
from ..database import get_db_connection
from ..mailer import send_email
from ..workers.base import enqueue_job_tx

router = APIRouter(prefix="/api/v1/system", tags=["System Settings"])

BACKUP_VERSION = 1
# Tipo del job único que reconcilia (y revierte si falla) un import de backup.
IMPORT_JOB_TYPE = "backup_import"
# Tope de duración del import (spec 016 §2.4); el pipeline lo verifica entre pasos.
IMPORT_JOB_TIMEOUT_SECONDS = 3600
# Solo configuración. La infraestructura (`infrastructure_nodes`) y sus
# artefactos (`tenant_vmalert_instances`) NO viajan: cada clúster la gestiona vía
# deploy (spec 015). Orden importa para las FKs: primero las tablas
# independientes/referenciadas (users, roles, profiles), luego los tenants, y
# finalmente las relaciones y dependencias.
BACKUP_TABLES = [
    "users",
    "roles",
    "profiles",
    "tenants",
    "user_roles",
    "role_profiles",
    "user_tenants",
    "role_tenants",
    "tenant_datasources",
    "ai_settings",
    "oidc_providers",
    "system_settings",
]

# PKs de las tablas del backup (las de relacion usan PK compuesta, no `id`).
BACKUP_PK = {
    "users": ["id"],
    "roles": ["id"],
    "profiles": ["id"],
    "user_roles": ["user_id", "role_id"],
    "role_profiles": ["role_id", "profile_id"],
    "user_tenants": ["user_id", "tenant_id"],
    "role_tenants": ["role_id", "tenant_id"],
    "tenants": ["id"],
    "tenant_datasources": ["tenant_id"],
    "ai_settings": ["id"],
    "oidc_providers": ["id"],
    "system_settings": ["id"],
}

# Columnas de tenants que referencian infrastructure_nodes(id) (spec 015).
TENANT_NODE_REF_COLUMNS = ("vmalert_node_id", "vmauth_node_id", "instance_id")


def _detach_tenant_node_refs(row: dict) -> dict:
    """Desacopla un tenant del backup de la infra del clúster origen (spec 015).

    El backup no incluye nodos, así que las FK de nodo apuntarían a ids ausentes
    (o ajenos). Se ponen en ``NULL`` y el placement pasa a ``auto`` para que la
    reconciliación post-import asigne un nodo local; el ``vmalert_port`` se
    limpia (es determinista: 8880 + tenant_id, lo recalcula el bootstrap).
    """
    detached = dict(row)
    for col in TENANT_NODE_REF_COLUMNS:
        if col in detached:
            detached[col] = None
    if "vmalert_port" in detached:
        detached["vmalert_port"] = None
    if "placement_mode" in detached:
        detached["placement_mode"] = "auto"
    if "vmauth_placement_mode" in detached:
        detached["vmauth_placement_mode"] = "auto"
    return detached


class SmtpConfigRequest(BaseModel):
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = Field(None, repr=False)
    smtp_use_tls: Optional[bool] = None
    smtp_from: Optional[str] = None


class ProxyConfigRequest(BaseModel):
    proxy_url: Optional[str] = None
    proxy_enabled: Optional[bool] = None


class SmtpTestRequest(BaseModel):
    to: str


def _get_system_config() -> dict:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT smtp_host, smtp_port, smtp_username, smtp_password,
                   smtp_use_tls, smtp_from, proxy_url, proxy_enabled
            FROM system_settings ORDER BY id LIMIT 1;
            """
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=500, detail="system_settings no inicializado.")
        return {
            "smtp_host": row[0],
            "smtp_port": row[1],
            "smtp_username": row[2],
            "smtp_password": row[3],
            "smtp_use_tls": row[4],
            "smtp_from": row[5],
            "proxy_url": row[6],
            "proxy_enabled": row[7],
        }
    finally:
        cursor.close()
        conn.close()


def _public_config(config: dict) -> dict:
    return {
        "smtp_host": config["smtp_host"],
        "smtp_port": config["smtp_port"],
        "smtp_username": config["smtp_username"],
        "has_smtp_password": bool(config["smtp_password"]),
        "smtp_use_tls": config["smtp_use_tls"],
        "smtp_from": config["smtp_from"],
        "proxy_url": config["proxy_url"],
        "proxy_enabled": config["proxy_enabled"],
    }


@router.get("/config", dependencies=[Depends(verify_admin_token)])
def get_system_config():
    config = _get_system_config()
    return _public_config(config)


@router.put("/smtp", dependencies=[Depends(verify_admin_token)])
def update_smtp_config(payload: SmtpConfigRequest):
    config = _get_system_config()
    updates = {}
    if payload.smtp_host is not None:
        updates["smtp_host"] = payload.smtp_host
    if payload.smtp_port is not None:
        updates["smtp_port"] = payload.smtp_port
    if payload.smtp_username is not None:
        updates["smtp_username"] = payload.smtp_username
    if payload.smtp_password is not None:
        updates["smtp_password"] = payload.smtp_password
    if payload.smtp_use_tls is not None:
        updates["smtp_use_tls"] = payload.smtp_use_tls
    if payload.smtp_from is not None:
        updates["smtp_from"] = payload.smtp_from

    if not updates:
        return _public_config(config)

    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [datetime.utcnow()]

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"UPDATE system_settings SET {set_clause}, updated_at = %s WHERE id = 1;",
            values,
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"Error guardando config SMTP: {str(e)}")
    finally:
        cursor.close()
        conn.close()

    return _public_config(_get_system_config())


@router.post("/smtp-test", dependencies=[Depends(verify_admin_token)])
async def test_smtp_config(payload: SmtpTestRequest):
    config = _get_system_config()
    if not config["smtp_host"]:
        raise HTTPException(status_code=400, detail="SMTP no configurado: falta smtp_host.")
    try:
        result = await send_email(
            to_addr=payload.to,
            subject="Anomalia - Prueba de SMTP",
            body="Este es un email de prueba enviado desde el panel de administracion de Anomalia.",
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error enviando email: {str(e)}")


@router.put("/proxy", dependencies=[Depends(verify_admin_token)])
def update_proxy_config(payload: ProxyConfigRequest):
    config = _get_system_config()
    updates = {}
    if payload.proxy_url is not None:
        updates["proxy_url"] = payload.proxy_url
    if payload.proxy_enabled is not None:
        updates["proxy_enabled"] = payload.proxy_enabled

    if not updates:
        return _public_config(config)

    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [datetime.utcnow()]

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"UPDATE system_settings SET {set_clause}, updated_at = %s WHERE id = 1;",
            values,
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"Error guardando config proxy: {str(e)}")
    finally:
        cursor.close()
        conn.close()

    return _public_config(_get_system_config())


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def _serialize_table(cursor, table: str) -> list:
    cursor.execute(f"SELECT * FROM {table};")
    columns = [desc[0] for desc in cursor.description]
    rows = []
    for row in cursor.fetchall():
        row_dict = {}
        for col, val in zip(columns, row):
            if isinstance(val, datetime):
                row_dict[col] = val.isoformat()
            elif isinstance(val, (dict, list)):
                row_dict[col] = val
            else:
                row_dict[col] = val
        rows.append(row_dict)
    return rows


@router.get("/backup/export", dependencies=[Depends(verify_admin_token)])
def export_backup(password: str = Query(..., min_length=1)):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        backup_data = {}
        for table in BACKUP_TABLES:
            backup_data[table] = _serialize_table(cursor, table)
    finally:
        cursor.close()
        conn.close()

    plaintext = json.dumps({"version": BACKUP_VERSION, "tables": backup_data}).encode()
    salt = os.urandom(16)
    key = _derive_key(password, salt)
    f = Fernet(key)
    ciphertext = f.encrypt(plaintext)

    envelope = {
        "version": BACKUP_VERSION,
        "salt": base64.b64encode(salt).decode(),
        "payload": base64.b64encode(ciphertext).decode(),
    }

    json_bytes = json.dumps(envelope, indent=2).encode()
    return StreamingResponse(
        io.BytesIO(json_bytes),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=anomalia-backup.json"},
    )


def _upsert_row(cursor, table: str, row: dict):
    pk = BACKUP_PK.get(table, ["id"])
    columns = list(row.keys())
    if not columns or not all(c in columns for c in pk):
        return
    placeholders = ", ".join(["%s"] * len(columns))
    col_names = ", ".join(columns)
    conflict = ", ".join(pk)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c not in pk)
    if updates:
        sql = (
            f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict}) DO UPDATE SET {updates};"
        )
    else:
        sql = (
            f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict}) DO NOTHING;"
        )
    cursor.execute(sql, list(row.get(c) for c in columns))


def _get_real_columns(cursor, table: str) -> set:
    cursor.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s;
        """,
        (table,),
    )
    return {r[0] for r in cursor.fetchall()}


def _fix_sequences(cursor, conn):
    """Realinea las sequences de los SERIAL tras importar ids explicitos.

    La sequence se referencia por nombre con ``%s::regclass`` (unico modo
    valido de parametrizarla); su ultimo valor se lee con
    ``pg_sequence_last_value`` porque ``SELECT last_value FROM <seq>`` no admite
    un literal en el ``FROM`` (spec 014 §6.3).
    """
    for table, pk in BACKUP_PK.items():
        if len(pk) != 1:
            continue
        column = pk[0]
        cursor.execute(
            f"SELECT COALESCE(MAX({column}), 0) FROM {table};"
        )
        max_id = cursor.fetchone()[0] or 0
        if max_id <= 0:
            continue
        cursor.execute(
            "SELECT pg_get_serial_sequence('public.' || %s, %s);",
            (table, column),
        )
        seq = cursor.fetchone()
        if seq and seq[0]:
            cursor.execute(
                "SELECT setval(%s::regclass, "
                "GREATEST(%s::bigint, pg_sequence_last_value(%s::regclass)), true);",
                (seq[0], max_id, seq[0]),
            )


def _pick_vmalert_node(vmalert_nodes: list, assigned: dict):
    """Elige el nodo vmalert menos cargado (spec 015 §5), repartiendo en memoria.

    ``assigned`` cuenta las asignaciones hechas en el mismo import para no amontonar
    todos los tenants sin nodo en el primero. Devuelve ``None`` si no hay nodos.
    """
    if not vmalert_nodes:
        return None
    return min(vmalert_nodes, key=lambda n: (assigned.get(n, 0), n))


def _resolve_user_id(cursor, username: Optional[str]) -> Optional[int]:
    """Resuelve el ``users.id`` a partir del ``sub`` (username) del token (spec 016 §2.3)."""
    if not username:
        return None
    cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
    row = cursor.fetchone()
    return row[0] if row else None


def _build_import_plan(cursor, tables: dict) -> tuple:
    """Calcula el plan de reconciliación de los tenants de un backup (spec 016).

    Es la antigua reconciliación node-aware (spec 015 §5) pero SIN encolar ni
    commitear: devuelve el plan que ejecutará el job ``backup_import``. El
    auto-placement de nodos vmalert se aplica aquí, dentro de la misma transacción
    del endpoint, para que el snapshot del rollback también lo cubra. Cada entrada
    es ``{"tenant_id": int, "steps": [{"type", "payload"}]}``. Devuelve
    ``(plan, tenants_count, skipped_count)``.

    Reglas de reconciliación (spec 011 §4.11):

      - tenants con alertas (no ``profiles``): ``vmalert_redeploy`` (recrea la
        unidad y dispara el regen de vmagent+vmauth).
      - resto (``profiles`` o sin alertas): ``vmalert_regen_scrapes`` con
        ``components`` limitado a los tipos de nodo presentes.
      - siempre que haya nodo ``parses``: ``parses_provision``.

    Node-aware: solo se crea lo que tiene nodo disponible en el clúster local. Un
    tenant con alertas sin ``vmalert_node_id`` recibe el nodo ``vmalert`` menos
    cargado; si no hay ninguno, se cuenta en ``skipped``. No toca
    ``tenants_count_active``. Solo reconcilia tenants presentes en el backup.
    Los tenants internos (``is_internal``) se omiten: ya existen y se aprovisionan
    en el deploy (Perf A, spec 016 §2.7).
    """
    raw = tables.get("tenants")
    if not isinstance(raw, list):
        return [], 0, 0
    tenant_ids = [
        row["id"] for row in raw
        if isinstance(row, dict) and isinstance(row.get("id"), int)
    ]
    if not tenant_ids:
        return [], 0, 0

    plan_out: list = []
    tenants_count = 0
    skipped = 0

    cursor.execute(
        """SELECT DISTINCT component_type FROM infrastructure_nodes
           WHERE is_active = TRUE AND status = 'operational'"""
    )
    node_types = {r[0] for r in cursor.fetchall()}
    cursor.execute(
        """SELECT id, tenants_count_active FROM infrastructure_nodes
           WHERE component_type = 'vmalert'
             AND is_active = TRUE AND status = 'operational'
           ORDER BY tenants_count_active ASC, id ASC"""
    )
    vmalert_rows = cursor.fetchall()
    vmalert_nodes = [r[0] for r in vmalert_rows]
    # La carga inicial incluye los tenants ya activos del nodo, para repartir
    # entre todos (no solo entre los reasignados en este import).
    vmalert_assigned: dict = {r[0]: (r[1] or 0) for r in vmalert_rows}

    cursor.execute(
        """SELECT id, type, has_alerts, vmalert_node_id FROM tenants
           WHERE id = ANY(%s) AND status = 'active' AND deleted_at IS NULL
             AND is_internal = FALSE""",
        (tenant_ids,),
    )
    for tenant_id, t_type, has_alerts, vmalert_node_id in cursor.fetchall():
        tenants_count += 1
        needs_vmalert = bool(has_alerts) and t_type != "profiles"
        steps: list = []

        if needs_vmalert:
            if not vmalert_nodes:
                skipped += 1
                continue
            if vmalert_node_id is None:
                vmalert_node_id = _pick_vmalert_node(
                    vmalert_nodes, vmalert_assigned
                )
                vmalert_assigned[vmalert_node_id] = (
                    vmalert_assigned.get(vmalert_node_id, 0) + 1
                )
                cursor.execute(
                    """UPDATE tenants SET vmalert_node_id = %s,
                       placement_mode = 'auto' WHERE id = %s""",
                    (vmalert_node_id, tenant_id),
                )
            steps.append({"type": "vmalert_redeploy", "payload": None})
        else:
            components = [
                c for c in ("vmagent", "vmauth") if c in node_types
            ]
            if components:
                steps.append(
                    {"type": "vmalert_regen_scrapes", "payload": {"components": components}}
                )

        if "parses" in node_types:
            steps.append({"type": "parses_provision", "payload": None})

        if not steps:
            skipped += 1
            continue

        # Idempotencia: no re-encolar un job del mismo tipo ya pendiente/en curso.
        filtered: list = []
        for step in steps:
            cursor.execute(
                """SELECT 1 FROM job_state
                   WHERE type = %s AND ref_id = %s
                     AND status IN ('queued', 'running')""",
                (step["type"], tenant_id),
            )
            if cursor.fetchone():
                continue
            filtered.append(step)
        if not filtered:
            skipped += 1
            continue
        plan_out.append({"tenant_id": tenant_id, "steps": filtered})

    return plan_out, tenants_count, skipped


def _snapshot_backup_tables(cursor) -> dict:
    """Copia fiel de las tablas del backup, para revertir al estado pre-import."""
    return {table: _serialize_table(cursor, table) for table in BACKUP_TABLES}


def _collect_imported_pks(tables: dict) -> dict:
    """PKs por tabla presentes en el backup, para borrar en el rollback.

    Solo se eliminan las PKs introducidas por el import y ausentes del snapshot;
    así no se tocan filas que ya existían antes.
    """
    imported: dict = {}
    for table in BACKUP_TABLES:
        pk_cols = BACKUP_PK.get(table, ["id"])
        keys: list = []
        for row in tables.get(table) or []:
            if isinstance(row, dict) and all(c in row for c in pk_cols):
                keys.append([row[c] for c in pk_cols])
        imported[table] = keys
    return imported


def _pk_tuple(value) -> tuple:
    """Normaliza una PK (escalar o lista guardada en JSONB) a tupla comparable."""
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def delete_import_state(job_id: str) -> None:
    """Elimina el snapshot del import tras reconciliar con éxito (spec 016)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM backup_import_state WHERE job_id = %s", (job_id,))
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def restore_import_snapshot(job_id: str) -> None:
    """Revierte las tablas del backup al snapshot previo al import (spec 016).

    Borra las filas introducidas por el import (PK en ``imported_pks`` y ausente
    del snapshot) y re-upserta todas las filas del snapshot. Realinea sequences.
    Todo en una transacción: o queda exactamente como estaba, o no aplica nada.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT snapshot, imported_pks FROM backup_import_state WHERE job_id = %s",
            (job_id,),
        )
        row = cursor.fetchone()
        if not row:
            return
        snapshot = row[0] or {}
        imported_pks = row[1] or {}
        for table in BACKUP_TABLES:
            pk_cols = BACKUP_PK.get(table, ["id"])
            snap_rows = snapshot.get(table) or []
            snap_keys = {
                _pk_tuple([r[c] for c in pk_cols])
                for r in snap_rows
                if isinstance(r, dict) and all(c in r for c in pk_cols)
            }
            for key in imported_pks.get(table) or []:
                key_t = _pk_tuple(key)
                if key_t in snap_keys:
                    continue
                where = " AND ".join(f"{c} = %s" for c in pk_cols)
                cursor.execute(f"DELETE FROM {table} WHERE {where}", list(key_t))
            real_columns = _get_real_columns(cursor, table)
            for snap_row in snap_rows:
                clean_row = {k: v for k, v in snap_row.items() if k in real_columns}
                if clean_row:
                    _upsert_row(cursor, table, clean_row)
        _fix_sequences(cursor, conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


@router.post("/backup/import")
async def import_backup(
    file: UploadFile = File(...),
    password: str = Form(...),
    admin: dict = Depends(verify_admin_token),
):
    content = await file.read()
    try:
        envelope = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Archivo de backup invalido: no es JSON.")

    if envelope.get("version") != BACKUP_VERSION:
        raise HTTPException(status_code=400, detail="Version de backup no soportada.")

    salt = base64.b64decode(envelope["salt"])
    ciphertext = base64.b64decode(envelope["payload"])
    key = _derive_key(password, salt)
    f = Fernet(key)

    try:
        plaintext = f.decrypt(ciphertext)
    except InvalidToken:
        raise HTTPException(status_code=400, detail="Contraseña incorrecta o backup corrupto.")

    try:
        data = json.loads(plaintext)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Backup corrupto: payload no es JSON valido.")

    tables = data.get("tables", {})
    if not tables:
        raise HTTPException(status_code=400, detail="Backup vacio: no contiene tablas.")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Guard: una sola importación en curso (spec 016). El job es único y
        # centraliza la reconciliación y el rollback.
        cursor.execute(
            "SELECT 1 FROM job_state WHERE type = %s AND status IN ('queued','running')",
            (IMPORT_JOB_TYPE,),
        )
        if cursor.fetchone():
            raise HTTPException(
                status_code=409,
                detail="Ya hay una importación de backup en curso.",
            )

        # 1. Snapshot previo de las tablas del backup (para rollback total).
        snapshot = _snapshot_backup_tables(cursor)
        cursor.execute("SELECT id FROM tenants;")
        preexisting_tenant_ids = [r[0] for r in cursor.fetchall()]

        # 2. Upsert de las tablas del backup. La infra no viaja (spec 015): los
        #    tenants se desacoplan de los nodos del clúster origen y la
        #    reconciliación post-import los reasigna.
        for table in BACKUP_TABLES:
            if table not in tables:
                continue
            rows = tables[table]
            if not isinstance(rows, list):
                continue
            real_columns = _get_real_columns(cursor, table)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                clean_row = {k: v for k, v in row.items() if k in real_columns}
                if table == "tenants":
                    clean_row = _detach_tenant_node_refs(clean_row)
                if not clean_row:
                    continue
                _upsert_row(cursor, table, clean_row)
        _fix_sequences(cursor, conn)

        # 3. Plan de reconciliación (incluye el auto-placement de nodos en esta
        #    misma transacción, para que el snapshot lo cubra).
        plan, tenants_count, skipped = _build_import_plan(cursor, tables)

        # 4. Estado del import + job único que ejecutará el plan y, si falla,
        #    revertirá BD y remoto. El job lleva created_by (dueño) y timeout_at
        #    (spec 016 §2.3/§2.4) para notificar al usuario y acotar su duración.
        created_by = _resolve_user_id(cursor, admin.get("sub"))
        job_id = enqueue_job_tx(
            cursor,
            IMPORT_JOB_TYPE,
            0,
            {"plan": plan, "tenants_count": tenants_count, "skipped": skipped},
            created_by=created_by,
            timeout_seconds=IMPORT_JOB_TIMEOUT_SECONDS,
        )
        cursor.execute(
            """INSERT INTO backup_import_state
                   (job_id, snapshot, preexisting_tenant_ids, imported_pks)
               VALUES (%s, %s, %s, %s)""",
            (
                job_id,
                Json(snapshot),
                preexisting_tenant_ids,
                Json(_collect_imported_pks(tables)),
            ),
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"Error importando backup: {str(e)}")
    finally:
        cursor.close()
        conn.close()

    if plan:
        message = (
            f"Backup importado. Reconciliando infraestructura de {tenants_count} "
            f"tenant(s) en segundo plano."
        )
    else:
        message = "Backup importado correctamente."
    if skipped:
        message += (
            f" {skipped} tenant(s) sin nodo compatible en este clúster: "
            "se aprovisionarán al agregar el nodo correspondiente."
        )

    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "message": message,
            "tenants_count": tenants_count,
            "skipped": skipped,
            "job_id": job_id,
        },
    )
