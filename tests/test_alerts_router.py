"""Unit tests for alerts_router search helpers (spec 018).

Estos tests no requieren BD: validan el escape de comodines del patrón ILIKE y la
forma de la condición de búsqueda (ESCAPE por operando, no al final).
"""
from app.routers.alerts_router import (
    SEARCH_COLUMNS,
    _ilike_contains,
    _text_search_condition,
)


def test_ilike_contains_wraps_with_wildcards():
    assert _ilike_contains("cpu") == "%cpu%"


def test_ilike_contains_trims_whitespace():
    assert _ilike_contains("  alta  ") == "%alta%"


def test_ilike_contains_escapes_user_percent():
    assert _ilike_contains("100%") == r"%100\%%"


def test_ilike_contains_escapes_user_underscore():
    assert _ilike_contains("foo_bar") == r"%foo\_bar%"


def test_ilike_contains_escapes_backslash():
    assert _ilike_contains("a\\b") == r"%a\\b%"


def test_text_search_condition_escapes_each_operand():
    condition = _text_search_condition()
    # Cada columna tiene su propio ESCAPE: un único ESCAPE al final de la
    # expresión agrupada con OR es sintaxis inválida en PostgreSQL.
    assert condition.count("ESCAPE '\\'") == len(SEARCH_COLUMNS)
    for col in SEARCH_COLUMNS:
        assert f"{col} ILIKE %s ESCAPE '\\'" in condition


def test_text_search_condition_does_not_end_with_escape():
    assert not _text_search_condition().endswith("ESCAPE '\\'")
    assert _text_search_condition().endswith(")")

