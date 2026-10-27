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

## 👨‍💻 Para Contribuidor/Developer

Trabajas con este repo cuando modificas la API, workers, routers o lógica de negocio. Flujo SSD:

### 1. Entorno
```bash
cd anomalia-backend
pip install -r requirements.txt          # o pip install -e . para desarrollo activo
```

### 2. Validación SSD (before commit)
- [ ] Cargar skill `anomalia-backend-skill`
- [ ] Leer spec relevante (`specs/003-aiops-alert-pipeline`, `specs/018-alert-ai-analysis-and-delivery`, etc.)
- [ ] Verificar `constitution.md` principios
- [ ] `pytest` pasa todos los tests
- [ ] `gitleaks dir .` (en `anomalia-meta/` o repo correspondiente) - pasa limpio

### 3. Especificaciones
- Si tu cambio altera comportamiento documentado → actualizar spec en `anomalia-meta/specs/`
- Si es nueva funcionalidad → crear spec nueva con número libre
- **Siempre** que agregues nuevos endpoints/comandos: actualizar `AGENTS.md` en `anomalia-meta`

### 4. Post-Change (si afecta infraestructura)
```bash
# En anomalia-infra (workspace padre):
cd ../anomalia-infra
ansible-playbook -i inventory.ini destroy-infra.yml && \
ansible-playbook -i inventory.ini deploy-infra.yml
```

### 5. Commiteo
```bash
git add .
git commit -S -m "feat: <descripción>"    # -S firma SSH
git push origin main
```

### 6. Tests
```bash
pytest                                    # tests unitarios
pytest -k "specific_test"                 # test específico
```

## Stack (detalle)

- **Python 3.10+**, **FastAPI** + **Uvicorn** (ASGI)
- **PostgreSQL 15**, **SQLAlchemy**, **psycopg2** (ORM)
- **JWT/OIDC** (`python-jose`, `PyJWT`, `passlib`/`bcrypt`)
- **OpenTelemetry** (API, SDK, FastAPI Instrumentation, OTLP HTTP)
- **Prometheus Client** + **Pyroscope** (telemetría)
- **Ansible** embebida (solo `ansible.builtin`; sin colecciones Galaxy)
- Web: **HTML5/CSS3/JS Vanilla** modular (`frontend/js/{api,state,ui,admin,main}.js`)

## Estructura

```
app/               # FastAPI: main, routers/, workers/, oidc*, ai_*, mailer, push
tests/             # pytest
frontend/          # Consola web (servida por el backend)
dashboards/        # Fuentes de dashboards (Grafana -> Perses)
media/             # Recursos estáticos
Dockerfile
requirements.txt
requirements-dev.txt
pytest.ini
```

## Requisitos

- Python 3.10+
- PostgreSQL 15 accesible vía `DATABASE_URL`
- (Opcional) Ollama para el proveedor IA local

## Desarrollo (detalle)

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Tests (detalle)

```bash
pip install -r requirements-dev.txt
pytest
```

## Docker

```bash
docker build -t anomalia-backend .
docker run -p 8000:8000 anomalia-backend
```

## Pruebas de integración (SMTP y OIDC)

Con el stack desplegado, `scripts/` incluye harness de prueba que no requieren infraestructura
externa:

- `scripts/smtp-test/run-test-smtp.sh` — receptor SMTP local (`aiosmtpd`); apunta el gateway a
  él para ver "aterrizar" los correos de alertas. `--reset` restaura la config.
- `scripts/smtp-test/etch-test.sh` — envío real por Ethereal (`smtp.ethereal.email:587`) para
  validar internet + STARTTLS + login.
- `scripts/oidc-test/run-test-idp.sh` — IdP OIDC mock (`--configure` lo registra en la BD) para
  probar SSO autorization_code + PKCE. `--stop`/`--reset` limpian.

Ver los comentarios de cabecera de cada script y las variables de entorno configurables.

El despliegue real lo orquesta el repo [`anomalia-infra`](https://github.com/anomalia-io/anomalia-infra) (Ansible), que
construye esta imagen en el host.

## Documentación

- [Arquitectura general](docs/architecture/overview.md) — componentes del sistema, multitenancy,
  gateway HTTPS y proxy de consolas.
- [Tenants internos](docs/architecture/internal-tenants.md) — tenants de control (`is_internal`)
  y reglas canónicas inmutables.
- [Referencia de API](docs/api-reference.md) — routers, RBAC, webhooks, chat IA.
- [Pipeline AIOps y asistente IA](docs/aiops-and-chat.md) — proveedores de IA, restricciones por contexto.
- [Procesamiento de alertas](docs/aiops/alert-processing.md) — ingesta desde Alertmanager hasta el enriquecido por IA.

## Licencia

[Apache-2.0](./LICENSE)
