# anomalia-backend

API y workers del backend de **Anomalia**, la plataforma multitenant de observabilidad con AIOps.

Incluye la **consola web** (JavaScript Vanilla) servida por el propio backend.

## Stack

- **Python 3.10+**, **FastAPI** + **Uvicorn**
- **PostgreSQL 15**, **SQLAlchemy**, **psycopg2**
- **JWT/OIDC** (`python-jose`, `PyJWT`, `passlib`/`bcrypt`)
- **OpenTelemetry** + **Prometheus** + **Pyroscope**
- **Ansible** embebida (jobs de infraestructura)
- Web: **HTML5/CSS3/JS Vanilla** (modular: `frontend/js/{api,state,ui,admin,main}.js`)

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

## Desarrollo

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Tests

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
