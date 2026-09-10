"""
Live Strategy DB — SQLAlchemy models and CRUD for live strategy execution.

Three tables:
  - live_strategy_accounts: broker accounts that can run live strategies
  - live_strategies: strategies configured to run live with status tracking
  - live_strategy_logs: audit log for every live strategy action
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

# All wall-clock timestamps in this module are stored as IST (the app is
# IST-centric): SQLite's func.now() would store UTC and the frontend would
# render it verbatim, showing times 5.5 hours behind.
IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import func

from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

# Conditionally create engine based on DB type
if DATABASE_URL and "sqlite" in DATABASE_URL:
    engine = create_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(DATABASE_URL, pool_size=50, max_overflow=100, pool_timeout=10)

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


# ---------------------------------------------------------------------------
# Valid status values
# ---------------------------------------------------------------------------

VALID_STATUSES = ("PAUSED", "RUNNING", "STOPPED", "ERROR")

VALID_LOG_ACTIONS = (
    "STARTED",
    "PAUSED",
    "RESUMED",
    "STOPPED",
    "ORDER_PLACED",
    "ORDER_FILLED",
    "RISK_TRIGGERED",
    "ERROR",
    "EVAL",  # per-poll evaluation heartbeat: price seen, indicator state, verdict
)


def prune_eval_logs(strategy_id: int, keep: int = 500) -> None:
    """Keep only the newest ``keep`` EVAL entries per strategy.

    EVAL fires every poll (e.g. every 60s), so without pruning the audit
    table would grow ~375 rows/day/strategy forever.
    """
    try:
        cutoff_ids = [
            row.id
            for row in LiveStrategyLog.query.filter_by(strategy_id=strategy_id, action="EVAL")
            .order_by(LiveStrategyLog.timestamp.desc())
            .offset(keep)
            .all()
        ]
        if cutoff_ids:
            LiveStrategyLog.query.filter(LiveStrategyLog.id.in_(cutoff_ids)).delete(
                synchronize_session=False
            )
            db_session.commit()
    except Exception:
        db_session.rollback()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class LiveStrategyAccount(Base):
    """A broker account that can run live strategies."""

    __tablename__ = "live_strategy_accounts"

    id = Column(Integer, primary_key=True)
    account_name = Column(String(255), nullable=False)
    broker = Column(String(50), nullable=False)  # e.g. "angel"
    client_code = Column(String(100), nullable=False)
    api_key_ref = Column(String(255), nullable=True)  # OpenAlgo API key ID
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=_now_ist)
    last_login_at = Column(DateTime(timezone=True), nullable=True)


class LiveStrategy(Base):
    """A strategy configured to run live."""

    __tablename__ = "live_strategies"

    id = Column(Integer, primary_key=True)
    strategy_key = Column(String(100), nullable=False)  # from STRATEGY_REGISTRY
    account_id = Column(
        Integer, ForeignKey("live_strategy_accounts.id"), nullable=False
    )
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(20), nullable=False)
    interval = Column(String(10), nullable=False)  # e.g. "1m", "5m", "15m"
    capital_allocated = Column(Float, default=0.0)
    lots = Column(Integer, default=1)
    status = Column(String(20), default="PAUSED")  # PAUSED|RUNNING|STOPPED|ERROR
    started_at = Column(DateTime(timezone=True), nullable=True)
    paused_at = Column(DateTime(timezone=True), nullable=True)
    stopped_at = Column(DateTime(timezone=True), nullable=True)
    last_signal_at = Column(DateTime(timezone=True), nullable=True)
    total_pnl = Column(Float, default=0.0)
    total_trades = Column(Integer, default=0)
    total_fees = Column(Float, default=0.0)
    risk_config_json = Column(Text, nullable=True)  # JSON string of risk overrides
    remarks = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now_ist)


class LiveStrategyLog(Base):
    """Audit log for live strategy actions."""

    __tablename__ = "live_strategy_logs"

    id = Column(Integer, primary_key=True)
    strategy_id = Column(
        Integer, ForeignKey("live_strategies.id"), nullable=False
    )
    timestamp = Column(DateTime(timezone=True), default=_now_ist)
    action = Column(String(30), nullable=False)  # STARTED|PAUSED|RESUMED|...
    details_json = Column(Text, nullable=True)
    pnl_at_action = Column(Float, default=0.0)


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


def init_db():
    """Initialize the live strategy database tables."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Live Strategy DB", logger)


# ---------------------------------------------------------------------------
# Account CRUD
# ---------------------------------------------------------------------------


def create_account(
    name: str,
    broker: str,
    client_code: str,
    api_key_ref: str | None = None,
) -> int | None:
    """Create a new broker account. Returns the new account ID."""
    try:
        account = LiveStrategyAccount(
            account_name=name,
            broker=broker,
            client_code=client_code,
            api_key_ref=api_key_ref,
            is_active=True,
        )
        db_session.add(account)
        db_session.commit()
        logger.info(f"Created live strategy account: {name} ({broker})")
        return account.id
    except Exception as e:
        logger.exception(f"Error creating account: {e}")
        db_session.rollback()
        return None


def list_accounts() -> list[dict[str, Any]]:
    """Return all broker accounts as dicts."""
    try:
        accounts = LiveStrategyAccount.query.all()
        return [
            {
                "id": a.id,
                "account_name": a.account_name,
                "broker": a.broker,
                "client_code": a.client_code,
                "api_key_ref": a.api_key_ref,
                "is_active": a.is_active,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "last_login_at": a.last_login_at.isoformat() if a.last_login_at else None,
            }
            for a in accounts
        ]
    except Exception as e:
        logger.exception(f"Error listing accounts: {e}")
        return []


def get_account(account_id: int) -> dict[str, Any] | None:
    """Return a single account by ID."""
    try:
        a = LiveStrategyAccount.query.get(account_id)
        if not a:
            return None
        return {
            "id": a.id,
            "account_name": a.account_name,
            "broker": a.broker,
            "client_code": a.client_code,
            "api_key_ref": a.api_key_ref,
            "is_active": a.is_active,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "last_login_at": a.last_login_at.isoformat() if a.last_login_at else None,
        }
    except Exception as e:
        logger.exception(f"Error getting account {account_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Live Strategy CRUD
# ---------------------------------------------------------------------------


def create_live_strategy(
    strategy_key: str,
    account_id: int,
    symbol: str,
    exchange: str,
    interval: str = "5m",
    capital_allocated: float = 0.0,
    lots: int = 1,
    risk_config_json: str | None = None,
    remarks: str | None = None,
) -> int | None:
    """Create a new live strategy. Returns the strategy ID."""
    try:
        strat = LiveStrategy(
            strategy_key=strategy_key,
            account_id=account_id,
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            capital_allocated=capital_allocated,
            lots=lots,
            status="PAUSED",
            risk_config_json=risk_config_json,
            remarks=remarks,
        )
        db_session.add(strat)
        db_session.commit()
        logger.info(
            f"Created live strategy id={strat.id}: {strategy_key} on {symbol} ({exchange})"
        )
        return strat.id
    except Exception as e:
        logger.exception(f"Error creating live strategy: {e}")
        db_session.rollback()
        return None


def update_strategy_status(strategy_id: int, status: str) -> bool:
    """Update the status of a live strategy."""
    if status not in VALID_STATUSES:
        logger.error(f"Invalid status: {status}")
        return False
    try:
        strat = LiveStrategy.query.get(strategy_id)
        if not strat:
            return False
        strat.status = status
        now = _now_ist()
        if status == "RUNNING":
            strat.started_at = now
        elif status == "PAUSED":
            strat.paused_at = now
        elif status == "STOPPED":
            strat.stopped_at = now
        db_session.commit()
        return True
    except Exception as e:
        logger.exception(f"Error updating strategy status {strategy_id}: {e}")
        db_session.rollback()
        return False


def update_strategy_pnl(
    strategy_id: int,
    pnl_delta: float = 0.0,
    trades_delta: int = 0,
    fees_delta: float = 0.0,
) -> bool:
    """Increment PnL / trade / fee counters for a strategy."""
    try:
        strat = LiveStrategy.query.get(strategy_id)
        if not strat:
            return False
        strat.total_pnl = (strat.total_pnl or 0.0) + pnl_delta
        strat.total_trades = (strat.total_trades or 0) + trades_delta
        strat.total_fees = (strat.total_fees or 0.0) + fees_delta
        db_session.commit()
        return True
    except Exception as e:
        logger.exception(f"Error updating strategy PnL {strategy_id}: {e}")
        db_session.rollback()
        return False


def update_last_signal(strategy_id: int) -> bool:
    """Touch the last_signal_at timestamp."""
    try:
        strat = LiveStrategy.query.get(strategy_id)
        if not strat:
            return False
        strat.last_signal_at = _now_ist()
        db_session.commit()
        return True
    except Exception as e:
        logger.exception(f"Error updating last signal {strategy_id}: {e}")
        db_session.rollback()
        return False


def list_live_strategies(account_id: int | None = None) -> list[dict[str, Any]]:
    """List all live strategies, optionally filtered by account."""
    try:
        query = LiveStrategy.query
        if account_id is not None:
            query = query.filter_by(account_id=account_id)
        strategies = query.all()
        return [_strategy_to_dict(s) for s in strategies]
    except Exception as e:
        logger.exception(f"Error listing live strategies: {e}")
        return []


def get_live_strategy(strategy_id: int) -> dict[str, Any] | None:
    """Return a single live strategy by ID."""
    try:
        s = LiveStrategy.query.get(strategy_id)
        if not s:
            return None
        return _strategy_to_dict(s)
    except Exception as e:
        logger.exception(f"Error getting live strategy {strategy_id}: {e}")
        return None


def _strategy_to_dict(s: LiveStrategy) -> dict[str, Any]:
    """Convert a LiveStrategy row to a JSON-safe dict."""
    risk_config = None
    if s.risk_config_json:
        try:
            risk_config = json.loads(s.risk_config_json)
        except (json.JSONDecodeError, TypeError):
            risk_config = s.risk_config_json
    return {
        "id": s.id,
        "strategy_key": s.strategy_key,
        "account_id": s.account_id,
        "symbol": s.symbol,
        "exchange": s.exchange,
        "interval": s.interval,
        "capital_allocated": s.capital_allocated,
        "lots": s.lots,
        "status": s.status,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "paused_at": s.paused_at.isoformat() if s.paused_at else None,
        "stopped_at": s.stopped_at.isoformat() if s.stopped_at else None,
        "last_signal_at": s.last_signal_at.isoformat() if s.last_signal_at else None,
        "total_pnl": s.total_pnl or 0.0,
        "total_trades": s.total_trades or 0,
        "total_fees": s.total_fees or 0.0,
        "risk_config": risk_config,
        "remarks": s.remarks,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


# ---------------------------------------------------------------------------
# Audit Log
# ---------------------------------------------------------------------------


def log_action(
    strategy_id: int,
    action: str,
    details: dict[str, Any] | str | None = None,
    pnl: float = 0.0,
) -> bool:
    """Write an audit log entry for a live strategy action."""
    if action not in VALID_LOG_ACTIONS:
        logger.warning(f"Unknown log action '{action}' for strategy {strategy_id}")
    try:
        details_str = None
        if details is not None:
            details_str = json.dumps(details) if isinstance(details, dict) else str(details)
        entry = LiveStrategyLog(
            strategy_id=strategy_id,
            action=action,
            details_json=details_str,
            pnl_at_action=pnl,
        )
        db_session.add(entry)
        db_session.commit()
        return True
    except Exception as e:
        logger.exception(f"Error logging action for strategy {strategy_id}: {e}")
        db_session.rollback()
        return False


def get_strategy_logs(
    strategy_id: int, limit: int = 100
) -> list[dict[str, Any]]:
    """Return recent audit logs for a live strategy."""
    try:
        logs = (
            LiveStrategyLog.query.filter_by(strategy_id=strategy_id)
            .order_by(LiveStrategyLog.timestamp.desc())
            .limit(limit)
            .all()
        )
        result = []
        for log_entry in logs:
            details = None
            if log_entry.details_json:
                try:
                    details = json.loads(log_entry.details_json)
                except (json.JSONDecodeError, TypeError):
                    details = log_entry.details_json
            result.append(
                {
                    "id": log_entry.id,
                    "strategy_id": log_entry.strategy_id,
                    "timestamp": log_entry.timestamp.isoformat() if log_entry.timestamp else None,
                    "action": log_entry.action,
                    "details": details,
                    "pnl_at_action": log_entry.pnl_at_action or 0.0,
                }
            )
        return result
    except Exception as e:
        logger.exception(f"Error getting logs for strategy {strategy_id}: {e}")
        return []
