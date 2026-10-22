# Pipeline AIOps y Asistente de IA

El pipeline de Inteligencia Artificial de Anomalia procesa, enriquece y diagnostica alertas de forma asíncrona, proporcionando además un asistente interactivo con restricciones estrictas de contexto.

---

## 1. Proveedores de IA Soportados

La configuración del proveedor se gestiona de forma centralizada en la tabla `ai_settings` de PostgreSQL (accesible desde el panel de administración):

| Proveedor | Identificador | Descripción y Consideraciones |
| :--- | :--- | :--- |
| **OpenCode Zen (Free)** | `space-bunny-free` | Espacio gratuito con cuota controlada. Requiere API Key válida de OpenCode. Es el único modelo gratuito autorizado a través del gateway (otros modelos Free de Zen devuelven `403 FreeTierError` fuera de la app de OpenCode). |
| **Local (Ollama)** | `anomalia_ollama` | Contenedor local integrado en el stack (`anomalia_ollama`). Sin límite de tokens. Modelo por defecto: `llama3.2:1b` (descargado automáticamente durante el despliegue). **Proveedor por defecto** (`DEFAULT_AI_PROVIDER`). |
| **Google Gemini API** | `gemini` | Modelo comercial basado en API Key y modelo configurable por el usuario (default `gemini-3.5-flash-lite`). |

Los identificadores anteriores son los `id` del catálogo `AI_PROVIDER_CATALOG`. La gestión (leer/guardar config, probar, listar modelos) se expone por `/api/v1/ai/{providers,config,test,gemini-models}` (solo `admin`).

> [!NOTE]
> Las credenciales y claves API se almacenan cifradas en la base de datos y nunca se versionan en el repositorio de código fuente.

---

## 2. Chat de IA con Restricción de Contexto

El endpoint `POST /api/v1/ai/chat` permite consultar al modelo de lenguaje aplicando un **aislamiento estricto por contexto** para evitar la contaminación cruzada de datos entre tenants o dominios de observabilidad.

### Contextos Permitidos y Restricciones

| Contexto (`context`) | Ámbito de Datos | Prompt Canónico / Restricción |
| :--- | :--- | :--- |
| `vmalert_rules` | Reglas del Tenant activo | Restringido exclusivamente a gramática PromQL y diseño de reglas vmalert. **Requiere `tenant_id`** (400 si falta) y valida el acceso al tenant (403). |
| `victoria_metrics_query` | Métricas generales | Limitado a sintaxis PromQL para VictoriaMetrics. |
| `victoria_logs_query` | Logs del sistema | Limitado a sintaxis LogsQL para VictoriaLogs. |
| `victoria_traces_query` | Trazas distribuidas | Limitado a consultas de trazas OTLP en VictoriaTraces. |
| `pyroscope_query` | Perfiles de rendimiento | Limitado a análisis de CPU/Memoria de Pyroscope. |
| `parses_query` | Dashboards | Limitado a la estructura y paneles JSON de Parses. |

El payload es `{ "context": "<valor>", "tenant_id": <int?>, "messages": [{ "role": "user", "content": "…" }] }`
y la respuesta incluye `{ "response": "…", "context": "<valor>" }`. El backend inyecta
contexto vivo (reglas del tenant + estado de vmalert para `vmalert_rules`; URLs de
lectura de los datasources para los contextos de consulta).

> [!WARNING]
> Si el usuario realiza una pregunta fuera del ámbito autorizado para el contexto activo, el modelo responde obligatoriamente con la frase canónica de fallback:
> `"Solo puedo ayudarte con [tema permitido]."`
