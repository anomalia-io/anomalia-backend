# Tenants de Control Interno y Reglas Canónicas

> [!NOTE]
> Los tenants de control interno (`is_internal = true`) garantizan que la infraestructura base del sistema esté permanentemente monitoreada, sin depender de la edición manual de reglas por parte de los operadores.

---

## 1. Motivación y Concepto

En una plataforma de observabilidad de nivel empresarial, los componentes críticos del stack (métricas generales, trazas, perfiles de rendimiento y registros de auditoría) deben contar con alertas críticas nativas e inquebrantables. 

Para lograr esto, Anomalia define cuatro dominios internos protegidos:
- **`METRICS`** (`type = "metrics"`): Supervisión general de VictoriaMetrics, salud del backend **y salud de Pyroscope** (sus reglas `PyroscopeDown`/`PyroscopeHeartbeatMissing` viven en `internal_rules/metrics.yaml`).
- **`TRACES`** (`type = "logs" / "traces"`): Disponibilidad y latencia de VictoriaTraces.
- **`PROFILES`** (`type = "profiles"`): Salud de Pyroscope y recolección de perfiles de CPU/Memoria. **No lleva instancia vmalert**: los perfiles no son series PromQL consultables por vmalert (Pyroscope no expone API compatible con Prometheus HTTP API), por lo que su monitoreo básico se hace desde el store METRICS.
- **`AUDIT_LOGS`** (`type = "logs"`): Integridad y flujo de VictoriaLogs y pistas de auditoría.

---

## 2. El Flag `is_internal = true`

Cada uno de estos tenants se inicializa con el flag `is_internal = true` a través de los scripts de migración y siembra inicial (`init.sql.j2` / `database.py`).

| Característica | Tenants Estándar | Tenants de Control Interno (`is_internal = true`) |
| :--- | :--- | :--- |
| **Creación** | Manual (vía Panel Admin o API) | Automática por siembra del sistema |
| **Edición de Reglas** | Completamente libre por API / UI | **Bloqueada (409 Conflict)** |
| **Placement** | Manual o Automático configurable | Automático (*Always-On*) |
| **Fuente de Reglas** | Definidas por el usuario | Reglas Canónicas inmutables en código |

---

## 3. Reglas Canónicas Inmutables (SSOT en Código)

Las reglas de alerta para estos tenants residen físicamente en el repositorio del backend:
`backend/app/internal_rules/<slug>.yaml`

> [!WARNING]
> Estos archivos se cargan mediante validación `yaml.safe_load` y se inyectan **verbatim** (sin procesar plantillas Jinja sobre PromQL para evitar corrupciones en las llaves `{{ $labels.* }}`).

### Flujo de Bootstrap y Despliegue (*Always-On*)

1. **Reconciliación periódica (`_bootstrap_internal` + `_reconcile_loop`):**
   - Al arrancar el worker (`VmalertWorker._bootstrap_internal`) y **después cada `interval`** (`_reconcile_loop`, spec 011 §4.9) se escanean los tenants activos con `is_internal=true`.
   - Si no poseen una instancia vmalert activa (`status='deployed'`) ni un trabajo `vmalert_deploy` queued/running, el sistema selecciona automáticamente el nodo con menor carga (`capacity_slots` / `tenants_count_active`).
   - Se encola un trabajo de tipo `vmalert_deploy` aplicando la regla canónica correspondiente. Un deploy que falle después del arranque (por ejemplo, timeout del playbook, `VMALERT_PLAYBOOK_TIMEOUT_SECONDS` default 600) se **re-encola solo** en <60s.
   - La misma pasada reconcilia el **puerto determinístico** por tenant (`8880 + tenant_id`) y los datasources internos usan **cluster URL** en formato `account:project` (dos puntos): `http://<vmselect>:8401/select/0:0/prometheus` y `http://<vminsert>:8400/insert/0:0/prometheus`. La forma con barra (`/select/0/0/prometheus`) responde 400. El tenant PROFILES queda fuera del always-on (no lleva vmalert); si persiste una instancia residual, la reconciliación la retira.

2. **Inmutabilidad:**
   - Si un operador intenta modificar las reglas mediante `PUT /api/v1/admin/tenants/{id}/vmalert/rules`, la API responde con un código **`409 Conflict`** indicando que el tenant es de control interno.
   - De igual forma, las operaciones destructivas (hard-delete o undeploy) sobre estos tenants están protegidas y prohibidas por el validador `_guard_internal_tenant`.

---

## 4. Interfaz de Usuario (UI)

En el panel de administración web:
- El editor de reglas YAML para tenants internos se abre automáticamente en modo **solo lectura** (`readOnly`).
- Se muestra un distintivo visual (*badge*): **"Control interno · reglas canónicas e inmutables"**.
- Los botones de guardado directo y sincronización se mantienen deshabilitados para prevenir modificaciones accidentales.
