from abc import ABC, abstractmethod
import json
import os
import re
import requests
from typing import Any, Dict, List, Optional

from .database import get_db_connection
from .http_client import get_requests_proxies

ZEN_BASE_URL = "https://opencode.ai/zen/v1"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Catálogo de proveedores IA (spec 003 §2).
# Nota: los modelos Free de OpenCode Zen distintos de "space-bunny-free"
# devuelven 403 FreeTierError ("free tier can only be used from within
# OpenCode") cuando se invocan con API key desde fuera de la app
# (verificado 2026-10-02 contra el gateway real). Por eso el grupo
# "free" solo incluye Space Bunny.
AI_PROVIDER_CATALOG: List[Dict[str, Any]] = [
    {"id": "space-bunny-free", "label": "Space Bunny Free", "group": "free", "model": "space-bunny-free", "endpoint": "chat", "requires": "zen"},
    {"id": "anomalia_ollama", "label": "anomalia_ollama (Local)", "group": "local", "model": None, "endpoint": None, "requires": None},
    {"id": "gemini", "label": "Gemini (Google)", "group": "apikey", "model": None, "endpoint": None, "requires": "gemini"},
]

DEFAULT_AI_PROVIDER = "anomalia_ollama"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"

GEMINI_MODEL_FALLBACK: List[str] = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.1-pro",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]

AI_TIMEOUT_SECONDS = 45

# spec 018: reintentos inline acotados ante fallos transitorios del proveedor.
AI_MAX_ATTEMPTS = 3
AI_RETRY_BACKOFF_SECONDS = (1, 4)  # espera entre intentos (len = MAX_ATTEMPTS - 1)

# spec 018: clasificación del error del proveedor. Determina si un fallo es
# transitorio (reintentable) o definitivo, y si debe considerarse agotamiento de
# cuota de tokens (IAs cloud con límite).
AI_ERROR_TRANSIENT = "transient"      # timeout, conexión, 5xx
AI_ERROR_RATE_LIMIT = "rate_limit"    # 429 temporal (reintentable)
AI_ERROR_QUOTA = "quota"              # 429/402 con cuota/tokens agotados
AI_ERROR_AUTH = "auth"                # 401/403 (credenciales)
AI_ERROR_INVALID = "invalid"          # 400 u otros 4xx
AI_RETRYABLE_KINDS = {AI_ERROR_TRANSIENT, AI_ERROR_RATE_LIMIT}

# Vista de estado global del proveedor IA (spec 018).
AI_STATE_OK = "ok"
AI_STATE_DEGRADED = "degraded"
AI_STATE_QUOTA_EXHAUSTED = "quota_exhausted"

# spec 018: escala de criticidad que asigna la IA (editable por alerts_manager).
ALERT_SEVERITIES: List[str] = ["critical", "high", "medium", "low", "info"]
DEFAULT_ALERT_SEVERITY = "info"

# spec 018: idioma del análisis (configurable en el panel de Proveedor IA).
# Mapa código -> nombre para el prompt. Default 'en' (el sitio pasará a inglés).
ANALYSIS_LANGUAGES: Dict[str, str] = {"en": "English", "es": "Spanish"}
DEFAULT_ANALYSIS_LANGUAGE = "en"


class AIProviderError(Exception):
    """Error de comunicación o configuración del proveedor de IA.

    ``kind`` clasifica el fallo (ver ``AI_ERROR_*``); ``retryable`` indica si
    conviene reintentar. ``status_code`` es el HTTP del proveedor, si lo hubo.
    """

    def __init__(
        self,
        message: str,
        kind: str = AI_ERROR_TRANSIENT,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        return self.kind in AI_RETRYABLE_KINDS


def classify_ai_error(status_code: Optional[int], detail: str = "") -> str:
    """Clasifica un error HTTP del proveedor en un ``AI_ERROR_*`` (spec 018)."""
    text = (detail or "").lower()
    if status_code in (401, 403):
        return AI_ERROR_AUTH
    if status_code in (402, 429):
        quota_markers = (
            "quota",
            "resource_exhausted",
            "insufficient_quota",
            "billing",
            "out of credits",
            "no credits",
        )
        if status_code == 402 or any(m in text for m in quota_markers):
            return AI_ERROR_QUOTA
        return AI_ERROR_RATE_LIMIT
    if status_code is not None and status_code >= 500:
        return AI_ERROR_TRANSIENT
    if status_code is not None:
        return AI_ERROR_INVALID
    return AI_ERROR_TRANSIENT



def build_alert_prompt(alert_data: Dict[str, Any]) -> str:
    labels = alert_data.get("labels", {}) or {}
    annotations = alert_data.get("annotations", {}) or {}
    labels_formatted = "\n".join([f"  - {k}: {v}" for k, v in labels.items()])
    annotations_formatted = "\n".join([f"  - {k}: {v}" for k, v in annotations.items()])
    return (
        "Actúa como un SRE y experto en DevOps senior. Analiza la siguiente alerta de infraestructura.\n"
        "Usa las etiquetas y anotaciones de forma interna para ubicar el problema (clúster, nodo, servicio, entorno), "
        "pero **no enumeres las etiquetas**. \n"
        "Escribe un diagnóstico ultra conciso de una sola oración y un listado de máximo 3 pasos cortos para resolverlo.\n\n"
        f"Etiquetas (Labels):\n{labels_formatted}\n\n"
        f"Anotaciones (Annotations):\n{annotations_formatted}"
    )


def build_alert_analysis_prompt(alert_data: Dict[str, Any], language: str = DEFAULT_ANALYSIS_LANGUAGE) -> str:
    """Prompt que exige a la IA una respuesta JSON estricta (spec 018).

    Contrato de salida:
      {
        "summary": str,        # explicación llana, para cualquier persona
        "root_cause": str,     # causa raíz probable en una frase
        "severity": str,       # critical | high | medium | low | info
        "checks": [str, ...]   # hasta 3 comprobaciones para corregirla
      }
    """
    labels = alert_data.get("labels", {}) or {}
    annotations = alert_data.get("annotations", {}) or {}
    labels_formatted = "\n".join([f"  - {k}: {v}" for k, v in labels.items()])
    annotations_formatted = "\n".join([f"  - {k}: {v}" for k, v in annotations.items()])
    lang_name = ANALYSIS_LANGUAGES.get((language or DEFAULT_ANALYSIS_LANGUAGE).lower(), "English")
    severities = ", ".join(ALERT_SEVERITIES)
    return (
        "Actúa como un SRE senior. Analiza la siguiente alerta de infraestructura y "
        "explícala de forma que cualquier persona no técnica pueda entenderla.\n"
        f"Redacta TODO el contenido en {lang_name}.\n\n"
        "Responde ÚNICAMENTE con un objeto JSON válido, sin texto adicional ni "
        "bloques de código, con EXACTAMENTE estas claves:\n"
        "{\n"
        '  "summary": "explicación breve y llana de qué está pasando y por qué importa",\n'
        '  "root_cause": "causa raíz más probable, en una frase",\n'
        f'  "severity": "una de: {severities}",\n'
        '  "checks": ["hasta 3 acciones concretas a revisar para corregirla"]\n'
        "}\n\n"
        "Reglas: 'checks' debe ser una lista de como máximo 3 cadenas cortas y "
        "accionables. Si no estás seguro de la causa, usa la severidad 'info'. "
        "No incluyas las etiquetas en la explicación.\n\n"
        f"Etiquetas (Labels):\n{labels_formatted}\n\n"
        f"Anotaciones (Annotations):\n{annotations_formatted}"
    )


def parse_alert_analysis(text: str) -> Dict[str, Any]:
    """Parsea la respuesta IA al JSON estructurado (spec 018), tolerante a ruido.

    Nunca lanza: ante cualquier fallo devuelve la estructura con el texto crudo
    como summary y severidad por defecto, para no perder la alerta.
    """
    fallback: Dict[str, Any] = {
        "summary": (text or "").strip(),
        "root_cause": "",
        "severity": DEFAULT_ALERT_SEVERITY,
        "checks": [],
    }
    if not text:
        return fallback

    raw = text.strip()
    # Retirar fences ```json ... ``` si el modelo las incluyó.
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()

    data: Optional[Dict[str, Any]] = None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        # Fallback: extraer el primer objeto {...} balanceado del texto.
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except (ValueError, TypeError):
                data = None

    if not isinstance(data, dict):
        return fallback

    severity = str(data.get("severity", "")).strip().lower()
    if severity not in ALERT_SEVERITIES:
        severity = DEFAULT_ALERT_SEVERITY

    checks = data.get("checks") or []
    if isinstance(checks, str):
        checks = [checks]
    if not isinstance(checks, list):
        checks = []
    checks = [str(c).strip() for c in checks if str(c).strip()][:3]

    return {
        "summary": str(data.get("summary") or fallback["summary"]).strip(),
        "root_cause": str(data.get("root_cause") or "").strip(),
        "severity": severity,
        "checks": checks,
    }


class BaseAIProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str) -> str:
        pass

    def analyze_alert(self, alert_data: Dict[str, Any]) -> str:
        try:
            return self.generate(build_alert_prompt(alert_data))
        except AIProviderError as e:
            return str(e)


class OllamaProvider(BaseAIProvider):
    def __init__(self, host: Optional[str] = None, model: Optional[str] = None) -> None:
        self.host = host or os.getenv("OLLAMA_HOST", "http://anomalia_ollama:11434")
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.2:1b")

    def generate(self, prompt: str) -> str:
        try:
            endpoint = f"{self.host}/api/generate"
            response = requests.post(
                endpoint,
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.2},
                },
                timeout=AI_TIMEOUT_SECONDS,
                # Pasa la URL para que el bypass de hosts internos aplique
                # (spec 014 §5): Ollama vive en la red interna del stack y no
                # debe salir por el proxy de egress.
                proxies=get_requests_proxies(endpoint),
            )
            if response.status_code == 200:
                return response.json().get("response", "No se obtuvo respuesta de la IA.")
            raise AIProviderError(
                f"Error en Ollama: Código {response.status_code}",
                kind=classify_ai_error(response.status_code),
                status_code=response.status_code,
            )
        except AIProviderError:
            raise
        except Exception as e:
            raise AIProviderError(f"No se pudo conectar con Ollama local: {str(e)}")


class ZenProvider(BaseAIProvider):
    """Cliente OpenAI-compatible del gateway OpenCode Zen (modelos Free)."""

    def __init__(self, api_key: str, model: str, endpoint: str = "chat") -> None:
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint

    def generate(self, prompt: str) -> str:
        if not self.api_key:
            raise AIProviderError("API Key Zen no configurada. Ingrésala en Proveedor IA.")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self.endpoint == "responses":
                response = requests.post(
                    f"{ZEN_BASE_URL}/responses",
                    headers=headers,
                    json={"model": self.model, "input": prompt, "temperature": 0.2},
                    timeout=AI_TIMEOUT_SECONDS,
                    proxies=get_requests_proxies(f"{ZEN_BASE_URL}/responses"),
                )
                if response.status_code == 200:
                    return self._parse_responses(response.json())
                detail = self._error_detail(response)
                raise AIProviderError(
                    f"Error en OpenCode Zen: Código {response.status_code} - {detail}",
                    kind=classify_ai_error(response.status_code, detail),
                    status_code=response.status_code,
                )
            response = requests.post(
                f"{ZEN_BASE_URL}/chat/completions",
                headers=headers,
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                },
                timeout=AI_TIMEOUT_SECONDS,
                proxies=get_requests_proxies(f"{ZEN_BASE_URL}/chat/completions"),
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "No se obtuvo respuesta de la IA.")
            detail = self._error_detail(response)
            raise AIProviderError(
                f"Error en OpenCode Zen: Código {response.status_code} - {detail}",
                kind=classify_ai_error(response.status_code, detail),
                status_code=response.status_code,
            )
        except AIProviderError:
            raise
        except Exception as e:
            raise AIProviderError(f"No se pudo conectar con OpenCode Zen: {str(e)}")

    @staticmethod
    def _parse_responses(data: Dict[str, Any]) -> str:
        for item in data.get("output", []) or []:
            if item.get("type") != "message":
                continue
            for content in item.get("content", []) or []:
                if content.get("type") == "output_text" and content.get("text"):
                    return content["text"]
        return "No se obtuvo respuesta de la IA."

    @staticmethod
    def _error_detail(response: requests.Response) -> str:
        try:
            return response.json().get("error", {}).get("message", response.text[:200])
        except Exception:
            return response.text[:200]


class GeminiProvider(BaseAIProvider):
    def __init__(self, api_key: str, model: Optional[str] = None) -> None:
        self.api_key = api_key
        self.model = model or DEFAULT_GEMINI_MODEL

    def generate(self, prompt: str) -> str:
        if not self.api_key:
            raise AIProviderError("API Key de Gemini no configurada. Ingrésala en Proveedor IA.")
        try:
            response = requests.post(
                f"{GEMINI_API_BASE}/models/{self.model}:generateContent",
                params={"key": self.api_key},
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2},
                },
                timeout=AI_TIMEOUT_SECONDS,
                proxies=get_requests_proxies(f"{GEMINI_API_BASE}/models/{self.model}:generateContent"),
            )
            if response.status_code == 200:
                data = response.json()
                candidates = data.get("candidates", []) or []
                parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
                text = "".join(p.get("text", "") for p in parts).strip()
                return text or "No se obtuvo respuesta de la IA."
            detail = self._error_detail(response)
            raise AIProviderError(
                f"Error en Gemini: Código {response.status_code} - {detail}",
                kind=classify_ai_error(response.status_code, detail),
                status_code=response.status_code,
            )
        except AIProviderError:
            raise
        except Exception as e:
            raise AIProviderError(f"No se pudo conectar con Gemini: {str(e)}")

    @staticmethod
    def _error_detail(response: requests.Response) -> str:
        try:
            return response.json().get("error", {}).get("message", response.text[:200])
        except Exception:
            return response.text[:200]


def get_gemini_models(api_key: str) -> List[str]:
    """Lista modelos con soporte generateContent desde la API de Google."""
    if not api_key:
        return list(GEMINI_MODEL_FALLBACK)
    try:
        response = requests.get(
            f"{GEMINI_API_BASE}/models",
            params={"key": api_key, "pageSize": 100},
            timeout=15,
            proxies=get_requests_proxies(f"{GEMINI_API_BASE}/models"),
        )
        if response.status_code != 200:
            return list(GEMINI_MODEL_FALLBACK)
        models: List[str] = []
        for m in response.json().get("models", []) or []:
            name = m.get("name", "")
            if name.startswith("models/"):
                name = name[len("models/"):]
            if name and "generateContent" in (m.get("supportedGenerationMethods") or []):
                models.append(name)
        return models or list(GEMINI_MODEL_FALLBACK)
    except Exception:
        return list(GEMINI_MODEL_FALLBACK)


def get_ai_settings() -> Dict[str, Any]:
    """Lee la fila única de ai_settings. Fallback a env si la tabla no existe."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT selected_provider, zen_api_key, gemini_api_key, gemini_model, "
                "analysis_language "
                "FROM ai_settings ORDER BY id LIMIT 1"
            )
            row = cursor.fetchone()
            if row:
                return {
                    "selected_provider": row[0] or DEFAULT_AI_PROVIDER,
                    "zen_api_key": row[1] or "",
                    "gemini_api_key": row[2] or "",
                    "gemini_model": row[3] or DEFAULT_GEMINI_MODEL,
                    "analysis_language": (row[4] or DEFAULT_ANALYSIS_LANGUAGE),
                }
        finally:
            cursor.close()
            conn.close()
    except Exception:
        pass
    return {
        "selected_provider": os.getenv("AI_PROVIDER", DEFAULT_AI_PROVIDER),
        "zen_api_key": os.getenv("OPENCODE_API_KEY", ""),
        "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
        "gemini_model": DEFAULT_GEMINI_MODEL,
        "analysis_language": DEFAULT_ANALYSIS_LANGUAGE,
    }


def get_provider(provider_id: Optional[str] = None) -> BaseAIProvider:
    settings = get_ai_settings()
    pid = (provider_id or settings.get("selected_provider") or DEFAULT_AI_PROVIDER).lower()
    catalog = {p["id"]: p for p in AI_PROVIDER_CATALOG}
    entry = catalog.get(pid)
    if entry is None:
        entry = catalog[DEFAULT_AI_PROVIDER]
    if entry["group"] == "free":
        return ZenProvider(
            api_key=settings.get("zen_api_key", ""),
            model=entry["model"] or "",
            endpoint=entry["endpoint"] or "chat",
        )
    if entry["group"] == "apikey":
        return GeminiProvider(
            api_key=settings.get("gemini_api_key", ""),
            model=settings.get("gemini_model") or DEFAULT_GEMINI_MODEL,
        )
    return OllamaProvider()
