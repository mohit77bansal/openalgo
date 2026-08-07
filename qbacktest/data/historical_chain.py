"""HistoricalChainSource — replay stored option-chain bars as ChainSnapshots.

Reads the Parquet OHLCV store (written by the ingestor / Celery tasks) and
reconstructs `ChainSnapshot` objects at a fixed interval. Each snapshot joins
the spot index close with every option strike's close at that timestamp.

Satisfies the same `ChainSource` protocol as `MockChainProducer` and
`LiveBrokerChainSource`, so the existing `ChainScannerWorker` and arb scanners
consume it unchanged — the difference is the data is historical, not live.

Used by `ArbBacktestRunner`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from qbacktest.core.calendar import IST
from qbacktest.core.chain import ChainQuote, ChainSnapshot
from qbacktest.core.lot_sizes import lot_size_on
from qbacktest.core.types import (
    Exchange,
    Instrument,
    InstrumentType,
    OptionType,
)
from qbacktest.data.io import parquet_to_bars
from qbacktest.data.store import DataStore

log = logging.getLogger(__name__)


def _floor_to_interval(ts: datetime, interval_minutes: int) -> datetime:
    """Round a timestamp down to the nearest interval boundary."""
    discard = timedelta(
        minutes=ts.minute % interval_minutes,
        seconds=ts.second,
        microseconds=ts.microsecond,
    )
    return ts - discard


@dataclass
class HistoricalChainSource:
    """Yields ChainSnapshot objects rebuilt from stored 1-min OHLCV bars.

    Parameters
    ----------
    store : DataStore
    underlying : str            e.g. "NIFTY"
    expiry : date               the option expiry to reconstruct
    start_date, end_date : date inclusive replay window
    interval_minutes : int      snapshot cadence (default 5m; 1m available if ingested)
    risk_free_rate : float
    timeframe : str             source bar timeframe in the store (default "1m")

    Note: fields are `start_date`/`end_date` (not `start`/`end`) because
    `start()` and `stop()` are methods of the ChainSource protocol — a field
    named `start` would shadow the method.
    """

    store: DataStore
    underlying: str
    expiry: date
    start_date: date
    end_date: date
    interval_minutes: int = 5
    risk_free_rate: float = 0.07
    timeframe: str = "1m"
    # Synthetic bid/ask spread applied around each option's close. 1-min OHLCV
    # gives us last-trade prices, not quotes — and last-trade prices across
    # strikes at the same minute are NOT a coherent arbitrage-free surface.
    # Without a realistic spread the scanners "find" arbitrage in every
    # snapshot. base_spread_pct is the half-spread at ATM; it widens with
    # distance from spot (illiquid wings quote wider).
    base_spread_pct: float = 0.004        # 0.4% half-spread at ATM
    wing_spread_pct_per_pct: float = 0.05  # +5% half-spread per 1% from spot
    min_spread_inr: float = 0.10

    _snapshots: list[ChainSnapshot] = field(default_factory=list, init=False)
    _cursor: int = field(default=0, init=False)
    _started: bool = field(default=False, init=False)

    # ------------------------------------------------------------------ #
    # ChainSource protocol
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._started:
            return
        self._snapshots = self._build_all_snapshots()
        self._cursor = 0
        self._started = True
        log.info(
            "HistoricalChainSource ready: %s %s — %d snapshots over %s..%s",
            self.underlying, self.expiry, len(self._snapshots), self.start_date, self.end_date,
        )

    async def stop(self) -> None:
        self._started = False

    async def next(self) -> ChainSnapshot:
        if not self._started:
            await self.start()
        if self._cursor >= len(self._snapshots):
            raise StopAsyncIteration("HistoricalChainSource exhausted")
        snap = self._snapshots[self._cursor]
        self._cursor += 1
        # Yield control so the worker's event loop stays responsive.
        await asyncio.sleep(0)
        return snap

    @property
    def exhausted(self) -> bool:
        return self._cursor >= len(self._snapshots)

    @property
    def total(self) -> int:
        return len(self._snapshots)

    # ------------------------------------------------------------------ #
    # Internal — build snapshots from Parquet
    # ------------------------------------------------------------------ #

    def _opt_base_dir(self) -> Path:
        return (
            self.store.parquet_root / "bars/opt"
            / self.underlying.upper()
            / self.expiry.isoformat()
        )

    def _discover_strikes(self) -> list[float]:
        """List strikes we actually have stored for this underlying+expiry."""
        base = self._opt_base_dir()
        if not base.exists():
            return []
        strikes: set[float] = set()
        for strike_dir in base.iterdir():
            if not strike_dir.is_dir():
                continue
            try:
                strikes.add(float(strike_dir.name))
            except ValueError:
                continue
        return sorted(strikes)

    def _option_instrument(self, strike: float, opt: OptionType, lot_size: int) -> Instrument:
        return Instrument(
            symbol=self.underlying.upper(),
            exchange=Exchange.NFO,
            instrument_type=InstrumentType.CE if opt == OptionType.CALL else InstrumentType.PE,
            lot_size=lot_size,
            tick_size=0.05,
            expiry=self.expiry,
            strike=strike,
            underlying=self.underlying.upper(),
        )

    # BSE-listed index families — their spot index bars are stored under the
    # BSE partition, not NSE. (Same exchange-mismatch class of bug as the
    # 'NIFTY 50' symbol issue.)
    _BSE_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50", "SNSX50"}

    def _spot_instrument(self) -> Instrument:
        exch = Exchange.BSE if self.underlying.upper() in self._BSE_UNDERLYINGS else Exchange.NSE
        return Instrument(
            symbol=self.underlying.upper(),
            exchange=exch,
            instrument_type=InstrumentType.INDEX,
            lot_size=1,
        )

    def _load_bars_indexed(self, inst: Instrument) -> dict[datetime, float]:
        """Load close prices for an instrument, keyed by interval-floored ts."""
        out: dict[datetime, float] = {}
        d = self.start_date
        while d <= self.end_date:
            if d.weekday() < 5:
                path = self.store.bar_path(inst, d, timeframe=self.timeframe)
                if path.exists():
                    for bar in parquet_to_bars(path):
                        bucket = _floor_to_interval(bar.ts.astimezone(IST), self.interval_minutes)
                        # Last bar within the bucket wins (close-of-bucket).
                        out[bucket] = bar.close
            d += timedelta(days=1)
        return out

    def _quote_spread(self, price: float, strike: float, spot: float) -> tuple[float, float]:
        """Synthesize a realistic bid/ask around a last-trade price.

        Half-spread widens with distance from spot — illiquid wings quote much
        wider than ATM. This absorbs the cross-strike inconsistency inherent in
        independently-sampled 1-min closes, so the arb scanners only flag
        mispricings that genuinely exceed a tradeable spread.
        """
        moneyness_pct = abs(strike - spot) / spot * 100.0 if spot > 0 else 0.0
        half_pct = self.base_spread_pct + self.wing_spread_pct_per_pct * moneyness_pct * 0.01
        half = max(price * half_pct, self.min_spread_inr)
        bid = max(price - half, 0.05)
        ask = price + half
        return bid, ask

    def _build_all_snapshots(self) -> list[ChainSnapshot]:
        strikes = self._discover_strikes()
        if not strikes:
            log.warning(
                "No stored option strikes for %s %s under %s",
                self.underlying, self.expiry, self._opt_base_dir(),
            )
            return []

        lot_size = lot_size_on(self.underlying, self.expiry)

        # Load spot index series.
        spot_series = self._load_bars_indexed(self._spot_instrument())
        if not spot_series:
            log.warning("No spot index bars for %s in window", self.underlying)
            return []

        # Load each strike's CE + PE series.
        ce_series: dict[float, dict[datetime, float]] = {}
        pe_series: dict[float, dict[datetime, float]] = {}
        for k in strikes:
            ce_inst = self._option_instrument(k, OptionType.CALL, lot_size)
            pe_inst = self._option_instrument(k, OptionType.PUT, lot_size)
            ce = self._load_bars_indexed(ce_inst)
            pe = self._load_bars_indexed(pe_inst)
            if ce:
                ce_series[k] = ce
            if pe:
                pe_series[k] = pe

        # Snapshot timestamps = intersection cadence over spot's timestamps.
        timestamps = sorted(spot_series.keys())
        snapshots: list[ChainSnapshot] = []
        for ts in timestamps:
            spot = spot_series[ts]
            quotes: list[ChainQuote] = []
            for k in strikes:
                ce_px = ce_series.get(k, {}).get(ts)
                pe_px = pe_series.get(k, {}).get(ts)
                if ce_px is not None:
                    bid, ask = self._quote_spread(ce_px, k, spot)
                    quotes.append(ChainQuote(
                        strike=k, option_type=OptionType.CALL,
                        last_price=ce_px, bid=bid, ask=ask,
                    ))
                if pe_px is not None:
                    bid, ask = self._quote_spread(pe_px, k, spot)
                    quotes.append(ChainQuote(
                        strike=k, option_type=OptionType.PUT,
                        last_price=pe_px, bid=bid, ask=ask,
                    ))
            if not quotes:
                continue
            snapshots.append(ChainSnapshot(
                underlying=self.underlying.upper(),
                expiry=self.expiry,
                spot=spot,
                ts=ts,
                risk_free_rate=self.risk_free_rate,
                quotes=tuple(quotes),
                lot_size=lot_size,
                futures_price=None,
            ))
        return snapshots
