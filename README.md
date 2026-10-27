# anomalia-backend

API y workers del backend de **Anomalia**, la plataforma multitenant de observabilidad con AIOps.

Incluye la **consola web** (JavaScript Vanilla) servida por el propio backend.

## 👤 Para Usuario Final (probar el backend)

No necesitas clonar este repo si solo quieres probar Anomalia. El stack completo se despliega desde `anomalia-infra`.

Si quieres correrlo localmente para pruebas:

```bash
cd anomalia-backend
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Endpoints principales:**
- `GET /healthz` - Health check
- `GET /api/v1/status` - Estado del sistema
- `POST /api/v1/system/backup/import` - Importar backup (auth requerido)
- `GET /api/v1/admin/jobs` - Listado de jobs (admin)

**Pruebas integradas** (ejecutables después de deploy):
- `scripts/smtp-test/run-test-smtp.sh` - receptor local `aiosmtpd`
- `scripts/smtp-test/etch-test.sh` - envío real a Ethereal
- `scripts/oidc-test/run-test-idp.sh` - IdP mock para SSO

El despliegue real lo orquesta el repo [`anomalia-infra`](https://github.com/anomalia-io/anomalia-infra) (Ansible), que construye esta imagen en el host.