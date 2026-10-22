# Arquitectura General

Anomalia está diseñada bajo una arquitectura modular y multitenant orientada a microservicios e infraestructura distribuida.

## Componentes del Sistema
1. **Backend API:** FastAPI (Python) para lógica de negocio, RBAC, gestión de tenants y webhooks.
2. **Base de Datos:** PostgreSQL 15 para persistencia transaccional.
3. **Observabilidad:** VictoriaMetrics, VictoriaLogs, VictoriaTraces, Pyroscope, vmalert, Alertmanager, Parses.
4. **Interfaces:** Panel Web Vanilla JS y App Móvil Flutter.

## Parses (dashboards por tenant)

Perses se provisiona **por tenant**: un project `anomalia-tenant-<slug>` (con
`spec.display.name` = nombre del tenant, para que la UI de Perses no muestre el slug
interno) con un datasource por tipo de telemetría. Los **dashboards se provisionan solo
para los tenants internos** (`is_internal=TRUE`: METRICS/TRACES/PROFILES/AUDIT_LOGS): los
**dashboards nativos** del backend, uno por tipo — `anomalia-metrics` (métricas de
`/metrics` + panel `Scrape health (up by job)`), `anomalia-traces` (trazas OTLP, con
**filtros de trazas en la cabecera**: traza por ID → Gantt, operación, tags, duración
mín/máx, que se aplican al salir del campo) y `anomalia-profiles` (perfilados de CPU y
memoria) — y, **solo para el tenant interno METRICS**, los dashboards de infraestructura
**migrados de Grafana** a Perses. Un tenant **nuevo** (`is_internal=FALSE`) arranca con su
project pero **sin dashboards**: no hereda los dashboards default del stack. La página
`/explore` queda habilitada con el selector nativo de perfiles de Pyroscope. Los
datasources se sirven vía el proxy HTTP de Perses hacia el gateway con token de vida
corta. Detalle en `specs/008-parses-datasources-and-tenant-scoping.md`.

Los dashboards se editan desde la propia consola: **Edit** → editor de panel con
**preview de queries en vivo** (pestaña *Query*, con ~10-15 s de carga de plugins hasta
que aparecen las pestañas) → **Apply** → **Save**, que hace un `PUT` sobre el proyecto
propio. Como la plantilla se re-provisiona en cada aprovisionamiento (arranque/alta de
tenant), los cambios de UI duran lo que dure la sesión; los permanentes se hacen en
`backend/dashboards/native/anomalia_{metrics,traces,profiles}.json`.
La página `/explore` se habilita con `frontend.explorer.enable: true` (config de Perses
montado por el rol `parses`, ver `specs/008` §9).

## Consolas de Victoria (proxy de tickets)

Las UIs de VictoriaMetrics, VictoriaTraces, VictoriaLogs y Pyroscope se abren
desde el panel **dentro del tenant del usuario**. No hay link directo: los
puertos de lectura (8401/8481/8491/4040) están aislados por firewall y el JWT
vive en `localStorage`, por lo que no viajaría en la navegación.

El flujo es: el frontend pide un ticket con su JWT
(`POST /api/v1/ui/ticket {tenant_id, scope}`) → el backend exige el perfil del
`scope` y la pertenencia del tenant, contra la base → el navegador navega a
`/ui/redeem/{ticket}` (único punto sin credencial) → se consume el ticket, se
deja una cookie `HttpOnly` de 30 minutos y se redirige a
`/ui/{ticket}/{ruta}` → el proxy reenvía al `read_url` del tenant con el
aislamiento inyectado. La vida del ticket es **deslizante**: se renueva mientras
la pestaña siga en uso, de modo que una consola abierta no muere por el reloj.

Cada tarjeta exige su perfil (`access_metrics`, `access_logs`, `access_traces`,
`access_Continuous_Profiling`, `access_dasboards`, `audit`), chequeado tanto en
`frontend/js/ui.js` como al emitir el ticket. La tarjeta **Dashboards** usa el
mismo camino con `scope: "dashboards"`: el backend monta Perses (Parses) bajo
`/ui/{ticket}/` y pone el aislamiento por proyecto, porque Perses corre sin
autenticación propia.

Detalle en `specs/010-ui-console-proxy.md`.

## Gateway HTTPS

El role `backend` despliega dos instancias de la misma imagen FastAPI:

| Servicio | Puertos host | TLS | Función |
|---|---|---|---|
| `anomaliagw` | `80`, `8000` | no | Redirige a HTTPS; sirve en claro si no hay certificado. **Único ejecutor de workers en background**. |
| `anomaliagw_tls` | `443` | sí | Sirve la aplicación con `ANOMALIA_RUN_WORKERS=false` (no corre workers). |

`anomaliagw_tls` solo se crea si existe `/opt/anomalia/certs/fullchain.pem`, que
genera el role `certs` de forma idempotente. El certificado es un **leaf firmado
por una CA local privada** (`ca.crt`/`ca.key`, CN `Anomalia Root CA`), no un
autofirmado simple. El directorio `/opt/anomalia/certs` **sobrevive** al
`destroy-infra.yml`; para rotarlo hace falta `-e certs_force=true`.

La redirección `FORCE_HTTPS` exime `/metrics`, `/healthz` y
`/api/v1/webhook/alertmanager` (este último porque Alertmanager postea en claro y
un redirect `301` descartaría las alertas), y exime los hosts de loopback para
permitir desarrollo local.

Detalle en `specs/007-https-gateway-and-tenant-access.md`.

