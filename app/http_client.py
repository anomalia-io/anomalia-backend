from typing import Optional
from urllib.parse import urlparse

import httpx
import requests

from .database import get_db_connection

_PRIVATE_SUBNETS = (
    ("10.",),
    ("192.168.",),
    ("127.",),
    ("172.",),
)


def _is_internal_url(url: str) -> bool:
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if not host:
        return True
    if host in ("localhost", "parses", "postgres", "anomalia_ollama", "anomalia_gw", "anomalia_gw_tls"):
        return True
    if host.endswith(".local"):
        return True
    if host.startswith(_PRIVATE_SUBNETS[0]):
        return True
    if host.startswith(_PRIVATE_SUBNETS[1]):
        return True
    if host.startswith(_PRIVATE_SUBNETS[2]):
        return True
    # 172.16.0.0 - 172.31.255.255
    if host.startswith(_PRIVATE_SUBNETS[3]):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (IndexError, ValueError):
            return True
    return False


def _get_proxy_url() -> Optional[str]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT proxy_url, proxy_enabled
            FROM system_settings ORDER BY id LIMIT 1;
            """
        )
        row = cursor.fetchone()
        if not row:
            return None
        proxy_url, proxy_enabled = row[0], row[1]
        if proxy_enabled and proxy_url:
            return proxy_url
        return None
    finally:
        cursor.close()
        conn.close()


def get_http_client(**kwargs) -> httpx.AsyncClient:
    """Cliente httpx async que sale por el proxy de egress si está configurado.

    Helper para futuras llamadas httpx *externas* (spec 014 §5): hoy el único
    egress externo del backend son los proveedores de IA, que usan `requests` y
    `get_requests_proxies`. Para tráfico a servicios internos del stack usar
    httpx directo (sin proxy).
    """
    proxy_url = _get_proxy_url()
    if proxy_url:
        kwargs.setdefault("proxy", proxy_url)
    return httpx.AsyncClient(**kwargs)


def get_requests_proxies(url: Optional[str] = None) -> Optional[dict]:
    proxy_url = _get_proxy_url()
    if not proxy_url:
        return None
    if url and _is_internal_url(url):
        return None
    return {"http": proxy_url, "https": proxy_url}