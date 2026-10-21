"""Regresión: análisis IA estructurado de alertas (spec 018).

`build_alert_analysis_prompt` exige un contrato JSON estricto y respeta el
idioma configurado; `parse_alert_analysis` es tolerante a fencs/ruido y nunca
lanza, devolviendo un fallback seguro con severidad por defecto.
"""
from app.ai_providers import (
    ALERT_SEVERITIES,
    AI_ERROR_AUTH,
    AI_ERROR_QUOTA,
    AI_ERROR_RATE_LIMIT,
    AI_ERROR_TRANSIENT,
    DEFAULT_ALERT_SEVERITY,
    AIProviderError,
    build_alert_analysis_prompt,
    classify_ai_error,
    parse_alert_analysis,
)


ALERT = {
    "labels": {"alertname": "HighCPU", "severity": "warning", "instance": "node-1"},
    "annotations": {"summary": "CPU alta", "description": "Uso sostenido de CPU"},
}


def test_prompt_requests_json_and_language():
    prompt = build_alert_analysis_prompt(ALERT, "es")
    assert "JSON" in prompt
    assert "Spanish" in prompt
    # El prompt enumera la escala completa de criticidad admitida.
    for sev in ALERT_SEVERITIES:
        assert sev in prompt
    assert "HighCPU" in prompt


def test_prompt_defaults_to_english():
    prompt = build_alert_analysis_prompt(ALERT)
    assert "English" in prompt


def test_parse_valid_json():
    text = (
        '{"summary": "El disco se llena", "root_cause": "logs sin rotar", '
        '"severity": "high", "checks": ["revisar /var/log", "rotar logs"]}'
    )
    result = parse_alert_analysis(text)
    assert result["summary"] == "El disco se llena"
    assert result["root_cause"] == "logs sin rotar"
    assert result["severity"] == "high"
    assert result["checks"] == ["revisar /var/log", "rotar logs"]


def test_parse_strips_code_fences_and_noise():
    text = '```json\n{"summary": "ok", "severity": "critical", "checks": []}\n```'
    result = parse_alert_analysis(text)
    assert result["summary"] == "ok"
    assert result["severity"] == "critical"


def test_parse_invalid_severity_falls_back():
    text = '{"summary": "ok", "severity": "catastrophic", "checks": []}'
    result = parse_alert_analysis(text)
    assert result["severity"] == DEFAULT_ALERT_SEVERITY


def test_parse_caps_checks_at_three():
    text = '{"summary": "ok", "severity": "low", "checks": ["a", "b", "c", "d", "e"]}'
    result = parse_alert_analysis(text)
    assert result["checks"] == ["a", "b", "c"]


def test_parse_non_json_returns_raw_fallback():
    result = parse_alert_analysis("no soy json")
    assert result["summary"] == "no soy json"
    assert result["severity"] == DEFAULT_ALERT_SEVERITY
    assert result["checks"] == []


def test_parse_empty_text():
    result = parse_alert_analysis("")
    assert result["severity"] == DEFAULT_ALERT_SEVERITY
    assert result["checks"] == []


def test_classify_ai_error_maps_status_codes():
    assert classify_ai_error(401) == AI_ERROR_AUTH
    assert classify_ai_error(403) == AI_ERROR_AUTH
    assert classify_ai_error(429, "rate limit exceeded") == AI_ERROR_RATE_LIMIT
    assert classify_ai_error(429, "RESOURCE_EXHAUSTED: quota") == AI_ERROR_QUOTA
    assert classify_ai_error(500) == AI_ERROR_TRANSIENT
    assert classify_ai_error(503) == AI_ERROR_TRANSIENT
    assert classify_ai_error(None) == AI_ERROR_TRANSIENT


def test_ai_provider_error_retryable_only_for_transient_and_rate_limit():
    assert AIProviderError("x", kind=AI_ERROR_TRANSIENT).retryable is True
    assert AIProviderError("x", kind=AI_ERROR_RATE_LIMIT).retryable is True
    assert AIProviderError("x", kind=AI_ERROR_QUOTA).retryable is False
    assert AIProviderError("x", kind=AI_ERROR_AUTH).retryable is False
    assert AIProviderError("x").retryable is True  # default: transient
