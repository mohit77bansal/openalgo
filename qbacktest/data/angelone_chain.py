"""Build a live ChainSource backed by AngelOne SmartAPI WebSocket quotes.

The flow:

  1. Fetch (and cache) the AngelOne symbol master.
  2. Locate the spot INDEX instrument for the underlying (e.g. NIFTY index).
  3. Find the next non-expired weekly expiry for that underlying.
  4. Pick ATM ± N strikes once we know spot. To avoid a chicken-and-egg
     problem, we ask AngelOne for the index LTP via historical_candles (last
     1-min bar) before subscribing.
  5. Wire a ChainBuilder + LiveBrokerChainSource and return it.

This is read-only: no orders placed, no money at risk.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from qbacktest.brokers.base import BrokerAdapter
from qbacktest.core.calendar import IST
from qbacktest.core.types import Exchange, Instrument, InstrumentType
from qbacktest.data.chain_builder import ChainBuilder
from qbacktest.data.chain_source import LiveBrokerChainSource

log = logging.getLogger(__name__)


# In-process cache so multiple chain sources share one symbol-master fetch.
_SYMBOL_MASTER_CACHE: Optional[list[Instrument]] = None
_SYMBOL_MASTER_CACHE_LOCK = asyncio.Lock()


async def _load_symbol_master(adapter: BrokerAdapter) -> list[Instrument]:
    global _SYMBOL_MASTER_CACHE
    async with _SYMBOL_MASTER_CACHE_LOCK:
        if _SYMBOL_MASTER_CACHE is None:
            log.info("Fetching AngelOne symbol master (~150k rows, may take 30-60s)")
            _SYMBOL_MASTER_CACHE = await adapter.fetch_symbol_master()
            log.info("Symbol master loaded: %d instruments", len(_SYMBOL_MASTER_CACHE))
    return _SYMBOL_MASTER_CACHE


def _canonicalize_index(inst: Instrument, canonical_symbol: str) -> Instrument:
    """Return a copy of the index instrument with the canonical underlying
    symbol (e.g. AngelOne's 'NIFTY 50' → 'NIFTY'), preserving broker_keys.

    Without this, the ingestor writes Parquet under 'NIFTY 50/' while every
    reader (backtest runners, chain source) looks under 'NIFTY/'.
    """
    from dataclasses import replace
    if inst.symbol.upper() == canonical_symbol.upper():
        return inst
    return replace(inst, symbol=canonical_symbol.upper())


def _find_spot_index(master: list[Instrument], underlying: str) -> Optional[Instrument]:
    """NIFTY/BANKNIFTY index has instrument_type == INDEX, exchange usually NSE.

    Always returns the instrument with the *canonical* symbol (the `underlying`
    arg), not AngelOne's display name, so storage paths stay consistent.
    """
    u = underlying.upper()
    for inst in master:
        if inst.instrument_type == InstrumentType.INDEX and inst.symbol.upper() == u:
            return _canonicalize_index(inst, u)
    # Fallback common names: NSE/BSE may list "NIFTY 50", "NIFTY BANK"
    aliases = {
        "NIFTY": ("NIFTY", "NIFTY50", "NIFTY 50"),
        "BANKNIFTY": ("BANKNIFTY", "NIFTY BANK", "NIFTYBANK"),
        "FINNIFTY": ("FINNIFTY", "NIFTY FIN SERVICE"),
        "MIDCPNIFTY": ("MIDCPNIFTY", "NIFTY MID SELECT", "NIFTYMIDSELECT"),
        "SENSEX": ("SENSEX",),
        "BANKEX": ("BANKEX", "BSE BANKEX"),
    }
    for alias in aliases.get(u, ()):
        for inst in master:
            if inst.instrument_type == InstrumentType.INDEX and inst.symbol.upper() == alias.upper():
                return _canonicalize_index(inst, u)
    return None


def _find_next_expiry(master: list[Instrument], underlying: str, today: Optional[date] = None) -> Optional[date]:
    """Return the nearest non-expired option expiry for the given underlying."""
    today = today or datetime.now(IST).date()
    u = underlying.upper()
    expiries: set[date] = set()
    for inst in master:
        if (
            inst.instrument_type in (InstrumentType.CE, InstrumentType.PE)
            and inst.underlying
            and inst.underlying.upper() == u
            and inst.expiry
            and inst.expiry >= today
        ):
            expiries.add(inst.expiry)
    if not expiries:
        return None
    return min(expiries)


def _strikes_for_expiry(master: list[Instrument], underlying: str, expiry: date) -> list[float]:
    """All listed strikes for an underlying+expiry, sorted ascending."""
    u = underlying.upper()
    strikes: set[float] = set()
    for inst in master:
        if (
            inst.instrument_type in (InstrumentType.CE, InstrumentType.PE)
            and inst.underlying
            and inst.underlying.upper() == u
            and inst.expiry == expiry
            and inst.strike is not None
        ):
            strikes.add(float(inst.strike))
    return sorted(strikes)


async def _fetch_spot_ltp(adapter: BrokerAdapter, spot: Instrument) -> Optional[float]:
    """Quick LTP via the last 1-min candle of today."""
    today = datetime.now(IST).date()
    try:
        bars = await adapter.historical_candles(spot, today - timedelta(days=4), today, timeframe="1d")
        if bars:
            return float(bars[-1].close)
    except Exception as e:
        log.warning("Spot LTP fetch failed for %s: %s", spot.symbol, e)
    return None


@dataclass
class AngelOneChainConfig:
    underlying: str = "NIFTY"
    n_strikes_each_side: int = 15
    risk_free_rate: float = 0.07
    interval_seconds: float = 1.0
    fallback_spot: Optional[float] = None  # used when LTP fetch fails (off-hours)


async def build_angelone_chain_source(
    adapter: BrokerAdapter,
    config: AngelOneChainConfig,
) -> Optional[LiveBrokerChainSource]:
    """Returns a live chain source, or None if the chain can't be assembled
    (e.g. symbol master fails, no expiry, no spot)."""

    if not adapter.is_authenticated():
        log.warning("AngelOne not authenticated; cannot build chain source")
        return None

    try:
        master = await _load_symbol_master(adapter)
    except Exception as e:
        log.exception("Symbol master fetch failed: %s", e)
        return None

    spot_inst = _find_spot_index(master, config.underlying)
    if spot_inst is None:
        log.warning("No spot index found for %s in symbol master", config.underlying)
        return None

    expiry = _find_next_expiry(master, config.underlying)
    if expiry is None:
        log.warning("No upcoming %s option expiry found", config.underlying)
        return None

    all_strikes = _strikes_for_expiry(master, config.underlying, expiry)
    if not all_strikes:
        log.warning("No strikes found for %s expiry %s", config.underlying, expiry)
        return None

    # Pick spot — try live LTP, fall back to config or median strike.
    spot = await _fetch_spot_ltp(adapter, spot_inst)
    if spot is None:
        spot = config.fallback_spot or all_strikes[len(all_strikes) // 2]
        log.warning("Using fallback spot %.2f for %s", spot, config.underlying)

    # ATM ± N strikes
    atm = min(all_strikes, key=lambda k: abs(k - spot))
    atm_idx = all_strikes.index(atm)
    lo = max(0, atm_idx - config.n_strikes_each_side)
    hi = min(len(all_strikes), atm_idx + config.n_strikes_each_side + 1)
    selected = all_strikes[lo:hi]

    log.info(
        "AngelOne chain: %s expiry=%s spot=%.2f atm=%.0f strikes=%d",
        config.underlying, expiry, spot, atm, len(selected),
    )

    # Build broker-keyed instruments for the strikes we care about by
    # cross-referencing the symbol master rows.
    u = config.underlying.upper()
    keyed_options: dict[tuple[float, str], Instrument] = {}
    for inst in master:
        if (
            inst.instrument_type in (InstrumentType.CE, InstrumentType.PE)
            and inst.underlying
            and inst.underlying.upper() == u
            and inst.expiry == expiry
            and inst.strike is not None
            and inst.strike in selected
        ):
            kind = "CE" if inst.instrument_type == InstrumentType.CE else "PE"
            keyed_options[(float(inst.strike), kind)] = inst

    # ChainBuilder constructs its own instruments — we need to inject the
    # broker tokens. Cleanest is to pre-build the instrument list with tokens
    # from the master and pass them through. We'll subclass-on-the-fly by
    # patching the _build_instruments method.
    builder = ChainBuilder(
        broker=adapter,
        underlying=config.underlying,
        expiry=expiry,
        strikes=selected,
        risk_free_rate=config.risk_free_rate,
        spot_instrument=spot_inst,
    )

    def _build_instruments_with_tokens() -> list[Instrument]:
        out: list[Instrument] = []
        for k in selected:
            for kind in ("CE", "PE"):
                inst = keyed_options.get((float(k), kind))
                if inst is not None:
                    out.append(inst)
        if spot_inst is not None:
            out.append(spot_inst)
        return out

    builder._build_instruments = _build_instruments_with_tokens  # type: ignore[assignment]

    return LiveBrokerChainSource(builder=builder, interval_seconds=config.interval_seconds)
