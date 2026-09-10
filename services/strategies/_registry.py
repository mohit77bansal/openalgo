"""Strategy registry — each strategy has a name, description, source, and factory."""

from __future__ import annotations

from typing import Any

STRATEGY_REGISTRY: dict[str, dict[str, Any]] = {}


def _register(key: str, name: str, description: str, factory, source: str = ""):
    STRATEGY_REGISTRY[key] = {"name": name, "description": description, "source": source, "factory": factory}


def list_strategies() -> list[dict[str, str]]:
    """Return all registered strategies with their descriptions and sources."""
    return [
        {"key": k, "name": v["name"], "description": v["description"], "source": v.get("source", "")}
        for k, v in STRATEGY_REGISTRY.items()
    ]


def _make_strategy(symbol: str, strategy_key: str = "atr_channel_breakout", **kwargs) -> Any:
    entry = STRATEGY_REGISTRY.get(strategy_key)
    if not entry:
        entry = STRATEGY_REGISTRY["atr_channel_breakout"]
    return entry["factory"](symbol, **kwargs)
