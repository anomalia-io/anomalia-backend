# Pipeline de Alertas y AIOps

El flujo de procesamiento automatizado de anomalías opera de la siguiente manera:
1. **vmalert** evalúa las reglas de métricas del tenant.
2. **Alertmanager** notifica vía webhook al backend FastAPI (`POST /api/v1/webhook/alertmanager`, exento del redirect HTTPS). El `firing` hace upsert de la única fila activa por `(tenant_id, fingerprint)` y encola `alert_analyze`; el `resolved` cierra esa fila y encola `alert_resolved`.
3. El **Backend** procesa la alerta y consulta al modelo de IA para diagnóstico (con fallback si el modelo no está disponible).
4. **Distribución multi-canal** de la alerta enriquecida:
   - **Web** (bandeja in-app de usuarios con perfil `access_alerts`) y **App Android** (push FCM).
   - **Email** a los destinatarios habilitados del tenant (`tenant_alert_recipients`, spec 018). El resultado se persiste en `alert_history.email_status` (`sent`/`failed`/`not_configured`/`skipped`) y `email_error`; la tarjeta web muestra el badge «Email no enviado» si falló.
   - El job `alert_resolved` notifica la regularización (in-app + push) sin re-análisis IA.
   - Avisos deduplicados a **admin**: SMTP sin configurar, fallos de envío y transiciones de cuota/recovery del proveedor IA.

## Proveedor de IA

El proveedor de IA se selecciona en el panel de control (pestaña «Proveedor IA») y
se guarda en la tabla `ai_settings` de PostgreSQL. Catálogo disponible:

- **Free (OpenCode Zen):** Space Bunny Free. Requiere API Key Zen ingresada en el
  panel (https://opencode.ai/auth). Es el único modelo Free que el gateway Zen
  permite invocar con API key desde fuera de la app; los demás modelos Free
  devuelven `403 FreeTierError` (verificado 2026-10-02, spec 003 §2.5).
- **Local:** `anomalia_ollama` (contenedor Ollama del stack, sin límite de tokens).
  Modelo por defecto `llama3.2:1b`, descargado automáticamente por el role
  `backend` durante el despliegue.
- **API Key:** Gemini (Google), con API Key y modelo configurables desde el panel.

Las API keys viven únicamente en la base de datos; nunca en archivos del repositorio.
Ver `specs/003-aiops-alert-pipeline.md` §2 para el detalle completo.
