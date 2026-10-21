"""Provisioning por tenant de Perses, token de servicio y dashboards (spec 008).

En cada alta de tenant (y en el catch-up de arranque) el backend garantiza que en
Perses exista un project por tenant (`anomalia-tenant-<slug>`) con su datasource y
sus dashboards. Cada datasource usa el proxy HTTP de Perses hacia el gateway
Anomalia e inyecta un token firmado de vida larga (`purpose=parses-proxy`,
claim `svc=true`) en el header `X-Anomalia-Token` (Perses filtra el header
Authorization al reenviar por su HTTPProxy, verificado empiricamente). El token
queda acotado a un unico tenant (`tenants=[tenant_id]`): es un token de servicio,
no un token de usuario, porque el proyecto pertenece al tenant y no a una persona.

El modelo per-usuario (`anomalia-<hash de username>`) queda obsoleto: el aislamiento
de Perses es por project y el project ahora representa al tenant. `delete_legacy_user_parses`
retira los projects per-usuario preexistentes.

Los dashboards se provisionan **solo para los tenants internos** (`is_internal = TRUE`:
METRICS/TRACES/PROFILES/AUDIT_LOGS, sembrados en el deploy). Un tenant nuevo creado por
el usuario (`is_internal = FALSE`) arranca con su project y su datasource pero **sin
dashboards**: no hereda los del stack.

Los tenants internos reciben:

  - los dashboards nativos de Anomalia (`backend/dashboards/native/*.json`): sus
    queries declaran el tipo de datasource requerido con el marcador
    `xAnomaliaDatasource`, que se resuelve contra el datasource del tenant y descarta
    el panel si ese tipo no esta disponible;
  - para el tenant interno METRICS, ademas, los dashboards de infraestructura del stack
    migrados de Grafana a Perses (`backend/dashboards/grafana/metrics/*.json`) via
    `POST /api/migrate` del propio Perses; el resultado se cachea en
    `backend/dashboards/migrated/`.

El nombre visible del project es el nombre del tenant (`spec.display.name`).

El navegador nunca ve el token: Perses server es quien lo
agrega al consultar al backend. La identidad del usuario la vuelve a verificar el
backend en la BD por `sub`, no por claims confiables del cliente.

El proxy apunta al gateway por HTTPS (`PARSES_PROXY_BASE`), porque el gateway en
claro corre con `FORCE_HTTPS=true` y su middleware de redireccion devuelve 301
al proxy de Parses, que no puede seguirlo contra un puerto HTTP plano. Como el
certificado del gateway lo firma una CA privada, Parses la recibe en un `Secret`
de proyecto (`tlsConfig.ca`) referenciado por `proxy.spec.secret`: Perses exige
`Secret` y no `GlobalSecret` para los datasources de proyecto.
"""
import hashlib
import json
import os
import re
import logging
import time
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any, Optional

import httpx
import jwt

from .auth import SECRET_KEY, ALGORITHM
from .database import get_db_connection

logger = logging.getLogger("anomalia.parses_provisioner")

# Base de la API REST de Perses y base URL del proxy Anomalia que recibira las queries.
PARSES_API_URL = os.getenv("PARSES_API_URL", "http://parses:8080/api/v1").rstrip("/")
# El endpoint de migracion NO vive bajo /api/v1: es `POST /api/migrate`.
PARSES_MIGRATE_URL = os.getenv(
    "PARSES_MIGRATE_URL",
    PARSES_API_URL.removesuffix("/v1"),
).rstrip("/")
PARSES_PROXY_BASE = os.getenv(
    "PARSES_PROXY_BASE",
    "https://anomalia_gw_tls:8443/api/v1/parses/datasources",
).rstrip("/")

# CA privada que firma el certificado del gateway, montada en solo lectura.
# Vacia = el gateway no tiene TLS: el proxy queda en http y no se crea Secret.
PARSES_TLS_CA_FILE = os.getenv("PARSES_TLS_CA_FILE", "").strip()

# Nombre del Secret de proyecto que lleva la CA del proxy.
PARSES_PROXY_SECRET_NAME = os.getenv("PARSES_PROXY_SECRET_NAME", "anomalia-proxy-ca")

# Vida del token de proxy (default 24h). Se re-emite en cada login; mantiene los
# datasources de Perses utilizables sin volver a iniciar sesion en el mismo dia.
PARSES_PROXY_TOKEN_TTL_SECONDS = int(os.getenv("PARSES_PROXY_TOKEN_TTL", "86400"))

PARSES_PROJECT_PREFIX = os.getenv("PARSES_PROJECT_PREFIX", "anomalia")

# Dashboards fuente de Grafana que se migran a Perses para el tenant interno METRICS.
# El role de backend copia `backend/` completo a /opt/anomalia/backend, asi que esta
# ruta llega al contenedor sin tocar Ansible.
GRAPHANA_DASHBOARDS_DIR = os.getenv(
    "PARSES_GRAFANA_DASHBOARDS_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dashboards", "grafana", "metrics")),
)

# Dashboards nativos de Anomalia (ya en modelo Perses, no se migran). Cubren la
# telemetria de la plataforma: metricas del backend, trazas OTLP y perfilado.
NATIVE_DASHBOARDS_DIR = os.getenv(
    "PARSES_NATIVE_DASHBOARDS_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dashboards", "native")),
)

# Cache del resultado de /api/migrate: la migracion es deterministica y depende solo
# del JSON fuente y de la logica de plugins del build fijado.
MIGRATED_CACHE_DIR = os.getenv(
    "PARSES_MIGRATED_CACHE_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dashboards", "migrated")),
)

# Mapeo backend de almacenamiento -> plugin Datasource de Perses (verificado en 008).
PLUGIN_KIND_BY_STORAGE = {
    "VictoriaMetrics": "PrometheusDatasource",
    "VictoriaLogs": "VictoriaLogsDatasource",
    "VictoriaTraces": "JaegerDatasource",
    "Pyroscope": "PyroscopeDatasource",
}

# Prefijo de routing que se embebe en la URL del proxy para los backends con
# scoping por header (clúster Victoria). Solo aplica si el plugin NO lo trae ya en
# su propia ruta:
#   VictoriaTraces  -> plugin pide /api/services         (falta)   -> /select/jaeger
#   VictoriaLogs    -> plugin pide /select/logsql/query  (ya esta) -> ""
#   Pyroscope       -> plugin pide /querier.v1.QuerierService/... (raiz) -> ""
# Embeber un prefijo que el plugin ya trae produce una ruta doble que el upstream
# rechaza con 400 "unsupported path requested"; omitir el que falta produce 404.
# Verificado contra los upstreams reales y contra los chunks del build de Perses
# desplegado, no supuesto (spec 008).
HEADER_UPSTREAM_PREFIX_BY_STORAGE = {
    "VictoriaTraces": "/select/jaeger",
    "VictoriaLogs": "",
    "Pyroscope": "",
}


def user_tenant_rows(username: str) -> list[dict[str, Any]]:
    """Tenants con acceso del usuario: user_tenants ∪ role_tenants (resueltos en BD)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT t.id, t.name, t.type, d.kind, d.scoping, d.account_id, d.project_id, d.org_id
            FROM tenants t
            JOIN tenant_datasources d ON d.tenant_id = t.id AND d.enabled = TRUE
            WHERE t.id IN (
                SELECT ut.tenant_id
                FROM user_tenants ut
                JOIN users u ON u.id = ut.user_id
                WHERE u.username = %s
                UNION
                SELECT rt.tenant_id
                FROM role_tenants rt
                JOIN user_roles ur ON ur.role_id = rt.role_id
                JOIN users ru ON ru.id = ur.user_id
                WHERE ru.username = %s
            )
            ORDER BY t.id;
            """,
            (username, username),
        )
        rows = cursor.fetchall()
        return [
            {
                "tenant_id": r[0],
                "name": r[1],
                "type": r[2],
                "kind": r[3],
                "scoping": r[4],
                "account_id": r[5],
                "project_id": r[6],
                "org_id": r[7],
            }
            for r in rows
        ]
    finally:
        cursor.close()
        conn.close()


def _legacy_user_project_name(username: str) -> str:
    """Project per-usuario del diseno anterior (`anomalia-<sha256[:10]>`).

    Se conserva unicamente para localizar y borrar los projects preexistentes de
    deployments viejos (spec 008): el modelo vigente es per-tenant.
    """
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:10]
    return f"{PARSES_PROJECT_PREFIX}-{digest}"


# Forma de los projects per-usuario legados: `anomalia-<10 hex>`. NO matchea
# `anomalia-tenant-<slug>` (el prefijo `-tenant-` no es hex).
LEGACY_USER_PROJECT_RE = re.compile(r"^" + re.escape(PARSES_PROJECT_PREFIX) + r"-[0-9a-f]{10}$")


def tenant_parses_project_name(slug: str) -> str:
    """Nombre del project de Parses del tenant (`anomalia-tenant-<slug>`).

    Version publica: el proxy de UIs necesita el mismo calculo para aislar cada
    tenant dentro de Perses, que corre con ``enable_auth: false``. Una sola
    fuente de verdad para el provisionador y el proxy.
    """
    return f"{PARSES_PROJECT_PREFIX}-tenant-{slug}"


def mint_tenant_parses_token(tenant_id: int, ttl_seconds: Optional[int] = None) -> str:
    """Token de servicio del proxy de datos de Parses, acotado a un tenant.

    No lleva identidad de usuario: el project de Perses pertenece al tenant, no a
    una persona. El router valida ``svc`` y que el ``tenant_id`` del path
    pertenezca a ``tenants`` (spec 008).
    """
    to_encode: dict[str, Any] = {
        "sub": f"tenant:{int(tenant_id)}",
        "svc": True,
        "purpose": "parses-proxy",
        "tenants": [int(tenant_id)],
    }
    to_encode.update({
        "exp": datetime.utcnow() + timedelta(seconds=ttl_seconds or PARSES_PROXY_TOKEN_TTL_SECONDS),
    })
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def tenant_datasource_row(tenant_id: int) -> Optional[dict[str, Any]]:
    """Fila de lectura del tenant (una sola: `tenant_datasources` es 1:1).

    Devuelve None si el tenant no existe o no tiene datasource habilitado.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT t.id, t.slug, t.name, t.type, t.display_name, t.is_internal,
                   d.kind, d.read_url, d.scoping, d.account_id, d.project_id, d.org_id
            FROM tenants t
            JOIN tenant_datasources d ON d.tenant_id = t.id AND d.enabled = TRUE
            WHERE t.id = %s
            """,
            (tenant_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "tenant_id": row[0],
            "slug": row[1],
            "name": row[2],
            "type": row[3],
            "display_name": row[4],
            "is_internal": bool(row[5]),
            "kind": row[6],
            "read_url": row[7],
            "scoping": row[8],
            "account_id": row[9],
            "project_id": row[10],
            "org_id": row[11],
        }
    finally:
        cursor.close()
        conn.close()


def _tenant_ids_for_user(cursor, username: str) -> list[int]:
    """Tenants activos a los que el usuario tiene acceso: user_tenants ∪ role_tenants."""
    cursor.execute(
        """
        SELECT t.id
        FROM tenants t
        WHERE t.status = 'active' AND t.id IN (
            SELECT ut.tenant_id
            FROM user_tenants ut
            JOIN users u ON u.id = ut.user_id
            WHERE u.username = %s
            UNION
            SELECT rt.tenant_id
            FROM role_tenants rt
            JOIN user_roles ur ON ur.role_id = rt.role_id
            JOIN users ru ON ru.id = ur.user_id
            WHERE ru.username = %s
        )
        ORDER BY t.id;
        """,
        (username, username),
    )
    return [r[0] for r in cursor.fetchall()]


def user_parses_projects(username: str) -> list[str]:
    """Projects de Parses a los que el usuario tiene acceso (uno por tenant activo).

    El proxy de UIs usa este conjunto para permitir la lectura de los projects de
    sus propios tenants y cortar el resto (spec 008/010).
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        tenant_ids = _tenant_ids_for_user(cursor, username)
        if not tenant_ids:
            return []
        placeholders = ",".join(["%s"] * len(tenant_ids))
        cursor.execute(
            f"SELECT slug FROM tenants WHERE id IN ({placeholders}) ORDER BY id;",
            tuple(tenant_ids),
        )
        return [tenant_parses_project_name(r[0]) for r in cursor.fetchall()]
    finally:
        cursor.close()
        conn.close()


def _datasource_name(tenant_name: str) -> str:
    return f"ds-{tenant_name.lower()}"


@lru_cache(maxsize=1)
def _proxy_ca_pem() -> str:
    """PEM de la CA que firma el certificado del gateway, o "" si no hay TLS."""
    if not PARSES_TLS_CA_FILE:
        return ""
    try:
        with open(PARSES_TLS_CA_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError as exc:
        logger.warning("No se pudo leer la CA del proxy en %s: %s", PARSES_TLS_CA_FILE, exc)
        return ""


def _ensure_proxy_ca_secret(client: httpx.Client, project: str) -> Optional[str]:
    """Crea o actualiza el Secret de proyecto con la CA del proxy del gateway.

    Perses solo acepta `Secret` (con proyecto) para los datasources de proyecto:
    `GlobalSecret` no aplica. Devuelve el nombre del Secret para referenciarlo en
    `proxy.spec.secret`, o None si no hay CA (gateway sin TLS).
    """
    ca = _proxy_ca_pem()
    if not ca:
        return None

    payload = {
        "kind": "Secret",
        "metadata": {"name": PARSES_PROXY_SECRET_NAME, "project": project},
        "spec": {"tlsConfig": {"ca": ca}},
    }
    url = f"{PARSES_API_URL}/projects/{project}/secrets"
    existing = client.get(f"{url}/{PARSES_PROXY_SECRET_NAME}")
    if existing.status_code == 200:
        resp = client.put(f"{url}/{PARSES_PROXY_SECRET_NAME}", json=payload)
    else:
        resp = client.post(url, json=payload)
        if resp.status_code == 409:
            resp = client.put(f"{url}/{PARSES_PROXY_SECRET_NAME}", json=payload)

    if resp.status_code not in (200, 201):
        # error y no warning: sin este Secret los datasources HTTPS no podran
        # validar el certificado del gateway y las consultas fallaran por TLS.
        logger.error(
            "Secret de proxy '%s' NO se pudo garantizar en %s: %s %s",
            PARSES_PROXY_SECRET_NAME, project, resp.status_code, resp.text[:200],
        )
        return None
    logger.info("Secret de proxy '%s' garantizado en %s (%s).", PARSES_PROXY_SECRET_NAME, project, resp.status_code)
    return PARSES_PROXY_SECRET_NAME


def _datasource_payload(
    project: str,
    source: dict[str, Any],
    token: str,
    proxy_secret: Optional[str] = None,
    is_default: bool = False,
) -> Optional[dict[str, Any]]:
    plugin_kind = PLUGIN_KIND_BY_STORAGE.get(source["kind"])
    if not plugin_kind:
        logger.warning("Tenant %s sin plugin Datasource mapeado (kind=%s); se omite.", source["name"], source["kind"])
        return None

    upstream_prefix = HEADER_UPSTREAM_PREFIX_BY_STORAGE.get(source["kind"], "")

    proxy_spec: dict[str, Any] = {
        "url": f"{PARSES_PROXY_BASE}/{source['tenant_id']}{upstream_prefix}",
        # Header custom a proposito: Perses filtra el header Authorization al
        # reenviar por su HTTPProxy (008).
        "headers": {"X-Anomalia-Token": f"Bearer {token}"},
    }
    if proxy_secret:
        # CA con la que Perses valida el certificado del gateway en el proxy TLS.
        proxy_spec["secret"] = proxy_secret

    return {
        "kind": "Datasource",
        "metadata": {
            "name": _datasource_name(source["name"]),
            "project": project,
        },
        "spec": {
            # Perses resuelve un datasource sin `name` (el que usan las variables
            # ListVariable y las queries sin selector explicito) contra el que
            # tenga `default: true` de ese plugin. Sin ninguno, la resolucion
            # falla en silencio y las variables quedan vacias -> paneles "No data".
            # El project es por tenant y solo tiene el datasource de ese tenant, que
            # por eso recibe siempre `default: true` (ver provision_tenant_parses).
            "default": is_default,
            "plugin": {
                "kind": plugin_kind,
                "spec": {
                    "proxy": {
                        "kind": "HTTPProxy",
                        "spec": proxy_spec,
                    }
                },
            },
        },
    }


def _ensure_project(client: httpx.Client, name: str, display_name: Optional[str] = None) -> None:
    """Garantiza el project de Perses del tenant (idempotente).

    ``metadata.name`` es el identificador estable (``anomalia-tenant-<slug>``) que
    usan el proxy y el aislamiento por project; nunca cambia. El nombre visible en
    la UI de Perses es ``spec.display.name`` (p.ej. el nombre del tenant), que sí se
    mantiene sincronizado en cada provisioning. Sin ``display_name`` solo se crea.
    """
    payload: dict[str, Any] = {"kind": "project", "metadata": {"name": name}}
    if display_name:
        payload["spec"] = {"display": {"name": display_name}}

    if client.get(f"{PARSES_API_URL}/projects/{name}").status_code == 200:
        if display_name:
            resp = client.put(f"{PARSES_API_URL}/projects/{name}", json=payload)
            if resp.status_code not in (200, 201):
                logger.warning("PUT project %s fallo: %s %s", name, resp.status_code, resp.text[:200])
        return

    resp = client.post(f"{PARSES_API_URL}/projects", json=payload)
    if resp.status_code == 409:
        if display_name:
            client.put(f"{PARSES_API_URL}/projects/{name}", json=payload)
    elif resp.status_code not in (200, 201):
        resp.raise_for_status()
    logger.info("Project Parses '%s' garantizado (%s).", name, resp.status_code)


def _ensure_datasource(client: httpx.Client, payload: dict[str, Any], name: str, project: str) -> None:
    existing = client.get(f"{PARSES_API_URL}/projects/{project}/datasources/{name}")
    if existing.status_code == 200:
        resp = client.put(
            f"{PARSES_API_URL}/projects/{project}/datasources/{name}",
            json=payload,
        )
        if resp.status_code not in (200, 201):
            logger.warning("PUT datasource %s fallo: %s", name, resp.text)
    else:
        resp = client.post(f"{PARSES_API_URL}/datasources", json=payload)
        if resp.status_code not in (200, 201, 409):
            resp.raise_for_status()
        if resp.status_code == 409:
            client.put(
                f"{PARSES_API_URL}/projects/{project}/datasources/{name}",
                json=payload,
            )
    logger.info("Datasource Parses '%s' garantizado (%s).", name, resp.status_code)


_MIGRATION_PLACEHOLDER = "migration_from_grafana_not_supported"
_MIGRATION_UNSUPPORTED_VALUES = ["grafana", "migration", "not", "supported"]

# Se incluye en la clave de cache: si cambian las reglas de saneamiento, la cache
# de migraciones anteriores queda invalidada.
_SANITIZER_VERSION = "v1"


def _iter_grafana_panels(panels: Any):
    """Recorre paneles de forma recursiva (Grafana anida paneles en filas colapsadas)."""
    for panel in panels or []:
        yield panel
        yield from _iter_grafana_panels(panel.get("panels"))


def sanitize_grafana_dashboard(dashboard: dict[str, Any]) -> tuple[int, int]:
    """Adapta constructs de Grafana que el migrador de Perses no resuelve.

    Sin esto la migracion es atomica y un unico panel no soportado tumba el
    dashboard completo. Verificado contra el build fijado:

    1. `table` + `fieldConfig.defaults.thresholds` -> el panel Table de Perses no
       tiene equivalente numerico y falla con `_thresholdNumericSteps: undefined
       field: value`. Se descarta el umbral (degradacion solo cosmetica: el panel
       conserva datos y formato).
    2. `fieldConfig.overrides[].properties` con el mismo `id` repetido -> Perses
       construye un `querySettings` por query, no por serie, asi que dos
       `custom.lineStyle` (p.ej. solid y dash) colisionan con
       `conflicting values "dashed" and "solid"`. Se conserva el primero.

    Devuelve (thresholds descartados, overrides duplicados descartados).
    """
    thresholds_dropped = 0
    duplicates_dropped = 0

    for panel in _iter_grafana_panels(dashboard.get("panels")):
        field_config = panel.get("fieldConfig") or {}

        if panel.get("type") == "table":
            defaults = field_config.get("defaults") or {}
            if isinstance(defaults, dict) and defaults.pop("thresholds", None) is not None:
                thresholds_dropped += 1

        seen: set[str] = set()
        for override in field_config.get("overrides") or []:
            kept = []
            for prop in override.get("properties") or []:
                prop_id = prop.get("id")
                if prop_id in seen:
                    duplicates_dropped += 1
                    continue
                seen.add(prop_id)
                kept.append(prop)
            override["properties"] = kept

    return thresholds_dropped, duplicates_dropped


def _query_placeholder_report(dashboard: dict[str, Any]) -> tuple[int, int]:
    """Cuenta (queries totales, queries placeholder) del dashboard migrado."""
    total = 0
    placeholders = 0
    for panel in (dashboard.get("spec", {}) or {}).get("panels", {}).values() or []:
        for query in (panel.get("spec") or {}).get("queries") or []:
            total += 1
            if _MIGRATION_PLACEHOLDER in json.dumps(query):
                placeholders += 1
    return total, placeholders


def migrate_grafana(client: httpx.Client, path: str) -> Optional[dict[str, Any]]:
    """Traduce un dashboard de Grafana al modelo Perses via POST /api/migrate.

    El resultado se cachea por sha256 del archivo fuente: la traduccion no depende
    del proyecto, asi que se calcula una sola vez por dashboard.
    """
    try:
        raw = open(path, "rb").read()
    except OSError:
        logger.warning("No se pudo leer el dashboard fuente %s", path)
        return None

    digest = hashlib.sha256(raw).hexdigest()
    cache_path = os.path.join(MIGRATED_CACHE_DIR, f"{digest}.{_SANITIZER_VERSION}.json")
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cached = json.load(fh)
        if isinstance(cached, dict) and cached.get("kind") == "Dashboard":
            return cached
    except (OSError, ValueError):
        pass

    try:
        grafana_dashboard = json.loads(raw)
    except ValueError as exc:
        logger.warning("JSON invalido en %s: %s", path, exc)
        return None

    thresholds, duplicates = sanitize_grafana_dashboard(grafana_dashboard)
    if thresholds or duplicates:
        logger.info(
            "Saneado de %s: %s umbrales de tabla y %s overrides duplicados descartados.",
            os.path.basename(path), thresholds, duplicates,
        )

    resp = client.post(f"{PARSES_MIGRATE_URL}/migrate", json={"grafanaDashboard": grafana_dashboard})
    if resp.status_code not in (200, 201):
        logger.warning("Migrate fallo para %s: %s %s", path, resp.status_code, resp.text[:200])
        return None

    migrated = resp.json()
    panels = len(migrated.get("spec", {}).get("panels", {}))
    logger.info("Dashboard '%s' migrado a Perses (%s paneles).", os.path.basename(path), panels)

    try:
        os.makedirs(MIGRATED_CACHE_DIR, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(migrated, fh)
    except OSError as exc:
        logger.debug("No se pudo cachear la migracion de %s: %s", path, exc)
    return migrated


def bind_metrics_datasource(dashboard: dict[str, Any], datasource_name: str) -> int:
    """Fija el datasource del tenant en un dashboard migrado.

    /api/migrate deja la variable de datasource como `DatasourceVariable` (un
    dropdown vacio en la UI). Se reemplaza por una `StaticListVariable` con un unico
    valor, de modo que todas las queries que referencian `$<variable>` resuelven al
    datasource provisionado del tenant.

    De paso oculta las variables que el migrador no sabe traducir (adhoc filters y
    similares, que quedan con los valores `grafana/migration/not/supported`): son
    widgets muertos que solo confunden al usuario.

    Devuelve cuantos bindings se aplicaron (0 = el dashboard no trae selector de
    datasource, por ejemplo solo variables de label).
    """
    bound = 0
    for variable in (dashboard.get("spec", {}) or {}).get("variables", []) or []:
        spec = variable.get("spec") or {}
        plugin = spec.get("plugin") or {}

        if plugin.get("kind") == "DatasourceVariable":
            spec["plugin"] = {
                "kind": "StaticListVariable",
                "spec": {"values": [datasource_name]},
            }
            spec["defaultValue"] = datasource_name
            spec["allowAllValue"] = False
            spec["allowMultiple"] = False
            spec.setdefault("display", {})["hidden"] = True
            bound += 1
        elif (
            plugin.get("kind") == "StaticListVariable"
            and (plugin.get("spec") or {}).get("values") == _MIGRATION_UNSUPPORTED_VALUES
        ):
            spec.setdefault("display", {})["hidden"] = True
    return bound


def _normalize_list_variable_defaults(dashboard: dict[str, Any]) -> int:
    """Restaura el valor por defecto ``$__all`` de las variables de lista.

    Perses representa la opcion "All" con la cadena ``$__all`` (no con una lista).
    El migrador de /api/migrate deja dos situaciones que impiden que las variables
    arranquen con todas las opciones seleccionadas:

      - ``defaultValue: ["$__all"]`` (lista heredada de ``current.value`` de
        Grafana): el valor no coincide con la opcion "All" y se usa literalmente
        como valor de la variable (``job=~"$__all"`` -> sin coincidencias);
      - sin ``defaultValue``: Perses selecciona la primera opcion, asi que los
        paneles quedan filtrados a un unico job/instance.

    Devuelve cuantas variables se tocaron.
    """
    patched = 0
    for variable in (dashboard.get("spec", {}) or {}).get("variables", []) or []:
        spec = variable.get("spec") or {}
        kind = (spec.get("plugin") or {}).get("kind")
        if kind in ("StaticListVariable", "DatasourceVariable"):
            continue
        default = spec.get("defaultValue")
        if isinstance(default, list):
            if "$__all" in default:
                spec["defaultValue"] = "$__all"
                patched += 1
        elif default is None and spec.get("allowAllValue"):
            spec["defaultValue"] = "$__all"
            patched += 1
    return patched


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "dashboard"


def _ensure_dashboard(client: httpx.Client, project: str, name: str, payload: dict[str, Any]) -> None:
    existing = client.get(f"{PARSES_API_URL}/projects/{project}/dashboards/{name}")
    if existing.status_code == 200:
        resp = client.put(f"{PARSES_API_URL}/projects/{project}/dashboards/{name}", json=payload)
    else:
        resp = client.post(f"{PARSES_API_URL}/dashboards", json=payload)
        if resp.status_code == 409:
            resp = client.put(f"{PARSES_API_URL}/projects/{project}/dashboards/{name}", json=payload)
    if resp.status_code not in (200, 201):
        logger.warning("Alta de dashboard %s fallo: %s %s", name, resp.status_code, resp.text[:200])


def _provision_metrics_dashboards(
    client: httpx.Client,
    project: str,
    datasource_name: str,
) -> list[str]:
    """Migra y publica los dashboards fuente de Grafana para el tenant interno METRICS.

    El project del tenant tiene un unico datasource, asi que el nombre del dashboard
    no necesita sufijo: no hay colision entre tenant y tenant.
    """
    if not os.path.isdir(GRAPHANA_DASHBOARDS_DIR):
        logger.info("Sin dashboards fuente en %s", GRAPHANA_DASHBOARDS_DIR)
        return []

    deployed: list[str] = []
    for filename in sorted(os.listdir(GRAPHANA_DASHBOARDS_DIR)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(GRAPHANA_DASHBOARDS_DIR, filename)
        migrated = migrate_grafana(client, path)
        if migrated is None:
            continue

        total_queries, placeholder_queries = _query_placeholder_report(migrated)
        if total_queries and placeholder_queries == total_queries:
            logger.warning(
                "Dashboard '%s' omitido: sus %s queries dependen de un datasource de "
                "Grafana que Perses no traduce (se migran a '%s'). Requiere el plugin "
                "de origen, no PromQL.",
                filename, total_queries, _MIGRATION_PLACEHOLDER,
            )
            continue
        if placeholder_queries:
            logger.warning(
                "Dashboard '%s': %s de %s queries quedaron como placeholder y no "
                "mostraran datos.", filename, placeholder_queries, total_queries,
            )

        stem = os.path.splitext(filename)[0]
        name = _slug(stem)
        bound = bind_metrics_datasource(migrated, datasource_name)
        defaults = _normalize_list_variable_defaults(migrated)
        migrated["metadata"]["name"] = name
        migrated["metadata"]["project"] = project

        _ensure_dashboard(client, project, name, migrated)
        deployed.append(name)
        logger.info(
            "Dashboard '%s' desplegado con datasource %s (%s bindings, %s defaults de variables).",
            name, datasource_name, bound, defaults,
        )
    return deployed


def provision_native_dashboards(
    client: httpx.Client,
    project: str,
    datasources_by_kind: dict[str, str],
) -> list[str]:
    """Publica los dashboards nativos de Anomalia, resolviendo sus datasources.

    Cada query del template declara el tipo de datasource que necesita con el marcador
    `xAnomaliaDatasource`. Se reemplaza por el selector concreto
    `{"kind": ..., "name": ...}` del datasource del usuario. Los paneles cuyo tipo de
    datasource no esta disponible (el usuario no tiene ese tenant) se descartan: es
    preferible un dashboard con menos secciones que uno con paneles rotos.
    """
    if not os.path.isdir(NATIVE_DASHBOARDS_DIR):
        logger.info("Sin dashboards nativos en %s", NATIVE_DASHBOARDS_DIR)
        return []

    deployed: list[str] = []
    for filename in sorted(os.listdir(NATIVE_DASHBOARDS_DIR)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(NATIVE_DASHBOARDS_DIR, filename)
        try:
            with open(path, encoding="utf-8") as fh:
                dashboard = json.load(fh)
        except (OSError, ValueError) as exc:
            logger.warning("Dashboard nativo ilegible %s: %s", path, exc)
            continue

        panels: dict[str, Any] = {}
        dropped: list[str] = []
        for key, panel in (dashboard.get("spec", {}).get("panels", {}) or {}).items():
            if not _resolve_panel_datasources(panel, datasources_by_kind):
                dropped.append((panel.get("spec", {}).get("display", {}) or {}).get("name", key))
                continue
            panels[key] = panel

        if not panels:
            logger.info("Dashboard nativo '%s' omitido: el usuario no tiene ningun datasource aplicable.", filename)
            continue

        dashboard["spec"]["panels"] = panels

        # Reconstruir layouts para que solo referencien paneles supervivientes: al
        # descartar paneles sin datasource, un Grid con `$ref` a un panel eliminado
        # hace que Perses rechace el dashboard ("no panel found for ref ..."). Los
        # Grids que quedan vacios se omiten; el resto de layouts se conserva tal cual.
        surviving = set(panels.keys())
        new_layouts: list[dict[str, Any]] = []
        for lay in (dashboard.get("spec", {}).get("layouts", []) or []):
            if lay.get("kind") == "Grid":
                grid_spec = dict((lay.get("spec") or {}))
                items = []
                for it in (grid_spec.get("items") or []):
                    ref = ((it.get("content") or {}).get("$ref") or "")
                    key = ref.rsplit("/", 1)[-1] if ref else ""
                    if key in surviving:
                        items.append(it)
                if not items:
                    continue
                grid_spec["items"] = items
                new_lay = dict(lay)
                new_lay["spec"] = grid_spec
                new_layouts.append(new_lay)
            else:
                new_layouts.append(lay)
        dashboard["spec"]["layouts"] = new_layouts

        name = dashboard.get("metadata", {}).get("name") or os.path.splitext(filename)[0]
        dashboard["metadata"]["name"] = name
        dashboard["metadata"]["project"] = project

        _ensure_dashboard(client, project, name, dashboard)
        deployed.append(name)
        if dropped:
            logger.info(
                "Dashboard nativo '%s': %s de %s paneles (sin datasource: %s).",
                name, len(panels), len(panels) + len(dropped), ", ".join(dropped),
            )
        else:
            logger.info("Dashboard nativo '%s' desplegado con %s paneles.", name, len(panels))
    return deployed


def _resolve_panel_datasources(panel: dict[str, Any], datasources_by_kind: dict[str, str]) -> bool:
    """Sustituye los marcadores `xAnomaliaDatasource` por selectores reales.

    Devuelve False si al panel le falta algun datasource requerido.
    """
    for query in (panel.get("spec", {}) or {}).get("queries", []) or []:
        spec = ((query.get("spec") or {}).get("plugin") or {}).get("spec") or {}
        required = spec.pop("xAnomaliaDatasource", None)
        if not required:
            continue
        name = datasources_by_kind.get(required)
        if not name:
            return False
        spec["datasource"] = {"kind": required, "name": name}
    return True


def provision_tenant_parses(tenant_id: int) -> dict[str, Any]:
    """Idempotente: crea/actualiza el project, datasource y dashboards del tenant.

    Se ejecuta dentro del job orquestado de alta (`tenant_create`) y en el catch-up
    de arranque (spec 008). Una falla aqui debe propagarse: el orquestador la trata
    como fallo de fase y dispara el rollback total. Ante errores transitorios
    (Parses aún arrancando, 5xx, timeout) se reintenta hasta 3 veces con backoff; el
    provisioning es idempotente (GET->POST/PUT) por lo que re-ejecutar es seguro.
    """
    backoff = [3, 6]  # segundos de espera entre intentos
    last_error: Optional[Exception] = None
    outputs: dict[str, Any] = {
        "tenant_id": tenant_id,
        "project": None,
        "datasources": [],
        "dashboards": [],
        "native_dashboards": [],
        "error": None,
    }
    for attempt in range(1, 4):
        try:
            tenant = tenant_datasource_row(tenant_id)
            if tenant is None:
                raise ValueError(f"Tenant {tenant_id} sin datasource habilitado")
            project = tenant_parses_project_name(tenant["slug"])
            token = mint_tenant_parses_token(tenant_id)
            outputs["project"] = project
            with httpx.Client(timeout=30.0) as client:
                _ensure_project(client, project, tenant.get("display_name") or tenant["name"])
                proxy_secret = _ensure_proxy_ca_secret(client, project)

                # El project es del tenant y tiene un unico datasource: siempre es el
                # default de su plugin (resuelve las variables ListVariable).
                payload = _datasource_payload(project, tenant, token, proxy_secret, is_default=True)
                datasources_by_kind: dict[str, str] = {}
                if payload is not None:
                    ds_name = payload["metadata"]["name"]
                    _ensure_datasource(client, payload, ds_name, project)
                    outputs["datasources"].append(tenant["name"])
                    datasources_by_kind[payload["spec"]["plugin"]["kind"]] = ds_name

                # Los dashboards son exclusivos de los tenants internos: un tenant
                # creado por el usuario arranca con su project vacio (no hereda los
                # dashboards default del stack).
                if tenant["is_internal"]:
                    if payload is not None and (tenant["type"] or "").lower() == "metrics":
                        outputs["dashboards"].extend(
                            _provision_metrics_dashboards(client, project, ds_name)
                        )
                    outputs["native_dashboards"].extend(
                        provision_native_dashboards(client, project, datasources_by_kind)
                    )
            break
        except Exception as exc:  # noqa: BLE001 - se propaga tras agotar reintentos
            last_error = exc
            logger.warning(
                "Provision Parses para tenant %s: intento %s/3 falló (%s%s)",
                tenant_id, attempt, type(exc).__name__,
                " - reintentando" if attempt < 3 else "",
            )
            if attempt < 3:
                time.sleep(backoff[attempt - 1])
    else:
        logger.exception("No se pudo provisionar Parses para tenant %s", tenant_id)
        outputs["error"] = str(last_error)
        raise RuntimeError(f"parses_provision tenant {tenant_id}: {last_error}") from last_error
    return outputs


def _delete_project(client: httpx.Client, project: str) -> bool:
    """Borra un project de Perses (idempotente: 404 = ya no existe)."""
    resp = client.delete(f"{PARSES_API_URL}/projects/{project}")
    if resp.status_code in (200, 202, 204, 404):
        logger.info("Project Parses '%s' retirado (%s).", project, resp.status_code)
        return True
    logger.warning("No se pudo borrar el project Parses '%s': %s %s", project, resp.status_code, resp.text[:200])
    return False


def _delete_dashboard(client: httpx.Client, project: str, name: str) -> bool:
    """Borra un dashboard de un project de Perses (idempotente: 404 = ya no existe)."""
    resp = client.delete(f"{PARSES_API_URL}/projects/{project}/dashboards/{name}")
    if resp.status_code in (200, 202, 204, 404):
        logger.info("Dashboard Parses '%s' retirado de '%s' (%s).", name, project, resp.status_code)
        return True
    logger.warning(
        "No se pudo borrar el dashboard '%s' de '%s': %s %s", name, project, resp.status_code, resp.text[:200]
    )
    return False


def delete_tenant_parses(tenant_id: int) -> dict[str, Any]:
    """Borra el project de Parses del tenant (hard-delete, spec 008/011).

    Idempotente. Resuelve el slug aun cuando el tenant ya no este 'active': lee la
    fila de `tenants` sin exigir datasource habilitado.
    """
    outputs: dict[str, Any] = {"tenant_id": tenant_id, "project": None, "deleted": False, "error": None}
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT slug FROM tenants WHERE id = %s;", (tenant_id,))
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    if not row:
        return outputs
    project = tenant_parses_project_name(row[0])
    outputs["project"] = project
    try:
        with httpx.Client(timeout=30.0) as client:
            outputs["deleted"] = _delete_project(client, project)
    except Exception as exc:  # noqa: BLE001
        outputs["error"] = str(exc)
        logger.warning("delete_tenant_parses(%s) falló: %s", tenant_id, exc)
        raise
    return outputs


def list_parses_projects() -> list[str]:
    """Listado de projects existentes en Perses (para limpieza idempotente)."""
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{PARSES_API_URL}/projects")
        if resp.status_code != 200:
            raise RuntimeError(f"GET projects: {resp.status_code} {resp.text[:200]}")
        payload = resp.json()
    projects: list[str] = []
    items = payload if isinstance(payload, list) else (payload.get("items") or [])
    for item in items:
        name = ((item or {}).get("metadata") or {}).get("name")
        if isinstance(name, str) and name:
            projects.append(name)
    return projects


def delete_legacy_user_parses() -> list[str]:
    """Retira los projects per-usuario del diseno anterior (spec 008).

    Solo borra los que matchean `anomalia-<10 hex>`; nunca toca
    `anomalia-tenant-<slug>`. Idempotente; tolera que Parses no responda.
    """
    try:
        names = list_parses_projects()
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete_legacy_user_parses: no se pudo listar projects: %s", exc)
        return []
    removed: list[str] = []
    with httpx.Client(timeout=30.0) as client:
        for name in names:
            if LEGACY_USER_PROJECT_RE.match(name):
                if _delete_project(client, name):
                    removed.append(name)
    if removed:
        logger.info("Projects per-usuario retirados: %s", ", ".join(removed))
    return removed


LEGACY_STACK_DASHBOARD = "anomalia-stack"


def delete_legacy_native_stack_dashboards() -> list[str]:
    """Retira los dashboards `anomalia-stack` ya desplegados (spec 008).

    El dashboard nativo monolitico se dividio en `anomalia-metrics`,
    `anomalia-traces` y `anomalia-profiles`; el fuente `anomalia_stack.json`
    ya no existe, pero los dashboards desplegados en los projects per-tenant
    quedan hasta que se limpien. Solo toca projects `anomalia-tenant-*`;
    idempotente; tolera que Parses no responda.
    """
    try:
        names = list_parses_projects()
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete_legacy_native_stack_dashboards: no se pudo listar projects: %s", exc)
        return []
    removed: list[str] = []
    with httpx.Client(timeout=30.0) as client:
        for project in names:
            if not project.startswith("anomalia-tenant-"):
                continue
            if _dashboard_exists(client, project, LEGACY_STACK_DASHBOARD):
                removed.append(f"{project}/{LEGACY_STACK_DASHBOARD}")
                _delete_dashboard(client, project, LEGACY_STACK_DASHBOARD)
    if removed:
        logger.info("Dashboards nativos legacy retirados: %s", ", ".join(removed))
    return removed


def _dashboard_exists(client: httpx.Client, project: str, name: str) -> bool:
    return client.get(f"{PARSES_API_URL}/projects/{project}/dashboards/{name}").status_code == 200