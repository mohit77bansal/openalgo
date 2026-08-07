"""FastAPI application entry point.

Endpoints:
    GET  /                       — health
    GET  /api/v1/status          — version + auth state
    POST /api/v1/auth/login      — single-user passphrase auth
    GET  /auth/{broker}          — start broker OAuth flow
    GET  /auth/{broker}/callback — broker redirect handler
    GET  /api/v1/strategies      — list reference strategies
    POST /api/v1/backtests       — start a backtest
    GET  /api/v1/runs/{id}       — fetch run result
    GET  /api/v1/data/symbols    — list symbols in store
    GET  /api/v1/data/availability/{symbol} — availability report
    WS   /ws/runs/{id}           — stream backtest progress
    WS   /ws/live                — live tick + position stream
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from qbacktest import __version__
from qbacktest.brokers.base import BrokerAdapter
from qbacktest.brokers.angelone import AngelOneAdapter
from qbacktest.brokers.fyers import FyersAdapter
from qbacktest.brokers.token_store import TokenStore
from qbacktest.brokers.upstox import UpstoxAdapter
from qbacktest.gateway.settings import Settings, load_settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------


_brokers: dict[str, BrokerAdapter] = {}
_runs: dict[str, dict[str, Any]] = {}

# Live arb scanner state (populated by ChainScannerWorker on startup)
_LATEST_CHAIN_SNAPSHOT: dict[str, Any] = {}
_LATEST_OPPORTUNITIES: dict[str, list[dict[str, Any]]] = {}
_ARB_WS_CLIENTS: set = set()
_arb_worker_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    settings.app_data_dir.mkdir(parents=True, exist_ok=True)
    token_store = TokenStore(
        path=settings.app_data_dir / "broker_tokens.db",
        passphrase=settings.app_secret,
    )
    if settings.upstox_client_id:
        _brokers["upstox"] = UpstoxAdapter(
            client_id=settings.upstox_client_id,
            client_secret=settings.upstox_client_secret,
            redirect_uri=settings.upstox_redirect_uri,
            token_store=token_store,
        )
    if settings.fyers_client_id:
        _brokers["fyers"] = FyersAdapter(
            client_id=settings.fyers_client_id,
            secret_key=settings.fyers_secret_key,
            redirect_uri=settings.fyers_redirect_uri,
            token_store=token_store,
        )
    if settings.angelone_client_code and settings.angelone_api_key and settings.angelone_totp_secret:
        _brokers["angelone"] = AngelOneAdapter(
            client_code=settings.angelone_client_code,
            password=settings.angelone_password,
            api_key=settings.angelone_api_key,
            totp_secret=settings.angelone_totp_secret,
            token_store=token_store,
        )
    app.state.settings = settings
    app.state.token_store = token_store
    log.info("Gateway started — brokers loaded: %s", list(_brokers))

    # Spin up an in-process arb scanner with mock data so the gateway is
    # immediately useful for UI demos. Disable by setting QB_ARB_SCANNER=off.
    global _arb_worker_task
    if os.environ.get("QB_ARB_SCANNER", "mock") == "mock":
        from qbacktest.data.chain_source import MockChainProducer
        from qbacktest.live import ChainScannerWorker
        producer = MockChainProducer(underlying="NIFTY", interval_seconds=2.0, mispricing_prob=0.30)
        worker = ChainScannerWorker(source=producer, min_edge_after_costs_inr=0.0)

        async def _on_scan(snap, opps):
            _LATEST_CHAIN_SNAPSHOT[snap.underlying] = snap
            _LATEST_OPPORTUNITIES[snap.underlying] = [o.to_json() for o in opps]
            payload = {
                "underlying": snap.underlying,
                "ts": snap.ts.isoformat(),
                "spot": snap.spot,
                "opportunities": _LATEST_OPPORTUNITIES[snap.underlying][:10],
            }
            for ws in list(_ARB_WS_CLIENTS):
                try:
                    await ws.send_json(payload)
                except Exception:
                    _ARB_WS_CLIENTS.discard(ws)

        worker.subscribe(_on_scan)
        app.state.arb_worker = worker
        _arb_worker_task = asyncio.create_task(worker.run())
        log.info("Mock arb scanner started — set QB_ARB_SCANNER=off to disable")

    yield

    if _arb_worker_task is not None:
        worker_obj = getattr(app.state, "arb_worker", None)
        if worker_obj is not None:
            worker_obj.stop()
        _arb_worker_task.cancel()
    log.info("Gateway shutting down")


app = FastAPI(
    title="qbacktest",
    description="Indian markets backtesting and live trading platform",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health & status
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return f"<h1>qbacktest v{__version__}</h1><p>API running. UI at <a href='http://localhost:3000'>localhost:3000</a></p>"


@app.get("/api/v1/status")
async def status() -> dict[str, Any]:
    return {
        "version": __version__,
        "brokers": {
            name: {"authenticated": adapter.is_authenticated()}
            for name, adapter in _brokers.items()
        },
    }


# ---------------------------------------------------------------------------
# Broker auth
# ---------------------------------------------------------------------------


@app.get("/auth/{broker}")
async def broker_authorize(broker: str) -> RedirectResponse:
    adapter = _brokers.get(broker)
    if adapter is None:
        raise HTTPException(404, f"Broker {broker} not configured")
    return RedirectResponse(adapter.authorize_url())


@app.get("/auth/{broker}/callback")
async def broker_callback(broker: str, code: Optional[str] = None, auth_code: Optional[str] = None) -> dict[str, Any]:
    adapter = _brokers.get(broker)
    if adapter is None:
        raise HTTPException(404, f"Broker {broker} not configured")
    use_code = code or auth_code
    if not use_code:
        raise HTTPException(400, "Missing code/auth_code")
    data = await adapter.exchange_code_for_token(use_code)
    return {"broker": broker, "ok": True, "user": data.get("user_id")}


@app.post("/api/v1/auth/angelone/connect")
async def angelone_connect() -> dict[str, Any]:
    """Headless AngelOne login — uses creds + TOTP from .env, no browser."""
    adapter = _brokers.get("angelone")
    if adapter is None:
        raise HTTPException(404, "AngelOne not configured — set ANGELONE_* in .env")
    if not isinstance(adapter, AngelOneAdapter):
        raise HTTPException(500, "AngelOne adapter type mismatch")
    try:
        data = await adapter.connect()
        return {"broker": "angelone", "ok": True, "user": data.get("clientcode")}
    except Exception as e:
        raise HTTPException(401, f"AngelOne auth failed: {e}")


# ---------------------------------------------------------------------------
# Strategies + backtests
# ---------------------------------------------------------------------------


class StrategyInfo(BaseModel):
    name: str
    module: str
    params: dict[str, Any]


class BacktestRequest(BaseModel):
    strategy: str
    params: dict[str, Any] = {}
    underlyings: list[str] = ["NIFTY"]
    start_date: date
    end_date: date
    starting_capital: float = 1_000_000.0
    cost_model: str = "zerodha"


class BacktestRunSummary(BaseModel):
    run_id: str
    status: str
    metrics: dict[str, Any] = {}
    error: Optional[str] = None


@app.get("/api/v1/strategies")
async def list_strategies() -> list[StrategyInfo]:
    from qbacktest.strategies import (
        IntradayORBreakout,
        NiftyIronCondor,
        NiftyShortStraddle,
    )
    return [
        StrategyInfo(name=s.name, module=s.__module__, params=dict(s.params))
        for s in (NiftyShortStraddle, NiftyIronCondor, IntradayORBreakout)
    ]


@app.post("/api/v1/backtests")
async def start_backtest(req: BacktestRequest) -> BacktestRunSummary:
    """Kick off a backtest. Synchronous for v1 (small runs).

    For v2: use a Celery/RQ worker queue.
    """
    from qbacktest.engine.engine import BacktestEngine
    from qbacktest.data.store import DataStore
    from qbacktest.data.historical import HistoricalSource
    from qbacktest.costs.costs import DEFAULT_COST_MODELS
    from qbacktest.strategies import (
        IntradayORBreakout,
        NiftyIronCondor,
        NiftyShortStraddle,
    )

    strategy_cls_map = {
        "NiftyShortStraddle": NiftyShortStraddle,
        "NiftyIronCondor": NiftyIronCondor,
        "IntradayORBreakout": IntradayORBreakout,
    }
    cls = strategy_cls_map.get(req.strategy)
    if cls is None:
        raise HTTPException(400, f"Unknown strategy {req.strategy}")
    strategy = cls(**req.params)

    settings: Settings = app.state.settings
    store = DataStore(root=settings.app_data_dir)
    src = HistoricalSource(store)

    # Subscribe each requested underlying as INDEX bars (and the chain it would touch)
    for u in req.underlyings:
        from qbacktest.core.types import Exchange, Instrument, InstrumentType
        idx = Instrument(symbol=u.upper(), exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX, lot_size=1)
        src.subscribe(idx, start=req.start_date, end=req.end_date, timeframe="1d")

    engine = BacktestEngine(
        strategy=strategy,
        data=src,
        starting_capital=req.starting_capital,
        cost_model=DEFAULT_COST_MODELS.get(req.cost_model.lower(), DEFAULT_COST_MODELS["zerodha"]),
    )
    run_id = str(uuid.uuid4())
    _runs[run_id] = {"status": "running", "metrics": {}}
    try:
        result = await asyncio.to_thread(engine.run)
        _runs[run_id] = {
            "status": "completed",
            "metrics": result.metrics,
            "summary": result.summary(),
            "n_fills": len(result.fills),
            "n_trades": len(result.trades),
            "equity_curve": [
                {"ts": ts.isoformat(), "value": float(v)}
                for ts, v in result.equity_curve
            ],
            "trades_data": [
                {
                    "trade_id": t.trade_id,
                    "instrument": t.instrument.id,
                    "direction": t.direction.value,
                    "entry_ts": t.entry_fill.ts.isoformat(),
                    "entry_price": t.entry_fill.price,
                    "exit_ts": t.exit_fill.ts.isoformat(),
                    "exit_price": t.exit_fill.price,
                    "quantity": t.entry_fill.quantity,
                    "gross_pnl": t.gross_pnl,
                    "fees": t.fees,
                    "net_pnl": t.net_pnl,
                    "duration_seconds": t.duration_seconds,
                }
                for t in result.trades
            ],
        }
    except Exception as e:
        log.exception("Backtest failed")
        _runs[run_id] = {"status": "failed", "error": str(e)}
        return BacktestRunSummary(run_id=run_id, status="failed", error=str(e))
    return BacktestRunSummary(run_id=run_id, status="completed", metrics=_runs[run_id]["metrics"])


@app.get("/api/v1/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    if run_id not in _runs:
        raise HTTPException(404, "Run not found")
    return {"run_id": run_id, **_runs[run_id]}


@app.get("/api/v1/runs/{run_id}/equity")
async def get_run_equity(run_id: str) -> dict[str, Any]:
    """Equity curve as [{ts, value}] for charting."""
    run = _runs.get(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    curve = run.get("equity_curve", [])
    return {"run_id": run_id, "curve": curve}


@app.get("/api/v1/runs/{run_id}/trades")
async def get_run_trades(run_id: str) -> dict[str, Any]:
    """Trade list with PnL, fees, duration."""
    run = _runs.get(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return {"run_id": run_id, "trades": run.get("trades_data", [])}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@app.get("/api/v1/data/symbols")
async def list_symbols() -> dict[str, list[str]]:
    from qbacktest.core.lot_sizes import known_underlyings
    return {"underlyings": known_underlyings()}


@app.get("/api/v1/arbitrage/scan")
async def arbitrage_scan(underlying: str = "NIFTY") -> dict[str, Any]:
    """Latest scanner output. Populated by the in-process ChainScannerWorker."""
    snapshot = _LATEST_CHAIN_SNAPSHOT.get(underlying.upper())
    opps = _LATEST_OPPORTUNITIES.get(underlying.upper(), [])
    if snapshot is None:
        return {
            "underlying": underlying,
            "opportunities": [],
            "message": "No chain snapshot yet. The mock scanner runs every 2s — wait a few seconds.",
        }
    return {
        "underlying": underlying,
        "snapshot_ts": snapshot.ts.isoformat(),
        "expiry": snapshot.expiry.isoformat(),
        "spot": snapshot.spot,
        "opportunities": opps,
    }


@app.get("/api/v1/signals/volatile-tomorrow")
async def signals_volatile_tomorrow(
    hours: int = 24,
    top: int = 10,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Pull news → score → return top stocks likely volatile tomorrow."""
    from qbacktest.signals.pipeline import run_pipeline
    items, signals = await run_pipeline(
        look_back_hours=hours,
        top_n=top,
        use_llm=use_llm,
    )
    return {
        "ts": datetime.now().isoformat(),
        "look_back_hours": hours,
        "items_fetched": len(items),
        "signals": [s.to_json() for s in signals],
        "headlines": [
            {
                "source": n.source,
                "title": n.title,
                "url": n.url,
                "published_at": n.published_at.isoformat(),
            }
            for n in items[:50]
        ],
    }


@app.get("/api/v1/signals/scalping")
async def signals_scalping(
    underlying: str = "NIFTY",
    bars: int = 200,
    interval: float = 0.05,
    min_edge: float = 0.0,
    regime: bool = True,
    seed: int = 42,
) -> dict[str, Any]:
    """Run the scalping engine against a mock intraday stream and return all signals.

    For UI demo only — synthesizes one simulated trading day quickly. Live
    AngelOne tick path will be wired alongside live arb chain ingest.
    """
    from qbacktest.signals.scalping import (
        GapFadeDetector, LastHourReversionDetector, MockIntradayTicker,
        ORBDetector, ScalpingEngine,
    )
    import random as _r
    ticker = MockIntradayTicker(underlying=underlying, interval_seconds=interval)
    ticker._rng = _r.Random(seed)
    engine = ScalpingEngine(
        source=ticker,
        detectors=(
            ORBDetector(underlying=underlying),
            GapFadeDetector(underlying=underlying),
            LastHourReversionDetector(underlying=underlying),
        ),
        apply_regime=regime,
        min_expectancy_inr=min_edge,
    )
    captured: list = []

    async def collect(sig):
        captured.append(sig)

    engine.subscribe(collect)
    await engine.run(max_bars=bars)
    return {
        "ts": datetime.now().isoformat(),
        "underlying": underlying,
        "bars_processed": engine.stats["bars_processed"],
        "signals_emitted": engine.stats["signals_emitted"],
        "signals_skipped": engine.stats["signals_skipped"],
        "signals": [s.to_json() for s in captured],
    }


@app.get("/api/v1/regime/check")
async def regime_check(
    on: str | None = None,
    kind: str = "PREMIUM_SELL",
    ticker: str | None = None,
) -> dict[str, Any]:
    from datetime import date as _date
    from qbacktest.signals.regime_filter import (
        DEFAULT_REGIME_FILTER,
        StrategyKind,
    )
    d = _date.fromisoformat(on) if on else _date.today()
    try:
        k = StrategyKind(kind)
    except ValueError:
        raise HTTPException(400, f"Bad kind: {kind}")
    decision = DEFAULT_REGIME_FILTER.check(d, kind=k, ticker=ticker)
    return {
        "on": d.isoformat(),
        "kind": kind,
        "ticker": ticker,
        "allowed": decision.allowed,
        "reason": decision.reason,
        "event_kind": decision.event_kind.value if decision.event_kind else None,
        "severity": decision.severity,
        "suggested_size_multiplier": decision.suggested_size_multiplier,
    }


@app.get("/api/v1/research/strategies")
async def research_strategies(
    days_back: int = 60,
    max_papers: int = 40,
    top: int = 20,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Pull arxiv finance papers → LLM-extract strategies → score Indian feasibility."""
    from qbacktest.research.pipeline import run_research_pipeline
    papers, candidates = await run_research_pipeline(
        days_back=days_back, max_papers=max_papers, use_llm=use_llm,
    )
    return {
        "ts": datetime.now().isoformat(),
        "papers_fetched": len(papers),
        "candidates": [c.to_json() for c in candidates[:top]],
    }


@app.websocket("/ws/arbitrage")
async def ws_arbitrage(websocket: WebSocket) -> None:
    """Stream live arbitrage opportunities pushed by the ChainScannerWorker."""
    await websocket.accept()
    _ARB_WS_CLIENTS.add(websocket)
    try:
        # Send snapshot of current state immediately so UI doesn't wait
        for u, snap in _LATEST_CHAIN_SNAPSHOT.items():
            await websocket.send_json({
                "underlying": u,
                "ts": snap.ts.isoformat(),
                "spot": snap.spot,
                "opportunities": _LATEST_OPPORTUNITIES.get(u, [])[:10],
            })
        # Keep connection alive — worker pushes via _on_scan
        while True:
            await asyncio.sleep(60)  # ping interval; client sees scanner pushes meanwhile
    except WebSocketDisconnect:
        pass
    finally:
        _ARB_WS_CLIENTS.discard(websocket)


@app.get("/api/v1/data/availability/{symbol}")
async def data_availability(symbol: str, timeframe: str = "1d") -> dict[str, Any]:
    from qbacktest.data.store import DataStore
    from qbacktest.data.availability import DataAvailability
    from qbacktest.core.types import Exchange, Instrument, InstrumentType
    settings: Settings = app.state.settings
    store = DataStore(root=settings.app_data_dir)
    avail = DataAvailability(store)
    idx = Instrument(symbol=symbol.upper(), exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX, lot_size=1)
    rep = avail.report(idx, timeframe)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "earliest": rep.earliest.isoformat() if rep.earliest else None,
        "latest": rep.latest.isoformat() if rep.latest else None,
        "n_days": rep.n_days,
    }


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@app.websocket("/ws/runs/{run_id}")
async def ws_run_progress(websocket: WebSocket, run_id: str) -> None:
    await websocket.accept()
    try:
        while True:
            await asyncio.sleep(1.0)
            run = _runs.get(run_id)
            if run is None:
                await websocket.send_json({"error": "run not found"})
                break
            await websocket.send_json({"run_id": run_id, **run})
            if run.get("status") in ("completed", "failed"):
                break
    except WebSocketDisconnect:
        return


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    """Stream live ticks + positions during paper/live trading."""
    await websocket.accept()
    try:
        # Placeholder: in a real session we'd register this websocket as a sink
        # for the LiveEngine's data + position events.
        await websocket.send_json({"status": "live websocket connected (no session active)"})
        while True:
            await asyncio.sleep(5.0)
            await websocket.send_json({"heartbeat": True})
    except WebSocketDisconnect:
        return
