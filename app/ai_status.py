"""Estado global del proveedor de IA (spec 018).

Persiste en ``ai_provider_status`` (singleton, id=1) el resultado de la última
llamada al proveedor. Sirve para dos cosas:

1. Mostrar un **banner global** cuando una IA cloud (Gemini/Zen) agota su cuota
   de tokens o queda degradada (el modelo local Ollama no tiene cuota).
2. Permitir que los administradores reciban **una sola notificación in-app** por
   transición de estado (agotado / recuperado).

Solo se escribe desde aquí; el envío de notificaciones vive en el worker para no
acoplar el módulo de proveedores a la capa de notificaciones.
"""
import logging
from typing import Any, Dict

from .ai_providers import AI_ERROR_QUOTA, AI_STATE_OK, AI_STATE_QUOTA_EXHAUSTED, AI_STATE_DEGRADED
from .database import get_db_connection

logger = logging.getLogger("anomalia.ai")


def _empty_status() -> Dict[str, Any]:
    return {
        "provider": "",
        "state": AI_STATE_OK,
        "kind": "",
        "message": "",
        "consecutive_failures": 0,
        "last_error_at": None,
        "last_success_at": None,
        "updated_at": None,
    }


def get_ai_status() -> Dict[str, Any]:
    """Lee el estado actual del proveedor IA (singleton)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT provider, state, kind, message, consecutive_failures, "
            "last_error_at, last_success_at, updated_at "
            "FROM ai_provider_status WHERE id = 1"
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    if not row:
        return _empty_status()
    return {
        "provider": row[0] or "",
        "state": row[1] or AI_STATE_OK,
        "kind": row[2] or "",
        "message": row[3] or "",
        "consecutive_failures": row[4] or 0,
        "last_error_at": row[5].isoformat() if row[5] else None,
        "last_success_at": row[6].isoformat() if row[6] else None,
        "updated_at": row[7].isoformat() if row[7] else None,
    }


def record_ai_success(provider_id: str) -> bool:
    """Marca el estado como ``ok``. Devuelve True si hubo recuperación."""
    recovered = get_ai_status().get("state") != AI_STATE_OK
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT INTO ai_provider_status
                   (id, provider, state, kind, message, consecutive_failures,
                    last_success_at, updated_at)
               VALUES (1, %s, 'ok', '', '', 0, NOW(), NOW())
               ON CONFLICT (id) DO UPDATE SET
                   provider = EXCLUDED.provider,
                   state = 'ok',
                   kind = '',
                   message = '',
                   consecutive_failures = 0,
                   last_success_at = NOW(),
                   updated_at = NOW()""",
            (provider_id,),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    return recovered


def record_ai_failure(provider_id: str, kind: str, message: str) -> Dict[str, Any]:
    """Registra un fallo. Devuelve el estado nuevo y si la transición es inédita.

    ``transitioned`` es True solo cuando se pasa a un estado distinto del previo,
    de modo que el worker notifica una sola vez (no en cada alerta fallida).
    """
    state = AI_STATE_QUOTA_EXHAUSTED if kind == AI_ERROR_QUOTA else AI_STATE_DEGRADED
    transitioned = get_ai_status().get("state") != state
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT INTO ai_provider_status
                   (id, provider, state, kind, message, consecutive_failures,
                    last_error_at, updated_at)
               VALUES (1, %s, %s, %s, %s, 1, NOW(), NOW())
               ON CONFLICT (id) DO UPDATE SET
                   provider = EXCLUDED.provider,
                   state = EXCLUDED.state,
                   kind = EXCLUDED.kind,
                   message = EXCLUDED.message,
                   consecutive_failures = ai_provider_status.consecutive_failures + 1,
                   last_error_at = NOW(),
                   updated_at = NOW()""",
            (provider_id, state, kind, (message or "")[:500]),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    return {"state": state, "kind": kind, "transitioned": transitioned, "provider": provider_id}
