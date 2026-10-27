# CONTRIBUTING.md - Anomalia Backend

## 👨‍💻 Flujo de Desarrollo (SSD Obligatorio)

Todo cambio en el backend de Anomalia debe seguir el protocolo **Specs-Anchored Development (SSD)**.

### ⚠️ Antes de Implementar

1. **Cargar skill** `anomalia-backend-skill` (segúdir `AGENTS.md §3.4`)

2. **Leer spec relevante** según el cambio:
   - `specs/003-aiops-alert-pipeline.md` - Pipeline AIOps y webhooks
   - `specs/018-alert-ai-analysis-and-delivery.md` - Análisis IA y entrega multi-canal
   - `specs/004-rbac-and-security.md` - RBAC y endpoints protegidos
   - `specs/017-notifications.md` - Notificaciones al usuario

3. **Verificar `constitution.md`** - Principios rectores del proyecto

4. **Ejecutar validaciones**:
   - `pytest` - Todos los tests deben pasar
   - `gitleaks dir .` - Debe pasar limpio (`no leaks found`)
   - Revisar que no hay secretos hardcodeados (strings `ctx7sk-` o `Bearer` de 20+ chars)

### 📝 Después de Implementar

1. **Actualizar spec** en `anomalia-meta/specs/` si el cambio altera comportamiento documentado:
   - Misma numeración: `NNN-titulo-en-kebab-case.md`
   - Si es nueva funcionalidad → crear spec nueva con número libre

2. **Actualizar `AGENTS.md`** en `anomalia-meta` si se agregan nuevos endpoints o comandos:
   - Agregar fila en tablas de skills/MCP
   - Nueva sección si corresponde (§3.5 flujo documentación)

3. **Invocar `@spec-sync`** desde la sesión `anomalia-backend`:
   - El subagente lee `git status`, `git diff`, `git log -n 5`
   - Propone update en `anomalia-meta/specs/`
   - El usuario approbar y commit en `anomalia-meta`

4. **Post-Change (si afecta infraestructura)**:
   ```bash
   # En anomalia-infra (workspace padre):
   cd ../anomalia-infra
   ansible-playbook -i inventory.ini destroy-infra.yml && \
   ansible-playbook -i inventory.ini deploy-infra.yml
   ```
   *Requerido si el cambio toca workers, workers supervisor o flujo de jobs.*

5. **Commit con firma SSH**:
   ```bash
   git add .
   git commit -S -m "feat: <descripción del cambio>"
   git push origin main
   ```
   - **Obligatorio** el flag `-S` para firma SSH verificada
   - `user.signingkey = ~/.ssh/id_ed25519_personal.pub`
   - `user.email = jo.asd.xd@gmail.com`

### 📋 Checklist Pre-Commit

| Item | Verificar |
|------|-----------|
| Skill `anomalia-backend-skill` cargada | ✅ |
| Spec relevante leída | ✅ |
| `constitution.md` revisada | ✅ |
| `pytest` pasa todos los tests | ✅ |
| `gitleaks dir .` pasa limpio | ✅ |
| Sin secretos hardcodeados en código | ✅ |
| Commit con firma SSH (`-S`) | ✅ |
| `@spec-sync` invocado (si corresponde) | ✅ |

### 📦 Piles y Tests

```bash
# Instalar dependencias
pip install -r requirements.txt

# Ejecutar tests
pytest                                    # tests unitarios
pytest -k "specific_test"                 # test específico

# Desarrollo local
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Docker (construir e inspeccionar)
docker build -t anomalia-backend .
docker run -p 8000:8000 anomalia-backend
```

## 📄 Licencia

[Apache-2.0](./LICENSE)