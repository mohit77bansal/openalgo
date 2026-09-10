"""Backtest service — bridges OpenAlgo to the vendored aladin `qbacktest` engine.

Thin facade: delegates to services/strategies/ for the strategy registry,
and keeps the engine invocation + result formatting logic here.

Data sources (the `source` argument):
- "demo": self-contained deterministic synthetic bars — no data required.
- "db"  : real OHLCV from the Historify DuckDB store via
          `database.historify_db.get_ohlcv` (populated from AngelOne).
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

DEMO_COST_MODELS = ("zerodha", "angelone", "upstox", "fyers", "zero")
DEFAULT_CAPITAL = 1_000_000.0
DEFAULT_SYMBOL = "NIFTY"

_INTERVAL_TF = {"D": "1d", "1d": "1d", "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h"}


# ---------------------------------------------------------------------------
# JSON sanitisation
# ---------------------------------------------------------------------------
def _json_safe(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _json_safe(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return str(obj)


# ---------------------------------------------------------------------------
# Instrument mapping
# ---------------------------------------------------------------------------
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
_FUT_RE = __import__("re").compile(r"(\d{2})([A-Z]{3})(\d{2})FUT$")
# NIFTY11AUG2624650CE -> underlying, dd, mon, yy, strike, CE/PE
_OPT_RE = __import__("re").compile(
    r"^([A-Z&\-]+?)(\d{2})([A-Z]{3})(\d{2})(\d+(?:\.\d+)?)(CE|PE)$")


def _parse_fut_expiry(symbol: str) -> date | None:
    m = _FUT_RE.search((symbol or "").upper())
    if not m:
        return None
    dd, mon, yy = m.groups()
    return date(2000 + int(yy), _MONTHS[mon], int(dd))


def _parse_option_symbol(symbol: str):
    """Parse a real option contract symbol into (underlying, expiry, strike, type).

    Returns None when the symbol is not a well-formed option contract.
    """
    m = _OPT_RE.match((symbol or "").upper())
    if not m:
        return None
    underlying, dd, mon, yy, strike, opt_type = m.groups()
    if mon not in _MONTHS:
        return None
    try:
        expiry = date(2000 + int(yy), _MONTHS[mon], int(dd))
    except ValueError:
        return None
    return underlying, expiry, float(strike), opt_type


def _lookup_lot_size(symbol: str, exchange: str) -> int:
    try:
        from database.symbol import db_session, SymToken
        row = db_session.query(SymToken).filter(
            SymToken.symbol == symbol, SymToken.exchange == exchange
        ).first()
        if row and row.lotsize and row.lotsize > 0:
            return int(row.lotsize)
    except Exception:
        pass
    for prefix, lot in [("BANKNIFTY", 30), ("FINNIFTY", 60), ("MIDCPNIFTY", 120), ("NIFTY", 65)]:
        if symbol.upper().startswith(prefix):
            return lot
    return 1


def _instrument_for(symbol: str, exchange: str) -> Any:
    from qbacktest.core.types import Exchange, Instrument, InstrumentType

    ex = (exchange or "NSE").upper()
    if ex in ("NSE_INDEX", "BSE_INDEX", "INDEX"):
        qex = Exchange.BSE if ex == "BSE_INDEX" else Exchange.NSE
        return Instrument(symbol=symbol, exchange=qex, instrument_type=InstrumentType.INDEX, lot_size=1)
    if ex in ("NFO", "BFO"):
        qex = Exchange.BFO if ex == "BFO" else Exchange.NFO
        lot = _lookup_lot_size(symbol, ex)
        # Real option contract (e.g. NIFTY11AUG2624650CE): build a proper
        # option Instrument so Instrument.id (which asserts expiry+strike for
        # options) works and the engine can trade it. Premium-native
        # strategies pin lot_size=1 downstream, but we parse the real
        # underlying/expiry/strike/type here so the id is canonical.
        opt = _parse_option_symbol(symbol)
        if opt is not None:
            underlying, expiry, strike, opt_type = opt
            return Instrument(
                symbol=underlying, exchange=qex,
                instrument_type=InstrumentType(opt_type),
                lot_size=lot, expiry=expiry, strike=strike, underlying=underlying,
            )
        return Instrument(symbol=symbol, exchange=qex, instrument_type=InstrumentType.FUT,
                          lot_size=lot, expiry=_parse_fut_expiry(symbol))
    qex = getattr(Exchange, ex, Exchange.NSE)
    return Instrument(symbol=symbol, exchange=qex, instrument_type=InstrumentType.EQ, lot_size=1)


class _StaticSource:
    def __init__(self, bars: list[Any]):
        self.bars = sorted(bars, key=lambda b: (b.ts, b.instrument.id))
        self._latest: dict[str, float] = {}

    def stream(self):
        for b in self.bars:
            self._latest[b.instrument.id] = b.close
            yield b

    def latest_price(self, instrument: Any) -> float | None:
        return self._latest.get(instrument.id)


def _build_synthetic_source(symbol: str, exchange: str, n_bars: int) -> _StaticSource:
    from qbacktest.core.calendar import IST
    from qbacktest.core.types import Bar

    inst = _instrument_for(symbol, exchange)
    base = 25_000.0
    bars: list[Any] = []
    d, made, i = date(2024, 1, 1), 0, 0
    while made < n_bars:
        d = d + timedelta(days=1)
        if d.weekday() >= 5:
            continue
        px = base * (1.0 + 0.0008 * i + 0.02 * math.sin(i / 6.0))
        bars.append(Bar(
            instrument=inst, ts=datetime.combine(d, time(15, 30), tzinfo=IST),
            timeframe="1d", open=px, high=px * 1.004, low=px * 0.996, close=px * 1.001, volume=1000,
        ))
        made += 1
        i += 1
    return _StaticSource(bars)


def _build_db_source(symbol: str, exchange: str, interval: str, start: str | None, end: str | None):
    from database.historify_db import get_connection, get_ohlcv
    from qbacktest.core.calendar import IST
    from qbacktest.core.types import Bar
    import pandas as pd

    start_ts = int(datetime.fromisoformat(start).timestamp()) if start else None
    end_ts = int(datetime.fromisoformat(end).timestamp()) if end else None

    df = get_ohlcv(symbol, exchange, interval, start_timestamp=start_ts, end_timestamp=end_ts)

    if df is None or len(df) == 0:
        try:
            with get_connection() as con:
                sql = "SELECT timestamp, open, high, low, close, volume, oi FROM market_data WHERE symbol=? AND exchange=? AND interval=? "
                params = [symbol, exchange, interval]
                if start_ts:
                    sql += "AND timestamp >= ? "
                    params.append(start_ts)
                if end_ts:
                    sql += "AND timestamp <= ? "
                    params.append(end_ts)
                sql += "ORDER BY timestamp ASC"
                df = con.execute(sql, params).fetchdf()
        except Exception:
            df = pd.DataFrame()

    if df is None or len(df) == 0:
        raise ValueError(
            f"no {interval} data for {symbol}/{exchange} in the Historify store — "
            f"ingest it from AngelOne first"
        )

    inst = _instrument_for(symbol, exchange)
    tf = _INTERVAL_TF.get(interval, "1d")
    bars: list[Any] = []
    for row in df.itertuples(index=False):
        ts = datetime.fromtimestamp(int(row.timestamp), tz=timezone.utc).astimezone(IST)
        bars.append(Bar(
            instrument=inst, ts=ts, timeframe=tf,
            open=float(row.open), high=float(row.high), low=float(row.low),
            close=float(row.close), volume=float(getattr(row, "volume", 0) or 0),
        ))
    return _StaticSource(bars), len(bars)


# ---------------------------------------------------------------------------
# Strategy registry — single source of truth in services/strategies/
# ---------------------------------------------------------------------------

from services.strategies import STRATEGY_REGISTRY, list_strategies, _make_strategy  # noqa: E402


# ---------------------------------------------------------------------------
# Options premium simulation (Black-Scholes with IV / theta / gamma)
# ---------------------------------------------------------------------------

_STRIKE_STEPS = {"BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "NIFTY": 50}
_RISK_FREE_RATE = 0.07
_IV_PREMIUM_FACTOR = 1.1  # implied vol typically trades above realized
_IV_MIN, _IV_MAX = 0.10, 0.80
_VOL_WINDOW = 100  # rolling bars for realized-vol estimate


def _strike_step_for(symbol: str) -> int:
    for prefix, step in _STRIKE_STEPS.items():
        if symbol.upper().startswith(prefix):
            return step
    return 50


def _next_weekly_expiry(ts):
    """Next Tuesday 15:30 (NIFTY weekly convention) strictly after ts."""
    from datetime import datetime as _dt, timedelta as _td
    days_ahead = (1 - ts.weekday()) % 7  # Tuesday = 1
    candidate = (ts + _td(days=days_ahead)).replace(hour=15, minute=30, second=0, microsecond=0)
    if candidate <= ts:
        candidate += _td(days=7)
    return candidate


def _transform_bars_to_option_premium(bars: list, symbol: str) -> list:
    """Convert underlying bars into a synthetic ATM call premium series.

    Models the real option-buying economics the linear proxy missed:
    - GAMMA: strike is FIXED for each weekly cycle, so premium is convex
      in the underlying (accelerates in-the-money, decays out-of-the-money).
    - THETA: time-to-expiry shrinks bar by bar; premium decays toward
      intrinsic value into each Tuesday 15:30 expiry.
    - IV: sigma is estimated from rolling realized vol (annualized,
      clamped 10-80%), so premiums inflate during fast markets.

    Limitation: bearish trades short this call series (a proxy for buying
    a put). Directionally correct; understates convexity gains and
    inverts theta on multi-day bearish holds. Intraday impact is small.
    """
    import dataclasses as _dc
    from qbacktest.greeks.black_scholes import bs_price

    if not bars:
        return bars

    step = _strike_step_for(symbol)
    opt_inst = _dc.replace(bars[0].instrument, lot_size=1)

    # Annualization factor from median bar spacing
    if len(bars) > 1:
        spacings = sorted(
            (bars[i + 1].ts - bars[i].ts).total_seconds()
            for i in range(min(len(bars) - 1, 200))
        )
        bar_seconds = max(spacings[len(spacings) // 2], 1)
    else:
        bar_seconds = 900
    bars_per_day = max(6.25 * 3600 / bar_seconds, 1)
    ann_factor = (252 * bars_per_day) ** 0.5

    out = []
    strike: float | None = None
    expiry = None
    returns: list[float] = []
    prev_close: float | None = None
    iv = 0.15  # seed until the vol window fills

    for b in bars:
        if prev_close and prev_close > 0:
            returns.append((b.close - prev_close) / prev_close)
            if len(returns) > _VOL_WINDOW:
                returns.pop(0)
        prev_close = b.close

        # Weekly cycle: fix a new ATM strike after each expiry
        if expiry is None or b.ts >= expiry:
            strike = round(b.close / step) * step
            expiry = _next_weekly_expiry(b.ts)

        if len(returns) >= 20:
            mean_r = sum(returns) / len(returns)
            var = sum((r - mean_r) ** 2 for r in returns) / len(returns)
            realized = (var ** 0.5) * ann_factor
            iv = min(max(realized * _IV_PREMIUM_FACTOR, _IV_MIN), _IV_MAX)

        tte_years = max((expiry - b.ts).total_seconds() / (365.25 * 24 * 3600), 1e-5)
        po = bs_price(b.open, strike, tte_years, _RISK_FREE_RATE, iv, "C")
        ph = bs_price(b.high, strike, tte_years, _RISK_FREE_RATE, iv, "C")
        pl = bs_price(b.low, strike, tte_years, _RISK_FREE_RATE, iv, "C")
        pc = bs_price(b.close, strike, tte_years, _RISK_FREE_RATE, iv, "C")
        # Floor at 0.05 (exchange tick) so the engine never divides by zero
        out.append(_dc.replace(
            b, instrument=opt_inst,
            open=max(po, 0.05), high=max(ph, 0.05),
            low=max(pl, 0.05), close=max(pc, 0.05),
        ))

    return out


# ---------------------------------------------------------------------------
# Engine invocation
# ---------------------------------------------------------------------------
def _run_engine(source_obj: Any, symbol: str, capital: float, cost: str,
                strategy_key: str = "atr_channel_breakout", **strategy_kwargs) -> Any:
    from qbacktest.costs.costs import DEFAULT_COST_MODELS
    from qbacktest.engine.engine import BacktestEngine

    cost_key = "zerodha" if cost == "angelone" else cost
    engine = BacktestEngine(
        strategy=_make_strategy(symbol, strategy_key, **strategy_kwargs),
        data=source_obj,
        starting_capital=capital,
        cost_model=DEFAULT_COST_MODELS[cost_key],
    )
    return engine.run()


def run_backtest(
    source: str = "demo",
    symbol: str = DEFAULT_SYMBOL,
    exchange: str = "NSE",
    interval: str = "D",
    start: str | None = None,
    end: str | None = None,
    capital: float = DEFAULT_CAPITAL,
    cost: str = "zerodha",
    strategy_key: str = "sma_momentum",
    n_bars: int = 120,
    _save: bool = True,
    **strategy_kwargs,
) -> dict[str, Any]:
    """Run a backtest and return a JSON-safe result."""
    if cost not in DEMO_COST_MODELS:
        cost = "zerodha"
    try:
        capital = float(capital)
    except (TypeError, ValueError):
        capital = DEFAULT_CAPITAL
    if not math.isfinite(capital) or capital <= 0:
        capital = DEFAULT_CAPITAL

    strat_entry = STRATEGY_REGISTRY.get(strategy_key, STRATEGY_REGISTRY.get("atr_channel_breakout", {}))
    strat_name = strat_entry.get("name", strategy_key)
    strat_desc = strat_entry.get("description", "")
    strat_source = strat_entry.get("source", "")

    # Options strategies trade qty=1 at the underlying price. The engine's
    # lot-size validation would reject qty=1 for BANKNIFTY (lot=30), so we
    # override the instrument to lot_size=1 and give enough capital.
    is_options_strategy = "option" in strategy_key
    user_capital = capital
    if is_options_strategy:
        capital = max(capital, 200_000)

    try:
        if source == "db":
            source_obj, n_bars = _build_db_source(symbol, exchange, interval, start, end)
        else:
            source = "demo"
            source_obj = _build_synthetic_source(symbol, exchange, n_bars)

        # BS-transform underlying bars into synthetic premiums — but NOT when
        # the symbol already IS an option contract (real premium bars from
        # the collector). Those are traded as-is.
        symbol_is_option = symbol.upper().endswith(("CE", "PE"))
        if is_options_strategy and not symbol_is_option and source_obj and source_obj.bars:
            source_obj.bars = _transform_bars_to_option_premium(source_obj.bars, symbol)
        elif symbol_is_option and source_obj and source_obj.bars:
            import dataclasses as _dc
            opt_inst = _dc.replace(source_obj.bars[0].instrument, lot_size=1)
            source_obj.bars = [_dc.replace(b, instrument=opt_inst) for b in source_obj.bars]

        result = _run_engine(source_obj, symbol, capital, cost, strategy_key, **strategy_kwargs)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception:
        logger.exception("backtest failed for %s/%s source=%s", symbol, exchange, source)
        return {"status": "error", "message": "backtest engine failed — see logs"}

    curve = result.equity_curve
    equity_curve = [[ts.isoformat(), _json_safe(eq)] for ts, eq in curve]
    equity = [{"date": ts.isoformat(), "value": _json_safe(eq)} for ts, eq in curve]

    xirr_pct = None
    if len(curve) >= 2:
        try:
            first_ts, first_eq = curve[0]
            last_ts, last_eq = curve[-1]
            days = (last_ts - first_ts).total_seconds() / 86400
            if days > 0 and first_eq > 0:
                total_return = (last_eq / first_eq) - 1.0
                years = days / 365.25
                if years > 0 and total_return > -1:
                    xirr_pct = ((1 + total_return) ** (1 / years) - 1) * 100
                elif years > 0:
                    xirr_pct = -100.0
        except Exception:
            pass

    metrics = _json_safe(getattr(result, "metrics", {}))
    if isinstance(metrics, dict):
        fixes: dict[str, Any] = {}
        # Normalize engine's raw-fraction max_drawdown_pct to percentage
        raw_dd = metrics.get("max_drawdown_pct")
        if isinstance(raw_dd, (int, float)) and -1 < raw_dd < 1 and raw_dd != 0:
            fixes["max_drawdown_pct"] = round(raw_dd * 100, 2)
        # Cap drawdown at -100%
        dd_val = fixes.get("max_drawdown_pct", raw_dd)
        if isinstance(dd_val, (int, float)) and dd_val < -100:
            fixes["max_drawdown_pct"] = -100.0
        # Sharpe sign consistency: negative total return must have negative Sharpe
        sharpe = metrics.get("sharpe")
        net_pnl = metrics.get("net_pnl", 0)
        if isinstance(sharpe, (int, float)) and isinstance(net_pnl, (int, float)):
            if net_pnl < 0 and sharpe > 0:
                fixes["sharpe"] = round(-abs(sharpe), 4)
        if fixes:
            metrics = {**metrics, **fixes}

    margin_per_lot = 0.0
    margin_used = 0.0
    return_on_margin_pct = None
    inst = _instrument_for(symbol, exchange)
    lot_size = max(getattr(inst, "lot_size", 1) or 1, 1)
    if lot_size > 1 and source_obj and source_obj.bars:
        mid_idx = len(source_obj.bars) // 2
        rep_price = source_obj.bars[mid_idx].close if source_obj.bars else 24500
        margin_pct = 0.12
        notional_per_lot = rep_price * lot_size
        margin_per_lot = notional_per_lot * margin_pct
        margin_used = margin_per_lot
        net_pnl = (metrics.get("net_pnl") or 0) if isinstance(metrics, dict) else 0
        if margin_used > 0 and net_pnl != 0:
            return_on_margin_pct = (net_pnl / margin_used) * 100

    ohlc = [
        {"time": b.ts.isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close}
        for b in source_obj.bars
    ]

    # For options strategies, show the option instrument name instead of futures
    display_symbol = symbol
    if is_options_strategy and "FUT" in symbol.upper():
        import re as _re
        # BANKNIFTY25AUG26FUT -> BANKNIFTY25AUG26 CE/PE (ATM)
        base = _re.sub(r'FUT$', '', symbol)
        display_symbol = f"{base} ATM CE/PE"

    out = {
        "status": "success",
        "strategy": strat_name,
        "strategy_description": strat_desc,
        "strategy_source": strat_source,
        "symbol": display_symbol,
        "exchange": exchange,
        "interval": interval,
        "source": source,
        "start": start,
        "end": end,
        "capital": user_capital if is_options_strategy else capital,
        "instrument_type": "Options (simulated)" if is_options_strategy else "Futures",
        "cost_model": cost,
        "n_bars": n_bars,
        "metrics": metrics,
        "xirr_pct": round(xirr_pct, 2) if xirr_pct is not None else None,
        "margin_used": round(margin_used, 0) if margin_used else None,
        "return_on_margin_pct": round(return_on_margin_pct, 2) if return_on_margin_pct is not None else None,
        **_compute_recent_metrics(equity, days=30),
        "equity": equity,
        "equity_curve": equity_curve,
        "trades": _json_safe(getattr(result, "trades", [])),
        "ohlc": ohlc,
        "data_note": (
            "Options simulation: uses underlying futures bars as signal source, trades at option premium levels (~1.5% of underlying). P&L reflects option-sized positions."
            if is_options_strategy
            else "Continuous NIFTY futures (stitched across contract rolls by broker API)" if "FUT" in symbol.upper()
            else None
        ),
    }

    if _save:
        try:
            from database.backtest_db import save_backtest_run
            run_id = save_backtest_run(out)
            if run_id:
                out["run_id"] = run_id
        except Exception:
            logger.debug("failed to persist backtest run", exc_info=True)

    return out


def run_demo_backtest(symbol: str = DEFAULT_SYMBOL, capital: float = DEFAULT_CAPITAL,
                      cost: str = "zerodha", n_bars: int = 120, window: int = 8) -> dict[str, Any]:
    return run_backtest(source="demo", symbol=symbol, capital=capital, cost=cost,
                        n_bars=n_bars, window=window)


def _compute_recent_metrics(equity: list[dict[str, Any]], days: int = 30) -> dict[str, Any]:
    """Compute P&L and Sharpe for the last N calendar days of the equity curve."""
    if not equity or len(equity) < 5:
        return {"recent_pnl": None, "recent_sharpe": None, "recent_days": days}
    last_date = equity[-1].get("date", "")[:10]
    if not last_date:
        return {"recent_pnl": None, "recent_sharpe": None, "recent_days": days}
    from datetime import datetime as _dt, timedelta
    try:
        cutoff = (_dt.fromisoformat(last_date) - timedelta(days=days)).isoformat()[:10]
    except Exception:
        return {"recent_pnl": None, "recent_sharpe": None, "recent_days": days}
    recent = [pt for pt in equity if (pt.get("date", "") or "")[:10] >= cutoff]
    if len(recent) < 5:
        return {"recent_pnl": None, "recent_sharpe": None, "recent_days": days}
    first_val = recent[0].get("value", 0)
    last_val = recent[-1].get("value", 0)
    recent_pnl = last_val - first_val if first_val > 0 else None
    recent_sharpe = _compute_sharpe(recent)
    return {"recent_pnl": round(recent_pnl, 2) if recent_pnl is not None else None,
            "recent_sharpe": recent_sharpe, "recent_days": days}


def _compute_sharpe(equity: list[dict[str, Any]], risk_free_rate: float = 0.07) -> float | None:
    """Annualized Sharpe. Filters non-positive equity, enforces sign consistency."""
    if not equity or len(equity) < 10:
        return None
    values = [pt.get("value", 0) for pt in equity if pt.get("value", 0) > 0]
    if len(values) < 10:
        return None
    returns = [(values[i] - values[i-1]) / values[i-1] for i in range(1, len(values)) if values[i-1] > 0]
    if len(returns) < 5:
        return None
    avg_ret = sum(returns) / len(returns)
    std_ret = (sum((r - avg_ret) ** 2 for r in returns) / len(returns)) ** 0.5
    if std_ret == 0:
        return None
    periods_per_year = 252 * 25
    annual_ret = avg_ret * periods_per_year
    annual_std = std_ret * (periods_per_year ** 0.5)
    sharpe = round((annual_ret - risk_free_rate) / annual_std, 2)
    total_return = (values[-1] - values[0]) / values[0] if values[0] > 0 else -1
    if total_return < 0 and sharpe > 0:
        return round(-abs(sharpe), 2)
    return sharpe


def _compute_max_drawdown(equity: list[dict[str, Any]]) -> float | None:
    """Max drawdown %, capped at -100% (margin call floor)."""
    if not equity or len(equity) < 2:
        return None
    peak = 0.0
    max_dd = 0.0
    for pt in equity:
        val = pt.get("value", 0)
        if val > peak:
            peak = val
        if peak > 0:
            dd = (peak - val) / peak
            if dd > max_dd:
                max_dd = dd
    dd_pct = round(-min(max_dd, 1.0) * 100, 2)
    return dd_pct if max_dd > 0 else 0.0


def real_option_contracts(
    underlying: str = "NIFTY", min_bars: int = 1500,
    strike_lo: int = 24000, strike_hi: int = 25000,
) -> tuple[list[str], str | None, str | None]:
    """Real collected CE/PE contracts for an underlying + their data window.

    Options backtests must run on ACTUAL premium series (these symbols), never
    Black-Scholes premium simulated on the index future — the simulated path
    both flatters results and labels trades with the underlying ("NIFTY")
    instead of the real contract. Returns ([symbols], start_date, end_date).
    """
    import re as _re2
    from datetime import datetime as _dt2, timedelta as _td2, timezone as _tz2

    import duckdb as _duck

    IST = _tz2(_td2(hours=5, minutes=30))
    try:
        conn = _duck.connect("db/historify.duckdb", read_only=True)
        rows = conn.execute(
            """
            SELECT symbol, COUNT(*) n, MIN(timestamp) mn, MAX(timestamp) mx
            FROM market_data
            WHERE (symbol LIKE ? OR symbol LIKE ?) AND interval='1m'
            GROUP BY symbol HAVING n > ?
            """,
            [f"{underlying}%CE", f"{underlying}%PE", min_bars],
        ).fetchall()
        conn.close()
    except Exception:
        logger.exception("real_option_contracts query failed")
        return [], None, None

    pat = _re2.compile(rf"^{underlying}\d{{2}}[A-Z]{{3}}\d{{2}}(\d+)(CE|PE)$")
    keep = []
    for sym, _n, mn, mx in rows:
        m = pat.match(sym)
        if m and strike_lo <= int(m.group(1)) <= strike_hi:
            keep.append((sym, mn, mx))
    if not keep:
        return [], None, None
    start = _dt2.fromtimestamp(min(r[1] for r in keep), IST).strftime("%Y-%m-%d")
    end = _dt2.fromtimestamp(max(r[2] for r in keep), IST).strftime("%Y-%m-%d")
    return [r[0] for r in keep], start, end


def _single_account_portfolio(per_instrument: list[dict], capital: float, cost: str) -> dict:
    """Collapse per-contract option runs into ONE realistic account.

    You can only hold one option position at a time with a single capital pool,
    so summing every contract's full-capital P&L (the old behaviour) reported
    absurdities like -139k on a 10k account. Here we pool every contract's
    trades, sort by entry time, and greedily take only NON-OVERLAPPING trades
    on one account (with a circuit breaker at zero) — the true single-account
    path. Fees are recomputed per fill with the same Indian cost model.
    """
    from datetime import date as _date

    from qbacktest.core.types import Exchange, InstrumentType, ProductType, Side
    from qbacktest.costs.costs import DEFAULT_COST_MODELS

    cm = DEFAULT_COST_MODELS.get(cost, DEFAULT_COST_MODELS["zerodha"])

    def _ts(fill):
        return str(fill.get("ts", fill.get("timestamp", "")))

    pooled = []
    for p in per_instrument:
        for t in (p.get("trades") or []):
            ef, xf = t.get("entry_fill") or {}, t.get("exit_fill") or {}
            if not ef or not xf:
                continue
            pooled.append((ef, xf, t.get("instrument") or ef.get("instrument") or {}))
    pooled.sort(key=lambda x: _ts(x[0]))

    def _fee(inst, side, price, qty, d):
        itype = {"CE": InstrumentType.CE, "PE": InstrumentType.PE}.get(
            inst.get("instrument_type"), InstrumentType.CE)
        try:
            return cm.compute_fill(
                exchange=Exchange.NFO, instrument_type=itype, product=ProductType.NRML,
                side=side, quantity=int(qty), price=float(price), trade_date=d,
            ).total
        except Exception:
            return 0.0

    equity = capital
    curve, sel_trades = [], []
    last_exit = ""
    net_total = fees_total = 0.0
    for ef, xf, inst in pooled:
        e_ts, x_ts = _ts(ef), _ts(xf)
        if e_ts < last_exit:            # overlaps an open position — skip
            continue
        try:
            qty = int(ef.get("quantity") or 0)
            e_px, x_px = float(ef.get("price") or 0), float(xf.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or e_px <= 0:
            continue
        long = (ef.get("side") or "BUY").upper() == "BUY"
        gross = (x_px - e_px) * qty * (1 if long else -1)
        d = _date.today()
        try:
            d = _date.fromisoformat(e_ts[:10])
        except ValueError:
            pass
        fees = (_fee(inst, Side.BUY if long else Side.SELL, e_px, qty, d)
                + _fee(inst, Side.SELL if long else Side.BUY, x_px, qty, d))
        net = gross - fees
        equity += net
        net_total += net
        fees_total += fees
        last_exit = x_ts
        curve.append({"date": x_ts[:10], "value": round(equity, 2)})
        sel_trades.append({"entry_fill": ef, "exit_fill": xf, "instrument": inst,
                           "net_pnl": round(net, 2), "direction": "LONG" if long else "SHORT"})
        if equity <= 0:                 # circuit breaker: account blown
            break

    return {
        "net_pnl": round(net_total, 2), "fees": round(fees_total, 2),
        "n_trades": len(sel_trades), "equity": curve, "trades": sel_trades,
    }


def _option_parent(symbol: str) -> str:
    """Underlying for an option contract symbol: NIFTY11AUG2624600CE -> NIFTY."""
    import re as _re4

    m = _re4.match(r"^([A-Za-z]+)", symbol or "")
    return m.group(1).upper() if m else (symbol or "UNKNOWN")


def _group_options_by_parent(per_instrument: list[dict], capital: float, cost: str,
                             start: str | None):
    """Collapse per-contract option runs into ONE account PER UNDERLYING.

    F&O contracts aren't independent instruments — every NIFTY strike/expiry is
    the same underlying. So capital is allocated at the PARENT level (one shared
    single account per underlying, split across underlyings), and the
    per-instrument breakdown shows the parent (NIFTY), not 74 contracts each
    pretending to hold the full capital.
    """
    groups: dict[str, list[dict]] = {}
    for p in per_instrument:
        groups.setdefault(_option_parent(p.get("symbol", "")), []).append(p)
    n_parents = max(len(groups), 1)
    parent_capital = capital / n_parents

    parent_rows: list[dict] = []
    all_trades: list[dict] = []
    total_pnl = total_fees = 0.0
    total_trades = 0
    for parent, contracts in groups.items():
        sa = _single_account_portfolio(contracts, parent_capital, cost)
        total_pnl += sa["net_pnl"]; total_fees += sa["fees"]; total_trades += sa["n_trades"]
        all_trades.extend(sa["trades"])
        eq = sa["equity"] or [{"date": start or "", "value": parent_capital}]
        tdays = len({pt.get("date", "")[:10] for pt in eq if pt.get("date", "")[:10]})
        adays = len({str((t.get("exit_fill") or {}).get("ts", ""))[:10]
                     for t in sa["trades"] if t.get("exit_fill")})
        parent_rows.append({
            "symbol": parent, "exchange": "NFO", "capital_allocated": round(parent_capital, 2),
            "weight": round(1.0 / n_parents, 4), "status": "success",
            "n_contracts": len(contracts), "n_bars": sum(c.get("n_bars", 0) for c in contracts),
            "n_trades": sa["n_trades"], "net_pnl": sa["net_pnl"],
            "pnl_pct": round(sa["net_pnl"] / parent_capital * 100, 2) if parent_capital else 0,
            "fees_total": sa["fees"], "equity": eq,
            "sharpe": _compute_sharpe(eq), "max_drawdown_pct": _compute_max_drawdown(eq),
            "total_trading_days": tdays, "active_trading_days": adays,
            **_compute_recent_metrics(eq, days=30),
        })

    combined: list[dict] = []
    if parent_rows:
        max_len = max(len(r["equity"]) for r in parent_rows)
        for step in range(max_len):
            tv = 0.0; ds = ""
            for r in parent_rows:
                eq = r["equity"]
                if step < len(eq): tv += eq[step].get("value", 0); ds = eq[step].get("date", ds)
                elif eq: tv += eq[-1].get("value", 0)
            combined.append({"date": ds, "value": round(tv, 2)})
    return parent_rows, combined, round(total_pnl, 2), round(total_fees, 2), total_trades, all_trades


def run_multi_instrument_backtest(
    symbols: list[str],
    exchange: str = "NFO",
    interval: str = "15m",
    start: str | None = None,
    end: str | None = None,
    capital: float = DEFAULT_CAPITAL,
    cost: str = "zerodha",
    strategy_key: str = "atr_channel_breakout",
    source: str = "db",
    weights: list[float] | None = None,
) -> dict[str, Any]:
    """Run one strategy across multiple instruments. Capital split by weights."""
    import re as _re
    if not symbols:
        return {"status": "error", "message": "no symbols provided"}
    n = len(symbols)
    if weights is None:
        weights = [1.0 / n] * n
    is_opt = "option" in strategy_key
    per_instrument = []
    total_pnl = total_fees = 0.0
    total_trades = 0
    combined_equity: list[dict[str, Any]] = []
    for i, sym in enumerate(symbols):
        # For options: run each instrument with FULL capital (can only trade 1 at a time)
        # For futures: split capital by weight
        alloc = capital if is_opt else capital * weights[i]
        result = run_backtest(source=source, symbol=sym, exchange=exchange, interval=interval,
                              start=start, end=end, capital=alloc, cost=cost, strategy_key=strategy_key, _save=False)
        m = result.get("metrics", {}) if isinstance(result.get("metrics"), dict) else {}
        pnl = m.get("net_pnl", 0) or 0
        fees = m.get("fees_total", 0) or 0
        trades = m.get("n_trades", 0) or 0
        equity_pts = result.get("equity", [])
        trade_list = result.get("trades", [])
        trading_days = len({pt.get("date", "")[:10] for pt in equity_pts if pt.get("date", "")[:10]})
        active_days = set()
        for t in trade_list:
            ef = t.get("entry_fill", {}) if isinstance(t, dict) else {}
            ts = str(ef.get("ts", ef.get("timestamp", "")))[:10]
            if ts and len(ts) >= 10:
                active_days.add(ts)
        per_instrument.append({
            "symbol": _re.sub(r'FUT$', ' ATM CE/PE', sym) if is_opt and 'FUT' in sym else sym,
            "exchange": exchange, "capital_allocated": alloc, "weight": weights[i],
            "status": result.get("status", "error"), "n_bars": result.get("n_bars", 0),
            "n_trades": trades, "net_pnl": pnl, "pnl_pct": (pnl / alloc * 100) if alloc else 0,
            "fees_total": fees, "metrics": m, "equity": equity_pts,
            "trades": trade_list, "ohlc": result.get("ohlc", []),
            "sharpe": _compute_sharpe(equity_pts),
            "max_drawdown_pct": _compute_max_drawdown(equity_pts),
            "run_id": result.get("run_id"),
            "total_trading_days": trading_days,
            "active_trading_days": len(active_days),
            **_compute_recent_metrics(equity_pts, days=30),
        })
        total_pnl += pnl; total_fees += fees; total_trades += trades
    # For OPTIONS, replace the "sum of N full-capital accounts" with ONE real
    # single-capital account (non-overlapping trades). This is the only figure
    # that reflects the user's actual capital — you can't lose 139k on 10k.
    single_account_trades: list[dict] = []
    if is_opt and per_instrument:
        # Group contracts under their parent underlying; allocate capital per
        # parent (one shared account per underlying), not per contract.
        (per_instrument, combined_equity, total_pnl, total_fees,
         total_trades, single_account_trades) = _group_options_by_parent(
            per_instrument, capital, cost, start)
    elif per_instrument:
        max_len = max(len(r["equity"]) for r in per_instrument)
        for step in range(max_len):
            total_val = 0.0; date_str = ""
            for r in per_instrument:
                eq = r["equity"]
                if step < len(eq): total_val += eq[step].get("value", 0); date_str = eq[step].get("date", date_str)
                elif eq: total_val += eq[-1].get("value", 0)
            combined_equity.append({"date": date_str, "value": total_val})
    strat_entry = STRATEGY_REGISTRY.get(strategy_key, {})
    display_syms = [_re.sub(r'FUT$', ' ATM CE/PE', s) if is_opt and 'FUT' in s else s for s in symbols]
    out = {
        "status": "success", "strategy": strat_entry.get("name", strategy_key),
        "strategy_description": strat_entry.get("description", ""),
        "strategy_source": strat_entry.get("source", ""),
        "symbol": " + ".join(display_syms),
        "symbols": symbols, "exchange": exchange, "interval": interval, "source": source,
        "instrument_type": "Options (real premium, single account)" if is_opt else "Futures",
        "start": start, "end": end, "capital": capital, "total_capital": capital, "cost_model": cost,
        "n_instruments": n, "n_bars": sum(p.get("n_bars", 0) for p in per_instrument),
        "metrics": {
            "net_pnl": round(total_pnl, 2),
            "fees_total": round(total_fees, 2),
            "n_trades": total_trades,
            "sharpe": _compute_sharpe(combined_equity),
            "max_drawdown_pct": _compute_max_drawdown(combined_equity),
        },
        "portfolio_metrics": {
            "total_pnl": round(total_pnl, 2), "total_pnl_pct": round(total_pnl / capital * 100, 2) if capital else 0,
            "total_fees": round(total_fees, 2), "total_trades": total_trades,
        },
        "equity": combined_equity,
        "per_instrument": [{k: v for k, v in p.items() if k not in ("equity", "trades", "ohlc")} for p in per_instrument],
        "combined_equity": combined_equity,
        # Options: the realistic single-account (non-overlapping) trades; futures: all.
        "trades": single_account_trades if (is_opt and single_account_trades)
                  else [t for p in per_instrument for t in (p.get("trades") or [])],
        "ohlc": [b for p in per_instrument[:1] for b in (p.get("ohlc") or [])],
    }

    if combined_equity and len(combined_equity) >= 2:
        try:
            first_val = combined_equity[0].get("value", capital)
            last_val = combined_equity[-1].get("value", capital)
            from datetime import datetime as _dt
            first_d = _dt.fromisoformat(combined_equity[0].get("date", "2026-01-01"))
            last_d = _dt.fromisoformat(combined_equity[-1].get("date", "2026-08-07"))
            days = (last_d - first_d).total_seconds() / 86400
            if days > 0 and first_val > 0:
                total_return = (last_val / first_val) - 1.0
                years = days / 365.25
                if years > 0 and total_return > -1:
                    out["xirr_pct"] = round(((1 + total_return) ** (1 / years) - 1) * 100, 2)
                elif years > 0:
                    out["xirr_pct"] = -100.0
        except Exception:
            pass

    total_margin = 0
    for p in per_instrument:
        alloc = p.get("capital_allocated", 0)
        if alloc > 0:
            total_margin += alloc * 0.12
    if total_margin > 0:
        out["margin_used"] = round(total_margin, 0)
        if total_pnl != 0:
            out["return_on_margin_pct"] = round((total_pnl / total_margin) * 100, 2)

    try:
        from database.backtest_db import save_backtest_run
        run_id = save_backtest_run(out)
        if run_id:
            out["run_id"] = run_id
    except Exception:
        logger.debug("failed to persist multi-instrument backtest", exc_info=True)

    return out
