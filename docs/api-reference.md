# Referencia de API Backend

El backend expone una API RESTful construida con **FastAPI**, protegida mediante autenticación basada en tokens JWT/OIDC y control de acceso basado en roles y perfiles (`RBAC`).

---

## 📋 Resumen de Routers Principales

| Prefijo | Módulo | Descripción |
| :--- | :--- | :--- |
| `/api/v1/auth` | Autenticación | Login (`login`/`ui-login`), usuario actual y tema (`theme`). |
| `/api/v1/admin` | Administración | Gestión de usuarios, roles, perfiles y tenants (`tenants_manager`). |
| `/api/infrastructure` | Infraestructura | Control y estado de nodos de infraestructura (`infrastructure_nodes`). |
| `/api/v1/tenants` | Tenants | Endpoints puente para consulta de tenants autorizados (`my-tenants`). |
| `/api/v1/approvals` | Aprobaciones | Solicitud y aprobación de acceso a roles/tenants (perfil `approvers`, spec 019). |
| `/api/v1/parses` | Parses | Lectura de datasources y proxy de dashboards por tenant (spec 008). |
| `/api/v1/ui` + `/ui` | Proxy Consolas | Proxy autenticado con tickets deslizantes para las UIs de VictoriaMetrics, Traces, Logs, Pyroscope y Parses. |
| `/api/v1/ai` | Inteligencia Artificial | Chat asistente contextual y gestión de proveedores LLM. |
| `/api/v1/system` | Configuración General | Gestión de SMTP, proxy de egress y backup de configuración (solo `admin`). |
| `/api/v1/notifications` | Notificaciones | Bandeja in-app por usuario (resultado de jobs, spec 017) y registro de dispositivos push (spec 018). |
| `/api/v1/alerts` | Alertas IA | Alertas enriquecidas por IA: recientes, historial y override de criticidad (spec 018). |
| `/api/v1/webhook` | Ingesta | Webhook de Alertmanager → `alert_history` + encolado de análisis IA (spec 018). |
| `/api/v1/settings` | Configuración de Auth | Modo de autenticación (`auth_mode`) y configuración SSO/OIDC (spec 020). |
| `/api/v1/oidc` | OIDC / SSO | Login, callback, discovery y CRUD de proveedores OIDC (spec 020). |

---

## 🔌 Endpoints Destacados

### Configuración General, SMTP, Proxy y Backup (specs/014)

Todos los endpoints bajo `/api/v1/system` requieren rol **`admin`** (`Depends(verify_admin_token)`).

* **Ruta:** `GET /api/v1/system/config`
* **Descripción:** Devuelve la configuración SMTP y proxy sin exponer secretos (solo flag `has_smtp_password`).
* **Respuesta:**
  ```json
  {
    "smtp_host": "smtp.empresa.com",
    "smtp_port": 587,
    "smtp_username": "user@empresa.com",
    "has_smtp_password": true,
    "smtp_use_tls": true,
    "smtp_from": "no-reply@empresa.com",
    "proxy_url": "http://proxy.empresa.com:8080",
    "proxy_enabled": true
  }
  ```

* **Ruta:** `PUT /api/v1/system/smtp`
* **Descripción:** Guarda la configuración SMTP. Campo ausente = sin cambios; `smtp_password=""` = borrar.
* **Payload:**
  ```json
  {
    "smtp_host": "smtp.empresa.com",
    "smtp_port": 587,
    "smtp_username": "user@empresa.com",
    "smtp_password": "secret",
    "smtp_use_tls": true,
    "smtp_from": "no-reply@empresa.com"
  }
  ```

* **Ruta:** `POST /api/v1/system/smtp-test`
* **Descripción:** Envía un email de prueba al destino indicado usando la configuración vigente. Devuelve `{ok, to}`; si el envío falla responde `502` con el detalle del error (`400` si falta `smtp_host`). Para verificar el circuito sin un servidor de correo real: `scripts/smtp-test/run-test-smtp.sh` (spec 018 §12), que además archiva cada mensaje en `scripts/smtp-test/received.log`. Para probar el envío real por internet (STARTTLS + login) con una bandeja web: `scripts/smtp-test/etch-test.sh` (Ethereal). Ejemplo de config para Gmail: `smtp.gmail.com:587` + TLS + contraseña de aplicación (el mailer usa STARTTLS; **no** soporta SSL implícito en 465).
* **Payload:**
  ```json
  { "to": "destino@empresa.com" }
  ```

* **Ruta:** `PUT /api/v1/system/proxy`
* **Descripción:** Guarda la URL del proxy y si está habilitado.
* **Payload:**
  ```json
  { "proxy_url": "http://proxy.empresa.com:8080", "proxy_enabled": true }
  ```

* **Ruta:** `GET /api/v1/system/backup/export?password=...`
* **Descripción:** Exporta un JSON cifrado (Fernet + PBKDF2) con las tablas de configuración: `users`, `roles`, `profiles`, `user_roles`, `role_profiles`, `user_tenants`, `role_tenants`, `tenants`, `tenant_datasources`, `ai_settings`, `oidc_providers`, `system_settings`. Excluye datos operativos (`ui_tickets`, `job_state`, `alert_history`, `port_mapping`, `audit_deletions`) y la **infraestructura** (`infrastructure_nodes`, `tenant_vmalert_instances`): cada clúster la gestiona vía deploy (spec 015).
* **Descarga:** `anomalia-backup.json`

* **Ruta:** `POST /api/v1/system/backup/import`
* **Descripción:** Restaura configuraciones desde un JSON cifrado con **upsert por PK** (no borra registros ausentes). Requiere `multipart/form-data` con `file` y `password`. El backup **no** incluye infraestructura (spec 015): al importar, cada tenant se **desacopla** de los nodos del origen (`vmalert_node_id`, `vmauth_node_id`, `instance_id` → `NULL`; `vmalert_port` → `NULL`; placement → `auto`) y la reconciliación lo reasigna a un nodo del clúster local. Todo ocurre en **un único job `backup_import`** (spec 016): la misma transacción toma un snapshot (`backup_import_state`), aplica el upsert + auto-placement y encola el job. El job reconcilia node-aware (`vmalert_redeploy` / `vmalert_regen_scrapes` / `parses_provision` solo con el nodo presente; el resto va a `skipped`) y publica el avance en `job_state.steps`. Si la reconciliación falla, **revierte todo**: infraestructura remota de los tenants nuevos (`vmalert_undeploy` + Parses, best-effort), BD al snapshot (`restore_import_snapshot`) y un regen global. No re-incrementa `tenants_count_active`. Devuelve **409** si ya hay un import en curso.
* **Respuesta Exitosa (`202 Accepted`):**
  ```json
  {
    "ok": true,
    "message": "Backup importado. Reconciliando infraestructura de 3 tenant(s)…",
    "tenants_count": 3,
    "skipped": 2,
    "job_id": "b5a41a31-c7c7-4f81-9b19-c4547281a]"
  }
  ```
* **Seguimiento:** sondea `GET /api/v1/admin/jobs/{job_id}`; su campo `steps` (lista `{name, status, detail?}`) alimenta la lista de pasos bajo la barra de progreso en `bkp-import-progress`.
* **Reconexión:** `GET /api/v1/admin/jobs?type=backup_import&limit=1` permite a la UI reengancharse a un import `queued`/`running` tras recargar la página (`admin.resumeBackupImport()`, spec 014 §6.6). El job sobrevive al vencimiento del token del usuario; al terminar se emite una notificación in-app y, si hay SMTP, un email.

### Notificaciones in-app (specs/017)
* **Ruta:** `GET /api/v1/notifications?unread=false&limit=50`
* **Permisos Requeridos:** cualquier usuario autenticado (`verify_any_user_token`).
* **Descripción:** Devuelve `{items: [...], unread_count}` con las notificaciones del usuario (`{id, type, title, body, job_id, link, is_read, created_at}`), ordenadas de más reciente a más antigua, acotado a 50 por defecto (máx. 200). Retención: 200 por usuario.
* **Rutas:** `POST /api/v1/notifications/{id}/read` (marca una) y `POST /api/v1/notifications/read-all` (marca todas). Devuelven `{ok: true}`; la lectura de una inexistente/ajena → **404**.
* **Rutas de dispositivos push:** `POST /api/v1/notifications/devices` (upsert por `fcm_token`, payload `{fcm_token, platform}`) y `DELETE /api/v1/notifications/devices?fcm_token=` (spec 018).

### Solicitudes de acceso y aprobaciones (specs/019)
* **Ruta:** `GET /api/v1/approvals/catalog`
* **Permisos Requeridos:** cualquier usuario autenticado (`verify_any_user_token`).
* **Descripción:** Devuelve `{roles: [...], tenants: [...]}` solicitables. Los roles excluyen `admin`; los tenants son los activos (`deleted_at IS NULL` y `status='active'`).
* **Ruta:** `POST /api/v1/approvals/requests` — **Payload:** `{ "roles": ["tenant_manager"], "tenants": ["METRICS"], "reason": "…" }`. Crea la solicitud del usuario autenticado (identidad desde el token). Exige al menos un rol/tenant válido y motivo no vacío. Notifica a los usuarios con perfil `approvers`.
* **Ruta:** `GET /api/v1/approvals/requests?status_filter=pending` — Approvers ven todas; el resto, solo las propias. Devuelve `{items: [...], is_approver}`; cada ítem incluye `{id, user_id, username, requested_roles, requested_tenants, reason, status, review_note, created_at, reviewed_at, reviewer, can_cancel, can_review}`.
* **Ruta:** `POST /api/v1/approvals/requests/{id}/approve` — **Permisos:** perfil `approvers`. Aplica los accesos de forma **aditiva** (`ON CONFLICT DO NOTHING`) y notifica al solicitante. No permite aprobar la propia solicitud (**403**); si ya fue resuelta, **409**.
* **Ruta:** `POST /api/v1/approvals/requests/{id}/reject` — **Permisos:** perfil `approvers`. Payload opcional `{ "review_note": "…" }`. Notifica al solicitante.
* **Ruta:** `POST /api/v1/approvals/requests/{id}/cancel` — el solicitante cancela su propia solicitud mientras esté `pending` (**403** si no es suya, **409** si ya fue resuelta).

### Autenticación OIDC / SSO (specs/020)
* **Ruta:** `GET /api/v1/settings/auth` — **Público.** Devuelve `{auth_mode, oidc_enabled, local_enabled}`. `local_enabled = auth_mode ∈ {local, local_only, hybrid}`. Web y móvil lo usan para decidir si muestran login local y/o el botón OIDC.
* **Ruta:** `GET /api/v1/settings/sso` — **Permisos:** `admin`. Config SSO + proveedor activo (sin `client_secret`).
* **Ruta:** `PUT /api/v1/settings/sso` — **Permisos:** `admin`. Upsert del proveedor OIDC y de `auth_mode`, `oidc_enabled`, `oidc_require_email_verified`, `oidc_auto_provision`, `default_oidc_provider_id`. Valores de `auth_mode`: `local`, `local_only`, `hybrid` (default), `oidc_only`.
* **Ruta:** `GET /api/v1/oidc/login?session_type=web|mobile&provider_id=&redirect_uri=` — **Público.** Resuelve endpoints (OIDC Discovery con fallback a manuales), genera `state`/`nonce`/PKCE (S256), persiste `oidc_sessions` (TTL 10 min) y devuelve `{auth_url, state}`. El `redirect_uri` web (`provider.redirect_uri`) **debe** apuntar a este endpoint de callback `…/api/v1/oidc/callback`, no a la página estática `/auth/callback`.
* **Ruta:** `GET /api/v1/oidc/callback?code&state` — **Público.** Valida `state`, intercambia el code por tokens (`client_secret_basic`/`client_secret_post` + `code_verifier`), valida el ID token (JWKS, `iss`, `aud`, `exp`, `nonce`), aplica reglas (email verificado, dominios, auto-provision **sin roles por defecto**) y emite el JWT interno. Responde según `session_type`: **web** → 302 a `/auth/callback?token=…`; **mobile** → JSON `{access_token, username, role}` (la app lo llama tras recibir el deep link con `code`/`state`).
* **Ruta:** `GET /auth/callback` — página mínima (`auth_callback.html`) de aterrizaje; guarda el token en `localStorage` y navega a `/`. Solo consume `token`/`error`; el intercambio de `code`/`state` ocurre en `/api/v1/oidc/callback`.
* **CRUD:** `GET|POST /api/v1/oidc/providers`, `PUT|DELETE /api/v1/oidc/providers/{id}` — **Permisos:** `admin`. Campos nuevos: `discovery_url`, endpoints manuales, `scopes`, `use_discovery`, `client_auth_method`, `pkce_enabled`, `allowed_domains`, `default_roles`. El listado GET incluye `has_client_secret` y **ninguna** respuesta expone `client_secret`.

### Autenticación de sesión (login local)
* **Ruta:** `POST /api/v1/auth/login` (alias `POST /api/v1/auth/ui-login`) — **Público.** Login local con credenciales; emite el JWT interno.
* **Ruta:** `GET /api/v1/auth/user` — **Permisos:** token de usuario. Devuelve el perfil/rol del usuario autenticado.
* **Ruta:** `PUT /api/v1/auth/theme` — **Permisos:** token de usuario. `multipart/form-data` con `theme`, `password` y/o `avatar`.
* **Ruta:** `GET /api/v1/tenants/my-tenants` — **Permisos:** token de usuario. Devuelve los tenants visibles para el usuario: unión de `user_tenants` ∪ `role_tenants` (`admin` ve todos).

### Alertas enriquecidas por IA (specs/018)

* **Ruta:** `GET /api/v1/alerts/recent?limit=50`
* **Permisos Requeridos:** perfil `access_alerts` (`verify_profile_access("access_alerts")`; scope por tenants visibles, `admin` ve todos).
* **Descripción:** Devuelve `{items: [...]}` con las **alertas activas** (`status='firing'`). Cada ítem: `{id, tenant_id, tenant_name, alertname, severity, severity_source, summary, description, ai_summary, ai_root_cause, ai_checks, ai_error, status, labels, annotations, generator_url, severity_override_by, severity_override_at, starts_at, ends_at, notified_at, updated_at, email_status, email_error}`. `ai_error` es `null` si el análisis IA fue correcto; si no, el `kind` del fallo (`quota`/`transient`/`auth`/`invalid`/`rate_limit`) y la tarjeta se marca «IA no disponible» conservando los datos de Alertmanager (spec 018). `email_status` (`sent`/`failed`/`not_configured`/`skipped`) registra el envío del email a los destinatarios del tenant; si es `failed` o `not_configured`, la tarjeta muestra el badge **«Email no enviado»** con el motivo en `email_error`. El enlace `generator_url` apunta a la consola **vmalert** («Ver en vmalert»).

* **Ruta:** `GET /api/v1/alerts/history?tenant_id=&status=&limit=&offset=&q=`
* **Descripción:** Historial paginado (activas y resueltas) → `{items: [...], total}`. El parámetro `q` busca de forma **case-insensitive** (`ILIKE`) sobre `alertname`, `summary`, `description` y `ai_summary`, escapando los comodines del usuario.

* **Ruta:** `PATCH /api/v1/alerts/{alert_id}/severity`
* **Permisos Requeridos:** perfil `alerts_manager`.
* **Payload:** `{ "severity": "high" }` (valores: `critical`, `high`, `medium`, `low`, `info`).
* **Descripción:** Fija la criticidad a mano (`severity_source='manual'`), prevaleciendo sobre reanálisis IA. Devuelve `{ok: true, id, severity, severity_source}`.

* **Destinatarios por tenant (perfil `alerts_manager`):**
  `GET/POST /api/v1/admin/alerts/tenants/{tenant_id}/recipients` y
  `PUT/DELETE /api/v1/admin/alerts/tenants/{tenant_id}/recipients/{recipient_id}`.
  El email se valida; el alta es upsert por `(tenant_id, email)`.

* **Webhook de Alertmanager:** `POST /api/v1/webhook/alertmanager`,
  protegido por `verify_webhook_token` (Bearer `ALERTMANAGER_WEBHOOK_TOKEN`;
  abierto si no está configurado). No bloquea a AM. Exento del redirect HTTPS.
* **Ciclo de vida (spec 018):** el `firing` hace upsert de la **única fila activa**
  por `(tenant_id, fingerprint)` (índice parcial `uq_alert_history_active ... WHERE
  status='firing'`) y encola `alert_analyze`. El `resolved` **cierra** esa fila
  (`UPDATE`, no inserta) y, si afecta exactamente una fila, encola `alert_resolved`
  para notificar la regularización (in-app + push). Un re-disparo tras resolver abre
  un nuevo episodio.

### 1. Sincronización y Materialización de Reglas vmalert
* **Ruta:** `POST /api/v1/admin/tenants/{tenant_id}/vmalert/sync_rules`
* **Permisos Requeridos:** `alerts_manager`
* **Descripción:** Encola un trabajo asíncrono (`vmalert_sync_rules`) que lee el campo `rules_yaml` desde la base de datos (fuente de verdad), lo materializa en la ruta del nodo correspondiente (`rules_path`) y ejecuta un `/-/reload` HTTP sobre la instancia vmalert.
* **Respuesta Exitosa (`202 Accepted`):**
  ```json
  {
    "status": "accepted",
    "job_id": "b5a41a31-62c7-4f81-9b19-c45472877a11"
  }
  ```

### 2. Chat Asistente con IA Restringida
* **Ruta:** `POST /api/v1/ai/chat`
* **Permisos Requeridos:** Token JWT válido (`verify_any_user_token`)
* **Payload de Entrada:**
  ```json
  {
    "context": "vmalert_rules",
    "tenant_id": 1,
    "messages": [
      { "role": "user", "content": "¿Cómo escribo una regla PromQL para detectar uso de CPU superior al 90%?" }
    ]
  }
  ```
* **Notas:** `vmalert_rules` **requiere** `tenant_id` (responde `400` si falta) y valida el acceso al tenant (`403`). El backend inyecta contexto vivo en el prompt (reglas del tenant + estado de vmalert para `vmalert_rules`; URLs de lectura de los datasources para `victoria_metrics_query`/`victoria_logs_query`).
* **Respuesta Exitosa (`200 OK`):**
  ```json
  {
    "response": "Para detectar un uso de CPU superior al 90%, puedes utilizar la siguiente regla en vmalert:\n\n- alert: HighCpuUsage\n  expr: sum(rate(node_cpu_seconds_total{mode=\"idle\"}[5m])) by (instance) < 0.1\n  for: 5m...",
    "context": "vmalert_rules"
  }
  ```

### 3. Estado del proveedor de IA (spec 018)
* **Ruta:** `GET /api/v1/ai/status`
* **Permisos Requeridos:** Token JWT válido (`verify_any_user_token`)
* **Descripción:** Estado global del proveedor de IA (tabla `ai_provider_status`), usado por el banner de la web. La prueba `POST /api/v1/ai/test` también actualiza este estado.
* **Respuesta Exitosa (`200 OK`):**
  ```json
  {
    "provider": "gemini-3.5-flash-lite",
    "state": "quota_exhausted",
    "kind": "quota",
    "message": "429 RESOURCE_EXHAUSTED",
    "consecutive_failures": 3,
    "last_error_at": "2026-01-01T12:00:00",
    "last_success_at": "2026-01-01T11:00:00",
    "updated_at": "2026-01-01T12:00:00"
  }
  ```
  `state` ∈ `ok` \| `degraded` \| `quota_exhausted`.

### 4. Gestión de IA (solo `admin`)
* **Rutas:** `GET /api/v1/ai/providers` (catálogo), `GET /api/v1/ai/config`, `PUT /api/v1/ai/config` (persiste en `ai_settings`), `POST /api/v1/ai/test` (prueba y actualiza `ai_provider_status`), `GET /api/v1/ai/gemini-models`.
* **Descripción:** Configuración del proveedor LLM usada por el chat y por el pipeline de análisis de alertas (spec 018).

### 5. Parses (spec 008)
* **Ruta:** `GET /api/v1/parses/datasources` — lista los datasources de Parses para el usuario.
* **Ruta:** `GET|POST /api/v1/parses/datasources/{tenant_id}/{path}` — proxy de lectura/escritura hacia la API de Perses con **aislamiento por tenant**.

### 6. Proxy de consolas (spec 010)
* **Ruta:** `POST /api/v1/ui/ticket` — emite un ticket deslizante.
* **Ruta:** `GET /ui/redeem/{ticket}` — canjea el ticket.
* **Ruta:** `GET|POST|PUT|PATCH|DELETE|OPTIONS /ui/{ticket}/{path}` — proxy autenticado hacia las consolas de VictoriaMetrics/Traces/Logs/Pyroscope/Parses.

### 7. Ciclo de vida de vmalert y Parses (spec 011/012)
Bajo `/api/v1/admin/tenants/{tenant_id}` (perfil `alerts_manager`):
* `GET|PUT .../vmalert/rules` — leer/editar `rules_yaml` (fuente de verdad).
* `POST .../vmalert/reload`, `.../vmalert/deploy`, `.../vmalert/undeploy`, `.../vmalert/redeploy` — despliegue de la instancia vmalert por tenant.
* `POST .../parses/provision` — (re)aprovisiona project/datasources de Parses del tenant.
* **Nota:** `POST .../vmalert/sync_rules` (documentada arriba) materializa las reglas en el nodo y recarga vmalert.

### 8. Jobs y control (solo `admin`)
* **Ruta:** `GET /api/v1/admin/jobs?type=&status=&limit=` → `{jobs: [{id, type, status, ...}]}`.
* **Ruta:** `GET /api/v1/admin/jobs/{job_id}` → incluye `steps`, `phase`, `progress_pct`, `timeout_at`. **Nota:** estos dos endpoints no declaran dependencia de auth en el código actual.

### 9. Infraestructura (solo `admin`)
* **Rutas:** `GET /api/infrastructure/`, `PUT /api/infrastructure/{node_id}`, `DELETE /api/infrastructure/{node_id}` (`infra_router.py`; protegidos a nivel de router con `verify_admin_token`).

### 10. Test de webhook (solo `admin`)
* **Ruta:** `GET /api/v1/webhook/alertmanager/test` — valida la configuración del token de ingesta.

---

## 🚨 Códigos de Error HTTP Comunes

| Código | Significado | Causa Habitual |
| :--- | :--- | :--- |
| `400 Bad Request` | Petición inválida | Sintaxis YAML incorrecta al guardar reglas vmalert. |
| `401 Unauthorized` | No autenticado | Token JWT ausente, vencido o malformado. |
| `403 Forbidden` | Acceso denegado | El usuario carece del perfil o rol necesario para la acción. |
| `404 Not Found` | No encontrado | El tenant, nodo o recurso solicitado no existe. |
| `409 Conflict` | Conflicto de estado | Intento de modificar o eliminar un tenant de control interno (`is_internal=true`) o instancia inexistente. |
