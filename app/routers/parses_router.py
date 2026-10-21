from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from typing import Any, Optional
import httpx
import json
import jwt

from ..database import get_db_connection
from ..auth import verify_any_user_token, verify_tenant_access, SECRET_KEY, ALGORITHM
from ..parses_provisioner import user_tenant_rows

router = APIRouter(prefix="/api/v1/parses", tags=["Parses"])

# Tope de la respuesta del backend Victoria. Evita que un dashboard con un rango
# enorme agote la memoria del gateway.
MAX_UPSTREAM_BYTES = 64 * 1024 * 1024

# Timeout de la llamada al backend Victoria. Los paneles de Perses consultan
# endpoints rapidos (/api/v1/query, /api/v1/labels); el render de Pyroscope es
# el mas pesado y aun asi responde en segundos.
UPSTREAM_TIMEOUT_SECONDS = 30.0

# Metodos permitidos. El proxy es de solo lectura: Perses usa GET para consultas
# y POST para las APIs basadas en body (LogsQL, render de Pyroscope).
ALLOWED_METHODS = {"GET", "POST"}

# Prefijos de routing que cada backend expone, ya con el scoping del tenant resuelto.
# Se derivan de los valores de tenant_datasources, no de lo que pida el cliente.
#
# Deben coincidir con lo que realmente pide cada plugin de Perses. Verificado
# extrayendo los chunks del build desplegado (0.7.0-beta.6 / 0.5.0-beta.6):
#   VictoriaLogs   -> /select/logsql/query, ...      (el prefijo ya viene en la ruta)
#   VictoriaTraces -> /api/services, /api/traces     (el prefijo NO viene)
#   Pyroscope      -> /querier.v1.QuerierService/... (en la raiz, sin prefijo)
PATH_SCOPED_PREFIX = "/select/{account_id}:{project_id}/prometheus"
HEADER_SCOPED_PREFIXES = {"/select/logsql", "/select/jaeger", "/querier.v1."}

# Los query params temporales del API Jaeger (start/end) viajan sin tocar. El
# estandar de Jaeger usa microsegundos y VictoriaTraces v0.12.0 lo confirma:
# medido contra /select/jaeger/api/traces con el mismo rango de 1h y 24h,
#   - microsegundos -> 200 en 8ms con las trazas del tenant
#   - nanosegundos  -> 200 en 30.000ms (= UPSTREAM_TIMEOUT_SECONDS) y total 0
#   - segundos      -> 400 'request time out of retention'
# El plugin Jaeger de Perses emite microsegundos (1e3 * Date.getTime()). La
# conversion x1000 a nanosegundos que existio aqui rompia la busqueda de trazas;
# ver specs/008 §7.


def _load_datasource(tenant_id: int) -> dict[str, Any]:
    """Lee la definicion de lectura del tenant. Devuelve 404 si no existe o esta deshabilitada."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT t.name, t.type, d.kind, d.read_url, d.scoping,
                   d.account_id, d.project_id, d.org_id
            FROM tenant_datasources d
            JOIN tenants t ON t.id = d.tenant_id
            WHERE d.tenant_id = %s AND d.enabled = TRUE
            """,
            (tenant_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"El tenant {tenant_id} no tiene datasource de lectura habilitado.",
            )
        return {
            "name": row[0],
            "type": row[1],
            "kind": row[2],
            "read_url": row[3].rstrip("/"),
            "scoping": row[4],
            "account_id": row[5],
            "project_id": row[6],
            "org_id": row[7],
        }
    finally:
        cursor.close()
        conn.close()


def _reject_path_traversal(path: str) -> None:
    """Impide que la ruta del cliente redirija la consulta a otro host o servicio.

    Sin esto, un path como 'http://otro-servicio/...' o '../../otro' escaparia del
    read_url resuelto desde la base de datos y convertiria al gateway en un proxy
    abierto a cualquier destino alcanzable desde la red de contenedores.
    """
    if "://" in path:
        raise HTTPException(status_code=400, detail="Ruta de datasource invalida.")
    if ".." in path:
        raise HTTPException(status_code=400, detail="Ruta de datasource invalida.")
    if not path or path.startswith("/"):
        raise HTTPException(status_code=400, detail="Ruta de datasource invalida.")


def _build_target(ds: dict[str, Any], path: str) -> tuple[str, dict[str, str]]:
    """Compone la URL final y los headers de aislamiento usando solo valores de la BD."""
    headers: dict[str, str] = {}

    if ds["scoping"] == "path":
        if ds["account_id"] is None or ds["project_id"] is None:
            raise HTTPException(
                status_code=500,
                detail=f"El tenant {ds['name']} declara scoping por path sin account/project id.",
            )
        prefix = PATH_SCOPED_PREFIX.format(
            account_id=ds["account_id"], project_id=ds["project_id"]
        )
        return f"{ds['read_url']}{prefix}/{path.lstrip('/')}", headers

    # scoping == 'header': el backend se selecciona por prefijo y el tenant viaja en headers.
    normalized = f"/{path.lstrip('/')}"
    if not any(normalized.startswith(p) for p in HEADER_SCOPED_PREFIXES):
        raise HTTPException(
            status_code=400,
            detail=f"Ruta no permitida para el backend del tenant {ds['name']}.",
        )
    if ds["org_id"]:
        headers["X-Scope-OrgID"] = str(ds["org_id"])
    else:
        if ds["account_id"] is None or ds["project_id"] is None:
            raise HTTPException(
                status_code=500,
                detail=f"El tenant {ds['name']} declara scoping por header sin account/project id.",
            )
        headers["AccountID"] = str(ds["account_id"])
        headers["ProjectID"] = str(ds["project_id"])
    return f"{ds['read_url']}{normalized}", headers


# VictoriaTraces no implementa el endpoint Jaeger /api/operations (responde 400
# "unsupported path requested"), pero el editor de consultas de Perses llama a
# searchOperations() para poblar el autocomplete de operacion. Se sintetiza aqui
# muestreando las trazas recientes del MISMO tenant, con el mismo aislamiento que
# cualquier otra llamada de este proxy: no se abre superficie de consulta nueva.
JAEGER_OPERATIONS_PATH = "select/jaeger/api/operations"
_JAEGER_OPERATIONS_SAMPLE_TRACES = 200

_JAEGER_OPERATIONS_EMPTY = json.dumps({"data": []})


def _jaeger_operations_response(names: list[str]) -> Response:
    """Payload que consume el plugin Jaeger de Perses.

    searchOperations() hace `.map(e => e.name)` sobre `data`, asi que cada entrada
    lleva `name` (verificado en Jaeger_JaegerTraceQuery.js del build desplegado).
    """
    payload = json.dumps({"data": [{"name": name} for name in names]})
    return Response(content=payload, media_type="application/json")


async def _synthesize_jaeger_operations(ds: dict[str, Any], request: Request) -> Response:
    """Deriva la lista de operaciones a partir de un muestreo de trazas del tenant."""
    service = (request.query_params.get("service") or "").strip()
    if not service:
        return Response(content=_JAEGER_OPERATIONS_EMPTY, media_type="application/json")

    target_url, headers = _build_target(ds, "select/jaeger/api/traces")
    params: list[tuple[str, str]] = [
        ("service", service),
        ("limit", str(_JAEGER_OPERATIONS_SAMPLE_TRACES)),
    ]

    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS, follow_redirects=False) as client:
            upstream = await client.get(url=target_url, params=params, headers=headers)
    except (httpx.TimeoutException, httpx.HTTPError):
        return Response(content=_JAEGER_OPERATIONS_EMPTY, media_type="application/json")

    if upstream.status_code >= 400:
        return Response(content=_JAEGER_OPERATIONS_EMPTY, media_type="application/json")

    try:
        traces: Any = upstream.json().get("data") or []
    except ValueError:
        return Response(content=_JAEGER_OPERATIONS_EMPTY, media_type="application/json")

    seen: set[str] = set()
    names: list[str] = []
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        for span in trace.get("spans") or []:
            if not isinstance(span, dict):
                continue
            name = span.get("operationName")
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)

    return _jaeger_operations_response(names)


@router.get("/datasources")
def list_my_datasources(payload: dict = Depends(verify_any_user_token)) -> dict[str, list[dict[str, Any]]]:
    """Devuelve los datasources de lectura que el usuario autenticado puede consultar.

    Es la contraparte de /tenants/my-tenants: el frontend puede pintar el selector de
    tenant sin conocer la topologia de Victoria.
    """
    username = payload.get("sub") or payload.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Token sin identidad de usuario.")

    return {"datasources": user_tenant_rows(username)}


def _extract_parses_token(request: Request) -> dict:
    """Extrae y valida el token de proxy en el header X-Anomalia-Token.

    Perses reenvía headers custom a través de su HTTPProxy, pero FILTRA el header
    Authorization (verificado empiricamente en 008). El token viaja entonces en un
    header propio; el frontend/servidor Parses nunca expone el token al navegador.
    """
    raw = request.headers.get("x-anomalia-token") or request.headers.get("x-anom-token")
    if not raw:
        raise HTTPException(status_code=401, detail="Token de proxy de Parses ausente.")
    token = raw.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Token de proxy de Parses invalido o expirado.")
    if payload.get("purpose") != "parses-proxy":
        raise HTTPException(status_code=401, detail="Token de proxy de Parses invalido.")
    return payload


def _authorize_tenant(request: Request, tenant_id: int) -> dict:
    """Valida el token de proxy y aplica la politica de acceso al tenant.

    FastAPI inyecta Request y resuelve tenant_id desde el path (coincide con el
    nombre del parametro de la ruta). Hay dos tipos de token con
    `purpose=parses-proxy`:

      - token de servicio por-tenant (`svc=true`, generado por el provisionador):
        el project de Perses pertenece al tenant, asi que basta comprobar que el
        `tenant_id` del path pertenezca a su claim `tenants`;
      - token de usuario legado (`sub=<username>`, `tenants=[ids]`): se revalida
        contra user_tenants ∪ role_tenants en la base con verify_tenant_access.

    Un token valido nunca amplía permisos: los valores de aislamiento salen de la
    BD en ambos casos.
    """
    payload = _extract_parses_token(request)
    if payload.get("svc"):
        allowed = [int(t) for t in (payload.get("tenants") or [])]
        if int(tenant_id) not in allowed:
            raise HTTPException(status_code=403, detail="El token de servicio no cubre el tenant solicitado.")
        return payload
    return verify_tenant_access(tenant_id)(payload)


@router.get("/datasources/{tenant_id}/{path:path}", operation_id="query_datasource_get")
@router.post("/datasources/{tenant_id}/{path:path}", operation_id="query_datasource_post")
async def proxy_datasource(
    tenant_id: int,
    path: str,
    request: Request,
    payload: dict = Depends(_authorize_tenant),
) -> Response:
    """Proxy de lectura con aislamiento por tenant.

    El tenant llega en el path y se valida contra user_tenants ∪ role_tenants en la base.
    Los valores de aislamiento (AccountID/ProjectID/X-Scope-OrgID) salen de
    tenant_datasources, nunca del request, de modo que un cliente no puede pedir el
    namespace de otro tenant manipulando query params o headers.
    """
    if not payload:
        raise HTTPException(status_code=401, detail="Token sin identidad de usuario.")

    if request.method not in ALLOWED_METHODS:
        raise HTTPException(status_code=405, detail="Metodo no permitido.")

    _reject_path_traversal(path)
    ds = _load_datasource(tenant_id)

    # VictoriaTraces no expone /api/operations; se sintetiza antes de componer el
    # target normal, porque este path nunca va a existir en el upstream.
    if ds["type"] == "traces" and path.lstrip("/") == JAEGER_OPERATIONS_PATH:
        return await _synthesize_jaeger_operations(ds, request)

    target_url, headers = _build_target(ds, path)

    # Se reenvian solo query params y body. Nunca los headers de autenticacion del
    # cliente, para no filtrar el JWT de Anomalia hacia el backend Victoria.
    forward_headers = dict(headers)
    content_type = request.headers.get("content-type")
    if content_type:
        forward_headers["Content-Type"] = content_type

    body: Optional[bytes] = None
    if request.method == "POST":
        body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS, follow_redirects=False) as client:
            upstream = await client.request(
                method=request.method,
                url=target_url,
                params=request.query_params,
                headers=forward_headers,
                content=body,
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout consultando el backend de telemetria.")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Error contacting telemetry backend: {e}")

    if upstream.status_code == 404:
        raise HTTPException(status_code=404, detail="Recurso no encontrado en el backend de telemetria.")
    if upstream.status_code >= 500:
        raise HTTPException(status_code=502, detail="El backend de telemetria fallo al procesar la consulta.")

    if len(upstream.content) > MAX_UPSTREAM_BYTES:
        raise HTTPException(status_code=413, detail="Respuesta demasiado grande.")

    media_type = upstream.headers.get("content-type", "application/json")
    return Response(content=upstream.content, status_code=upstream.status_code, media_type=media_type)
