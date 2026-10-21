import os
import time
import json
import psutil
import pyroscope
import httpx
import asyncio
import contextlib
import logging
from typing import Any, Awaitable, Callable
from fastapi import FastAPI, Request, Response, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource

from .database import init_db
from .auth import hash_password, verify_any_user_token
from .oidc import router as oidc_router
from .ai_router import router as ai_router
from .routers import auth_router, admin_router, infra_router, parses_router, ui_proxy_router, webhook_router, system_router, notifications_router, alerts_router, approvals_router, settings_router
from .workers import WorkerSupervisor

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    # spec 016: el gateway corre en dos contenedores (anomalia_gw/anomalia_gw_tls)
    # que comparten la app. Solo uno debe ejecutar los workers (ansible + jobs),
    # o ambos correrian playbooks en paralelo sobre el mismo tmp_dir. El rol
    # backend fija ANOMALIA_RUN_WORKERS=false en el contenedor TLS; el supervisor
    # ademas usa un advisory lock de PostgreSQL como salvaguarda.
    run_workers = os.getenv("ANOMALIA_RUN_WORKERS", "true").strip().lower() not in (
        "0", "false", "no", "off"
    )
    supervisor = None
    if run_workers:
        supervisor = WorkerSupervisor(interval=30)
        await supervisor.start()
    else:
        logging.getLogger("anomalia").info(
            "ANOMALIA_RUN_WORKERS=false: workers de background deshabilitados en este proceso"
        )
    # Re-provisiona Parses al arranque de forma NO bloqueante (spec 008): tras un
    # destroy+deploy los datos de Perses (/perses) se pierden y el proyecto solo
    # se recrearía en el siguiente login; este catch-up lo cubre para todos los
    # usuarios activos. Idempotente (GET->POST/PUT) y tolerante a que Parses aún
    # no responda (reintentos internos + próximo login como respaldo).
    asyncio.create_task(_parses_provision_catchup())
    yield
    # Shutdown
    if supervisor is not None:
        await supervisor.stop()


async def _parses_provision_catchup() -> None:
    """Catch-up de provisioning de Parses para todos los tenants activos.

    No bloquea el arranque y nunca lanza: cualquier fallo queda en los logs y el
    próximo alta/tenant re-ejecuta el provisioning. Corre en ambos contenedores del
    gateway (anomalia_gw/anomalia_gw_tls) y es benigno por ser idempotente. Ademas
    retira los projects per-usuario del diseno anterior (spec 008).
    """
    from .database import get_db_connection
    from .parses_provisioner import (
        provision_tenant_parses,
        delete_legacy_user_parses,
        delete_legacy_native_stack_dashboards,
    )

    logger = logging.getLogger("anomalia")

    def _collect_tenant_ids() -> list[int]:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id FROM tenants WHERE status = 'active' ORDER BY id")
            return [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()
            conn.close()

    tenant_ids = await asyncio.to_thread(_collect_tenant_ids)

    if tenant_ids:
        logger.info("Catch-up Parses: provisionando %d tenant(s)", len(tenant_ids))
        for tenant_id in tenant_ids:
            try:
                await asyncio.to_thread(provision_tenant_parses, tenant_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Catch-up Parses falló para tenant %s: %s", tenant_id, exc)

    # Retira los projects per-usuario legados (idempotente; tolera Parses caido).
    try:
        await asyncio.to_thread(delete_legacy_user_parses)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Catch-up Parses: limpieza de projects per-usuario falló: %s", exc)

    # Retira los dashboards nativos monoliticos `anomalia-stack` legados.
    try:
        await asyncio.to_thread(delete_legacy_native_stack_dashboards)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Catch-up Parses: limpieza de dashboards legacy falló: %s", exc)


app = FastAPI(title="Backend ABM - Alert Management", lifespan=lifespan)

# Directorios base para archivos estáticos y plantillas basados en la ubicación del script
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.abspath(os.path.join(BASE_DIR, "../frontend"))
MEDIA_DIR = os.getenv("MEDIA_DIR", os.path.abspath(os.path.join(BASE_DIR, "../media")))

# Montaje de archivos estáticos y multimedia con rutas absolutas seguras
app.mount("/js", StaticFiles(directory=os.path.join(FRONTEND_DIR, "js")), name="js")
app.mount("/css", StaticFiles(directory=os.path.join(FRONTEND_DIR, "css")), name="css")
app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIR, "assets")), name="assets")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(os.path.join(FRONTEND_DIR, "assets", "icons", "favicon.ico"))

# Asegurar que el directorio media exista en el host/contenedor
os.makedirs(MEDIA_DIR, exist_ok=True)
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

SERVICE_NAME = "anomalia_system"
AUDIT_LOGS_ENDPOINT = os.getenv("AUDIT_LOGS_ENDPOINT")
PYROSCOPE_SERVER_ADDRESS = os.getenv("PYROSCOPE_SERVER_ADDRESS", "http://localhost:4040")

# --- CONFIGURACIÓN HTTPS DEL GATEWAY ---
FORCE_HTTPS = os.getenv("FORCE_HTTPS", "false").strip().lower() == "true"
PUBLIC_EXTERNAL_PORT = os.getenv("PUBLIC_EXTERNAL_PORT", "443" if FORCE_HTTPS else "80")

# Rutas internas que nunca se redirigen: vmagent scrapea /metrics por HTTP
# contra el gateway en claro y los health checks no deben seguir redirects.
# spec 018: Alertmanager postea el webhook al gateway en claro (el stack no
# configura TLS entre AM y el gateway); un 301 lo dejaría sin entregar alertas.
HTTPS_EXEMPT_PATHS = frozenset({"/metrics", "/healthz", "/api/v1/webhook/alertmanager"})

# Hosts de loopback: escapes de desarrollo local sobre HTTP.
HTTPS_EXEMPT_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})


@app.middleware("http")
async def enforce_https_redirect(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Redirige el tráfico del gateway en claro hacia HTTPS cuando hay TLS.

    Solo se activa si FORCE_HTTPS=true, que Ansible inyecta únicamente cuando
    existe un certificado en /opt/anomalia/certs. Sin certificado el gateway
    sigue sirviendo en HTTP para no bloquear el desarrollo.
    """
    if not FORCE_HTTPS or request.url.path in HTTPS_EXEMPT_PATHS:
        return await call_next(request)

    host = request.headers.get("host", "").split(":")[0]
    if host.lower() in HTTPS_EXEMPT_HOSTS:
        return await call_next(request)

    port_suffix = "" if PUBLIC_EXTERNAL_PORT == "443" else f":{PUBLIC_EXTERNAL_PORT}"
    target = f"https://{host}{port_suffix}{request.url.path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=target, status_code=301)


# --- CONFIGURACIÓN DE AUDITORÍA A VICTORIALOGS (LOKI API) ---
def send_audit_log(username: str, action: str, details: str, ip_address: str, status: str = "SUCCESS"):
    """Envía un evento de auditoría estructurado y legible a VictoriaLogs."""
    if not AUDIT_LOGS_ENDPOINT:
        return  
    
    timestamp_ns = str(int(time.time() * 1e9))
    human_readable_msg = f"Usuario [{username or 'anonymous'}] ejecutó [{action}] - Detalle: {details} - IP: {ip_address} - Estado: {status}"
    
    log_payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "_msg": human_readable_msg,
        "username": username or "anonymous",
        "action": action,
        "details": details,
        "ip_address": ip_address,
        "status": status
    }
    
    loki_body = {
        "streams": [
            {
                "stream": {
                    "service": SERVICE_NAME,
                    "type": "AUDIT",
                    "username": username or "anonymous",
                    "action": action,
                    "status": status
                },
                "values": [
                    [timestamp_ns, json.dumps(log_payload)]
                ]
            }
        ]
    }
    
    try:
        headers = {"Content-Type": "application/json"}
        httpx.post(AUDIT_LOGS_ENDPOINT, json=loki_body, headers=headers, timeout=1.0)
    except Exception as e:
        print(f"Error al enviar log de auditoría a Loki: {e}")

# --- MIDDLEWARE DE AUDITORÍA AUTOMÁTICA DE ACTIVIDAD DE USUARIOS ---
@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    start_time = time.time()
    client_ip = request.client.host if request.client else "unknown"
    
    username = "anonymous"
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        try:
            token = auth_header.split(" ")[1]
            import jwt
            decoded = jwt.decode(token, options={"verify_signature": False})
            username = decoded.get("sub", "unknown")
        except Exception:
            pass

    response = await call_next(request)
    duration_ms = round((time.time() - start_time) * 1000, 2)
    
    path = request.url.path
    method = request.method
    status_code = response.status_code

    try:
        current_span = trace.get_current_span()
        if current_span:
            current_span.set_attribute("enduser.id", username)
            current_span.set_attribute("http.client_ip", client_ip)
            current_span.set_attribute("app.action", f"{method} {path}")
    except Exception:
        pass

    if path.startswith("/api/v1/"):
        action = f"{method} {path}"
        details = f"Status: {status_code} | Duration: {duration_ms}ms"
        
        if "login" in path and status_code == 200:
            action = "USER_LOGIN"
            details = f"Inicio de sesión exitoso desde IP {client_ip}"
            
        send_audit_log(
            username=username,
            action=action,
            details=details,
            ip_address=client_ip,
            status="SUCCESS" if status_code < 400 else "ERROR"
        )
        
    return response

# 1. Configuración de Pyroscope para Profiling Continuo (CPU + Memoria)
try:
    pyroscope.configure(
        application_name=os.getenv("PYROSCOPE_APPLICATION_NAME", SERVICE_NAME),
        server_address=PYROSCOPE_SERVER_ADDRESS,
        tags={"environment": os.getenv("ENVIRONMENT", "production")},
        cpu_enabled=True,
        mem_enabled=True,
    )
    print(f"Pyroscope configurado correctamente (CPU + Memoria) para: {SERVICE_NAME}")
except Exception as e:
    print(f"Aviso: No se pudo iniciar el cliente de Pyroscope: {e}")

# --- CONFIGURACIÓN DE OPENTELEMETRY (TRACES & METRICS) ---
resource = Resource.create(attributes={"service.name": os.getenv("PYROSCOPE_APPLICATION_NAME", SERVICE_NAME)})

try:
    traces_endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if traces_endpoint:
        tracer_provider = TracerProvider(resource=resource)
        span_processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_endpoint))
        tracer_provider.add_span_processor(span_processor)
        trace.set_tracer_provider(tracer_provider)
except Exception as e:
    print(f"Advertencia: Error al configurar OpenTelemetry Traces: {e}")

try:
    metrics_endpoint = os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
    if metrics_endpoint:
        metric_exporter = OTLPMetricExporter(endpoint=metrics_endpoint)
        metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=15000)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
except Exception as e:
    print(f"Advertencia: Error al configurar OpenTelemetry Metrics: {e}")

# 2. Inicializar base de datos pasando la función de hash
init_db(hash_password)

# 3. Instrumentar FastAPI automáticamente con OpenTelemetry
try:
    FastAPIInstrumentor.instrument_app(app)
except Exception as e:
    print(f"Advertencia: No se pudo instrumentar FastAPI con OpenTelemetry: {e}")

# 4. Incluir routers de la API
app.include_router(oidc_router)
app.include_router(ai_router)
app.include_router(auth_router.router)
app.include_router(admin_router.router)
app.include_router(infra_router.router)
app.include_router(parses_router.router)
app.include_router(ui_proxy_router.router)
app.include_router(webhook_router.router)
app.include_router(system_router.router)
app.include_router(notifications_router.router)
app.include_router(alerts_router.router)
app.include_router(approvals_router.router)
app.include_router(settings_router.router)

# Estrategia de aislamiento en vmauth derivada del tipo de tenant.
# metrics/traces/logs se aíslan por AccountID/ProjectID numéricos;
# profiles se aíslan por el header X-Scope-OrgID.
TENANT_STRATEGY_BY_TYPE = {
    "metrics": "numeric",
    "traces": "numeric",
    "logs": "numeric",
    "profiles": "x-scope-orgid",
}


@app.get("/api/v1/tenants/my-tenants")
def get_my_tenants_alias(payload: dict = Depends(verify_any_user_token)) -> dict[str, list[dict[str, Any]]]:
    """Devuelve los tenants visibles para el usuario autenticado.

    El acceso es la unión de dos caminos:
      1. Asignación directa en user_tenants.
      2. Asignación por cualquiera de los roles del usuario en role_tenants.

    No hay bypass hardcodeado para el admin: su acceso proviene de las filas en
    role_tenants sembradas para el rol 'admin', de modo que la granting es
    auditable en la base. Un tenant nuevo debe asignarse explícitamente.
    """
    from .database import get_db_connection

    username = payload.get("sub") or payload.get("username")
    if not username:
        return {"tenants": []}

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT t.id, t.name, t.type, t.account_id, t.project_id,
                   t.environment, t.port, t.description, t.is_audit
            FROM tenants t
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
                JOIN roles r ON r.id = rt.role_id
                WHERE ru.username = %s
            )
            ORDER BY t.name;
            """,
            (username, username),
        )
        rows = cursor.fetchall()
        tenants_list = [
            {
                "id": r[0],
                "name": r[1],
                "type": r[2] or "metrics",
                "account_id": r[3],
                "project_id": r[4],
                "environment": r[5],
                "port": r[6],
                "description": r[7],
                # Bandera que separa el tenant de auditoría del resto de logs
                # en la UI (ui.js: CONSOLES.logs / CONSOLES.audit).
                "is_audit": r[8],
                "strategy": TENANT_STRATEGY_BY_TYPE.get(r[2] or "metrics", "numeric"),
            }
            for r in rows
        ]
        return {"tenants": tenants_list}
    except Exception:
        return {"tenants": []}
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

# --- MÉTRICAS DE SISTEMA (ANOMALIA) ---
CPU_USAGE_GAUGE = Gauge('anomalia_system_cpu_usage_percent', 'Uso actual de CPU del sistema')
RAM_USAGE_GAUGE = Gauge('anomalia_system_ram_usage_percent', 'Uso actual de RAM del sistema')
HEALTH_STATUS_GAUGE = Gauge('anomalia_health_status', 'Estado de salud del servicio (1 = OK, 0 = Error)')

# 5. Configuración de plantillas Jinja2 apuntando a la carpeta frontend externa
templates = Jinja2Templates(directory=FRONTEND_DIR)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body": str(exc.body)},
    )

@app.get("/metrics")
def metrics_endpoint_prometheus():
    try:
        CPU_USAGE_GAUGE.set(psutil.cpu_percent(interval=None))
        RAM_USAGE_GAUGE.set(psutil.virtual_memory().percent)
        HEALTH_STATUS_GAUGE.set(1)
    except Exception:
        HEALTH_STATUS_GAUGE.set(0)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/healthz")
def healthcheck():
    return {"status": "healthy", "service": SERVICE_NAME}

@app.get("/", response_class=HTMLResponse)
def serve_index(request: Request):
    return templates.TemplateResponse(request, "index.html")

@app.get("/login", response_class=HTMLResponse)
def serve_login(request: Request):
    return templates.TemplateResponse(request, "login.html")

@app.get("/auth/callback", response_class=HTMLResponse)
def serve_auth_callback(request: Request):
    return templates.TemplateResponse(request, "auth_callback.html")

@app.get("/admin", response_class=HTMLResponse)
def serve_admin(request: Request):
    return templates.TemplateResponse(request, "admin.html")
