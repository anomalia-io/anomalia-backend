# anomalia-backend

API y workers del backend de **Anomalia**, la plataforma multitenant de observabilidad con AIOps.

Incluye la **consola web** (JavaScript Vanilla) servida por el propio backend, con el mount
estático `/guides` para las guías operativas HTML.

## 👤 Para Usuario Final (probar el backend)

No necesitas clonar este repo ni correr el backend a mano: el stack completo se despliega desde
[`anomalia-infra`](https://github.com/anomalia-io/anomalia-infra) con Ansible. Esa es la forma
soportada para producción; allí se gestionan Nginx, TLS, firewall, PostgreSQL, vmagent y el resto
del ecosistema.

## Desarrollo local

Prerrequisitos:

- Python 3.10
- PostgreSQL accesible mediante `DATABASE_URL`
- Dependencias de `requirements.txt`

```bash
cd anomalia-backend
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

El proceso escucha en `8000` y **no termina TLS**: en el deploy es Nginx
(`anomalia_gw_tls`) quien publica `80`/`443` y reenvía al backend en claro. `--reload` es solo
para desarrollo. En producción, los workers en background se ejecutan una sola vez gracias a
`ANOMALIA_RUN_WORKERS` y un `pg_try_advisory_lock` de PostgreSQL; el gate evita duplicar jobs
si hay más de un proceso de la app.

Al arrancar, `init_db` crea el esquema y siembra roles, tenants internos y el **catálogo de
perfiles predefinido**. Los perfiles no se crean ni se editan desde la API:
`GET /api/v1/admin/profiles` es de solo lectura y sirve para poblar los checkboxes de roles.
No existe un perfil `alertmanager`: el perfil canónico para reglas vmalert y Alertmanager es
`alerts_manager`. Las operaciones por tenant exigen **perfil y acceso a ese tenant**
(`user_tenants ∪ role_tenants`); el rol `alerts_manager` recibe asignaciones de tenants
explícitamente.

### Endpoints principales

- `GET /healthz` - Health check
- `GET /metrics` - Métricas Prometheus
- `GET /api/v1/status` - Estado del sistema
- `POST /api/v1/auth/login` - Login local
- `POST /api/v1/admin/tenants` - Alta de tenant (202 + `job_id`)
- `POST /api/v1/system/backup/import` - Importar backup (auth requerido)
- `GET /api/v1/admin/jobs` - Listado de jobs (admin)
- `GET /guides/` - Guías operativas HTML

### Configuración y secretos

La configuración operativa (SMTP, proveedor OIDC, service account JSON de FCM) se guarda en la
base de datos y no se devuelve al navegador: las respuestas exponen solo flags como
`has_smtp_password`, `has_client_secret` o `has_service_account`. El token del webhook de
Alertmanager se lee de `ALERTMANAGER_WEBHOOK_TOKEN`. El JWT interno usa `ADMIN_JWT_SECRET`; el
fallback por defecto del código **debe cambiarse en producción**.

El bootstrap inicial crea solo si la tabla `users` está vacía el usuario
`root@anomalia.local` con contraseña `admin123` (solo primera instalación). Además, el login
local mantiene un fallback que acepta esa combinación; **cambiala y desactivá/reeplazá el
fallback antes de exponer el servicio**.

### Pruebas integradas (ejecutables después de deploy)

- `scripts/smtp-test/run-test-smtp.sh` - receptor local `aiosmtpd`
- `scripts/smtp-test/etch-test.sh` - envío real a Ethereal
- `scripts/oidc-test/run-test-idp.sh` - IdP mock para SSO

## Licencia

- Código: [Apache-2.0](./LICENSE)
- Documentación: [CC BY-SA 4.0](./LICENSE-DOCS)
