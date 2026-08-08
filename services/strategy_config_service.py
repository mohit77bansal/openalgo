"""Strategy configuration service — active/inactive status + auto-run.

Manages which strategies are active, what instruments they run on,
and triggers backtest runs automatically when a strategy is activated.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

_DB_PATH = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db").replace("sqlite:///", "")


def _conn():
    return sqlite3.connect(_DB_PATH)


def _ensure_table():
    con = _conn()
    con.execute("""CREATE TABLE IF NOT EXISTS strategy_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_key TEXT UNIQUE NOT NULL,
        is_active INTEGER DEFAULT 0,
        default_instruments TEXT DEFAULT '',
        default_interval TEXT DEFAULT '15m',
        default_capital REAL DEFAULT 500000,
        default_cost TEXT DEFAULT 'zerodha',
        remarks TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    con.commit()
    con.close()


_ensure_table()

ALL_INSTRUMENTS = [
    "NIFTY25AUG26FUT",
    "BANKNIFTY25AUG26FUT",
    "FINNIFTY25AUG26FUT",
    "MIDCPNIFTY25AUG26FUT",
]


def list_strategy_configs() -> list[dict[str, Any]]:
    """List all strategies with their active/inactive config."""
    from services.backtest_service import list_strategies

    strategies = {s["key"]: s for s in list_strategies()}
    con = _conn()
    rows = con.execute("SELECT strategy_key, is_active, default_instruments, default_interval, default_capital, default_cost, remarks FROM strategy_config ORDER BY is_active DESC, strategy_key").fetchall()
    con.close()

    result = []
    seen = set()
    for key, active, instruments, interval, capital, cost, remarks in rows:
        seen.add(key)
        s = strategies.get(key, {})
        result.append({
            "key": key,
            "name": s.get("name", key),
            "description": s.get("description", ""),
            "source": s.get("source", ""),
            "is_active": bool(active),
            "default_instruments": instruments.split(",") if instruments else [],
            "default_interval": interval or "15m",
            "default_capital": capital or 500000,
            "default_cost": cost or "zerodha",
            "remarks": remarks or "",
        })

    for key, s in strategies.items():
        if key not in seen:
            result.append({
                "key": key, "name": s["name"], "description": s["description"],
                "source": s.get("source", ""), "is_active": False,
                "default_instruments": [], "default_interval": "15m",
                "default_capital": 500000, "default_cost": "zerodha", "remarks": "",
            })

    return result


def set_strategy_active(key: str, active: bool, instruments: list[str] | None = None,
                        interval: str = "15m", capital: float = 500000, cost: str = "zerodha") -> dict[str, Any]:
    """Activate or deactivate a strategy. If activating, auto-run backtests."""
    con = _conn()
    inst_str = ",".join(instruments or ALL_INSTRUMENTS)
    con.execute("""INSERT INTO strategy_config (strategy_key, is_active, default_instruments, default_interval, default_capital, default_cost, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(strategy_key) DO UPDATE SET is_active=?, default_instruments=?, default_interval=?, default_capital=?, default_cost=?, updated_at=CURRENT_TIMESTAMP""",
                (key, int(active), inst_str, interval, capital, cost,
                 int(active), inst_str, interval, capital, cost))
    con.commit()
    con.close()

    result = {"status": "success", "key": key, "is_active": active}

    if active:
        result["auto_run"] = auto_run_strategy(key, instruments or ALL_INSTRUMENTS, interval, capital, cost)

    return result


def auto_run_strategy(key: str, instruments: list[str], interval: str = "15m",
                      capital: float = 500000, cost: str = "zerodha") -> dict[str, Any]:
    """Auto-run a strategy on all specified instruments. Returns the multi-instrument result."""
    from services.backtest_service import run_multi_instrument_backtest

    result = run_multi_instrument_backtest(
        symbols=instruments,
        exchange="NFO",
        interval=interval,
        start="2026-02-01",
        end="2026-08-07",
        capital=capital,
        cost=cost,
        strategy_key=key,
        source="db",
    )
    return result


def auto_run_all_active() -> list[dict[str, Any]]:
    """Auto-run all active strategies. Called on startup or on demand."""
    configs = list_strategy_configs()
    active = [c for c in configs if c["is_active"]]
    results = []
    for c in active:
        logger.info("auto-running active strategy: %s on %s", c["key"], c["default_instruments"])
        r = auto_run_strategy(
            c["key"],
            c["default_instruments"] or ALL_INSTRUMENTS,
            c["default_interval"],
            c["default_capital"],
            c["default_cost"],
        )
        results.append({"key": c["key"], "status": r.get("status"), "pnl": r.get("portfolio_metrics", {}).get("total_pnl")})
    return results
