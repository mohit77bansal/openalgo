"""
Chain Service — build a qbacktest ChainSnapshot from OpenAlgo's live data.

Queries the symbol DB for NIFTY option chain instruments, fetches live quotes
via BrokerData.get_multiquotes(), and assembles an immutable ChainSnapshot
that the qbacktest arb scanners can consume directly.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Optional

from utils.logging import get_logger

logger = get_logger(__name__)

# Current NIFTY lot size (NSE effective from 2021-10-29).
# The symbol DB also carries lotsize per row; we use this as the fallback.
_DEFAULT_NIFTY_LOT_SIZE = 75

# Risk-free rate assumption (RBI repo rate ballpark).
_DEFAULT_RFR = 0.065


def _parse_expiry_str(expiry_str: str) -> Optional[date]:
    """Parse an expiry string like '07-AUG-26' into a date object."""
    if not expiry_str:
        return None
    for fmt in ("%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(expiry_str.strip().upper(), fmt).date()
        except ValueError:
            continue
    return None


def build_nifty_chain(
    auth_token: str,
    underlying: str = "NIFTY",
    exchange: str = "NFO",
    risk_free_rate: float = _DEFAULT_RFR,
    strike_range: int = 20,
) -> "ChainSnapshot":
    """Build a ChainSnapshot for the nearest weekly expiry from live broker data.

    Args:
        auth_token: Angel broker auth token (NOT the OpenAlgo API key).
        underlying: Underlying name in the symbol DB (default ``NIFTY``).
        exchange: Exchange code (default ``NFO``).
        risk_free_rate: Decimal risk-free rate for the snapshot (default 0.065).
        strike_range: Number of strikes above and below ATM to include.
            Set to 0 to include ALL strikes (may be 200+, slow).

    Returns:
        A fully-populated ``ChainSnapshot`` ready for arb scanners.

    Raises:
        ValueError: If no option instruments or quotes are available.
    """
    from qbacktest.core.chain import ChainQuote, ChainSnapshot
    from qbacktest.core.types import OptionType

    # ------------------------------------------------------------------
    # 1. Discover the nearest expiry
    # ------------------------------------------------------------------
    from database.symbol import get_distinct_expiries

    expiries = get_distinct_expiries(
        exchange=exchange, underlying=underlying, instrumenttype="options"
    )
    if not expiries:
        raise ValueError(
            f"No option expiries found for {underlying} on {exchange}. "
            "Is the master contract loaded?"
        )

    nearest_expiry_str = expiries[0]
    nearest_expiry = _parse_expiry_str(nearest_expiry_str)
    if nearest_expiry is None:
        raise ValueError(f"Cannot parse nearest expiry string: {nearest_expiry_str!r}")

    logger.info(
        f"Building chain for {underlying} expiry {nearest_expiry_str} on {exchange}"
    )

    # ------------------------------------------------------------------
    # 2. Fetch all CE and PE instruments for this expiry
    # ------------------------------------------------------------------
    from database.symbol import fno_search_symbols_db

    ce_rows = fno_search_symbols_db(
        exchange=exchange,
        underlying=underlying,
        expiry=nearest_expiry_str,
        instrumenttype="CE",
        limit=500,
    )
    pe_rows = fno_search_symbols_db(
        exchange=exchange,
        underlying=underlying,
        expiry=nearest_expiry_str,
        instrumenttype="PE",
        limit=500,
    )

    all_option_rows = ce_rows + pe_rows
    if not all_option_rows:
        raise ValueError(
            f"No option instruments found for {underlying} "
            f"expiry {nearest_expiry_str} on {exchange}"
        )

    # Determine lot size from the first row (all same underlying share lot size).
    lot_size = int(all_option_rows[0].get("lotsize") or _DEFAULT_NIFTY_LOT_SIZE)

    logger.info(
        f"Found {len(ce_rows)} CE + {len(pe_rows)} PE instruments, lot_size={lot_size}"
    )

    # ------------------------------------------------------------------
    # 3. Fetch NIFTY spot (index price)
    # ------------------------------------------------------------------
    from broker.angel.api.data import BrokerData

    broker_data = BrokerData(auth_token)

    spot_price = 0.0
    try:
        spot_quote = broker_data.get_quotes(underlying, "NSE_INDEX")
        spot_price = float(spot_quote.get("ltp", 0))
    except Exception as exc:
        logger.warning(f"Failed to fetch {underlying} spot price: {exc}")

    if spot_price <= 0:
        raise ValueError(
            f"Could not fetch a valid spot price for {underlying}. "
            "Is the market open and broker connected?"
        )

    logger.info(f"{underlying} spot = {spot_price}")

    # ------------------------------------------------------------------
    # 3b. Optionally filter strikes to ATM +/- strike_range
    # ------------------------------------------------------------------
    if strike_range > 0:
        atm_strike = round(spot_price / 50) * 50  # NIFTY strike step = 50
        all_strikes = sorted({float(r.get("strike", 0)) for r in all_option_rows})
        # Find the index of the ATM strike in the sorted list
        atm_idx = min(
            range(len(all_strikes)),
            key=lambda i: abs(all_strikes[i] - atm_strike),
        )
        lo = max(0, atm_idx - strike_range)
        hi = min(len(all_strikes), atm_idx + strike_range + 1)
        allowed_strikes = set(all_strikes[lo:hi])

        all_option_rows = [
            r for r in all_option_rows
            if float(r.get("strike", 0)) in allowed_strikes
        ]
        logger.info(
            f"Filtered to {len(all_option_rows)} instruments "
            f"(ATM={atm_strike}, range={strike_range})"
        )

    # ------------------------------------------------------------------
    # 4. Fetch NIFTY near-month future price (best effort)
    # ------------------------------------------------------------------
    futures_price: Optional[float] = None
    try:
        fut_rows = fno_search_symbols_db(
            exchange=exchange,
            underlying=underlying,
            instrumenttype="FUT",
            limit=10,
        )
        if fut_rows:
            # Sort by expiry, take nearest
            fut_rows.sort(key=lambda r: _parse_expiry_str(r.get("expiry", "")) or date.max)
            nearest_fut = fut_rows[0]
            fut_quote = broker_data.get_quotes(nearest_fut["symbol"], exchange)
            futures_price = float(fut_quote.get("ltp", 0)) or None
            if futures_price:
                logger.info(
                    f"{underlying} futures ({nearest_fut['symbol']}) = {futures_price}"
                )
    except Exception as exc:
        logger.warning(f"Failed to fetch futures price: {exc}")

    # ------------------------------------------------------------------
    # 5. Batch-fetch live quotes for all option instruments
    # ------------------------------------------------------------------
    # Build the list for get_multiquotes (symbol + exchange dicts).
    symbols_for_quotes = [
        {"symbol": row["symbol"], "exchange": row["exchange"]}
        for row in all_option_rows
    ]

    logger.info(f"Fetching quotes for {len(symbols_for_quotes)} instruments...")
    t0 = time.monotonic()

    raw_quotes = broker_data.get_multiquotes(symbols_for_quotes)

    elapsed = time.monotonic() - t0
    logger.info(f"Quote fetch completed in {elapsed:.1f}s")

    # Build a lookup: symbol -> quote data
    quote_lookup: dict[str, dict] = {}
    for item in raw_quotes:
        sym = item.get("symbol")
        data = item.get("data")
        if sym and data and "error" not in item:
            quote_lookup[sym] = data

    # ------------------------------------------------------------------
    # 6. Assemble ChainQuote objects
    # ------------------------------------------------------------------
    chain_quotes: list[ChainQuote] = []
    skipped = 0

    for row in all_option_rows:
        symbol = row["symbol"]
        data = quote_lookup.get(symbol)
        if data is None:
            skipped += 1
            continue

        strike = float(row.get("strike", 0))
        inst_type = (row.get("instrumenttype") or "").upper()

        if inst_type == "CE":
            opt_type = OptionType.CALL
        elif inst_type == "PE":
            opt_type = OptionType.PUT
        else:
            skipped += 1
            continue

        ltp = float(data.get("ltp", 0))
        bid = float(data.get("bid", 0)) or None
        ask = float(data.get("ask", 0)) or None
        volume = int(data.get("volume", 0))
        oi = int(data.get("oi", 0))

        # Skip instruments with zero LTP (probably illiquid / no trade today)
        if ltp <= 0:
            skipped += 1
            continue

        cq = ChainQuote(
            strike=strike,
            option_type=opt_type,
            last_price=ltp,
            bid=bid,
            ask=ask,
            volume=volume,
            open_interest=oi,
            iv=None,  # IV computed by scanners if needed
        )
        chain_quotes.append(cq)

    if not chain_quotes:
        raise ValueError(
            f"No valid quotes returned for {underlying} options. "
            f"Skipped {skipped} instruments. Is the market open?"
        )

    logger.info(
        f"Built {len(chain_quotes)} ChainQuote objects ({skipped} skipped)"
    )

    # ------------------------------------------------------------------
    # 7. Assemble and return the ChainSnapshot
    # ------------------------------------------------------------------
    snapshot = ChainSnapshot(
        underlying=underlying,
        expiry=nearest_expiry,
        spot=spot_price,
        ts=datetime.now(),
        risk_free_rate=risk_free_rate,
        quotes=tuple(chain_quotes),
        lot_size=lot_size,
        futures_price=futures_price,
    )

    logger.info(
        f"ChainSnapshot ready: {underlying} expiry={nearest_expiry} "
        f"spot={spot_price} strikes={len(snapshot.strikes)} "
        f"quotes={len(snapshot.quotes)}"
    )

    return snapshot
