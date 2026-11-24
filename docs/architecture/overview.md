# Arquitectura General

Anomalia está diseñada bajo una arquitectura modular y multitenant orientada a microservicios e infraestructura distribuida.

## Componentes del Sistema
1. **Backend API:** FastAPI/Uvicorn (Python 3.10) en el puerto `8000`, sin terminación TLS; lógica de negocio, RBAC, gestión de tenants, workers y webhooks.
2. **Base de Datos:** PostgreSQL 15 para persistencia transaccional.
3. **Observabilidad:** VictoriaMetrics, VictoriaLogs, VictoriaTraces, Pyroscope, vmagent, vmalert, Alertmanager, Parses.
4. **Interfaces:** Panel Web Vanilla JS servido por FastAPI (incluye `/guides`) y App Móvil Flutter.

El despliegue de producción lo orquesta `anomalia-infra`; este documento describe la arquitectura
de la aplicación, no el procedimiento de instalación.

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

## Autorización: perfiles y tenants

Los perfiles son capacidades **predefinidas por el seed** y asignadas a roles mediante
`role_profiles`. No se crean ni se editan desde la API: `GET /api/v1/admin/profiles` es de solo
lectura y se usa para poblar los checkboxes de roles. El permiso correcto es `profile_manager`.

No existe un perfil `alertmanager`. El perfil canónico para reglas vmalert y Alertmanager es
`alerts_manager`, que también habilita los scopes de consola `vmalert` y `alertmanager_global`.
El rol `alerts_manager` es una incorporación explícita y recibe asignaciones de tenants
explícitas: para cualquier operación por tenant se exigen **el perfil** y el acceso a ese tenant
(`user_tenants ∪ role_tenants`, resuelto en base). La consola global de Alertmanager no tiene
tenant, pero sí revalida el perfil `alerts_manager` en la emisión del ticket y en cada request
del proxy.

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

La terminación TLS la hace **Nginx**, no Uvicorn. El role `backend` despliega un contenedor
`anomalia_gw_tls` con Nginx y el backend en `anomalia_gw`:

| Servicio | Puertos host | TLS | Función |
|---|---|---|---|
| `anomalia_gw` | `8000` | no | Uvicorn sirve la API, `/healthz`, `/metrics` y ejecuta los workers en background. |
| `anomalia_gw_tls` | `80`, `443`, `8443` | sí | Nginx redirige `80 → 443`, termina TLS en `443`, expone `8443` solo a la red de contenedores para Parses y hace proxy a `anomalia_gw:8000`. |

En modo generado (`self_signed`), el role `certs` crea una **CA privada** (`external/ca.crt`,
CN `Anomalia Root CA`) y un leaf firmado por esa CA (`external/fullchain.pem` y
`external/privkey.pem`), con SAN para el hostname configurado, `localhost`, `127.0.0.1` y la IP
del nodo. No es un leaf autofirmado aislado. Los clientes deben confiar manualmente en la CA;
ningún componente del deploy la instala en navegadores o dispositivos. En modo `provided`, el
operador entrega el fullchain y la clave sin passphrase, con un SAN que cubra el nombre/IP usado
por los clientes.

El directorio `/opt/anomalia/certs` **sobrevive** al `destroy-infra.yml`. Para rotar el material
generado o cambiar el SAN se usa `--tls-force` en el wrapper o `-e certs_force=true` en el
playbook; los cambios de `certs_mode` requieren un deploy completo.

Las rutas `/metrics`, `/healthz` y `/api/v1/webhook/alertmanager` están exentas del redirect
HTTPS (el webhook porque Alertmanager postea en claro y un `301` descartaría las alertas); los
hosts de loopback también quedan exentos para desarrollo local.

Detalle operativo en `docs/ansible-deployment.md` del repo `anomalia-infra`.

