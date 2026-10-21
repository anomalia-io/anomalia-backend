"""Regresión: proveedores de IA y ruteo por proxy (spec 014 §5).

Ollama vive en la red interna del stack: su llamada debe pasar la URL a
`get_requests_proxies` para que el bypass de hosts internos aplique y no salga
por el proxy de egress (bug corregido). Zen y Gemini son externos y sí lo usan.
"""
import app.ai_providers as aip
from app.ai_providers import OllamaProvider, ZenProvider, GeminiProvider


class _FakeResponse:
    status_code = 200

    def json(self):
        return {"response": "ok"}

    text = "ok"


def test_ollama_passes_url_to_proxy_helper(monkeypatch):
    calls = {"args": []}

    def fake_proxies(url=None):
        calls["args"].append(url)
        return None  # host interno: sin proxy

    captured = {}

    def fake_post(endpoint, **kwargs):
        captured["endpoint"] = endpoint
        captured["proxies"] = kwargs.get("proxies")
        return _FakeResponse()

    monkeypatch.setattr(aip, "get_requests_proxies", fake_proxies)
    monkeypatch.setattr(aip.requests, "post", fake_post)

    provider = OllamaProvider(host="http://anomalia_ollama:11434", model="llama3.2:1b")
    assert provider.generate("hola") == "ok"

    # El helper se llamó con la URL concreta (no vacío) -> bypass interno.
    assert calls["args"] == ["http://anomalia_ollama:11434/api/generate"]
    assert captured["endpoint"] == "http://anomalia_ollama:11434/api/generate"
    assert captured["proxies"] is None


def test_zen_passes_external_url(monkeypatch):
    calls = {"args": []}

    def fake_proxies(url=None):
        calls["args"].append(url)
        return {"https": "http://proxy:8080"}

    class _ZenResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(aip, "get_requests_proxies", fake_proxies)
    monkeypatch.setattr(aip.requests, "post", lambda *a, **k: _ZenResponse())

    provider = ZenProvider(api_key="k", model="space-bunny-free")
    assert provider.generate("hola") == "ok"
    assert calls["args"] == [f"{aip.ZEN_BASE_URL}/chat/completions"]


def test_gemini_passes_external_url(monkeypatch):
    calls = {"args": []}

    def fake_proxies(url=None):
        calls["args"].append(url)
        return {"https": "http://proxy:8080"}

    class _GeminiResponse:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    monkeypatch.setattr(aip, "get_requests_proxies", fake_proxies)
    monkeypatch.setattr(aip.requests, "post", lambda *a, **k: _GeminiResponse())

    provider = GeminiProvider(api_key="k", model="gemini-3.5-flash-lite")
    assert provider.generate("hola") == "ok"
    assert calls["args"] == [
        f"{aip.GEMINI_API_BASE}/models/gemini-3.5-flash-lite:generateContent"
    ]
