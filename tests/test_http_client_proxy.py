"""Ruteo del proxy de egress (spec 014 §5).

El proxy solo debe aplicarse al tráfico HTTP/HTTPS *externo* (p. ej. los
proveedores de IA Zen/Gemini). Todo host interno del stack (localhost,
subredes privadas, `.local`, contenedores del stack) debe salir directo para no
romperse. Estos tests no tocan la BD: monkeypatchean `_get_proxy_url`.
"""
import pytest

from app import http_client as hc


@pytest.fixture
def proxy_enabled(monkeypatch):
    monkeypatch.setattr(hc, "_get_proxy_url", lambda: "http://proxy.empresa.com:8080")


@pytest.fixture
def proxy_disabled(monkeypatch):
    monkeypatch.setattr(hc, "_get_proxy_url", lambda: None)


def test_external_url_uses_proxy(proxy_enabled):
    proxies = hc.get_requests_proxies("https://generativelanguage.googleapis.com/v1beta")
    assert proxies == {
        "http": "http://proxy.empresa.com:8080",
        "https": "http://proxy.empresa.com:8080",
    }


def test_zen_url_uses_proxy(proxy_enabled):
    proxies = hc.get_requests_proxies("https://opencode.ai/zen/v1/chat/completions")
    assert proxies is not None
    assert proxies["https"] == "http://proxy.empresa.com:8080"


def test_proxy_disabled_returns_none(proxy_disabled):
    assert hc.get_requests_proxies("https://generativelanguage.googleapis.com") is None


@pytest.mark.parametrize(
    "url",
    [
        "http://anomalia_ollama:11434/api/generate",
        "http://parses:8080/api",
        "http://postgres:5432",
        "http://localhost:8000/healthz",
        "http://127.0.0.1:8000/metrics",
        "http://192.168.1.27:8427/insert/loki/api/v1/push",
        "http://10.0.0.5:9000",
        "http://172.18.0.1:8427",
        "http://anomalia.local:2525",
        "http://anomalia_gw_tls:8443/api/v1/parses/datasources",
    ],
)
def test_internal_urls_bypass_proxy(proxy_enabled, url):
    assert hc.get_requests_proxies(url) is None


def test_is_internal_url_public_host():
    assert hc._is_internal_url("https://example.com/api") is False


def test_no_url_does_not_crash(proxy_enabled):
    # Ollama (antes del fix) llamaba sin URL: con el proxy activo devolvía el
    # proxy y rompía el host interno. El comportamiento genérico sin URL es
    # devolver el proxy (el llamador debe pasar la URL para el bypass interno).
    proxies = hc.get_requests_proxies()
    assert proxies is not None
