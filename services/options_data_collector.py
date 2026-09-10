# services/options_data_collector.py
"""
Options Data Collector Service

Background daemon that collects 1-minute option chain OHLCV data during
market hours (9:15 -- 15:30 IST) and persists it to DuckDB via historify_db.

Tracked symbols: ATM +/- 10 strikes for NIFTY, BANKNIFTY, FINNIFTY,
MIDCPNIFTY on the nearest two expiries, both CE and PE.  The symbol list is
refreshed once per trading day at 9:15 AM.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from broker.angel.api.data import BrokerData
from database.auth_db import get_auth_token
from database.historify_db import init_database, upsert_market_data
from database.token_db_enhanced import fno_search_symbols, get_distinct_expiries_cached
from utils.logging import get_logger

logger = get_logger(__name__)

# IST = UTC+05:30
IST = timezone(timedelta(hours=5, minutes=30))


class OptionsDataCollector:
    """Background daemon that collects 1m option chain data during market hours."""

    # --- Class-level configuration ----------------------------------------

    UNDERLYINGS: dict[str, dict] = {
        "NIFTY": {
            "index_exchange": "NSE_INDEX",
            "option_exchange": "NFO",
            "strike_step": 50,
        },
        "BANKNIFTY": {
            "index_exchange": "NSE_INDEX",
            "option_exchange": "NFO",
            "strike_step": 100,
        },
        "FINNIFTY": {
            "index_exchange": "NSE_INDEX",
            "option_exchange": "NFO",
            "strike_step": 50,
        },
        "MIDCPNIFTY": {
            "index_exchange": "NSE_INDEX",
            "option_exchange": "NFO",
            "strike_step": 25,
        },
    }

    STRIKES_AROUND_ATM: int = 10
    NUM_EXPIRIES: int = 2
    COLLECTION_INTERVAL_SECONDS: int = 60
    MARKET_OPEN: tuple[int, int] = (9, 15)
    MARKET_CLOSE: tuple[int, int] = (15, 30)
    RATE_LIMIT_INTERVAL: float = 0.5  # seconds between API calls (2 req/s)

    # --- Instance state ---------------------------------------------------

    def __init__(self) -> None:
        self._tracked_symbols: list[dict] = []
        self._last_refresh_date: Optional[date] = None
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._lock: threading.Lock = threading.Lock()

    # --- Public API -------------------------------------------------------

    def start(self) -> None:
        """Start the collector as a daemon thread."""
        with self._lock:
            if self._running:
                logger.warning("OptionsDataCollector already running")
                return
            self._running = True

        self._thread = threading.Thread(
            target=self._run_loop, name="options-data-collector", daemon=True
        )
        self._thread.start()
        logger.info("OptionsDataCollector started")

    def stop(self) -> None:
        """Signal the collector to stop gracefully."""
        with self._lock:
            if not self._running:
                return
            self._running = False

        logger.info("OptionsDataCollector stopping ...")
        if self._thread is not None:
            self._thread.join(timeout=10)
        logger.info("OptionsDataCollector stopped")

    # --- Main loop --------------------------------------------------------

    def _run_loop(self) -> None:
        """Sleep until market open, then collect every minute until close."""
        logger.info("OptionsDataCollector loop started")

        while self._is_running():
            try:
                if not self._is_market_hours():
                    time.sleep(10)
                    continue

                auth_token = self._get_auth_token()
                if auth_token is None:
                    logger.error("No auth token available; sleeping 60s before retry")
                    time.sleep(60)
                    continue

                broker = BrokerData(auth_token)

                # Refresh symbol list once per day (at or after 9:15)
                today = datetime.now(tz=IST).date()
                if self._last_refresh_date != today:
                    self._refresh_symbols(broker)
                    with self._lock:
                        self._last_refresh_date = today

                self._collect_once(broker)

                # Sleep until the next minute boundary
                self._sleep_until_next_minute()

            except Exception:
                logger.exception("Unhandled error in collector loop")
                time.sleep(30)

        logger.info("OptionsDataCollector loop exited")

    # --- Market hours -----------------------------------------------------

    def _is_market_hours(self) -> bool:
        """Return True when IST wall-clock is inside 9:15 -- 15:30."""
        now = datetime.now(tz=IST)
        open_time = now.replace(
            hour=self.MARKET_OPEN[0],
            minute=self.MARKET_OPEN[1],
            second=0,
            microsecond=0,
        )
        close_time = now.replace(
            hour=self.MARKET_CLOSE[0],
            minute=self.MARKET_CLOSE[1],
            second=0,
            microsecond=0,
        )
        return open_time <= now <= close_time

    # --- Auth helper ------------------------------------------------------

    def _get_auth_token(self) -> Optional[str]:
        """Get auth token from the database, retrying once with fresh lookup."""
        username = os.getenv("LOGIN_USERNAME") or os.getenv("BROKER_API_KEY")
        if not username:
            logger.error("Neither LOGIN_USERNAME nor BROKER_API_KEY set in environment")
            return None

        token = get_auth_token(username)
        if token is not None:
            return token

        # Retry once with cache bypass (handles stale / recently rotated tokens)
        logger.warning("Auth token was None; retrying with bypass_cache=True")
        return get_auth_token(username, bypass_cache=True)

    # --- Symbol refresh ---------------------------------------------------

    def _refresh_symbols(self, broker: BrokerData) -> None:
        """Rebuild the tracked symbol list based on current ATM strikes."""
        logger.info("Refreshing tracked option symbols ...")
        new_symbols: list[dict] = []

        for underlying, cfg in self.UNDERLYINGS.items():
            try:
                atm = self._get_atm_strike(broker, underlying, cfg)
                if atm is None:
                    logger.warning("Could not determine ATM for %s; skipping", underlying)
                    continue

                expiries = self._nearest_expiries(
                    cfg["option_exchange"], underlying, self.NUM_EXPIRIES
                )
                if not expiries:
                    logger.warning("No expiries found for %s; skipping", underlying)
                    continue

                step = cfg["strike_step"]
                strike_lo = atm - self.STRIKES_AROUND_ATM * step
                strike_hi = atm + self.STRIKES_AROUND_ATM * step

                for expiry in expiries:
                    for opt_type in ("CE", "PE"):
                        syms = fno_search_symbols(
                            exchange=cfg["option_exchange"],
                            underlying=underlying,
                            expiry=expiry,
                            instrumenttype=opt_type,
                            strike_min=strike_lo,
                            strike_max=strike_hi,
                            limit=10000,
                        )
                        for s in syms:
                            new_symbols.append(
                                {
                                    "symbol": s["symbol"],
                                    "exchange": s["exchange"],
                                }
                            )

                logger.info(
                    "Underlying %s: ATM=%s, expiries=%s, symbols added=%d",
                    underlying,
                    atm,
                    expiries,
                    sum(
                        1
                        for s in new_symbols
                        if underlying in s["symbol"]
                    ),
                )

            except Exception:
                logger.exception("Error refreshing symbols for %s", underlying)

        with self._lock:
            self._tracked_symbols = new_symbols

        logger.info("Symbol refresh complete: %d symbols tracked", len(new_symbols))

    def _get_atm_strike(
        self, broker: BrokerData, underlying: str, config: dict
    ) -> Optional[float]:
        """Fetch the spot price and round to the nearest strike step."""
        try:
            quote = broker.get_quotes(underlying, config["index_exchange"])
            spot = quote.get("ltp", 0.0)
            if spot <= 0:
                return None
            step = config["strike_step"]
            return round(spot / step) * step
        except Exception:
            logger.exception("Failed to get spot price for %s", underlying)
            return None

    @staticmethod
    def _nearest_expiries(
        exchange: str, underlying: str, count: int
    ) -> list[str]:
        """Return the *count* nearest expiry strings, sorted ascending."""
        all_expiries = get_distinct_expiries_cached(
            exchange=exchange, underlying=underlying
        )
        if not all_expiries:
            return []

        today = datetime.now(tz=IST).date()
        dated: list[tuple[date, str]] = []
        for exp_str in all_expiries:
            try:
                exp_date = datetime.strptime(exp_str, "%d-%b-%y").date()
            except ValueError:
                try:
                    exp_date = datetime.strptime(exp_str, "%d-%b-%Y").date()
                except ValueError:
                    continue
            if exp_date >= today:
                dated.append((exp_date, exp_str))

        dated.sort(key=lambda t: t[0])
        return [exp_str for _, exp_str in dated[:count]]

    # --- Data collection --------------------------------------------------

    def _collect_once(self, broker: BrokerData) -> None:
        """Fetch the latest 1m candle for every tracked symbol and store it."""
        with self._lock:
            symbols = list(self._tracked_symbols)

        if not symbols:
            logger.debug("No tracked symbols; skipping collection pass")
            return

        today_str = datetime.now(tz=IST).strftime("%Y-%m-%d")
        success_count = 0
        error_count = 0

        for entry in symbols:
            if not self._is_running():
                break

            symbol = entry["symbol"]
            exchange = entry["exchange"]

            try:
                df: pd.DataFrame = broker.get_history(
                    symbol, exchange, "1m", today_str, today_str
                )

                if df is not None and not df.empty:
                    upsert_market_data(df, symbol, exchange, "1m")
                    success_count += 1
                else:
                    # Empty frame is normal before the first candle of the day
                    pass

            except Exception:
                logger.debug("Error fetching %s/%s", symbol, exchange, exc_info=True)
                error_count += 1

            # Rate-limit: the broker module already has its own limiter,
            # but we add an extra safety gap between calls.
            time.sleep(self.RATE_LIMIT_INTERVAL)

        logger.info(
            "Collection pass complete: %d stored, %d errors, %d total symbols",
            success_count,
            error_count,
            len(symbols),
        )

    # --- Helpers ----------------------------------------------------------

    def _is_running(self) -> bool:
        with self._lock:
            return self._running

    @staticmethod
    def _sleep_until_next_minute() -> None:
        """Sleep until the next whole-minute boundary (IST)."""
        now = datetime.now(tz=IST)
        next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        delta = (next_minute - now).total_seconds()
        if delta > 0:
            time.sleep(delta)


# ---------------------------------------------------------------------------
# Convenience entry points
# ---------------------------------------------------------------------------

def start_collector() -> OptionsDataCollector:
    """Convenience function to start the collector daemon thread."""
    collector = OptionsDataCollector()
    collector.start()
    return collector


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()

    init_database()

    collector = OptionsDataCollector()
    collector.start()

    logger.info("Options data collector running. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down ...")
        collector.stop()
        sys.exit(0)
