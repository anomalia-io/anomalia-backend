# AGENTS.md — anomalia-backend

Guía para agentes y desarrolladores que trabajen en **`anomalia-backend`**: la API FastAPI,
los workers y la consola web de **Anomalia**.

> Este repo es parte del proyecto multi-repo **Anomalia** (`anomalia-io`). Las specs SSD y la
> gobernanza viven en el repo privado `anomalia-meta`. Trabajá **una sesión por repo**.

## Stack

- **Python 3.10+**, **FastAPI** + **Uvicorn**
- **PostgreSQL 15**, **SQLAlchemy**, **psycopg2**
- **JWT/OIDC** (`python-jose`, `PyJWT`, `passlib`/`bcrypt`)
- **OpenTelemetry** + **Prometheus** + **Pyroscope**
- **Ansible** embebida (jobs de infraestructura)
- Web: **HTML5/CSS3/JS Vanilla** modular (`frontend/js/{api,state,ui,admin,main}.js`)

## Comandos

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000   # servidor de desarrollo

pip install -r requirements-dev.txt
pytest                                                     # pruebas unitarias
gitleaks dir .                                             # escaneo de secretos (obligatorio)
```

## Estructura

```
app/               # FastAPI: main, routers/, workers/, oidc*, ai_*, mailer, push
tests/             # pytest
frontend/          # Consola web (servida por el backend)
dashboards/        # Fuentes de dashboards (Grafana -> Perses)
scripts/           # Harness de prueba SMTP y OIDC
```

## Convenciones

- **Tipado estricto** y esquemas **Pydantic** en endpoints y funciones internas.
- Endpoints administrativos protegidos con `Depends(verify_admin_token)` /
  `Depends(verify_profile_access)`.
- Frontend: mantener la modularidad JS vanilla (`api.js`, `state.js`, `ui.js`, `admin.js`, `main.js`)
  y el cliente API centralizado con gestión de JWT/OIDC.
- **Sin secretos**: nunca versionar credenciales; referenciar por variable de entorno.
- **Commits firmados**: `commit.gpgsign=true` con SSH (heredado del `~/.gitconfig` global).
- **Un PR por repo**: si un cambio impacta otro repo (infra/mobile/meta), abrí una sesión
  en ese repo; no mezcles repos en un mismo PR.

## Documentación

- [Arquitectura general](docs/architecture/overview.md)
- [Tenants internos](docs/architecture/internal-tenants.md)
- [Referencia de API](docs/api-reference.md)
- [Pipeline AIOps y asistente IA](docs/aiops-and-chat.md)
- [Procesamiento de alertas](docs/aiops/alert-processing.md)

## Enlaces

- Specs SSD y gobernanza: repo privado `anomalia-meta` (`specs/`).
- Despliegue: [`anomalia-infra`](https://github.com/anomalia-io/anomalia-infra).
