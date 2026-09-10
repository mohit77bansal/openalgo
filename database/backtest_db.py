"""Backtest run history — persists every backtest result for the list view."""

from __future__ import annotations

import json
import os
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, defer, scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and "sqlite" in DATABASE_URL:
    engine = create_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(DATABASE_URL, pool_size=5, max_overflow=10)

Base = declarative_base()
db_session = scoped_session(sessionmaker(bind=engine))


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    strategy = Column(String(100), nullable=False)
    strategy_description = Column(Text)
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(20), nullable=False)
    interval = Column(String(10), nullable=False, default="D")
    source = Column(String(10), nullable=False, default="demo")
    start_date = Column(String(20))
    end_date = Column(String(20))
    capital = Column(Float, nullable=False, default=1000000.0)
    cost_model = Column(String(20), nullable=False, default="zerodha")
    n_bars = Column(Integer, default=0)
    n_trades = Column(Integer, default=0)
    net_pnl = Column(Float, default=0.0)
    fees_total = Column(Float, default=0.0)
    sharpe = Column(Float)
    max_drawdown_pct = Column(Float)
    xirr_pct = Column(Float)
    margin_used = Column(Float)
    return_on_margin_pct = Column(Float)
    is_favorite = Column(Integer, default=0)
    remarks = Column(Text)
    strategy_source = Column(Text)
    status = Column(String(10), nullable=False, default="success")
    error_message = Column(Text)
    metrics_json = Column(Text)
    equity_json = Column(Text)
    trades_json = Column(Text)
    ohlc_json = Column(Text)
    per_instrument_json = Column(Text)


def init_db():
    Base.metadata.create_all(bind=engine)


def save_backtest_run(result: dict) -> int | None:
    """Persist a backtest result dict and return the row id."""
    try:
        metrics = result.get("metrics", {})
        run = BacktestRun(
            strategy=result.get("strategy", "unknown"),
            strategy_description=result.get("strategy_description"),
            symbol=result.get("symbol", ""),
            exchange=result.get("exchange", ""),
            interval=result.get("interval", "D"),
            source=result.get("source", "demo"),
            start_date=result.get("start"),
            end_date=result.get("end"),
            capital=result.get("capital", 1000000),
            cost_model=result.get("cost_model", "zerodha"),
            n_bars=result.get("n_bars", 0),
            n_trades=metrics.get("n_trades", 0),
            net_pnl=metrics.get("net_pnl", 0),
            fees_total=metrics.get("fees_total", 0),
            sharpe=metrics.get("sharpe"),
            max_drawdown_pct=metrics.get("max_drawdown_pct"),
            xirr_pct=result.get("xirr_pct"),
            margin_used=result.get("margin_used"),
            return_on_margin_pct=result.get("return_on_margin_pct"),
            strategy_source=result.get("strategy_source"),
            status=result.get("status", "error"),
            error_message=result.get("message"),
            metrics_json=json.dumps(metrics) if metrics else None,
            equity_json=json.dumps(result.get("equity", [])),
            trades_json=json.dumps(result.get("trades", [])),
            ohlc_json=json.dumps(result.get("ohlc", [])),
            per_instrument_json=json.dumps(result.get("per_instrument", [])),
        )
        db_session.add(run)
        db_session.commit()
        return run.id
    except Exception:
        db_session.rollback()
        return None
    finally:
        db_session.remove()


def list_backtest_runs(limit: int = 50) -> list[dict]:
    """Return recent backtest runs, newest first."""
    try:
        # Defer the large JSON blob columns — this listing never returns them,
        # so there's no reason to pull equity/trades/ohlc/etc. for every row.
        runs = (
            db_session.query(BacktestRun)
            .options(
                defer(BacktestRun.equity_json),
                defer(BacktestRun.trades_json),
                defer(BacktestRun.ohlc_json),
                defer(BacktestRun.per_instrument_json),
                defer(BacktestRun.metrics_json),
            )
            .order_by(BacktestRun.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "strategy": r.strategy,
                "strategy_description": r.strategy_description,
                "symbol": r.symbol,
                "exchange": r.exchange,
                "interval": r.interval,
                "source": r.source,
                "start_date": r.start_date,
                "end_date": r.end_date,
                "capital": r.capital,
                "cost_model": r.cost_model,
                "n_bars": r.n_bars,
                "n_trades": r.n_trades,
                "net_pnl": r.net_pnl,
                "fees_total": r.fees_total,
                "sharpe": r.sharpe,
                "max_drawdown_pct": r.max_drawdown_pct,
                "xirr_pct": r.xirr_pct,
                "margin_used": r.margin_used,
                "return_on_margin_pct": r.return_on_margin_pct,
                "is_favorite": bool(r.is_favorite),
                "remarks": r.remarks,
                "strategy_source": r.strategy_source,
                "status": r.status,
            }
            for r in runs
        ]
    finally:
        db_session.remove()


def get_backtest_run(run_id: int) -> dict | None:
    """Return a single backtest run with full equity curve."""
    try:
        r = db_session.query(BacktestRun).get(run_id)
        if not r:
            return None
        return {
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "strategy": r.strategy,
            "symbol": r.symbol,
            "exchange": r.exchange,
            "interval": r.interval,
            "source": r.source,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "capital": r.capital,
            "cost_model": r.cost_model,
            "n_bars": r.n_bars,
            "n_trades": r.n_trades,
            "net_pnl": r.net_pnl,
            "fees_total": r.fees_total,
            "sharpe": r.sharpe,
            "max_drawdown_pct": r.max_drawdown_pct,
            "status": r.status,
            "error_message": r.error_message,
            "metrics": json.loads(r.metrics_json) if r.metrics_json else {},
            "xirr_pct": r.xirr_pct,
            "margin_used": r.margin_used,
            "return_on_margin_pct": r.return_on_margin_pct,
            "is_favorite": bool(r.is_favorite) if hasattr(r, 'is_favorite') else False,
            "remarks": getattr(r, 'remarks', None),
            "strategy_source": getattr(r, 'strategy_source', None),
            "strategy_description": r.strategy_description,
            "equity": json.loads(r.equity_json) if r.equity_json else [],
            "trades": json.loads(r.trades_json) if r.trades_json else [],
            "ohlc": json.loads(r.ohlc_json) if getattr(r, 'ohlc_json', None) else [],
            "per_instrument": json.loads(r.per_instrument_json) if getattr(r, 'per_instrument_json', None) else [],
        }
    finally:
        db_session.remove()


def get_backtest_equities(run_ids: list[int]) -> dict[int, list]:
    """Fetch only the equity curve for many runs in a single query.

    The strategy-overview page needs each run's equity curve to compute a
    recent-P&L metric, but NOT its (potentially large) trades / ohlc /
    per_instrument blobs. Selecting only ``id`` + ``equity_json`` for all
    requested runs at once avoids an N+1 of full-row deserialisations
    (which made the overview take ~16s across ~40 strategies).
    """
    ids = [rid for rid in run_ids if rid]
    if not ids:
        return {}
    try:
        rows = (
            db_session.query(BacktestRun.id, BacktestRun.equity_json)
            .filter(BacktestRun.id.in_(ids))
            .all()
        )
        return {rid: (json.loads(eq) if eq else []) for rid, eq in rows}
    finally:
        db_session.remove()
