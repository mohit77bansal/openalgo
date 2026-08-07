"""Backtest run history — persists every backtest result for the list view."""

from __future__ import annotations

import json
import os
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker
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
    status = Column(String(10), nullable=False, default="success")
    error_message = Column(Text)
    metrics_json = Column(Text)
    equity_json = Column(Text)
    trades_json = Column(Text)


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
            status=result.get("status", "error"),
            error_message=result.get("message"),
            metrics_json=json.dumps(metrics) if metrics else None,
            equity_json=json.dumps(result.get("equity", [])),
            trades_json=json.dumps(result.get("trades", [])),
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
        runs = (
            db_session.query(BacktestRun)
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
            "equity": json.loads(r.equity_json) if r.equity_json else [],
            "trades": json.loads(r.trades_json) if r.trades_json else [],
            "strategy_description": r.strategy_description,
        }
    finally:
        db_session.remove()
