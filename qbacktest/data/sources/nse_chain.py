"""NSE direct option chain fetcher.

NSE publishes the option chain at https://www.nseindia.com/api/option-chain-indices
(used by their own website). Public, no auth needed, but Akamai bot-protected:
- Plain HTTP (httpx/requests) gets through Akamai's edge for the homepage but
  the API endpoint returns `{}` because the `_abck` cookie hasn't been validated
  by their JS challenge.
- Real Chromium passes the challenge naturally → use Playwright.

Returns a single ChainSnapshot with all listed strikes for the requested expiry
(default: nearest).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from qbacktest.core.calendar import IST
from qbacktest.core.chain import ChainQuote, ChainSnapshot
from qbacktest.core.lot_sizes import lot_size_on
from qbacktest.core.types import OptionType

log = logging.getLogger(__name__)


_NSE_OPTION_CHAIN_PAGE = "https://www.nseindia.com/option-chain"
_NSE_OPTION_CHAIN_URL = "https://www.nseindia.com/api/option-chain-indices"

_INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}


@dataclass
class NSEChainFetcher:
    """Playwright-backed NSE option chain fetcher.

    First call boots a headless Chromium, completes Akamai's JS challenge by
    visiting the option-chain page, then fetches the API JSON inside the same
    browser context (so the `_abck` cookie is honored).
    """

    risk_free_rate: float = 0.0675   # NSE 10y G-Sec ~mid-2025; refresh quarterly
    timeout_ms: int = 25_000

    def fetch(self, underlying: str = "NIFTY", expiry: Optional[date] = None) -> ChainSnapshot:
        return asyncio.run(self.afetch(underlying, expiry))

    async def afetch(self, underlying: str = "NIFTY", expiry: Optional[date] = None) -> ChainSnapshot:
        symbol = underlying.upper()
        if symbol not in _INDEX_SYMBOLS:
            raise ValueError(f"NSE option-chain endpoint supports indices only: {_INDEX_SYMBOLS}")

        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "Playwright not installed. Run `make install-broker` "
                "(installs Playwright + Chromium) and retry."
            ) from e

        payload: dict | None = None
        async with async_playwright() as p:
            # Stealth-ish launch args; disable some webdriver telltales Akamai checks
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-sandbox",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1366, "height": 768},
                extra_http_headers={
                    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
                },
            )
            # Hide webdriver flag
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )
            page = await context.new_page()
            try:
                # Step 1: visit homepage first (broader, less protected)
                await page.goto("https://www.nseindia.com/", timeout=self.timeout_ms,
                                wait_until="load")
                await page.wait_for_timeout(1500)
                # Step 2: navigate to option-chain landing
                try:
                    await page.goto(_NSE_OPTION_CHAIN_PAGE, timeout=self.timeout_ms,
                                    wait_until="domcontentloaded")
                except Exception as e:
                    log.warning("option-chain page nav failed (%s); using homepage cookies anyway", e)
                await page.wait_for_timeout(1500)
                # Step 3: fetch API in-page
                payload = await page.evaluate(
                    f"""
                    async () => {{
                        const resp = await fetch(
                            '{_NSE_OPTION_CHAIN_URL}?symbol={symbol}',
                            {{ headers: {{ 'Accept': 'application/json' }}, credentials: 'include' }}
                        );
                        if (!resp.ok) return {{ __error: 'status ' + resp.status }};
                        const text = await resp.text();
                        try {{ return JSON.parse(text); }}
                        catch (e) {{ return {{ __error: 'parse', body: text.slice(0, 200) }}; }}
                    }}
                    """
                )
            finally:
                await context.close()
                await browser.close()

        if not isinstance(payload, dict) or payload.get("__error"):
            raise RuntimeError(f"NSE response invalid or empty: {payload}")
        return self._payload_to_snapshot(payload, symbol, expiry)

    # ------------------------------------------------------------------ #

    def _payload_to_snapshot(
        self, payload: dict, symbol: str, expiry: Optional[date],
    ) -> ChainSnapshot:
        records = payload.get("records") or {}
        spot = float(records.get("underlyingValue") or 0)
        all_expiries = records.get("expiryDates") or []
        if not all_expiries:
            raise RuntimeError("NSE response had no expiryDates")

        # Pick the requested expiry, else the nearest
        if expiry is None:
            chosen = all_expiries[0]      # nearest first
        else:
            iso_target = expiry.strftime("%d-%b-%Y")
            chosen = next((e for e in all_expiries if e == iso_target), all_expiries[0])
        chosen_dt = datetime.strptime(chosen, "%d-%b-%Y").date()

        rows = [r for r in records.get("data", []) if r.get("expiryDate") == chosen]
        quotes: list[ChainQuote] = []
        for r in rows:
            strike = float(r.get("strikePrice"))
            for side, ot in (("CE", OptionType.CALL), ("PE", OptionType.PUT)):
                leg = r.get(side)
                if leg is None:
                    continue
                last = float(leg.get("lastPrice") or 0)
                bid = float(leg.get("bidprice") or 0) or None
                ask = float(leg.get("askPrice") or 0) or None
                iv_pct = leg.get("impliedVolatility")
                iv = (float(iv_pct) / 100.0) if iv_pct not in (None, 0) else None
                quotes.append(ChainQuote(
                    strike=strike,
                    option_type=ot,
                    last_price=last,
                    bid=bid,
                    ask=ask,
                    volume=int(leg.get("totalTradedVolume") or 0),
                    open_interest=int(leg.get("openInterest") or 0),
                    iv=iv,
                ))

        try:
            lot = lot_size_on(symbol, date.today())
        except KeyError:
            lot = 75   # NIFTY default

        return ChainSnapshot(
            underlying=symbol,
            expiry=chosen_dt,
            spot=spot,
            ts=datetime.now(IST),
            risk_free_rate=self.risk_free_rate,
            quotes=tuple(quotes),
            lot_size=lot,
            futures_price=None,  # not in chain payload; can fetch separately
        )
