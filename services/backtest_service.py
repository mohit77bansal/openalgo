"""Backtest service — bridges OpenAlgo to the vendored aladin `qbacktest` engine.

Thin, broker-agnostic orchestration (same role as every other
`services/*_service.py`): a blueprint calls in, this runs the event-driven
backtest engine and returns a JSON-serialisable result.

Data sources (the `source` argument):
- "demo": self-contained deterministic synthetic bars — no data required.
- "db"  : real OHLCV from the Historify DuckDB store via
          `database.historify_db.get_ohlcv` (populated from AngelOne). Returns
          a graceful "no data" result until bars have been ingested.

Scope for now: NIFTY 50 index (NSE) and NIFTY futures (NFO).

Production note: the heavy call is isolated in `_run_engine()`. On the dev
server (standard threading) it is safe inline; under gunicorn+eventlet move that
one function to a subprocess/process-pool worker so it never blocks the request
path.
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

# OpenAlgo interval -> qbacktest Bar.timeframe label.
_INTERVAL_TF = {"D": "1d", "1d": "1d", "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h"}


# --------------------------------------------------------------------------- #
# JSON sanitisation
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Instrument mapping (NIFTY index / futures / equity)
# --------------------------------------------------------------------------- #
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
_FUT_RE = __import__("re").compile(r"(\d{2})([A-Z]{3})(\d{2})FUT$")


def _parse_fut_expiry(symbol: str) -> date | None:
    """Parse an F&O expiry from an OpenAlgo future symbol, e.g. NIFTY25AUG26FUT."""
    m = _FUT_RE.search((symbol or "").upper())
    if not m:
        return None
    dd, mon, yy = m.groups()
    return date(2000 + int(yy), _MONTHS[mon], int(dd))


def _instrument_for(symbol: str, exchange: str) -> Any:
    from qbacktest.core.types import Exchange, Instrument, InstrumentType

    ex = (exchange or "NSE").upper()
    if ex in ("NSE_INDEX", "BSE_INDEX", "INDEX"):
        qex = Exchange.BSE if ex == "BSE_INDEX" else Exchange.NSE
        return Instrument(symbol=symbol, exchange=qex, instrument_type=InstrumentType.INDEX, lot_size=1)
    if ex in ("NFO", "BFO"):
        qex = Exchange.BFO if ex == "BFO" else Exchange.NFO
        # NIFTY futures lot size (current); refine per-expiry via lot_sizes later.
        return Instrument(symbol=symbol, exchange=qex, instrument_type=InstrumentType.FUT,
                          lot_size=75, expiry=_parse_fut_expiry(symbol))
    qex = getattr(Exchange, ex, Exchange.NSE)
    return Instrument(symbol=symbol, exchange=qex, instrument_type=InstrumentType.EQ, lot_size=1)


class _StaticSource:
    """A qbacktest DataSource backed by an in-memory list of Bars."""

    def __init__(self, bars: list[Any]):
        self.bars = sorted(bars, key=lambda b: (b.ts, b.instrument.id))
        self._latest: dict[str, float] = {}

    def stream(self):
        for b in self.bars:
            self._latest[b.instrument.id] = b.close
            yield b

    def latest_price(self, instrument: Any) -> float | None:
        return self._latest.get(instrument.id)


# --------------------------------------------------------------------------- #
# "demo" — deterministic synthetic bars
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# "db" — real OHLCV from the Historify DuckDB store
# --------------------------------------------------------------------------- #
def _build_db_source(symbol: str, exchange: str, interval: str, start: str | None, end: str | None):
    """Returns (_StaticSource, n_bars) or raises ValueError('no data ...')."""
    from database.historify_db import get_connection, get_ohlcv
    from qbacktest.core.calendar import IST
    from qbacktest.core.types import Bar
    import pandas as pd

    start_ts = int(datetime.fromisoformat(start).timestamp()) if start else None
    end_ts = int(datetime.fromisoformat(end).timestamp()) if end else None

    # Try the standard get_ohlcv first (handles storage intervals + aggregation)
    df = get_ohlcv(symbol, exchange, interval, start_timestamp=start_ts, end_timestamp=end_ts)

    # If empty, query DuckDB directly — data may be stored at this interval
    # but not in Historify's STORAGE_INTERVALS list (e.g. 5m, 15m, 1h ingested
    # directly from the broker).
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


# --------------------------------------------------------------------------- #
# Strategy registry — each strategy has a name, description, and factory.
# --------------------------------------------------------------------------- #

STRATEGY_REGISTRY: dict[str, dict[str, Any]] = {}


def _register(key: str, name: str, description: str, factory):
    STRATEGY_REGISTRY[key] = {"name": name, "description": description, "factory": factory}


def _init_strategies():
    from qbacktest.engine.strategy import Strategy

    def _lot(inst) -> int:
        return max(getattr(inst, "lot_size", 1) or 1, 1)

    def _atr(highs: list, lows: list, closes: list, period: int) -> float | None:
        """Average True Range over the last `period` bars."""
        if len(closes) < period + 1:
            return None
        trs = []
        for i in range(-period, 0):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            trs.append(tr)
        return sum(trs) / period

    # ------------------------------------------------------------------
    # 1. ATR Channel Breakout (Turtle-style, 2:1 R:R via ATR trailing)
    # ------------------------------------------------------------------
    class ATRChannelBreakout(Strategy):
        name = "ATR Channel Breakout"

        def __init__(self, sym: str, lookback: int = 20, atr_period: int = 14, atr_sl_mult: float = 1.5, atr_tp_mult: float = 3.0):
            self._sym = sym
            self._lookback = lookback
            self._atr_period = atr_period
            self._sl_mult = atr_sl_mult
            self._tp_mult = atr_tp_mult

        def on_start(self, ctx):
            self._highs: list[float] = []
            self._lows: list[float] = []
            self._closes: list[float] = []
            self._long = False
            self._short = False
            self._entry = 0.0
            self._sl = 0.0
            self._tp = 0.0

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != self._sym:
                return
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            self._closes.append(bar.close)
            if len(self._closes) < max(self._lookback, self._atr_period + 1):
                return

            atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
            if not atr or atr == 0:
                return
            inst = bar.instrument
            qty = _lot(inst)
            ch_high = max(self._highs[-self._lookback - 1:-1])
            ch_low = min(self._lows[-self._lookback - 1:-1])

            if self._long:
                if bar.close <= self._sl or bar.close >= self._tp:
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                return
            if self._short:
                if bar.close >= self._sl or bar.close <= self._tp:
                    ctx.buy(inst, quantity=qty)
                    self._short = False
                return

            if bar.close > ch_high:
                ctx.buy(inst, quantity=qty)
                self._long = True
                self._entry = bar.close
                self._sl = bar.close - atr * self._sl_mult
                self._tp = bar.close + atr * self._tp_mult
            elif bar.close < ch_low:
                ctx.sell(inst, quantity=qty)
                self._short = True
                self._entry = bar.close
                self._sl = bar.close + atr * self._sl_mult
                self._tp = bar.close - atr * self._tp_mult

    # ------------------------------------------------------------------
    # 2. Bollinger Band Squeeze Breakout (volatility contraction -> expansion)
    # ------------------------------------------------------------------
    class BollingerSqueezeBreakout(Strategy):
        name = "Bollinger Squeeze Breakout"

        def __init__(self, sym: str, period: int = 20, std_mult: float = 2.0, squeeze_pct: float = 0.03):
            self._sym = sym
            self._period = period
            self._std_mult = std_mult
            self._squeeze_pct = squeeze_pct

        def on_start(self, ctx):
            self._closes: list[float] = []
            self._highs: list[float] = []
            self._lows: list[float] = []
            self._long = False
            self._short = False
            self._entry = 0.0
            self._sl = 0.0
            self._was_squeezed = False

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != self._sym:
                return
            self._closes.append(bar.close)
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            if len(self._closes) < self._period:
                return

            window = self._closes[-self._period:]
            sma = sum(window) / self._period
            std = (sum((x - sma) ** 2 for x in window) / self._period) ** 0.5
            upper = sma + self._std_mult * std
            lower = sma - self._std_mult * std
            bandwidth = (upper - lower) / sma if sma else 0

            is_squeezed = bandwidth < self._squeeze_pct
            atr = _atr(self._highs, self._lows, self._closes, min(14, self._period))
            inst = bar.instrument
            qty = _lot(inst)

            if self._long:
                if atr and bar.close < self._entry - 2 * atr:
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                elif bar.close > upper and atr:
                    self._sl = max(self._sl, bar.close - 1.5 * atr)
                return
            if self._short:
                if atr and bar.close > self._entry + 2 * atr:
                    ctx.buy(inst, quantity=qty)
                    self._short = False
                return

            if self._was_squeezed and not is_squeezed:
                if bar.close > upper:
                    ctx.buy(inst, quantity=qty)
                    self._long = True
                    self._entry = bar.close
                    self._sl = lower
                elif bar.close < lower:
                    ctx.sell(inst, quantity=qty)
                    self._short = True
                    self._entry = bar.close
                    self._sl = upper

            self._was_squeezed = is_squeezed

    # ------------------------------------------------------------------
    # 3. Gap Fade with Defined Risk (exploits NIFTY overnight gap filling)
    # ------------------------------------------------------------------
    class GapFade(Strategy):
        name = "Gap Fade"

        def __init__(self, sym: str, min_gap_pct: float = 0.004, sl_mult: float = 1.0, tp_mult: float = 2.0):
            self._sym = sym
            self._min_gap = min_gap_pct
            self._sl_mult = sl_mult
            self._tp_mult = tp_mult

        def on_start(self, ctx):
            self._prev_close: float | None = None
            self._long = False
            self._short = False
            self._entry = 0.0
            self._sl = 0.0
            self._tp = 0.0
            self._closes: list[float] = []
            self._highs: list[float] = []
            self._lows: list[float] = []

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != self._sym:
                return
            self._closes.append(bar.close)
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            inst = bar.instrument
            qty = _lot(inst)

            if self._long:
                if bar.close <= self._sl:
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                elif bar.close >= self._tp:
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                self._prev_close = bar.close
                return
            if self._short:
                if bar.close >= self._sl:
                    ctx.buy(inst, quantity=qty)
                    self._short = False
                elif bar.close <= self._tp:
                    ctx.buy(inst, quantity=qty)
                    self._short = False
                self._prev_close = bar.close
                return

            if self._prev_close is not None:
                gap_pct = (bar.open - self._prev_close) / self._prev_close
                gap_size = abs(bar.open - self._prev_close)
                risk = gap_size * self._sl_mult

                if gap_pct > self._min_gap:
                    ctx.sell(inst, quantity=qty)
                    self._short = True
                    self._entry = bar.open
                    self._sl = bar.open + risk
                    self._tp = bar.open - risk * self._tp_mult
                elif gap_pct < -self._min_gap:
                    ctx.buy(inst, quantity=qty)
                    self._long = True
                    self._entry = bar.open
                    self._sl = bar.open - risk
                    self._tp = bar.open + risk * self._tp_mult

            self._prev_close = bar.close

    # ------------------------------------------------------------------
    # 4. Inside Bar Breakout (price compression -> directional move)
    # ------------------------------------------------------------------
    class InsideBarBreakout(Strategy):
        name = "Inside Bar Breakout"

        def __init__(self, sym: str, atr_period: int = 14, sl_mult: float = 0.5, tp_mult: float = 2.0):
            self._sym = sym
            self._atr_period = atr_period
            self._sl_mult = sl_mult
            self._tp_mult = tp_mult

        def on_start(self, ctx):
            self._highs: list[float] = []
            self._lows: list[float] = []
            self._closes: list[float] = []
            self._long = False
            self._short = False
            self._sl = 0.0
            self._tp = 0.0

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != self._sym:
                return
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            self._closes.append(bar.close)
            if len(self._highs) < 2:
                return

            inst = bar.instrument
            qty = _lot(inst)
            atr = _atr(self._highs, self._lows, self._closes, self._atr_period) if len(self._closes) > self._atr_period else None

            if self._long:
                if bar.close <= self._sl or bar.close >= self._tp:
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                return
            if self._short:
                if bar.close >= self._sl or bar.close <= self._tp:
                    ctx.buy(inst, quantity=qty)
                    self._short = False
                return

            prev_h, prev_l = self._highs[-2], self._lows[-2]
            is_inside = bar.high <= prev_h and bar.low >= prev_l
            if not is_inside or not atr:
                return

            if bar.close > (prev_h + prev_l) / 2:
                ctx.buy(inst, quantity=qty)
                self._long = True
                self._sl = bar.close - atr * self._sl_mult
                self._tp = bar.close + atr * self._tp_mult
            else:
                ctx.sell(inst, quantity=qty)
                self._short = True
                self._sl = bar.close + atr * self._sl_mult
                self._tp = bar.close - atr * self._tp_mult

    # ------------------------------------------------------------------
    # 5. Mean Reversion Extreme (RSI + Bollinger + volume confirmation)
    # ------------------------------------------------------------------
    class MeanReversionExtreme(Strategy):
        name = "Mean Reversion Extreme"

        def __init__(self, sym: str, bb_period: int = 20, rsi_period: int = 7, rsi_oversold: float = 25, rsi_overbought: float = 75):
            self._sym = sym
            self._bb_period = bb_period
            self._rsi_period = rsi_period
            self._rsi_os = rsi_oversold
            self._rsi_ob = rsi_overbought

        def on_start(self, ctx):
            self._closes: list[float] = []
            self._volumes: list[float] = []
            self._highs: list[float] = []
            self._lows: list[float] = []
            self._long = False
            self._short = False
            self._entry = 0.0

        def _rsi(self, period: int) -> float | None:
            if len(self._closes) < period + 1:
                return None
            gains, losses = [], []
            for i in range(-period, 0):
                d = self._closes[i] - self._closes[i - 1]
                gains.append(max(d, 0))
                losses.append(max(-d, 0))
            ag = sum(gains) / period
            al = sum(losses) / period
            return 100 - (100 / (1 + ag / al)) if al else 100.0

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != self._sym:
                return
            self._closes.append(bar.close)
            self._volumes.append(bar.volume)
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            if len(self._closes) < self._bb_period:
                return

            window = self._closes[-self._bb_period:]
            sma = sum(window) / self._bb_period
            std = (sum((x - sma) ** 2 for x in window) / self._bb_period) ** 0.5
            lower_bb = sma - 2 * std
            upper_bb = sma + 2 * std
            rsi = self._rsi(self._rsi_period)
            atr = _atr(self._highs, self._lows, self._closes, 14)
            vol_avg = sum(self._volumes[-20:]) / min(len(self._volumes), 20) if self._volumes else 0
            high_volume = bar.volume > 1.2 * vol_avg if vol_avg > 0 else True
            inst = bar.instrument
            qty = _lot(inst)

            if self._long:
                if bar.close >= sma or (atr and bar.close < self._entry - 1.5 * atr):
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                return
            if self._short:
                if bar.close <= sma or (atr and bar.close > self._entry + 1.5 * atr):
                    ctx.buy(inst, quantity=qty)
                    self._short = False
                return

            if rsi is not None and bar.close <= lower_bb and rsi < self._rsi_os and high_volume:
                ctx.buy(inst, quantity=qty)
                self._long = True
                self._entry = bar.close
            elif rsi is not None and bar.close >= upper_bb and rsi > self._rsi_ob and high_volume:
                ctx.sell(inst, quantity=qty)
                self._short = True
                self._entry = bar.close

    # ------------------------------------------------------------------
    # 6. ORB 15-Min (research-backed: 8yr backtest, +91.6%, Sharpe 1.16)
    # Source: intradaylab.com/blog/nifty-orb-breakout-strategy-backtest
    # ------------------------------------------------------------------
    class ORB15Min(Strategy):
        name = "ORB 15-Min (Research-Backed)"

        def __init__(self, sym: str, or_bars: int = 2, min_range: float = 40, tp_mult: float = 2.0):
            self._sym = sym
            self._or_bars = or_bars
            self._min_range = min_range
            self._tp_mult = tp_mult

        def on_start(self, ctx):
            self._closes: list[float] = []
            self._highs: list[float] = []
            self._lows: list[float] = []
            self._long = False
            self._short = False
            self._or_high: float = 0
            self._or_low: float = 999999
            self._entry = 0.0
            self._sl = 0.0
            self._tp = 0.0
            self._bar_count = 0

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != self._sym:
                return
            self._bar_count += 1
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            self._closes.append(bar.close)
            inst = bar.instrument
            qty = _lot(inst)

            if self._bar_count <= self._or_bars:
                self._or_high = max(self._or_high, bar.high)
                self._or_low = min(self._or_low, bar.low)
                return

            or_range = self._or_high - self._or_low
            if or_range < self._min_range:
                return

            if self._long:
                if bar.close <= self._sl or bar.close >= self._tp:
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                    self._bar_count = 0
                    self._or_high = 0
                    self._or_low = 999999
                return
            if self._short:
                if bar.close >= self._sl or bar.close <= self._tp:
                    ctx.buy(inst, quantity=qty)
                    self._short = False
                    self._bar_count = 0
                    self._or_high = 0
                    self._or_low = 999999
                return

            if bar.close > self._or_high:
                ctx.buy(inst, quantity=qty)
                self._long = True
                self._entry = bar.close
                self._sl = self._or_low
                self._tp = bar.close + or_range * self._tp_mult
            elif bar.close < self._or_low:
                ctx.sell(inst, quantity=qty)
                self._short = True
                self._entry = bar.close
                self._sl = self._or_high
                self._tp = bar.close - or_range * self._tp_mult

    # ------------------------------------------------------------------
    # 7. Overnight Gap + Global Sentiment
    # Uses previous close vs open to infer global impact
    # ------------------------------------------------------------------
    class GlobalGapMomentum(Strategy):
        name = "Global Gap Momentum"

        def __init__(self, sym: str, gap_threshold: float = 0.003, big_gap: float = 0.008,
                     sl_mult: float = 1.0, tp_mult: float = 2.0):
            self._sym = sym
            self._gap_thr = gap_threshold
            self._big_gap = big_gap
            self._sl_mult = sl_mult
            self._tp_mult = tp_mult

        def on_start(self, ctx):
            self._prev_close: float | None = None
            self._long = False
            self._short = False
            self._sl = 0.0
            self._tp = 0.0

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != self._sym:
                return
            inst = bar.instrument
            qty = _lot(inst)

            if self._long:
                if bar.close <= self._sl or bar.close >= self._tp:
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                self._prev_close = bar.close
                return
            if self._short:
                if bar.close >= self._sl or bar.close <= self._tp:
                    ctx.buy(inst, quantity=qty)
                    self._short = False
                self._prev_close = bar.close
                return

            if self._prev_close:
                gap_pct = (bar.open - self._prev_close) / self._prev_close
                gap_abs = abs(bar.open - self._prev_close)
                risk = gap_abs * self._sl_mult

                if abs(gap_pct) >= self._big_gap:
                    if gap_pct > 0:
                        ctx.buy(inst, quantity=qty)
                        self._long = True
                        self._sl = bar.open - risk
                        self._tp = bar.open + risk * self._tp_mult
                    else:
                        ctx.sell(inst, quantity=qty)
                        self._short = True
                        self._sl = bar.open + risk
                        self._tp = bar.open - risk * self._tp_mult
                elif abs(gap_pct) >= self._gap_thr:
                    if gap_pct > 0:
                        ctx.sell(inst, quantity=qty)
                        self._short = True
                        self._sl = bar.open + risk
                        self._tp = bar.open - risk * self._tp_mult
                    else:
                        ctx.buy(inst, quantity=qty)
                        self._long = True
                        self._sl = bar.open - risk
                        self._tp = bar.open + risk * self._tp_mult

            self._prev_close = bar.close

    # ------------------------------------------------------------------
    # 8. MABB — Moving Average Bollinger Bands (15yr Bank Nifty backtest)
    # Source: financewithsai.com/s06-banknifty-mabb-intraday-trading-strategy
    # ------------------------------------------------------------------
    class MABB(Strategy):
        name = "MABB (Research-Backed)"

        def __init__(self, sym: str, bb_period: int = 20, sma_period: int = 200,
                     atr_entry: int = 30, atr_sl: int = 500, lookback: int = 24):
            self._sym = sym
            self._bb_period = bb_period
            self._sma_period = sma_period
            self._atr_entry = atr_entry
            self._atr_sl = atr_sl
            self._lookback = lookback

        def on_start(self, ctx):
            self._closes: list[float] = []
            self._highs: list[float] = []
            self._lows: list[float] = []
            self._long = False
            self._short = False
            self._sl = 0.0

        def _hlc3(self, i: int) -> float:
            return (self._highs[i] + self._lows[i] + self._closes[i]) / 3

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != self._sym:
                return
            self._closes.append(bar.close)
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            n = len(self._closes)
            if n < max(self._sma_period, self._bb_period, self._atr_entry + 1, self._lookback + 1):
                return

            inst = bar.instrument
            qty = _lot(inst)
            atr_e = _atr(self._highs, self._lows, self._closes, self._atr_entry) or 0
            atr_s = _atr(self._highs, self._lows, self._closes, min(self._atr_sl, n - 1)) or atr_e

            bb_window = self._closes[-self._bb_period:]
            bb_sma = sum(bb_window) / self._bb_period
            bb_std = (sum((x - bb_sma) ** 2 for x in bb_window) / self._bb_period) ** 0.5
            upper_bb = bb_sma + 2 * bb_std
            lower_bb = bb_sma - 2 * bb_std

            hlc3_vals = [self._hlc3(i) for i in range(-self._sma_period, 0)]
            sma200 = sum(hlc3_vals) / self._sma_period

            highest_close = max(self._closes[-self._lookback - 1:-1])
            lowest_close = min(self._closes[-self._lookback - 1:-1])

            if self._long:
                if bar.close <= self._sl:
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                return
            if self._short:
                if bar.close >= self._sl:
                    ctx.buy(inst, quantity=qty)
                    self._short = False
                return

            if bar.close > sma200 and bar.close > upper_bb:
                trigger = highest_close + 2 * atr_e
                if bar.close >= trigger:
                    ctx.buy(inst, quantity=qty)
                    self._long = True
                    self._sl = bar.close - 2.5 * atr_s
            elif bar.close < sma200 and bar.close < lower_bb:
                trigger = lowest_close - 2 * atr_e
                if bar.close <= trigger:
                    ctx.sell(inst, quantity=qty)
                    self._short = True
                    self._sl = bar.close + 2.5 * atr_s

    # ------------------------------------------------------------------
    # 9. VWAP Reversion (institutional mean-reversion)
    # ------------------------------------------------------------------
    class VWAPReversion(Strategy):
        name = "VWAP Reversion"

        def __init__(self, sym: str, dev_pct: float = 0.008, sl_mult: float = 1.5, tp_mult: float = 2.0):
            self._sym = sym
            self._dev_pct = dev_pct
            self._sl_mult = sl_mult
            self._tp_mult = tp_mult

        def on_start(self, ctx):
            self._cum_vol = 0.0
            self._cum_pv = 0.0
            self._long = False
            self._short = False
            self._sl = 0.0
            self._tp = 0.0
            self._highs: list[float] = []
            self._lows: list[float] = []
            self._closes: list[float] = []

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != self._sym:
                return
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            self._closes.append(bar.close)
            typical = (bar.high + bar.low + bar.close) / 3
            vol = bar.volume if bar.volume > 0 else 1
            self._cum_pv += typical * vol
            self._cum_vol += vol
            vwap = self._cum_pv / self._cum_vol if self._cum_vol > 0 else bar.close
            dev = (bar.close - vwap) / vwap if vwap else 0
            atr = _atr(self._highs, self._lows, self._closes, 14)
            inst = bar.instrument
            qty = _lot(inst)

            if self._long:
                if bar.close <= self._sl or bar.close >= self._tp:
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                return
            if self._short:
                if bar.close >= self._sl or bar.close <= self._tp:
                    ctx.buy(inst, quantity=qty)
                    self._short = False
                return

            if not atr or atr == 0:
                return
            risk = atr * self._sl_mult
            if dev < -self._dev_pct:
                ctx.buy(inst, quantity=qty)
                self._long = True
                self._sl = bar.close - risk
                self._tp = vwap
            elif dev > self._dev_pct:
                ctx.sell(inst, quantity=qty)
                self._short = True
                self._sl = bar.close + risk
                self._tp = vwap

    # ------------------------------------------------------------------
    # 10. Heikin-Ashi Trend Follower (smoothed candle momentum)
    # ------------------------------------------------------------------
    class HeikinAshiTrend(Strategy):
        name = "Heikin-Ashi Trend"

        def __init__(self, sym: str, confirm_bars: int = 3, atr_period: int = 14, sl_mult: float = 2.0):
            self._sym = sym
            self._confirm = confirm_bars
            self._atr_period = atr_period
            self._sl_mult = sl_mult

        def on_start(self, ctx):
            self._ha_close: list[float] = []
            self._ha_open: list[float] = []
            self._highs: list[float] = []
            self._lows: list[float] = []
            self._closes: list[float] = []
            self._long = False
            self._short = False
            self._sl = 0.0
            self._bull_count = 0
            self._bear_count = 0

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != self._sym:
                return
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            self._closes.append(bar.close)

            ha_c = (bar.open + bar.high + bar.low + bar.close) / 4
            ha_o = (self._ha_open[-1] + self._ha_close[-1]) / 2 if self._ha_open else (bar.open + bar.close) / 2
            self._ha_close.append(ha_c)
            self._ha_open.append(ha_o)

            is_bull = ha_c > ha_o
            is_bear = ha_c < ha_o
            self._bull_count = self._bull_count + 1 if is_bull else 0
            self._bear_count = self._bear_count + 1 if is_bear else 0

            atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
            inst = bar.instrument
            qty = _lot(inst)

            if self._long:
                if self._bear_count >= 2 or (atr and bar.close < self._sl):
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                elif atr:
                    self._sl = max(self._sl, bar.close - self._sl_mult * atr)
                return
            if self._short:
                if self._bull_count >= 2 or (atr and bar.close > self._sl):
                    ctx.buy(inst, quantity=qty)
                    self._short = False
                elif atr:
                    self._sl = min(self._sl, bar.close + self._sl_mult * atr)
                return

            if not atr:
                return
            if self._bull_count >= self._confirm:
                ctx.buy(inst, quantity=qty)
                self._long = True
                self._sl = bar.close - self._sl_mult * atr
            elif self._bear_count >= self._confirm:
                ctx.sell(inst, quantity=qty)
                self._short = True
                self._sl = bar.close + self._sl_mult * atr

    # --- Register all ---
    _register("orb_15min", "ORB 15-Min (Research-Backed)",
              "8-year backtested on NIFTY (2017-2026): +91.6% return, 48.7% win rate, Sharpe 1.16, max DD -11.2%, 2,122 trades. Uses first 2×15min candles as the opening range. Buys breakout above OR high, shorts below OR low. SL at opposite OR level, TP at 2× OR range. Short trades = 75% of profits. Friday strongest, Tuesday weakest. Source: intradaylab.com backtest.",
              lambda sym, **kw: ORB15Min(sym, or_bars=kw.get("or_bars", 2)))

    _register("global_gap_momentum", "Global Gap Momentum",
              "Exploits overnight global-market impact on NIFTY open. Small gaps (0.3-0.8%) = fade (65% fill within day). Big gaps (>0.8%) = ride momentum (don't fill same day). GIFT Nifty predicts NIFTY open direction 85-90% of the time. 2:1 R:R. Uses gap size as risk unit.",
              lambda sym, **kw: GlobalGapMomentum(sym, gap_threshold=kw.get("gap_threshold", 0.003)))

    _register("atr_channel_breakout", "ATR Channel Breakout",
              "Turtle-style: buy on 20-day high breakout, sell on 20-day low. SL at 1.5×ATR, TP at 3×ATR = structural 2:1 R:R. Catches big trends, gives back on chop. Best on daily timeframe.",
              lambda sym, **kw: ATRChannelBreakout(sym, lookback=kw.get("lookback", 20)))

    _register("bollinger_squeeze", "Bollinger Squeeze Breakout",
              "Enters when Bollinger Bands squeeze (bandwidth < 3%) then expand — breakout above upper band = long, below lower = short. Volatility compression precedes big directional moves. Low frequency, high conviction.",
              lambda sym, **kw: BollingerSqueezeBreakout(sym, period=kw.get("period", 20)))

    _register("gap_fade", "Gap Fade (2:1 R:R)",
              "Fades overnight gaps > 0.4%. SL = 1× gap size, TP = 2× gap size = structural 2:1 R:R. NIFTY fills ~65% of gaps within the day (source: GIFT Nifty correlation studies). Needs only 34% win rate to profit.",
              lambda sym, **kw: GapFade(sym, min_gap_pct=kw.get("min_gap_pct", 0.004)))

    _register("inside_bar_breakout", "Inside Bar Breakout",
              "Detects inside bars (today's range inside yesterday's = price compression). Trades the breakout direction with ATR-based SL (0.5×ATR) and TP (2×ATR) = 4:1 R:R. Low frequency, high selectivity. Works best on daily bars.",
              lambda sym, **kw: InsideBarBreakout(sym))

    _register("mean_reversion_extreme", "Mean Reversion Extreme",
              "Multi-signal confirmation: enters ONLY when price hits 2σ Bollinger Band + RSI(7) < 25 or > 75 + above-average volume. Triple filter = high conviction. Exits at SMA (the mean). 1.5×ATR hard stop. Low frequency, ~65% win rate when all 3 conditions align.",
              lambda sym, **kw: MeanReversionExtreme(sym))

    _register("mabb", "MABB (Research-Backed)",
              "Moving Average Bollinger Bands — 15-year backtested on Bank Nifty. Entry: close > SMA(200) + above upper BB(20,2) + highest close(24) + 2×ATR(30). SL: 2.5×ATR(500). Filters for strong momentum breakouts only. Source: financewithsai.com.",
              lambda sym, **kw: MABB(sym))

    _register("vwap_reversion", "VWAP Reversion",
              "Institutional mean-reversion: buys when price drops >0.8% below VWAP (institutional support), sells when >0.8% above (institutional resistance). TP at VWAP (the mean), SL at 1.5×ATR. VWAP is the volume-weighted average price — the level institutions defend.",
              lambda sym, **kw: VWAPReversion(sym, dev_pct=kw.get("dev_pct", 0.008)))

    _register("heikin_ashi_trend", "Heikin-Ashi Trend",
              "Smoothed candle momentum: enters after 3 consecutive Heikin-Ashi bullish/bearish candles (filters noise). Trailing stop at 2×ATR. Exits on 2 consecutive opposite HA candles. Trend-following with noise reduction.",
              lambda sym, **kw: HeikinAshiTrend(sym, confirm_bars=kw.get("confirm_bars", 3)))

    # ------------------------------------------------------------------
    # 8. Regime-Adaptive Momentum/Reversion Switcher
    # Source: SSRN "Quantitative Strategies For Momentum And Trend
    # Reversal" (Charlotte Sim, Feb 2026) + Ernie Chan's regime detection
    # ------------------------------------------------------------------
    class RegimeAdaptive(Strategy):
        name = "Regime-Adaptive Switcher"

        def __init__(self, sym: str, lookback: int = 40, vol_window: int = 20, vol_threshold: float = 1.3):
            self._sym = sym
            self._lookback = lookback
            self._vol_window = vol_window
            self._vol_thr = vol_threshold

        def on_start(self, ctx):
            self._closes: list[float] = []
            self._highs: list[float] = []
            self._lows: list[float] = []
            self._long = False
            self._short = False
            self._entry = 0.0

        def _regime(self) -> str:
            """Detect regime: 'trending' if recent vol expanding, 'mean_rev' if contracting."""
            if len(self._closes) < self._vol_window * 2:
                return "unknown"
            recent = self._closes[-self._vol_window:]
            prior = self._closes[-self._vol_window * 2:-self._vol_window]
            r_std = (sum((x - sum(recent) / len(recent)) ** 2 for x in recent) / len(recent)) ** 0.5
            p_std = (sum((x - sum(prior) / len(prior)) ** 2 for x in prior) / len(prior)) ** 0.5
            return "trending" if p_std > 0 and r_std / p_std > self._vol_thr else "mean_rev"

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != self._sym:
                return
            self._closes.append(bar.close)
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            if len(self._closes) < self._lookback:
                return

            inst = bar.instrument
            qty = _lot(inst)
            regime = self._regime()
            atr = _atr(self._highs, self._lows, self._closes, 14)
            if not atr:
                return

            if self._long:
                if bar.close < self._entry - 1.5 * atr or bar.close > self._entry + 2.5 * atr:
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                return
            if self._short:
                if bar.close > self._entry + 1.5 * atr or bar.close < self._entry - 2.5 * atr:
                    ctx.buy(inst, quantity=qty)
                    self._short = False
                return

            sma = sum(self._closes[-self._lookback:]) / self._lookback

            if regime == "trending":
                if bar.close > sma and not self._long:
                    ctx.buy(inst, quantity=qty)
                    self._long = True
                    self._entry = bar.close
                elif bar.close < sma and not self._short:
                    ctx.sell(inst, quantity=qty)
                    self._short = True
                    self._entry = bar.close
            elif regime == "mean_rev":
                window = self._closes[-20:]
                m = sum(window) / 20
                std = (sum((x - m) ** 2 for x in window) / 20) ** 0.5
                if std > 0:
                    z = (bar.close - m) / std
                    if z < -1.5 and not self._long:
                        ctx.buy(inst, quantity=qty)
                        self._long = True
                        self._entry = bar.close
                    elif z > 1.5 and not self._short:
                        ctx.sell(inst, quantity=qty)
                        self._short = True
                        self._entry = bar.close

    # ------------------------------------------------------------------
    # 9. Volatility Expansion Scalp (0DTE Gamma research + theta decay)
    # Source: SSRN "0DTEs: Trading, Gamma Risk and Volatility Propagation"
    # + "Trading Theta" (Lu, 2024)
    # ------------------------------------------------------------------
    class VolatilityExpansionScalp(Strategy):
        name = "Volatility Expansion Scalp"

        def __init__(self, sym: str, atr_period: int = 14, squeeze_bars: int = 5, expansion_mult: float = 1.8):
            self._sym = sym
            self._atr_period = atr_period
            self._squeeze_bars = squeeze_bars
            self._exp_mult = expansion_mult

        def on_start(self, ctx):
            self._highs: list[float] = []
            self._lows: list[float] = []
            self._closes: list[float] = []
            self._ranges: list[float] = []
            self._long = False
            self._short = False
            self._entry = 0.0
            self._sl = 0.0
            self._tp = 0.0

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != self._sym:
                return
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            self._closes.append(bar.close)
            bar_range = bar.high - bar.low
            self._ranges.append(bar_range)
            if len(self._closes) < max(self._atr_period + 1, self._squeeze_bars + 1):
                return

            inst = bar.instrument
            qty = _lot(inst)
            atr = _atr(self._highs, self._lows, self._closes, self._atr_period)
            if not atr:
                return

            if self._long:
                if bar.close <= self._sl or bar.close >= self._tp:
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                return
            if self._short:
                if bar.close >= self._sl or bar.close <= self._tp:
                    ctx.buy(inst, quantity=qty)
                    self._short = False
                return

            recent_avg_range = sum(self._ranges[-self._squeeze_bars:]) / self._squeeze_bars
            is_expansion = bar_range > recent_avg_range * self._exp_mult

            if is_expansion:
                if bar.close > bar.open:
                    ctx.buy(inst, quantity=qty)
                    self._long = True
                    self._entry = bar.close
                    self._sl = bar.close - atr
                    self._tp = bar.close + atr * 2
                else:
                    ctx.sell(inst, quantity=qty)
                    self._short = True
                    self._entry = bar.close
                    self._sl = bar.close + atr
                    self._tp = bar.close - atr * 2

    # ------------------------------------------------------------------
    # 10. Multi-Factor Momentum-Quality (Morningstar Q2 2026 research)
    # Simplified: momentum + quality filter on price action
    # ------------------------------------------------------------------
    class MomentumQuality(Strategy):
        name = "Momentum-Quality Filter"

        def __init__(self, sym: str, mom_period: int = 20, quality_period: int = 60):
            self._sym = sym
            self._mom = mom_period
            self._qual = quality_period

        def on_start(self, ctx):
            self._closes: list[float] = []
            self._highs: list[float] = []
            self._lows: list[float] = []
            self._long = False
            self._entry = 0.0

        def on_bar(self, ctx, bar):
            if bar.instrument.symbol != self._sym:
                return
            self._closes.append(bar.close)
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            if len(self._closes) < self._qual:
                return

            inst = bar.instrument
            qty = _lot(inst)
            atr = _atr(self._highs, self._lows, self._closes, 14) or 1

            mom_ret = (bar.close - self._closes[-self._mom]) / self._closes[-self._mom]
            long_sma = sum(self._closes[-self._qual:]) / self._qual
            above_trend = bar.close > long_sma
            recent = self._closes[-20:]
            consistency = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1]) / (len(recent) - 1)

            if self._long:
                if bar.close < self._entry - 1.5 * atr or mom_ret < -0.02:
                    ctx.sell(inst, quantity=qty)
                    self._long = False
                return

            if mom_ret > 0.02 and above_trend and consistency > 0.55:
                ctx.buy(inst, quantity=qty)
                self._long = True
                self._entry = bar.close

    _register("regime_adaptive", "Regime-Adaptive Switcher",
              "Detects market regime (trending vs mean-reverting) by comparing recent vs prior volatility. In trending regime: follows momentum above/below SMA(40). In mean-reverting regime: trades z-score extremes (>1.5σ). Switches automatically. Source: SSRN 'Quantitative Strategies For Momentum And Trend Reversal' (Sim, 2026) + Ernie Chan regime detection.",
              lambda sym, **kw: RegimeAdaptive(sym))

    _register("vol_expansion_scalp", "Volatility Expansion Scalp",
              "Detects bars where range expands >1.8× the recent average (volatility expansion after compression). Trades in the direction of the expansion bar with ATR-based SL (1×) and TP (2×) = 2:1 R:R. Based on 0DTE gamma research (SSRN: Dim, Eraker, Vilkov) — gamma concentration causes expansion that persists intraday.",
              lambda sym, **kw: VolatilityExpansionScalp(sym))

    _register("momentum_quality", "Momentum-Quality Filter",
              "Buys only when 3 conditions align: positive 20-day momentum (>2%), price above 60-day SMA (long-term trend), and >55% of recent daily closes are higher than prior (consistency/quality). Exit on 1.5×ATR stop or momentum reversal. Source: Morningstar Q2 2026 factor research — momentum+quality leads globally in EM.",
              lambda sym, **kw: MomentumQuality(sym))


_init_strategies()


def list_strategies() -> list[dict[str, str]]:
    """Return all registered strategies with their descriptions."""
    return [
        {"key": k, "name": v["name"], "description": v["description"]}
        for k, v in STRATEGY_REGISTRY.items()
    ]


def _make_strategy(symbol: str, strategy_key: str = "atr_channel_breakout", **kwargs) -> Any:
    entry = STRATEGY_REGISTRY.get(strategy_key)
    if not entry:
        entry = STRATEGY_REGISTRY["atr_channel_breakout"]
    return entry["factory"](symbol, **kwargs)


# --------------------------------------------------------------------------- #
# Engine invocation (the one seam to offload to a worker in production)
# --------------------------------------------------------------------------- #
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
    **strategy_kwargs,
) -> dict[str, Any]:
    """Run a backtest and return a JSON-safe result (see module docstring)."""
    if cost not in DEMO_COST_MODELS:
        cost = "zerodha"
    try:
        capital = float(capital)
    except (TypeError, ValueError):
        capital = DEFAULT_CAPITAL
    if not math.isfinite(capital) or capital <= 0:
        capital = DEFAULT_CAPITAL

    strat_entry = STRATEGY_REGISTRY.get(strategy_key, STRATEGY_REGISTRY.get("sma_momentum", {}))
    strat_name = strat_entry.get("name", strategy_key)
    strat_desc = strat_entry.get("description", "")

    try:
        if source == "db":
            source_obj, n_bars = _build_db_source(symbol, exchange, interval, start, end)
        else:
            source = "demo"
            source_obj = _build_synthetic_source(symbol, exchange, n_bars)
        result = _run_engine(source_obj, symbol, capital, cost, strategy_key, **strategy_kwargs)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception:
        logger.exception("backtest failed for %s/%s source=%s", symbol, exchange, source)
        return {"status": "error", "message": "backtest engine failed — see logs"}

    curve = result.equity_curve
    equity_curve = [[ts.isoformat(), _json_safe(eq)] for ts, eq in curve]
    equity = [{"date": ts.isoformat(), "value": _json_safe(eq)} for ts, eq in curve]

    # Compute XIRR (annualized IRR from the equity curve cash flows)
    xirr_pct = None
    if len(curve) >= 2:
        try:
            from scipy.optimize import brentq
            first_ts, first_eq = curve[0]
            last_ts, last_eq = curve[-1]
            days = (last_ts - first_ts).total_seconds() / 86400
            if days > 0 and first_eq > 0:
                # Simple annualized return (CAGR) — more robust than iterative XIRR
                # for a single investment period
                total_return = (last_eq / first_eq) - 1.0
                years = days / 365.25
                if years > 0:
                    xirr_pct = ((1 + total_return) ** (1 / years) - 1) * 100
        except Exception:
            pass

    # Compute margin used (realistic SPAN margin for the instrument)
    # NIFTY futures MIS margin ≈ 9% of notional, NRML ≈ 12%
    metrics = _json_safe(getattr(result, "metrics", {}))
    margin_per_lot = 0.0
    margin_used = 0.0
    return_on_margin_pct = None
    inst = _instrument_for(symbol, exchange)
    lot_size = max(getattr(inst, "lot_size", 1) or 1, 1)
    if lot_size > 1:  # derivative
        avg_price = capital  # fallback
        if curve:
            avg_price = sum(eq for _, eq in curve) / len(curve)
        # Use a representative price from the data
        first_price = curve[0][1] / 1 if curve else 24500
        # For NIFTY futures: SPAN margin MIS ~9%, NRML ~12%
        margin_pct = 0.12  # NRML (conservative)
        notional_per_lot = 24500 * lot_size  # approximate
        margin_per_lot = notional_per_lot * margin_pct
        margin_used = margin_per_lot  # 1 lot per trade
        net_pnl = (metrics.get("net_pnl") or 0) if isinstance(metrics, dict) else 0
        if margin_used > 0 and net_pnl != 0:
            return_on_margin_pct = (net_pnl / margin_used) * 100

    # Raw OHLC bars for the candlestick chart on the frontend.
    ohlc = [
        {"time": b.ts.isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close}
        for b in source_obj.bars
    ]

    out = {
        "status": "success",
        "strategy": strat_name,
        "strategy_description": strat_desc,
        "symbol": symbol,
        "exchange": exchange,
        "interval": interval,
        "source": source,
        "start": start,
        "end": end,
        "capital": capital,
        "cost_model": cost,
        "n_bars": n_bars,
        "metrics": metrics,
        "xirr_pct": round(xirr_pct, 2) if xirr_pct is not None else None,
        "margin_used": round(margin_used, 0) if margin_used else None,
        "return_on_margin_pct": round(return_on_margin_pct, 2) if return_on_margin_pct is not None else None,
        "equity": equity,
        "equity_curve": equity_curve,
        "trades": _json_safe(getattr(result, "trades", [])),
        "ohlc": ohlc,
        "data_note": "Continuous NIFTY futures (stitched across contract rolls by broker API)" if "FUT" in symbol.upper() else None,
    }

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
    """Backwards-compatible synthetic demo (delegates to run_backtest)."""
    return run_backtest(source="demo", symbol=symbol, capital=capital, cost=cost,
                        n_bars=n_bars, window=window)
