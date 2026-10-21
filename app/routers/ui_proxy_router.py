"""Proxy autenticado de las UIs de Victoria y de Parses por tenant.

Problema que resuelve
---------------------
El token JWT de Anomalia vive en ``localStorage``, asi que no viaja en la
navegacion: al abrir la UI de Victoria en otra pestana el navegador no envia
ninguna credencial y el proxy no tiene forma de saber quien es el usuario.
Ademas los puertos de lectura de Victoria (8401/8481/8491/4040) estan aislados
por firewall y no son alcanzables desde la LAN, asi que un link directo falla.

Flujo implementado
------------------
1. El frontend, con su JWT en el header ``Authorization``, pide un ticket
   ``POST /api/v1/ui/ticket {tenant_id, scope}``. Ahi si hay credencial: se
   valida el token, ``verify_tenant_access`` re-chequea la pertenencia del
   tenant en la base y ``scope`` determina que perfil se exige para esa
   tarjeta (UI_SCOPE_PROFILE).
2. Se emite un ticket opaco de un solo uso con vida corta y se navega a
   ``/ui/redeem/{ticket}``, que no requiere header.
3. El canje marca el ticket como consumido, deja una cookie de sesion y
   redirige al path real de la UI.
4. ``/ui/{ticket}/{ruta}`` sirve la UI y todas sus llamadas API reenviandolas al
   backend con el scoping del tenant inyectado.

El ``scope`` viaja persistido en ``ui_tickets`` y decide el upstream: los scopes
de Victoria se montan en ``read_url`` (vmui/vlui/vtui/vmui de Pyroscope), mientras
que ``dashboards`` se monta en el contenedor ``parses`` y habilita ademas los
metodos de escritura y el aislamiento por proyecto de Parses (ver ``_proxy_parses``).

Como la UI servida no vuelve a consultar la autorizacion (el ticket ya se
canjeo), la vigencia se renueva de forma deslizante mientras el usuario siga
usando la pestana (ver ``_touch_ticket``) y se pierde a los
``UI_TICKET_TTL_MINUTES`` de inactividad. Sin esa renovacion la UI moria a los
cinco minutos del canje, devolvia un JSON crudo de 401 como si fuera el
documento y entraba en un bucle de reintentos que dejaba la pantalla en blanco.

Nota sobre el modelo de scoping: los valores de AccountID/ProjectID son por
TENANT, no por usuario, y salen siempre de ``tenant_datasources``. No se acepta
ningun valor de scoping del cliente.
"""

import json
import asyncio
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from ..auth import verify_any_user_token, verify_tenant_access
from ..database import get_db_connection
from ..parses_provisioner import PARSES_PROXY_BASE, user_parses_projects

logger = logging.getLogger("anomalia.ui_proxy")

router = APIRouter(tags=["UI Proxy"])

# Vida del ticket. El canje es un solo uso, pero una vez abierta la pestana la
# vigencia se renueva en cada peticion mientras falte menos de la mitad del TTL
# (_touch_ticket). Asi la consola nunca muere con el usuario delante y una
# pestana abandonada caduca a los UI_TICKET_TTL_MINUTES de inactividad.
UI_TICKET_TTL_MINUTES = 30

# Caduca solo si queda menos de esto de vida: evita un UPDATE por cada asset.
UI_TICKET_TOUCH_THRESHOLD_MINUTES = UI_TICKET_TTL_MINUTES // 2

# Tope de la respuesta de assets (los bundles JS de las UIs rondan varios MB).
MAX_UPSTREAM_BYTES = 32 * 1024 * 1024

UPSTREAM_TIMEOUT_SECONDS = 30.0

ALLOWED_METHODS = {"GET", "POST", "OPTIONS"}

# Content types de assets que se sirven con cache. El HTML nunca se cachea:
# lleva el prefijo del ticket en sus referencias.
CACHEABLE_CONTENT_TYPES = (
    "text/javascript",
    "application/javascript",
    "text/css",
    "image/",
    "font/",
    "application/manifest+json",
)

# Donde vive la UI de cada backend, relativo a read_url.
#
# Verificado empiricamente contra los servicios desplegados:
#   VictoriaMetrics  : read_url + /select/<acc>:<proj>/vmui/   (scoping por path)
#   VictoriaTraces   : read_url + /select/vmui/                 (scoping por header)
#   VictoriaLogs     : read_url + /select/vmui/                 (scoping por header)
#   Pyroscope        : read_url + /                             (scoping por header)
#
# VictoriaMetrics exige el prefijo /select/<accountID>:<projectID>/ dentro de la
# ruta y responde 400 sin el; VictoriaTraces y VictoriaLogs lo llevan en el
# header; Pyroscope corre en la raiz de su read_url.
UI_MOUNT_BY_KIND = {
    "VictoriaMetrics": "/select/{account_id}:{project_id}/vmui/",
    "VictoriaTraces": "/select/vmui/",
    "VictoriaLogs": "/select/vmui/",
    "Pyroscope": "/",
}

# Prefijo bajo el que se sirve la UI.
#
# vmui/vlui/vtui derivan su base desde window.location.href quitando el sufijo
# /vmui/..., asi que montarlas bajo una ruta propia funciona sin reescribir ni
# el HTML ni los bundles. Pyroscope declara <base href="/"> y si requiere
# reescritura (ver _rewrite_base_href).
UI_BASE_PREFIX = "/ui"

# Ruta de canje del ticket. Va separada del montaje de la consola porque
# Pyroscope se monta en "/": con el canje en /ui/{ticket}/ las dos rutas se
# superpondrian y se generaria un redirect infinito (redeem -> "/" -> redeem).
UI_REDEEM_PATH = "/ui/redeem/{ticket}"

# Pagina que sustituye al JSON crudo cuando el navegador pide una consola cuyo
# ticket ya no tiene vigencia. Un 401 con body JSON en la ventana principal se
# lee como pantalla en blanco; ademas el bucle de reintentos de la UI seguia
# martilleando el proxy. Reutiliza las variables y clases de
# frontend/css/styles.css: el color no se define aqui.
_CONSOLE_EXPIRED_HTML = """<!DOCTYPE html>
<html lang="es" class="theme-enterprise-blue">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="/css/styles.css">
  <title>Consola expirada | Anomalia</title>
  <style>
    body { min-height: 100vh; display: flex; align-items: center; justify-content: center; margin: 0; font-family: system-ui, sans-serif; }
    .expired { max-width: 28rem; padding: 2rem; border: 1px solid var(--border-color); border-radius: 1rem; text-align: center; }
    .expired h1 { font-size: 1.1rem; margin: 0 0 .75rem; }
    .expired p { color: var(--text-muted); font-size: .875rem; line-height: 1.5; margin: 0 0 1.5rem; }
    .expired a { display: inline-block; padding: .65rem 1.25rem; border-radius: .75rem; background: var(--accent); color: #fff; text-decoration: none; font-size: .875rem; }
    .expired a:hover { background: var(--accent-hover); }
  </style>
</head>
<body>
  <main class="expired dynamic-card">
    <h1 class="dynamic-text-accent">La consola expiro</h1>
    <p>__DETAIL__</p>
    <p>Vuelve al panel de Anomalia y abre la tarjeta de nuevo para continuar.</p>
    <a href="/">Volver a Anomalia</a>
  </main>
</body>
</html>
"""

# Prefijos de ruta admitidos por backend cuando el scoping NO viaja en el path.
# Actua igual que HEADER_SCOPED_PREFIXES en parses_router: el proxy de UI no es
# un escape al resto de endpoints del nodo de lectura.
#
#   VictoriaTraces/VictoriaLogs: la UI vive en select/vmui/ y sus llamadas en
#     select/jaeger/... y select/logsql/... -> basta exigir que arranque en
#     select/, que es un unico prefijo que cubre los tres.
#   Pyroscope: SPA servida desde la raiz. Sus assets cuelgan de assets/... y su
#     API es gRPC-web bajo /querier.v1.QuerierService/... (los cinco metodos del
#     bundle: LabelNames, LabelValues, Series, SelectSeries, SelectMergeStacktraces).
#     La raiz ("/") se admite aparte en _validate_ui_path: es el HTML de la SPA.
UI_HEADER_PREFIXES = {
    "VictoriaTraces": ("select/",),
    "VictoriaLogs": ("select/",),
    "Pyroscope": (
        "assets/",
        "icons/",
        "querier.v1.",
        "pyroscope/",
        "static/",
        "favicon",
        "manifest",
        "robots",
        "index.html",
    ),
}

# vmui consulta /admin/tenants (en la raiz de vmselect, no bajo /select/0:0/) para
# llenar su selector de tenant. Sin admitirla el selector devolvia 403. Se admite
# unicamente el endpoint y su respuesta se recorta al tenant autorizado en
# _filter_admin_tenants, de modo que no se filtran los IDs de otros tenants.
ADMIN_TENANTS_PATH = "admin/tenants"

# Pyroscope declara <base href="/" /> en su HTML, lo que hace que todos sus
# assets y llamadas relativas apunten a la raiz y escapen del prefijo del
# ticket. Solo se reescribe ese atributo: los bundles JS no se tocan.
PYROSCOPE_BASE_HREF = '<base href="/" />'

# Perfil requerido por cada tarjeta que abre una consola. El mismo tenant puede
# abrirse desde varias tarjetas con permisos distintos (METRICS abre tanto la
# vista de metricas como la de dashboards, AUDIT_LOGS abre la de logs y la de
# auditoria), asi que el perfil no se puede deducir del tenant_id: viaja en el
# request y se chequea contra la base, resolviendo roles y perfiles del usuario
# en el momento de emitir el ticket.
UI_SCOPE_PROFILE = {
    "metrics": "access_metrics",
    "logs": "access_logs",
    "traces": "access_traces",
    "profiling": "access_Continuous_Profiling",
    "dashboards": "access_dasboards",
    "audit": "audit",
    "vmalert": "alerts_manager",
    "alertmanager_global": "alerts_manager",
}

# ---------------------------------------------------------------------------
# Parses (Perses)
# ---------------------------------------------------------------------------

# Alcance cuyo upstream es Parses en vez de una UI de Victoria.
PARSES_SCOPE = "dashboards"

# Scopes de consola de administracion que se montan en la raiz del ticket, sin
# scoping de Victoria: Parses, el vmalert del tenant y el Alertmanager global.
# La consola queda en /ui/{ticket}/ y el proxy antepone el upstream resuelto.
VMALERT_SCOPE = "vmalert"
ALERTMANAGER_SCOPE = "alertmanager_global"
ROOT_MOUNTED_SCOPES = {PARSES_SCOPE, VMALERT_SCOPE, ALERTMANAGER_SCOPE}

# Scopes que son de solo lectura: la consola se abre para consultar, no para
# escribir. Parses queda aparte porque si admite escritura (specs/010).
ADMIN_CONSOLE_SCOPES = {VMALERT_SCOPE, ALERTMANAGER_SCOPE}

# UI de Parses dentro de la red de Anomalia (el contenedor se llama "parses").
PARSES_UI_URL = os.getenv("PARSES_UI_URL", "http://parses:8080").rstrip("/")

# Metodos que solo se admiten sobre Parses. Las consolas de Victoria son de
# solo lectura; Perses admite crear, editar y borrar dashboards.
PARSES_WRITE_METHODS = {"PUT", "PATCH", "DELETE"}

# Rutas de Parses que nunca son un documento SPA: si devuelven 404 el 404 se
# respeta en lugar de devolver el index (evita que un error de API parezca una
# pagina cargada).
_PARSES_NON_SPA_PREFIXES = ("api/", "plugins/")

# Prefijo de proyecto dentro de la API de Parses. Toda lectura/escritura de un
# documento concreto pasa por aqui, asi que cortar en este punto aislado al
# usuario de los proyectos de los demas.
_PARSES_PROJECTS_PREFIX = "api/v1/projects"

# Endpoint de forward de Perses: el navegador le manda {method, spec, body} y
# Perses hace la peticion desde dentro de la red contra el ``spec.proxy.url``
# guardado en la base de datos. El proyecto viaja en la ruta, asi que se puede
# aislar con el mismo criterio que ``/api/v1/projects``.
_PARSES_PROXY_PREFIX = "proxy/"

# Listado y escritura de datasources globales (fuera de proyecto). No se usan
# y escribirlos daria un datasource apuntando a cualquier URL de la red.
_PARSES_GLOBAL_DS_PREFIX = "api/v1/globaldatasources"

# Respuesta 404 que Perses espera leer: {"message":"document not found"}.
# Devolver el JSON {"detail": ...} de FastAPI hace que la UI muestre errores
# distintos a los que mostraria contra su propio backend.
_PARSES_NOT_FOUND = b'{"message":"document not found"}'

# Bootstrapping inyectado en el HTML de Perses.
#
# Perses resuelve sus URLs desde window.location, asi que servida bajo
# /ui/{ticket}/ pide /main.js, /api/v1/... y /plugins/... a la raiz del dominio,
# donde no hay ruta. Se le declara el prefijo (api_prefix alimenta el basename
# del router, la i18n, el proxy de datasources y el constructor de URLs de la
# API) y, como red de seguridad, se parchea fetch/XHR para prefijar cualquier
# URL relativa a la raiz que no lleve ya el prefijo.
_PARSES_BOOTSTRAP = """<script>
window.PERSES_APP_CONFIG = { api_prefix: "__PREFIX__" };
(function () {
  var p = "__PREFIX__";
  function fix(u) {
    if (typeof u !== "string") return u;
    var o = location.origin;
    if (u.indexOf(o) === 0) u = u.slice(o.length);
    if (u.charAt(0) !== "/") return u;
    if (u === p || u.indexOf(p + "/") === 0) return u;
    return p + u;
  }
  var of = window.fetch;
  if (typeof of === "function") {
    window.fetch = function (input, init) {
      if (typeof input === "string") {
        input = fix(input);
      } else if (input && typeof input.url === "string") {
        var k = fix(input.url);
        if (k !== input.url) input = new Request(k, input);
      }
      return of.call(this, input, init);
    };
  }
  var oo = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function () {
    if (arguments.length > 1) arguments[1] = fix(arguments[1]);
    return oo.apply(this, arguments);
  };
})();
</script>
"""


class UITicketRequest(BaseModel):
    # tenant_id es opcional: el scope 'alertmanager_global' abre una consola sin
    # tenant (specs/011 5.7). Para el resto de scopes es obligatorio y lo exige
    # create_ui_ticket.
    tenant_id: Optional[int] = None
    scope: str


class UITicketResponse(BaseModel):
    url: str


def _load_tenant_datasource(tenant_id: int) -> dict[str, Any]:
    """Lee tenant_datasources para el tenant. 404 si no existe o esta deshabilitado."""
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


def _node_base_url(ip: Optional[str], port: Optional[int], default_port: int) -> str:
    """Compone la URL base de un nodo de infraestructura.

    ``service_ip`` manda sobre ``ip_address`` porque el nodo puede tener la IP
    de acceso (ansible_host) y otra de servicio; para las consolas de
    administracion se alcanza por la de servicio. Misma convencion que el ABM
    de nodos.
    """
    host = ip or "127.0.0.1"
    return f"http://{host}:{port or default_port}"


def _resolve_vmalert_upstream(tenant_id: int) -> str:
    """URL base de la consola vmalert del tenant (specs/011 5.7).

    La instancia se resuelve SIEMPRE por ``tenant_vmalert_instances.tenant_id``:
    el cliente nunca elige el nodo, asi que no hay forma de pedir la consola de
    otra instancia. Exige que el tenant siga activo, que la instancia este
    ``deployed`` y habilitada.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT n.service_ip, n.ip_address, vi.port, vi.unit_name
            FROM tenant_vmalert_instances vi
            JOIN infrastructure_nodes n ON n.id = vi.instance_id
            JOIN tenants t ON t.id = vi.tenant_id
            WHERE vi.tenant_id = %s
              AND vi.status = 'deployed'
              AND vi.enabled = TRUE
              AND t.status = 'active'
              AND t.deleted_at IS NULL
            """,
            (tenant_id,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    if not row:
        raise HTTPException(
            status_code=409,
            detail=(
                f"El tenant {tenant_id} no tiene una instancia vmalert desplegada "
                "y habilitada."
            ),
        )
    return _node_base_url(row[0] or row[1], row[2], 8880)


def _resolve_alertmanager_upstream() -> str:
    """URL base de la consola global de Alertmanager (specs/011 5.7).

    Alertmanager es un nodo singleton del stack: se toma el activo del
    inventario. No hay tenant implicado, por eso el control de acceso es el
    perfil ``alerts_manager`` del usuario, no la pertenencia a un tenant.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT service_ip, ip_address, port
            FROM infrastructure_nodes
            WHERE component_type = 'alertmanager'
              AND is_active = TRUE
            ORDER BY id
            LIMIT 1
            """
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    if not row:
        raise HTTPException(
            status_code=503,
            detail="No hay ningun nodo Alertmanager activo en la infrastructura.",
        )
    return _node_base_url(row[0] or row[1], row[2], 9093)


def _user_has_profile(user_id: int, profile_code: str) -> bool:
    """True si el usuario tiene el perfil, resuelto desde la base.

    user_roles -> role_profiles -> profiles. No se leen los claims del token: un
    JWT con claims alterados no amplia privilegios.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM user_roles ur
            JOIN role_profiles rp ON rp.role_id = ur.role_id
            JOIN profiles p ON p.id = rp.profile_id
            WHERE ur.user_id = %s AND p.code = %s
            """,
            (user_id, profile_code),
        )
        return cursor.fetchone()[0] > 0
    finally:
        cursor.close()
        conn.close()


def _issue_ticket(user_id: int, tenant_id: Optional[int], scope: str) -> str:
    """Persiste un ticket de un solo uso para el par (usuario, tenant).

    El ``scope`` queda guardado en la fila: es el que decide a que upstream se
    monta la consola al canjear y al servir cada request (specs/010).

    ``tenant_id`` es NULL para el scope ``alertmanager_global``: es la unica
    consola sin tenant (specs/011 5.7).
    """
    ticket = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=UI_TICKET_TTL_MINUTES)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO ui_tickets (ticket, user_id, tenant_id, scope, expires_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (ticket, user_id, tenant_id, scope, expires_at),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    return ticket


def _consume_ticket(ticket: str) -> dict[str, Any]:
    """Canjea el ticket: lo marca consumido y devuelve tenant y alcance.

    Un ticket es de un solo uso: la primera peticion lo consume. Como la UI
    hace varias llamadas tras la carga, el canje devuelve ademas una sesion
    corta ligada al mismo ticket para las peticiones siguientes.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT k.user_id, k.tenant_id, k.scope, k.expires_at, k.consumed_at
            FROM ui_tickets k
            WHERE k.ticket = %s
            """,
            (ticket,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Ticket de UI invalido.")

        user_id, tenant_id, scope, expires_at, consumed_at = row
        if consumed_at is not None or datetime.utcnow() >= expires_at:
            raise HTTPException(status_code=401, detail="El ticket de UI expiro o ya fue usado.")

        cursor.execute(
            "UPDATE ui_tickets SET consumed_at = CURRENT_TIMESTAMP WHERE ticket = %s",
            (ticket,),
        )
        conn.commit()
        return {"user_id": user_id, "tenant_id": tenant_id, "scope": scope}
    finally:
        cursor.close()
        conn.close()


def _touch_ticket(ticket: str) -> bool:
    """Renueva la vigencia de un ticket ya canjeado si le queda poca vida.

    Es la renovacion deslizante que impide que una pestana en uso muera a los
    ``UI_TICKET_TTL_MINUTES`` del canje. Solo escribe cuando queda menos de
    ``UI_TICKET_TOUCH_THRESHOLD_MINUTES``, para no hacer un UPDATE por cada
    asset que sirve el proxy. Devuelve True si la renovo.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE ui_tickets
            SET expires_at = %s
            WHERE ticket = %s
              AND consumed_at IS NOT NULL
              AND expires_at < %s
            """,
            (
                datetime.utcnow() + timedelta(minutes=UI_TICKET_TTL_MINUTES),
                ticket,
                datetime.utcnow()
                + timedelta(minutes=UI_TICKET_TOUCH_THRESHOLD_MINUTES),
            ),
        )
        renewed = bool(cursor.rowcount)
        if renewed:
            conn.commit()
        return renewed
    finally:
        cursor.close()
        conn.close()


def _set_ui_cookie(response: Response, ticket: str, request: Request) -> None:
    """Fija la cookie de sesion de la consola.

    ``max_age`` replica el TTL del ticket. Por eso hay que volver a llamarla
    cada vez que ``_touch_ticket`` renueva la vigencia: si no, el navegador
    descarta la cookie con el TTL original mientras el ticket sigue vivo en la
    BD y la peticion posterior falla por falta de sesion.
    """
    response.set_cookie(
        key="anomalia_ui",
        value=ticket,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=UI_TICKET_TTL_MINUTES * 60,
        path=f"{UI_BASE_PREFIX}/{ticket}",
    )


def _unauthorized_response(request: Request, detail: str) -> Response:
    """401 de una consola, con cuerpo acorde al tipo de petición.

    Si la pidió el navegador como documento (``Accept: text/html``) devuelve una
    pagina legible: un JSON crudo en la ventana principal se lee como "pantalla
    en blanco". Si la pidió un fetch de la propia UI devuelve JSON, que es lo
    que esos bundles esperan leer en ``response.text()``.
    """
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(
            _CONSOLE_EXPIRED_HTML.replace("__DETAIL__", _esc_html(detail)),
            status_code=401,
        )
    return JSONResponse({"detail": detail}, status_code=401)


def _esc_html(value: str) -> str:
    """Escapa un texto para poder incrustarlo en el HTML de error."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _mount_path(ds: dict[str, Any]) -> str:
    """Path donde el backend expone la UI, ya con el scoping resuelto."""
    template = UI_MOUNT_BY_KIND.get(ds["kind"])
    if not template:
        raise HTTPException(
            status_code=404,
            detail=f"El backend {ds['kind']} no tiene UI expuesta por el proxy.",
        )
    if ds["scoping"] == "path":
        if ds["account_id"] is None or ds["project_id"] is None:
            raise HTTPException(
                status_code=500,
                detail=f"El tenant {ds['name']} declara scoping por path sin account/project id.",
            )
    return template.format(
        account_id=ds["account_id"] if ds["account_id"] is not None else 0,
        project_id=ds["project_id"] if ds["project_id"] is not None else 0,
    )


def _scoping_headers(ds: dict[str, Any]) -> dict[str, str]:
    """Headers de aislamiento por tenant. Salen de la BD, nunca del request."""
    if ds["scoping"] != "header":
        return {}
    headers: dict[str, str] = {}
    if ds["org_id"]:
        headers["X-Scope-OrgID"] = str(ds["org_id"])
    elif ds["account_id"] is not None and ds["project_id"] is not None:
        headers["AccountID"] = str(ds["account_id"])
        headers["ProjectID"] = str(ds["project_id"])
    return headers


def _reject_path_traversal(path: str) -> None:
    """Impide que la ruta del cliente redirija la peticion a otro destino.

    Sin esto, un path como 'http://otro-servicio/...' escaparia del read_url
    resuelto desde la base y convertiria al gateway en un proxy abierto a
    cualquier destino alcanzable desde la red de contenedores.
    """
    if "://" in path:
        raise HTTPException(status_code=400, detail="Ruta de UI invalida.")
    if ".." in path:
        raise HTTPException(status_code=400, detail="Ruta de UI invalida.")


def _strip_scoping_params(params: Any) -> list[tuple[str, str]]:
    """Quita los params de scoping que el cliente pudiera intentar inyectar.

    Las UIs leen accountID/projectID de la query string para armar sus headers.
    El canje los inyecta de fabrica (para que el selector de tenant de la UI
    muestre el tenant real), pero aqui se descartan siempre: el scoping que
    llega al upstream sale de tenant_datasources, de modo que editar la URL no
    cambia el tenant efectivo.
    """
    forbidden = {"accountID", "projectID", "accountid", "projectid", "orgID", "orgid"}
    # QueryParams es un MultiDict: iterarlo directo da las claves, no los pares.
    return [(k, v) for k, v in params.multi_items() if k not in forbidden]


# vtselect/vlselect rechazan /select/tenant_ids si llega con AccountID/ProjectID
# presentes (aunque sea "0"): es un endpoint global de catalogo, no de tenant.
# Devuelve los IDs del cluster; no hay datos de telemetria y el scoping de las
# demas rutas sigue saliendo de la BD, asi que no habilita ninguna fuga.
UNSCOPED_PATHS = {"select/tenant_ids"}


def _validate_ui_path(ds: dict[str, Any], path: str) -> None:
    """Cierra la ruta al tenant correcto segun el modo de scoping del backend.

    El caso critico es VictoriaMetrics: el scoping va DENTRO del path
    (``/select/<accountID>:<projectID>/...``), de modo que sin esta comprobacion
    un usuario con acceso al tenant 1 podria pedir
    ``/ui/{ticket}/select/999:999/prometheus/api/v1/query`` y leer el namespace
    de otro tenant. El prefijo se construye SIEMPRE desde tenant_datasources.
    """
    if ds["scoping"] == "path":
        if ds["account_id"] is None or ds["project_id"] is None:
            raise HTTPException(
                status_code=500,
                detail=f"El tenant {ds['name']} declara scoping por path sin account/project id.",
            )
        # vmui consulta /admin/tenants en la raiz de vmselect, fuera del prefijo
        # /select/<acc>:<proj>/. Es el unico caso admitido sin ese prefijo y su
        # respuesta se recorta al tenant autorizado en _filter_admin_tenants.
        if path == ADMIN_TENANTS_PATH:
            return
        expected = f"select/{ds['account_id']}:{ds['project_id']}/"
        if not path.startswith(expected):
            raise HTTPException(
                status_code=403,
                detail="La ruta solicitada no pertenece al tenant autorizado.",
            )
        return

    prefixes = UI_HEADER_PREFIXES.get(ds["kind"], ())
    # La raiz ("/") es el HTML de la SPA de Pyroscope; en los demas backends el
    # canje siempre redirige con su montaje, asi que no se llega con path vacio.
    if path == "" or any(path.startswith(p) for p in prefixes):
        return
    raise HTTPException(
        status_code=400,
        detail=f"Ruta no permitida para el backend {ds['kind']}.",
    )


def _filter_admin_tenants(content: bytes, ds: dict[str, Any]) -> bytes:
    """Deja unicamente el tenant autorizado en la respuesta de /admin/tenants.

    vmselect devuelve el catalogo completo de tenants del cluster, no el del
    usuario. vmui solo lo usa para poblar su selector, asi que recortarlo al
    tenant que ya esta cableado en la ruta no cambia su comportamiento y evita
    filtrar los IDs de los demas tenants.
    """
    if ds["account_id"] is None or ds["project_id"] is None:
        return content
    try:
        payload = json.loads(content)
    except ValueError:
        return content
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return content
    allowed = f"{ds['account_id']}:{ds['project_id']}"
    payload["data"] = [entry for entry in payload["data"] if entry == allowed]
    return json.dumps(payload).encode("utf-8")


def _rewrite_base_href(
    ds: dict[str, Any], ticket: str, content: bytes, media_type: str
) -> bytes:
    """Ajusta el <base> de Pyroscope para que sus assets cuelguen del ticket.

    Pyroscope es la unica de las cuatro UIs que declara ``<base href="/" />``:
    ese atributo fuerza a que ``./assets/...`` resuelva en la raiz del dominio y
    las llamadas escapen del prefijo del ticket, devolviendo 404 desde el
    gateway. Solo se reescribe ese atributo del HTML raiz; los bundles JS no se
    tocan.
    """
    if ds["kind"] != "Pyroscope":
        return content
    if not media_type.startswith("text/html"):
        return content
    original = PYROSCOPE_BASE_HREF.encode("utf-8")
    if original not in content:
        return content
    replacement = f'<base href="{UI_BASE_PREFIX}/{ticket}/" />'.encode("utf-8")
    return content.replace(original, replacement)


# ---------------------------------------------------------------------------
# Helpers del upstream de Parses
# ---------------------------------------------------------------------------


def _parses_not_found() -> Response:
    """404 con la forma que Perses espera ({"message": ...}), no la de FastAPI."""
    return Response(content=_PARSES_NOT_FOUND, status_code=404, media_type="application/json")


def _username_for_session(session: dict[str, Any]) -> str:
    """Identidad de usuario de la sesion, venga del JWT o de la cookie."""
    if session["auth"] == "jwt":
        payload = session["payload"]
        username = payload.get("sub") or payload.get("username")
        if username:
            return str(username)
    else:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT username FROM users WHERE id = %s", (session["user_id"],))
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()
        if row:
            return str(row[0])
    raise HTTPException(status_code=401, detail="Sesion sin identidad de usuario.")


def _parses_projects_for(session: dict[str, Any]) -> set[str]:
    """Projects de Parses de los tenants a los que la sesion tiene acceso.

    Perses corre con ``enable_auth: false``, asi que el aislamiento entre tenants es
    por project. El conjunto se deriva de la misma funcion que usa el provisionador,
    de modo que no hay dos fuentes de verdad (spec 008/010).
    """
    return set(user_parses_projects(_username_for_session(session)))


def _parses_document_project(payload: Any) -> Optional[str]:
    """Proyecto al que pertenece un documento de la API de Perses.

    Los documentos llevan ``metadata.project``. Los proyectos mismos se
    identifican por ``metadata.name``, porque ahi el proyecto ES el documento.
    Devuelve None cuando el documento no es de un proyecto concreto (catalogo de
    plugins, errores, etc.): esos se sirven tal cual.
    """
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    if payload.get("kind") == "Project":
        owner = metadata.get("name")
    else:
        owner = metadata.get("project")
    if isinstance(owner, str) and owner:
        return owner
    return None


def _parses_guard_path(path: str, method: str, projects: set[str]) -> Optional[Response]:
    """Corta el acceso a projects ajenos por ruta. None = la ruta pasa.

    Toda lectura/escritura de un documento concreto de Perses cuelga de
    ``/api/v1/projects/{proyecto}/...``, asi que comparando el segmento de
    proyecto aqui se cubren dashboards, datasources, secrets y variables de una
    sola vez. Devuelve 404 (no 403) para no confirmar que el project existe.
    """
    if path.startswith(_PARSES_PROXY_PREFIX):
        # Forward de datasource: /proxy/projects/{proyecto}/datasources/{ds}/...
        # El proyecto va en la ruta y Perses resuelve el datasource dentro de el
        # antes de reenviar, asi que compararlo aqui basta.
        parts = path[len(_PARSES_PROXY_PREFIX):].split("/")
        if parts[0] != "projects" or len(parts) < 2 or parts[1] not in projects:
            return _parses_not_found()
        return None

    if path == _PARSES_GLOBAL_DS_PREFIX or path.startswith(_PARSES_GLOBAL_DS_PREFIX + "/"):
        if method == "GET":
            return None
        return Response(
            content=json.dumps({"detail": "Las datasources globales no estan soportadas."}),
            status_code=403,
            media_type="application/json",
        )

    if not path.startswith(_PARSES_PROJECTS_PREFIX):
        return None
    rest = path[len(_PARSES_PROJECTS_PREFIX):].lstrip("/")
    if not rest:
        # Listado de proyectos o creacion de uno nuevo.
        if method == "GET":
            return None
        return Response(
            content=json.dumps({"detail": "Los proyectos de Parses los crea el provisionador."}),
            status_code=403,
            media_type="application/json",
        )
    owner = rest.split("/", 1)[0]
    if owner not in projects:
        return _parses_not_found()
    # Borrar un project borraria sus datasources y dashboards. El ciclo de vida lo
    # maneja el provisionador (alta/baja de tenant), no la UI.
    if rest == owner and method == "DELETE":
        return Response(
            content=json.dumps({"detail": "El proyecto de Parses lo gestiona el provisionador."}),
            status_code=403,
            media_type="application/json",
        )
    return None


def _parses_guard_body(body: Optional[bytes], method: str, projects: set[str]) -> Optional[Response]:
    """Comprueba el ``metadata.project`` de un documento escrito por el cliente.

    Ademas valida el ``proxy.spec.url`` de cualquier datasource: Perses guarda
    esa URL tal cual y despues la llama desde dentro de la red, de modo que un
    cliente que pudiera escribirla convertiria el contenedor de Parses en un
    proxy SSRF contra postgres, Victoria o el host. Solo se admite el prefijo
    del gateway que usa el provisionador.
    """
    if not body or method not in ("POST", "PUT", "PATCH"):
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    owner = _parses_document_project(payload)
    if owner and owner not in projects:
        return _parses_not_found()
    if _parses_bad_datasource_spec(payload):
        return Response(
            content=json.dumps({"detail": "El proxy del datasource debe apuntar al gateway de Anomalia."}),
            status_code=403,
            media_type="application/json",
        )
    return None


def _parses_bad_datasource_spec(payload: Any) -> bool:
    """True si el cuerpo declara un datasource apuntando fuera del gateway.

    Se busca en cualquier punto del arbol, no solo en el raiz: un ``PATCH``
    parcial trae el ``spec`` sin ``kind`` y sigue siendo capaz de cambiar la
    URL del proxy. Un dashboard no contiene jamas un ``proxy.spec.url``, asi
    que la busqueda es segura para los demas tipos de documento.
    """
    if isinstance(payload, dict):
        proxy = payload.get("proxy")
        if isinstance(proxy, dict):
            proxy_spec = proxy.get("spec")
            if isinstance(proxy_spec, dict):
                url = proxy_spec.get("url")
                if url is not None and not (
                    isinstance(url, str) and url.startswith(PARSES_PROXY_BASE)
                ):
                    return True
        plugin = payload.get("plugin")
        if isinstance(plugin, dict):
            plugin_spec = plugin.get("spec")
            if isinstance(plugin_spec, dict) and isinstance(plugin_spec.get("directUrl"), str):
                return True
        return any(_parses_bad_datasource_spec(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_parses_bad_datasource_spec(value) for value in payload)
    return False


def _parses_isolate_response(content: bytes, projects: set[str]) -> Optional[bytes]:
    """Recorta la respuesta de Perses a los documentos de los projects permitidos.

    El listado raiz (``/api/v1/dashboards``, ``/api/v1/datasources``,
    ``/api/v1/secrets``, ...) cruza proyectos. ``/api/v1/datasources`` ademas
    devuelve los headers de proxy con el token de cada datasource, asi que
    filtrarlo no es solo coherencia de UI: es la unica barrera contra la fuga de
    credenciales. Devuelve None si el documento es de otro project (404).
    """
    try:
        payload = json.loads(content)
    except ValueError:
        return content

    if isinstance(payload, list):
        # Los items sin proyecto (catalogo de plugins, listas vacias) pasan sin
        # filtrar; solo se descartan los que declaran un project no permitido.
        def _keep(item: Any) -> bool:
            owner = _parses_document_project(item)
            return owner is None or owner in projects

        return json.dumps([item for item in payload if _keep(item)]).encode("utf-8")

    owner = _parses_document_project(payload)
    if owner and owner not in projects:
        return None
    return content


def _parses_is_spa_route(path: str) -> bool:
    """True si la ruta es una ruta de cliente que debe resolver en el index.

    Perses es una SPA con basename: al refrescar, ``/ui/{ticket}/dashboards/...``
    no existe en el upstream. Ese 404 se resuelve devolviendo el HTML de
    arranque para que el router del cliente tome el control. Los errores reales
    de API y de assets si se devuelven como 404.
    """
    if path == "":
        return False
    if path.startswith(_PARSES_NON_SPA_PREFIXES):
        return False
    return "." not in path.rsplit("/", 1)[-1]


def _rewrite_parses_content(ticket: str, content: bytes, media_type: str) -> bytes:
    """Reescribe las URLs raiz de Perses para que cuelguen del ticket.

    Cinco puntos de fuga, todos verificados en el build desplegado:
      - el HTML referencia ``/main.<hash>.js``, ``/main.<hash>.css`` y
        ``/favicon.ico`` con rutas absolutas;
      - el runtime de rspack fija ``a.p="/"`` (publicPath) y cada chunk se pide
        como ``a.p + a.u(id)``: sin reescribirlo el ``import()`` va a la raiz del
        dominio y devuelve ``Loading chunk 567 failed``;
      - el CSS apunta sus ``url(/<hash>.woff)`` a la raiz del origen, donde no
        hay nada;
      - el modulo ``11042`` fija ``api_prefix:""`` y lo exporta como ``Ao.u``: es
        el valor que usa ``<BrowserRouter basename>``. El bootstrap inyecta su
        propio ``window.PERSES_APP_CONFIG``, que ``??`` no reemplaza, asi que sin
        esta reescritura la URL se queda en ``/projects/...`` (fuera del ticket)
        y refrescar pierde la consola;
      - el resto de llamadas lo resuelve el parche de fetch.

    Y un punto que deliberadamente **no** se toca: el literal ``"/plugins"``. El
    ``RemotePluginLoader`` (modulo 16271) compone
    ``pluginsAssetsPath = baseURL + "/plugins"`` y su unico llamador (modulo
    ``18040`` de los chunks ``368``/``526``) pasa ``baseURL = Ao.u.api_prefix``,
    que ya lleva el prefijo. Prefijar ademas el literal producia
    ``/ui/{tk}/ui/{tk}/plugins/...`` y el runtime de Module Federation devolvia
    ``#RUNTIME-013: manifest is not a valid Module Federation manifest`` con
    todos los paneles en error.
    """
    prefix = f"{UI_BASE_PREFIX}/{ticket}"

    if media_type.startswith("text/html"):
        # Primero el bootstrap (su texto no contiene src="/ ni href="/, asi que
        # el orden con respecto al reescritura de atributos es indiferente).
        markup = _PARSES_BOOTSTRAP.replace("__PREFIX__", prefix).encode("utf-8")
        anchor = b"<script defer src="
        at = content.find(anchor)
        if at < 0:
            at = content.find(b"</head>")
        if at >= 0:
            content = content[:at] + markup + content[at:]
        content = content.replace(b'src="/', f'src="{prefix}/'.encode("utf-8"))
        content = content.replace(b'href="/', f'href="{prefix}/'.encode("utf-8"))
        return content

    if media_type.startswith("text/javascript") or media_type.startswith("application/javascript"):
        # No tocar el literal "/plugins" (ver docstring): el RemotePluginLoader ya
        # le antepone Ao.u.api_prefix a traves de `baseURL` y hacerlo dos veces
        # rompe los manifests de federacion de modulos (#RUNTIME-013).
        # api_prefix del modulo 11042: `const i={api_prefix:""}` es el objeto que se
        # exporta como `Ao.u` y el que consume <BrowserRouter basename>. El
        # window.PERSES_APP_CONFIG del bootstrap es otro objeto (el `??` no lo
        # reemplaza si ya existe), asi que hay que tocar el literal del bundle.
        # Idempotente: despues de reescribirlo ya no queda el valor vacio.
        encoded_prefix = prefix.encode("utf-8")
        out = re.sub(
            rb'api_prefix\s*:\s*(?:""|\'\')',
            lambda _match: b'api_prefix:"' + encoded_prefix + b'"',
            content,
        )
        # publicPath del runtime: el nombre de la variable minificada (a, n, $...)
        # cambia en cada build, por eso se busca la asignacion y no el identificador.
        # Idempotente: despues de reescribir el valor ya no es "/" ni "".
        out = re.sub(
            rb'([\w$])\.p="/"',
            rb'\1.p="' + prefix.encode("utf-8") + rb'/"',
            out,
        )
        out = re.sub(
            rb'([\w$])\.p=""',
            rb'\1.p="' + prefix.encode("utf-8") + rb'/"',
            out,
        )
        return out

    if media_type.startswith("text/css"):
        # El lookahead evita prefijar dos veces: url(/ui/tk/... ya lleva el prefijo.
        marker = prefix.encode("utf-8").lstrip(b"/")
        return re.sub(
            rb'url\(\s*(["\']?)/(?!' + re.escape(marker) + rb')',
            rb"url(\1" + prefix.encode("utf-8") + b"/",
            content,
        )

    return content


@router.post("/api/v1/ui/ticket", response_model=UITicketResponse)
def create_ui_ticket(
    body: UITicketRequest,
    payload: dict = Depends(verify_any_user_token),
) -> UITicketResponse:
    """Emite un ticket de un solo uso para abrir la UI de un tenant.

    Este es el unico punto del flujo que exige el JWT: valida la identidad, que
    el usuario tenga acceso al tenant y que tenga el perfil que habilita la
    tarjeta desde la que abre la consola.
    """
    username = payload.get("sub") or payload.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Token sin identidad de usuario.")

    required_profile = UI_SCOPE_PROFILE.get(body.scope)
    if required_profile is None:
        raise HTTPException(status_code=400, detail="Alcance de consola no reconocido.")

    # El scope global no tiene tenant; el resto lo necesita para resolver el
    # upstream y para validar la pertenencia del usuario (specs/011 5.7).
    if body.scope == ALERTMANAGER_SCOPE:
        tenant_id: Optional[int] = None
        upstream_label = "Alertmanager global"
    else:
        if body.tenant_id is None:
            raise HTTPException(
                status_code=422,
                detail=f"El alcance '{body.scope}' requiere tenant_id.",
            )
        tenant_id = body.tenant_id
        upstream_label = f"tenant {tenant_id}"

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
        user_row = cursor.fetchone()
        if not user_row:
            raise HTTPException(status_code=401, detail="Usuario no encontrado.")
        user_id = user_row[0]

        # El perfil se resuelve desde la base (user_roles -> role_profiles ->
        # profiles) y no desde los claims del token, por la misma razon que
        # verify_tenant_access: un token con claims alterados no amplia privilegios.
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM user_roles ur
            JOIN role_profiles rp ON rp.role_id = ur.role_id
            JOIN profiles p ON p.id = rp.profile_id
            JOIN users u ON u.id = ur.user_id
            WHERE u.username = %s AND p.code = %s
            """,
            (username, required_profile),
        )
        if cursor.fetchone()[0] == 0:
            raise HTTPException(
                status_code=403,
                detail=f"Acceso denegado. Se requiere el perfil '{required_profile}'.",
            )
    finally:
        cursor.close()
        conn.close()

    # Fail-fast: se resuelve el upstream ANTES de emitir el ticket para no
    # devolver una URL que al canjear va a fallar (instancia sin desplegar, nodo
    # de Alertmanager inexistente, tenant sin datasource de lectura).
    if body.scope == VMALERT_SCOPE:
        _resolve_vmalert_upstream(tenant_id)
    elif body.scope == ALERTMANAGER_SCOPE:
        _resolve_alertmanager_upstream()
    else:
        _load_tenant_datasource(tenant_id)

    if tenant_id is not None:
        # Re-checa la pertenencia del tenant en la base. verify_tenant_access es
        # el que valida user_tenants ∪ role_tenants; se reutiliza para no
        # duplicar reglas.
        verify_tenant_access(tenant_id)(payload)

    ticket = _issue_ticket(user_id, tenant_id, body.scope)
    logger.info(
        "Ticket de UI emitido para tenant=%s (%s) scope=%s usuario=%s",
        tenant_id, upstream_label, body.scope, username,
    )
    return UITicketResponse(url=f"{UI_BASE_PREFIX}/redeem/{ticket}")


def _resolve_session(request: Request) -> dict[str, Any]:
    """Resuelve el contexto de un request de UI ya autenticado.

    Acepta dos formas:
      - ``Authorization: Bearer <jwt>``: el fetch de la propia UI y las llamadas
        del frontend que llevan credencial.
      - Cookie ``anomalia_ui=<ticket>``: la navegacion, que no puede mandar
        headers. Se acepta mientras el ticket no haya expirado, aunque ya se
        haya canjeado (la UI necesita varias llamadas tras la carga).
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        payload = verify_any_user_token(request)
        return {"auth": "jwt", "payload": payload}

    cookie = request.cookies.get("anomalia_ui")
    if cookie:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT user_id, tenant_id, expires_at
                FROM ui_tickets WHERE ticket = %s
                """,
                (cookie,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()
        if row and datetime.utcnow() < row[2]:
            return {"auth": "cookie", "user_id": row[0], "tenant_id": row[1]}
        # Habia cookie pero ya no vale: es la situacion normal de una pestana
        # abierta demasiado tiempo, y conviene distinguirla de "sin sesion".
        raise HTTPException(
            status_code=401, detail="El acceso a esta consola ha caducado."
        )

    raise HTTPException(status_code=401, detail="Sesion de UI requerida.")


def _authorize_session(session: dict[str, Any], tenant_id: Optional[int]) -> None:
    """Verifica que la sesion resuelta corresponda al tenant solicitado.

    ``tenant_id`` es NULL en el scope ``alertmanager_global``: no hay tenant que
    validar. En ese caso el control de acceso no es la pertenencia al tenant sino
    el perfil ``alerts_manager``, que el proxy re-chequea en cada request
    (``_authorize_admin_console``).
    """
    if tenant_id is None:
        return
    if session["auth"] == "jwt":
        payload = session["payload"]
        verify_tenant_access(tenant_id)(payload)
        return
    if session["tenant_id"] != tenant_id:
        raise HTTPException(status_code=403, detail="El ticket no corresponde a ese tenant.")


def _authorize_admin_console(ticket_user_id: int, scope: str) -> None:
    """Re-valida el perfil de las consolas de administracion en cada request.

    En la emision del ticket el perfil ya se comprueba, pero la consola vive
    muchos minutos tras el canje y el usuario puede perder el perfil mientras la
    pestana sigue abierta. Para los scopes de administracion el perfil se vuelve
    a resolver desde la base en cada peticion.

    Devuelve ``None``: es una comprobacion, no un permiso.
    """
    required_profile = UI_SCOPE_PROFILE.get(scope)
    if required_profile is None:
        raise HTTPException(status_code=400, detail="Alcance de consola no reconocido.")
    if not _user_has_profile(ticket_user_id, required_profile):
        raise HTTPException(
            status_code=403,
            detail=f"Acceso denegado. Se requiere el perfil '{required_profile}'.",
        )


@router.get(UI_REDEEM_PATH, response_class=HTMLResponse)
def redeem_ui_ticket(ticket: str, request: Request) -> Response:
    """Canjea el ticket y redirige a la UI real del backend.

    Es el unico punto navegable sin credencial. Marca el ticket como consumido y
    deja una cookie de sesion para que las llamadas posteriores de la UI
    (assets, /api/v1/...) pasen por el proxy con la cookie.

    La ruta es /ui/redeem/{ticket} y no /ui/{ticket}/ porque Pyroscope se monta
    en "/": con un montaje en la raiz el canje y el proxy se superpondrian y
    generarian un redirect infinito.

    Con scoping por header el redirect lleva ademas accountID/projectID en la
    query string: vlui/vtui leen el tenant de ahi y sin el montaje quedan en
    "0:0", con un selector que no coincide con el tenant_ids devuelto. El
    scoping efectivo no cambia: sigue saliendo de la BD por headers.
    """
    try:
        consumed = _consume_ticket(ticket)
    except HTTPException as exc:
        if exc.status_code == 401:
            return _unauthorized_response(request, str(exc.detail))
        raise

    if consumed.get("scope") in ROOT_MOUNTED_SCOPES:
        # Parses, el vmalert del tenant y el Alertmanager global no tienen
        # scoping de Victoria que inyectar: se montan en la raiz del ticket. La
        # barra final hace que el proxy reciba path="".
        target = f"{UI_BASE_PREFIX}/{ticket}/"
        response = RedirectResponse(url=target, status_code=307)
        _set_ui_cookie(response, ticket, request)
        logger.info(
            "Ticket de UI canjeado: tenant=%s scope=%s -> consola en la raiz %s",
            consumed["tenant_id"], consumed["scope"], target,
        )
        return response

    ds = _load_tenant_datasource(consumed["tenant_id"])
    mount = _mount_path(ds)

    target = f"{UI_BASE_PREFIX}/{ticket}{mount}"
    if (
        ds["scoping"] == "header"
        and ds["account_id"] is not None
        and ds["project_id"] is not None
    ):
        target += f"?accountID={ds['account_id']}&projectID={ds['project_id']}"

    response = RedirectResponse(url=target, status_code=307)
    _set_ui_cookie(response, ticket, request)
    logger.info("Ticket de UI canjeado: tenant=%s -> %s", ds["name"], target)
    return response


def _proxy_prepare(
    ticket: str, path: str, request: Request
) -> tuple[Optional[Response], dict[str, Any]]:
    """Fase de autorizacion y resolucion de upstream de ``proxy_ui`` (sync).

    Se ejecuta via asyncio.to_thread: aqui hay varias queries cortas
    (ui_tickets, tenant_datasources, vmalert/alertmanager) y ninguna espera de
    lock debe poder congelar el event loop de uvicorn (spec 011 4.2). Devuelve
    la respuesta temprana (si la hay) o el contexto ya validado en ``ctx``.
    """
    try:
        session = _resolve_session(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return _unauthorized_response(request, str(exc.detail)), {}
        raise

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT user_id, tenant_id, scope, expires_at FROM ui_tickets WHERE ticket = %s",
            (ticket,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    if not row:
        return _unauthorized_response(request, "Ticket de UI invalido."), {}
    if datetime.utcnow() >= row[3]:
        return _unauthorized_response(request, "El acceso a esta consola ha caducado."), {}

    tenant_id, scope = row[1], row[2]
    _authorize_session(session, tenant_id)

    parses = scope == PARSES_SCOPE
    admin_console = scope in ADMIN_CONSOLE_SCOPES
    projects: set[str] = set()
    upstream_url: Optional[str] = None
    ds: Optional[dict[str, Any]] = None

    if admin_console:
        # Consolas de administracion (vmalert del tenant / Alertmanager global).
        # El perfil se re-valida en cada request y el upstream se resuelve desde
        # la base: el cliente no elige ni el nodo ni el tenant (specs/011 5.7).
        _authorize_admin_console(row[0], scope)
        if request.method in PARSES_WRITE_METHODS:
            raise HTTPException(status_code=405, detail="Metodo no permitido.")
        _reject_path_traversal(path)
        upstream_url = (
            _resolve_vmalert_upstream(tenant_id)
            if scope == VMALERT_SCOPE
            else _resolve_alertmanager_upstream()
        )
    elif parses:
        projects = _parses_projects_for(session)
        if "://" in path or ".." in path or path.startswith("/"):
            raise HTTPException(status_code=400, detail="Ruta de UI invalida.")
        rejected = _parses_guard_path(path, request.method, projects)
        if rejected is not None:
            return rejected, {}
    else:
        if request.method in PARSES_WRITE_METHODS:
            raise HTTPException(status_code=405, detail="Metodo no permitido.")
        ds = _load_tenant_datasource(tenant_id)
        _reject_path_traversal(path)
        _validate_ui_path(ds, path)

    # Renovacion deslizante: si la pestana sigue viva y le queda poca vida, se
    # empuja su vencimiento. Se hace despues de validar todo lo de autorizacion
    # para que una llamada rechazada no alargue la sesion de nadie. La cookie
    # se renueva a la par: su max_age replica el TTL y si no la refrescamos el
    # navegador la descarta mientras el ticket sigue vivo en la BD.
    ticket_renewed = _touch_ticket(ticket)

    ctx: dict[str, Any] = {
        "parses": parses,
        "admin_console": admin_console,
        "projects": projects,
        "upstream_url": upstream_url,
        "ds": ds,
        "ticket_renewed": ticket_renewed,
    }
    return None, ctx


@router.api_route(
    UI_BASE_PREFIX + "/{ticket}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_ui(
    ticket: str,
    path: str,
    request: Request,
) -> Response:
    """Reenvía la UI y sus llamadas API al upstream que corresponde al scope.

    Dos upstream posibles, decididos por el ``scope`` persistido en el ticket:
      - scopes de Victoria -> ``read_url`` con el scoping del tenant inyectado
        desde tenant_datasources;
      - ``dashboards``     -> el contenedor ``parses``, con los metodos de
        escritura habilitados y el aislamiento por proyecto de Perses.

    Los headers de autenticación del cliente nunca se reenvían al upstream.
    """
    early, ctx = await asyncio.to_thread(_proxy_prepare, ticket, path, request)
    if early is not None:
        return early

    parses = ctx["parses"]
    admin_console = ctx["admin_console"]
    projects = ctx["projects"]
    upstream_url = ctx["upstream_url"]
    ds = ctx["ds"]
    ticket_renewed = ctx["ticket_renewed"]

    body: Optional[bytes] = None
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        body = await request.body()

    if admin_console:
        # Consola montada en la raiz: el upstream resuelto por el servidor es la
        # base y la ruta del cliente se le anade tal cual. Sin scoping de Victoria
        # y sin reenviar la cookie de sesion del gateway al nodo.
        target_url = f"{upstream_url}/{path}"
        forward_headers: dict[str, str] = {}
    elif parses:
        rejected = _parses_guard_body(body, request.method, projects)
        if rejected is not None:
            return rejected
        target_url = f"{PARSES_UI_URL}/{path}"
        # Sin scoping de Victoria que inyectar, y sin reenviar la cookie de
        # sesion del gateway a Perses.
        forward_headers: dict[str, str] = {}
    else:
        # La ruta que llega es ya la ruta correcta respecto de read_url. Las UIs
        # de Victoria derivan su base desde window.location.href quitando el
        # sufijo /vmui/..., y vuelven a consultar bajo ese mismo prefijo:
        #
        #   pagina   /ui/{tk}/select/1:1/vmui/        -> up  /select/1:1/vmui/
        #   activos  /ui/{tk}/select/1:1/vmui/assets/ -> up  /select/1:1/vmui/assets/
        #   API      /ui/{tk}/select/1:1/prometheus/api/v1/query -> up  igual
        #
        # _validate_ui_path garantiza que ese prefijo de /select/<acc>:<proj>/ venga
        # de tenant_datasources, asi el cliente no puede elegir el namespace.
        target_url = f"{ds['read_url'].rstrip('/')}/{path}"
        forward_headers = _scoping_headers(ds)
        if path in UNSCOPED_PATHS:
            forward_headers = {}

    content_type = request.headers.get("content-type")
    if content_type:
        forward_headers["Content-Type"] = content_type
    accept = request.headers.get("accept")
    if accept:
        forward_headers["Accept"] = accept

    params = _strip_scoping_params(request.query_params)

    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS, follow_redirects=False) as client:
            upstream = await client.request(
                method=request.method,
                url=target_url,
                params=params,
                headers=forward_headers,
                content=body,
            )

            upstream_content = upstream.content
            media_type = upstream.headers.get("content-type", "application/octet-stream")
            status_code = upstream.status_code

            if parses and status_code == 404 and request.method == "GET" and _parses_is_spa_route(path):
                # Ruta de cliente (nombre sin extension): la SPA la resuelve sola.
                # Se devuelve su HTML de arranque y el router toma el control.
                index = await client.get(f"{PARSES_UI_URL}/", headers=forward_headers)
                if index.status_code == 200:
                    upstream_content = index.content
                    media_type = index.headers.get("content-type", "text/html")
                    status_code = 200
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout contactando la UI de telemetria.")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Error contactando la UI de telemetria: {e}")

    if len(upstream_content) > MAX_UPSTREAM_BYTES:
        raise HTTPException(status_code=413, detail="Respuesta demasiado grande.")

    if parses:
        if request.method == "GET" and "json" in media_type:
            isolated = _parses_isolate_response(upstream_content, projects)
            if isolated is None:
                return _parses_not_found()
            upstream_content = isolated
        upstream_content = _rewrite_parses_content(ticket, upstream_content, media_type)
    elif admin_console:
        # Consolas de administracion montadas en la raiz (vmalert del tenant y
        # Alertmanager global): se sirven tal cual. Sus assets son relativos y
        # resuelven bajo /ui/{ticket}/, sin scoping de Victoria, asi que no hay
        # rewrite de contenido (y tampoco existe `ds` para ellas: intentarlo
        # lanzaba UnboundLocalError -> 500, specs/011 5.7).
        pass
    else:
        if path == ADMIN_TENANTS_PATH:
            upstream_content = _filter_admin_tenants(upstream_content, ds)
        upstream_content = _rewrite_base_href(ds, ticket, upstream_content, media_type)

    response = Response(
        content=upstream_content,
        status_code=status_code,
        media_type=media_type,
    )
    if ticket_renewed:
        _set_ui_cookie(response, ticket, request)
    if any(media_type.startswith(ct) for ct in CACHEABLE_CONTENT_TYPES):
        response.headers["Cache-Control"] = "private, max-age=3600"
    return response
