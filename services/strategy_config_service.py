"""Strategy configuration service — active/inactive status + auto-run.

Manages which strategies are active, what instruments they run on,
and triggers backtest runs automatically when a strategy is activated.

Uses the same SQLAlchemy engine/session as backtest_db for consistency.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column, Integer, String, Float, Text, DateTime, func
from database.backtest_db import Base, db_session, engine

from utils.logging import get_logger

logger = get_logger(__name__)


class StrategyConfig(Base):
    __tablename__ = "strategy_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_key = Column(String(100), unique=True, nullable=False)
    is_active = Column(Integer, default=0)
    default_instruments = Column(Text, default="")
    default_interval = Column(String(10), default="15m")
    default_capital = Column(Float, default=500000)
    default_cost = Column(String(20), default="zerodha")
    remarks = Column(Text, default="")
    created_at = Column(DateTime, server_default=func.now(), index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


Base.metadata.create_all(bind=engine)


def _ensure_created_at_column() -> None:
    """Add created_at to an existing strategy_config table and backfill it.

    The column was added after the table already existed, so create_all won't
    add it to old DBs. Backfill each strategy's created_at from its EARLIEST
    backtest run (the genuine "first seen in the system" time); rows with no
    run fall back to updated_at, then now.
    """
    from sqlalchemy import inspect, text

    try:
        cols = {c["name"] for c in inspect(engine).get_columns("strategy_config")}
        if "created_at" in cols:
            return
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE strategy_config ADD COLUMN created_at DATETIME"))
            # Backfill from earliest backtest run per strategy (joined by name).
            conn.execute(text(
                """
                UPDATE strategy_config
                SET created_at = (
                    SELECT MIN(br.created_at) FROM backtest_runs br
                    WHERE br.strategy = strategy_config.strategy_key
                )
                WHERE created_at IS NULL
                """
            ))
            # Anything still null (never backtested) → updated_at, then now.
            conn.execute(text(
                "UPDATE strategy_config SET created_at = COALESCE(updated_at, CURRENT_TIMESTAMP) "
                "WHERE created_at IS NULL"
            ))
        logger.info("Added and backfilled strategy_config.created_at")
    except Exception:
        logger.exception("Could not add/backfill strategy_config.created_at")


_ensure_created_at_column()


def _sync_missing_configs() -> None:
    """Ensure every registered strategy has a config row so it carries a real
    created_at (new strategies added in code get their true add date instead of
    showing blank in the listing)."""
    try:
        from services.backtest_service import list_strategies

        from datetime import datetime

        from sqlalchemy import text

        existing = {r[0] for r in db_session.query(StrategyConfig.strategy_key).all()}
        missing = [s["key"] for s in list_strategies() if s["key"] not in existing]
        now = datetime.utcnow()
        for key in missing:
            # Set created_at explicitly: the ALTER-added column has no DB-level
            # default, so a bare insert would leave it NULL.
            db_session.add(StrategyConfig(strategy_key=key, is_active=0, created_at=now))
        if missing:
            db_session.commit()
            logger.info("Created %d missing strategy_config rows", len(missing))

        # Repair any rows left with NULL created_at (e.g. inserted before this
        # fix): earliest backtest run first, then "now" for the truly new ones.
        with engine.begin() as conn:
            conn.execute(text(
                """
                UPDATE strategy_config
                SET created_at = (
                    SELECT MIN(br.created_at) FROM backtest_runs br
                    WHERE br.strategy = strategy_config.strategy_key
                )
                WHERE created_at IS NULL
                """
            ))
            conn.execute(text(
                "UPDATE strategy_config SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
            ))
    except Exception:
        db_session.rollback()
        logger.exception("Could not sync missing strategy configs")
    finally:
        db_session.remove()


_sync_missing_configs()

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
    try:
        rows = db_session.query(StrategyConfig).order_by(
            StrategyConfig.is_active.desc(), StrategyConfig.strategy_key
        ).all()

        result = []
        seen: set[str] = set()
        for row in rows:
            seen.add(row.strategy_key)
            s = strategies.get(row.strategy_key, {})
            result.append({
                "key": row.strategy_key,
                "name": s.get("name", row.strategy_key),
                "description": s.get("description", ""),
                "source": s.get("source", ""),
                "is_active": bool(row.is_active),
                "default_instruments": row.default_instruments.split(",") if row.default_instruments else [],
                "default_interval": row.default_interval or "15m",
                "default_capital": row.default_capital or 500000,
                "default_cost": row.default_cost or "zerodha",
                "remarks": row.remarks or "",
                "created_at": row.created_at.isoformat() if row.created_at else None,
            })

        for key, s in strategies.items():
            if key not in seen:
                result.append({
                    "key": key, "name": s["name"], "description": s["description"],
                    "source": s.get("source", ""), "is_active": False,
                    "default_instruments": [], "default_interval": "15m",
                    "default_capital": 500000, "default_cost": "zerodha", "remarks": "",
                    "created_at": None,
                })

        return result
    finally:
        db_session.remove()


def set_strategy_active(key: str, active: bool, instruments: list[str] | None = None,
                        interval: str = "15m", capital: float = 500000, cost: str = "zerodha") -> dict[str, Any]:
    """Activate or deactivate a strategy. If activating, auto-run backtests."""
    inst_str = ",".join(instruments or ALL_INSTRUMENTS)
    try:
        existing = db_session.query(StrategyConfig).filter_by(strategy_key=key).first()
        if existing:
            existing.is_active = int(active)
            existing.default_instruments = inst_str
            existing.default_interval = interval
            existing.default_capital = capital
            existing.default_cost = cost
        else:
            row = StrategyConfig(
                strategy_key=key,
                is_active=int(active),
                default_instruments=inst_str,
                default_interval=interval,
                default_capital=capital,
                default_cost=cost,
            )
            db_session.add(row)
        db_session.commit()
    finally:
        db_session.remove()

    result: dict[str, Any] = {"status": "success", "key": key, "is_active": active}

    if active:
        # Run the backtest in the BACKGROUND. On real 1m option data a run takes
        # ~40s, which blocked the activate HTTP request past its timeout — the
        # strategy activated in the DB (committed above) but the UI reported a
        # failure and never refreshed. Returning immediately fixes that; the
        # backtest result appears in the list a few seconds later.
        import threading

        threading.Thread(
            target=auto_run_strategy,
            args=(key, instruments or ALL_INSTRUMENTS, interval, capital, cost),
            daemon=True,
            name=f"autorun-{key}",
        ).start()
        result["auto_run"] = {"status": "started", "message": "backtest running in background"}

    return result


def auto_run_strategy(key: str, instruments: list[str], interval: str = "15m",
                      capital: float = 500000, cost: str = "zerodha") -> dict[str, Any]:
    """Auto-run a strategy and persist the result.

    Options strategies run on REAL collected CE/PE premium contracts at 1m over
    the available real-data window (never Black-Scholes premium simulated on the
    index future). Futures strategies run on the real index futures. This keeps
    the backtest — and the trade list — on genuine instruments.
    """
    from services.backtest_service import real_option_contracts, run_multi_instrument_backtest

    if "option" in key:
        symbols, start, end = real_option_contracts()
        if symbols:
            return run_multi_instrument_backtest(
                symbols=symbols, exchange="NFO", interval="1m",
                start=start, end=end, capital=capital, cost=cost,
                strategy_key=key, source="db",
            )
        logger.warning("No real option contracts available for %s; skipping", key)
        return {"status": "error", "message": "no real option premium data available"}

    return run_multi_instrument_backtest(
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


def auto_run_all_active() -> list[dict[str, Any]]:
    """Auto-run all active strategies."""
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
