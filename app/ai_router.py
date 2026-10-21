from typing import Any, Dict, List, Optional
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .auth import verify_admin_token, verify_any_user_token
from .database import get_db_connection
from .ai_providers import (
    AI_PROVIDER_CATALOG,
    DEFAULT_AI_PROVIDER,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_ANALYSIS_LANGUAGE,
    ANALYSIS_LANGUAGES,
    AIProviderError,
    get_ai_settings,
    get_gemini_models,
    get_provider,
)
from .ai_status import get_ai_status, record_ai_failure, record_ai_success

router = APIRouter(prefix="/api/v1/ai", tags=["AI Providers"])

TEST_PROMPT = "Responde únicamente con la palabra OK."


class ChatContext(str, Enum):
    VMALET_RULES = "vmalert_rules"
    VICTORIA_METRICS_QUERY = "victoria_metrics_query"
    VICTORIA_LOGS_QUERY = "victoria_logs_query"
    VICTORIA_TRACES_QUERY = "victoria_traces_query"
    PYROSCOPE_QUERY = "pyroscope_query"
    PARSES_QUERY = "parses_query"


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant" | "system"
    content: str


class ChatRequest(BaseModel):
    context: ChatContext
    messages: List[ChatMessage]
    tenant_id: Optional[int] = None  # Required for vmalert_rules context


class ChatResponse(BaseModel):
    response: str
    context: ChatContext


# System prompts per context - STRICTLY limited to prevent injection
SYSTEM_PROMPTS = {
    ChatContext.VMALET_RULES: (
        "Sos un experto en vmalert y Prometheus Alerting Rules. "
        "SOLO podés responder preguntas relacionadas con:\n"
        "- Sintaxis de reglas de alerta vmalert/PromQL\n"
        "- Funciones de agregación (rate, increase, sum, avg, max, min, etc.)\n"
        "- Expresiones de alerta (expr, for, labels, annotations)\n"
        "- Buenas prácticas de alerting (umbrales, duración, severidad)\n"
        "- Métricas comunes de VictoriaMetrics/VictoriaLogs/VictoriaTraces/Pyroscope\n\n"
        "REGLAS ESTRICTAS:\n"
        "1. NO respondas nada que no sea sobre reglas de alerta vmalert/PromQL\n"
        "2. NO generes código que no sea reglas de alerta\n"
        "3. NO expliques conceptos fuera del alerting\n"
        "4. Si la pregunta no es sobre reglas de alerta, respondé: 'Solo puedo ayudarte con reglas de alerta vmalert/PromQL.'\n"
        "5. Tus respuestas deben ser concisas y técnicas\n"
    ),
    ChatContext.VICTORIA_METRICS_QUERY: (
        "Sos un experto en consultas PromQL para VictoriaMetrics. "
        "SOLO podés responder preguntas relacionadas con:\n"
        "- Sintaxis PromQL (selectores, operadores, funciones)\n"
        "- Funciones de agregación y transformación\n"
        "- Consultas de métricas, histogramas, summaries\n"
        "- Dashboards y graficas en VictoriaMetrics\n\n"
        "REGLAS ESTRICTAS:\n"
        "1. NO respondas nada que no sea sobre consultas PromQL/VictoriaMetrics\n"
        "2. NO generes código que no sea PromQL\n"
        "3. Si la pregunta no es sobre consultas VictoriaMetrics, respondé: 'Solo puedo ayudarte con consultas PromQL para VictoriaMetrics.'\n"
    ),
    ChatContext.VICTORIA_LOGS_QUERY: (
        "Sos un experto en consultas LogsQL para VictoriaLogs. "
        "SOLO podés responder preguntas relacionadas con:\n"
        "- Sintaxis LogsQL (filtros, pipes, operadores)\n"
        "- Búsqueda de logs, parsing, extracción de campos\n"
        "- Agregaciones sobre logs (count, stats, facets)\n"
        "- Dashboards de logs en VictoriaLogs\n\n"
        "REGLAS ESTRICTAS:\n"
        "1. NO respondas nada que no sea sobre consultas LogsQL/VictoriaLogs\n"
        "2. NO generes código que no sea LogsQL\n"
        "3. Si la pregunta no es sobre consultas VictoriaLogs, respondé: 'Solo puedo ayudarte con consultas LogsQL para VictoriaLogs.'\n"
    ),
    ChatContext.VICTORIA_TRACES_QUERY: (
        "Sos un experto en consultas de trazas para VictoriaTraces/Jaeger. "
        "SOLO podés responder preguntas relacionadas con:\n"
        "- API de trazas (servicios, operaciones, spans)\n"
        "- Búsqueda de trazas por tags, duración, errores\n"
        "- Análisis de latencia y dependencias\n\n"
        "REGLAS ESTRICTAS:\n"
        "1. NO respondas nada que no sea sobre consultas de trazas VictoriaTraces\n"
        "2. Si la pregunta no es sobre trazas, respondé: 'Solo puedo ayudarte con consultas de trazas VictoriaTraces.'\n"
    ),
    ChatContext.PYROSCOPE_QUERY: (
        "Sos un experto en profiling continuo con Pyroscope. "
        "SOLO podés responder preguntas relacionadas con:\n"
        "- Consultas de perfilado CPU/memoria\n"
        "- Flamegraphs, comparativas, diff\n"
        "- Análisis de cuellos de botella\n\n"
        "REGLAS ESTRICTAS:\n"
        "1. NO respondas nada que no sea sobre profiling Pyroscope\n"
        "3. Si la pregunta no es sobre profiling, respondé: 'Solo puedo ayudarte con profiling Pyroscope.'\n"
    ),
    ChatContext.PARSES_QUERY: (
        "Sos un experto en dashboards y consultas en Parses/Perses. "
        "SOLO podés responder preguntas relacionadas con:\n"
        "- Creación de dashboards en Parses/Perses\n"
        "- Paneles: Time series, Table, Logs, Traces, Flamegraph\n"
        "- Datasources: VictoriaMetrics, VictoriaLogs, VictoriaTraces, Pyroscope\n"
        "- Variables, templating, linking\n\n"
        "REGLAS ESTRICTAS:\n"
        "1. NO respondas nada que no sea sobre dashboards/consultas Parses\n"
        "2. Si la pregunta no es sobre Parses, respondé: 'Solo puedo ayudarte con dashboards y consultas en Parses.'\n"
    ),
}


def _fetch_stack_context_data(context: ChatContext, tenant_id: Optional[int]) -> str:
    """Recupera opcionalmente datos reales del stack según el contexto y el tenant para enriquecer el prompt."""
    if not tenant_id:
        return ""
    
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Obtener información del tenant y sus datasources
        cursor.execute("SELECT name, slug, type, account_id, project_id FROM tenants WHERE id = %s;", (tenant_id,))
        t_row = cursor.fetchone()
        if not t_row:
            return ""
        
        t_name, t_slug, t_type, acc_id, proj_id = t_row
        context_info = f"\n\n[CONTEXTO DE DATOS EN VIVO - Tenant: {t_name} (Slug: {t_slug}, Tipo: {t_type}, AccountID: {acc_id}, ProjectID: {proj_id})]"

        if context == ChatContext.VMALET_RULES:
            cursor.execute("SELECT rules_yaml, status, health FROM tenant_vmalert_instances WHERE tenant_id = %s;", (tenant_id,))
            inst_row = cursor.fetchone()
            if inst_row:
                yaml_content, status, health = inst_row
                context_info += f"\n- Instancia vmalert Status: {status}, Health: {health}"
                if yaml_content:
                    context_info += f"\n- Reglas Actuales YAML:\n```yaml\n{yaml_content[:2000]}\n```"
        
        elif context == ChatContext.VICTORIA_METRICS_QUERY:
            cursor.execute("SELECT read_url FROM tenant_datasources WHERE tenant_id = %s AND type = 'metrics';", (tenant_id,))
            ds_row = cursor.fetchone()
            if ds_row:
                context_info += f"\n- Datasource VictoriaMetrics Read URL: {ds_row[0]}"
                
        elif context == ChatContext.VICTORIA_LOGS_QUERY:
            cursor.execute("SELECT read_url FROM tenant_datasources WHERE tenant_id = %s AND type = 'logs';", (tenant_id,))
            ds_row = cursor.fetchone()
            if ds_row:
                context_info += f"\n- Datasource VictoriaLogs Read URL: {ds_row[0]}"

        return context_info
    except Exception as e:
        logger.warning(f"No se pudo inyectar contexto de stack en IA: {e}")
        return ""
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


def _build_chat_prompt(context: ChatContext, messages: List[ChatMessage], tenant_id: Optional[int] = None) -> str:
    """Build the full prompt with system prompt + live stack context + conversation history."""
    system = SYSTEM_PROMPTS[context]
    live_data = _fetch_stack_context_data(context, tenant_id)
    conversation = "\n".join([f"{m.role.upper()}: {m.content}" for m in messages])
    return f"{system}{live_data}\n\nCONVERSACIÓN:\n{conversation}\n\nASSISTANT:"


class AIConfigPayload(BaseModel):
    provider: str
    zen_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = None
    analysis_language: Optional[str] = None


class AITestPayload(BaseModel):
    provider: Optional[str] = None


@router.get("/providers")
def list_ai_providers(admin_payload: dict = Depends(verify_admin_token)) -> Dict[str, Any]:
    settings = get_ai_settings()
    providers: List[Dict[str, Any]] = []
    for p in AI_PROVIDER_CATALOG:
        if p["group"] == "free":
            configured = bool(settings.get("zen_api_key"))
        elif p["group"] == "apikey":
            configured = bool(settings.get("gemini_api_key"))
        else:
            configured = True
        providers.append({**p, "configured": configured})
    return {"providers": providers}


@router.get("/config")
def get_ai_config(admin_payload: dict = Depends(verify_admin_token)) -> Dict[str, Any]:
    settings = get_ai_settings()
    catalog_ids = {p["id"] for p in AI_PROVIDER_CATALOG}
    stored = settings.get("selected_provider") or DEFAULT_AI_PROVIDER
    # Fallback: si el proveedor guardado ya no existe en el catálogo
    # (p. ej. un modelo free retirado), se expone el proveedor por defecto.
    provider = stored if stored in catalog_ids else DEFAULT_AI_PROVIDER
    return {
        "provider": provider,
        "has_zen_key": bool(settings.get("zen_api_key")),
        "has_gemini_key": bool(settings.get("gemini_api_key")),
        "gemini_model": settings.get("gemini_model") or DEFAULT_GEMINI_MODEL,
        "analysis_language": settings.get("analysis_language") or DEFAULT_ANALYSIS_LANGUAGE,
        "analysis_languages": ANALYSIS_LANGUAGES,
    }


@router.put("/config")
def save_ai_config(payload: AIConfigPayload, admin_payload: dict = Depends(verify_admin_token)) -> Dict[str, str]:
    catalog_ids = {p["id"] for p in AI_PROVIDER_CATALOG}
    if payload.provider not in catalog_ids:
        raise HTTPException(status_code=400, detail="Proveedor de IA desconocido.")
    gemini_model = (payload.gemini_model or "").strip() or DEFAULT_GEMINI_MODEL

    analysis_language = None
    if payload.analysis_language is not None:
        analysis_language = payload.analysis_language.strip().lower()
        if analysis_language not in ANALYSIS_LANGUAGES:
            raise HTTPException(
                status_code=400,
                detail=f"Idioma de análisis no soportado: {payload.analysis_language}",
            )

    updates = ["selected_provider = %s", "gemini_model = %s", "updated_at = CURRENT_TIMESTAMP"]
    params: List[Any] = [payload.provider, gemini_model]
    if analysis_language is not None:
        updates.append("analysis_language = %s")
        params.append(analysis_language)
    if payload.zen_api_key is not None:
        updates.append("zen_api_key = %s")
        params.append(payload.zen_api_key)
    if payload.gemini_api_key is not None:
        updates.append("gemini_api_key = %s")
        params.append(payload.gemini_api_key)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM ai_settings;")
        exists = cursor.fetchone()[0] > 0
        if exists:
            cursor.execute(
                f"UPDATE ai_settings SET {', '.join(updates)} "
                "WHERE id = (SELECT id FROM ai_settings ORDER BY id LIMIT 1)",
                params,
            )
        else:
            cursor.execute(
                "INSERT INTO ai_settings (selected_provider, zen_api_key, gemini_api_key, gemini_model, analysis_language) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    payload.provider,
                    payload.zen_api_key or "",
                    payload.gemini_api_key or "",
                    gemini_model,
                    analysis_language or DEFAULT_ANALYSIS_LANGUAGE,
                ),
            )
        conn.commit()
        return {"status": "success", "message": "Configuración de IA guardada correctamente."}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"No se pudo guardar la configuración: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@router.get("/status", dependencies=[Depends(verify_any_user_token)])
def get_provider_status() -> Dict[str, Any]:
    """Estado global del proveedor IA (para el banner de cuota agotada, spec 018)."""
    return get_ai_status()


@router.post("/test")
def test_ai_provider(payload: AITestPayload, admin_payload: dict = Depends(verify_admin_token)) -> Dict[str, Any]:
    settings = get_ai_settings()
    provider_id = payload.provider or settings.get("selected_provider") or DEFAULT_AI_PROVIDER
    try:
        provider = get_provider(payload.provider)
        response = provider.generate(TEST_PROMPT)
    except AIProviderError as e:
        try:
            record_ai_failure(provider_id, e.kind, str(e))
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "provider": provider_id, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        try:
            record_ai_failure(provider_id, "invalid", str(e))
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "provider": provider_id, "error": str(e)}
    try:
        record_ai_success(provider_id)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "provider": provider_id, "response": response}


@router.get("/gemini-models")
def list_gemini_models(admin_payload: dict = Depends(verify_admin_token)) -> Dict[str, Any]:
    settings = get_ai_settings()
    models = get_gemini_models(settings.get("gemini_api_key", ""))
    current = settings.get("gemini_model") or DEFAULT_GEMINI_MODEL
    if current not in models:
        models.insert(0, current)
    return {"models": models, "current": current}


@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(verify_any_user_token)])
def chat(payload: ChatRequest, user_payload: dict = Depends(verify_any_user_token)) -> ChatResponse:
    """Chat endpoint with strict context restriction.
    
    - vmalert_rules: requires tenant_id, only answers about vmalert/PromQL rules
    - victoria_metrics_query: only PromQL queries for VictoriaMetrics
    - victoria_logs_query: only LogsQL queries for VictoriaLogs
    - victoria_traces_query: only trace queries for VictoriaTraces
    - pyroscope_query: only profiling queries for Pyroscope
    - parses_query: only dashboard/query questions for Parses/Perses
    """
    # Validate context-specific requirements
    if payload.context == ChatContext.VMALET_RULES and not payload.tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id es requerido para contexto vmalert_rules")
    
    # Verify tenant access for vmalert_rules
    if payload.context == ChatContext.VMALET_RULES:
        from .auth import verify_tenant_access
        # This will raise 403 if user doesn't have access to the tenant
        verify_tenant_access(payload.tenant_id)(user_payload)
    
    provider = get_provider()
    prompt = _build_chat_prompt(payload.context, payload.messages, payload.tenant_id)
    
    try:
        response = provider.generate(prompt)
    except AIProviderError as e:
        raise HTTPException(status_code=502, detail=f"Error del proveedor de IA: {str(e)}")
    
    return ChatResponse(response=response, context=payload.context)
