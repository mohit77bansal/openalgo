"""Upstox v2 broker adapter.

Reference: https://upstox.com/developer/api-documentation/

OAuth flow:
    1. User visits authorize_url() → Upstox login page → redirected back with `code`.
    2. App POSTs code to /v2/login/authorization/token → access_token + (no refresh).
    3. Token is valid until ~03:30 IST next day (daily expiry — Upstox quirk).
    4. User must re-auth daily; we surface this clearly in the UI.

The Python SDK (`upstox-python-sdk`) is used where available; for endpoints not in
the SDK or for streaming, we fall back to direct HTTPS/WebSocket.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

from qbacktest.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResponse,
    BrokerPosition,
    LiveQuote,
)
from qbacktest.brokers.rate_limit import upstox_limits
from qbacktest.brokers.token_store import StoredToken, TokenStore
from qbacktest.core.calendar import IST
from qbacktest.core.exceptions import BrokerAuthError, BrokerError, RateLimitError
from qbacktest.core.types import (
    Bar,
    Exchange,
    Instrument,
    InstrumentType,
    OrderType,
    ProductType,
    Side,
)

log = logging.getLogger(__name__)


_UPSTOX_API_BASE = "https://api.upstox.com/v2"
_UPSTOX_AUTHORIZE = "https://api.upstox.com/v2/login/authorization/dialog"
_UPSTOX_TOKEN = "https://api.upstox.com/v2/login/authorization/token"


# Upstox order type strings
_ORDER_TYPE_MAP = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.SL: "SL",
    OrderType.SL_M: "SL-M",
}

_PRODUCT_MAP = {
    ProductType.INTRADAY: "I",
    ProductType.DELIVERY: "D",
    ProductType.NRML: "D",   # Upstox uses "D" for delivery; F&O carry-forward is also "D" historically
    ProductType.COVER: "CO",
}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class UpstoxAdapter(BrokerAdapter):
    """Upstox v2 adapter."""

    name = "upstox"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        token_store: TokenStore,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_store = token_store
        self.rate_limiter = upstox_limits()
        self._http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        self._ws = None  # set on subscribe
        self._token: Optional[StoredToken] = None
        self._load_existing_token()

    def _load_existing_token(self) -> None:
        self._token = self.token_store.load(self.name)

    # --- Auth ---------------------------------------------------------------

    def authorize_url(self) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
        }
        return f"{_UPSTOX_AUTHORIZE}?{urllib.parse.urlencode(params)}"

    async def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        body = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }
        await self.rate_limiter.acquire()
        resp = await self._http.post(
            _UPSTOX_TOKEN,
            data=body,
            headers={"accept": "application/json", "Api-Version": "2.0"},
        )
        if resp.status_code != 200:
            raise BrokerAuthError(f"Upstox token exchange failed: {resp.status_code} {resp.text}")
        data = resp.json()
        access_token = data.get("access_token")
        if not access_token:
            raise BrokerAuthError(f"No access_token in response: {data}")
        # Upstox tokens expire at next day 03:30 IST. Compute that.
        now_ist = datetime.now(IST)
        next_day = (now_ist + timedelta(days=1)).replace(hour=3, minute=30, second=0, microsecond=0)
        expires_at = next_day.astimezone(timezone.utc)
        self._token = StoredToken(
            broker=self.name,
            access_token=access_token,
            refresh_token=None,
            expires_at=expires_at,
            user_id=data.get("user_id"),
            raw=data,
        )
        self.token_store.save(self._token)
        return data

    async def refresh_token(self) -> dict[str, Any]:
        # Upstox v2 does NOT support refresh tokens — daily re-login required.
        raise BrokerAuthError("Upstox v2 has no refresh-token flow; user must re-login daily")

    def is_authenticated(self) -> bool:
        if self._token is None:
            return False
        if self._token.expires_at and self._token.expires_at < datetime.now(timezone.utc):
            return False
        return True

    # --- Internal helpers ---------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        if not self.is_authenticated():
            raise BrokerAuthError("Upstox token absent or expired — re-authorize")
        assert self._token is not None
        return {
            "Authorization": f"Bearer {self._token.access_token}",
            "Accept": "application/json",
            "Api-Version": "2.0",
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        await self.rate_limiter.acquire()
        url = f"{_UPSTOX_API_BASE}{path}"
        resp = await self._http.request(method, url, headers=self._auth_headers(), **kwargs)
        if resp.status_code == 429:
            raise RateLimitError(f"Upstox rate limit hit on {path}")
        if resp.status_code in (401, 403):
            raise BrokerAuthError(f"Upstox auth failed on {path}: {resp.text}")
        if resp.status_code >= 400:
            raise BrokerError(f"Upstox {path} {resp.status_code}: {resp.text}")
        return resp.json()

    # --- Symbol master ------------------------------------------------------

    async def fetch_symbol_master(self) -> list[Instrument]:
        """Fetch & parse Upstox's instrument master (CSV/JSON, ~1M rows).

        Returns Instruments. Mapping from Upstox `instrument_key` is preserved on
        the Instrument's `broker_keys` field.
        """
        # Upstox publishes instrument master at:
        # https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz
        url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
        await self.rate_limiter.acquire()
        resp = await self._http.get(url)
        if resp.status_code != 200:
            raise BrokerError(f"Symbol master download failed: {resp.status_code}")
        import gzip
        import io
        import csv

        out: list[Instrument] = []
        with gzip.open(io.BytesIO(resp.content), mode="rt") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    seg = (row.get("segment") or row.get("exchange") or "").upper()
                    name = row.get("name") or ""
                    inst_type_raw = (row.get("instrument_type") or "").upper()
                    expiry_str = row.get("expiry")
                    strike_str = row.get("strike")
                    lot = int(row.get("lot_size") or 1)
                    instrument_key = row.get("instrument_key") or ""
                    tick = float(row.get("tick_size") or 0.05)
                    if seg.startswith("NSE_EQ") or seg == "NSE":
                        if inst_type_raw == "INDEX":
                            it = InstrumentType.INDEX
                            ex = Exchange.NSE
                            sym = name
                        else:
                            it = InstrumentType.EQ
                            ex = Exchange.NSE
                            sym = row.get("trading_symbol", "")
                        inst = Instrument(symbol=sym, exchange=ex, instrument_type=it, lot_size=lot, tick_size=tick, broker_keys={"upstox": instrument_key})
                    elif seg.startswith("NSE_FO"):
                        # F&O: parse instrument_type
                        if inst_type_raw == "FUT":
                            it = InstrumentType.FUT
                        elif inst_type_raw == "CE":
                            it = InstrumentType.CE
                        elif inst_type_raw == "PE":
                            it = InstrumentType.PE
                        else:
                            continue
                        underlying = row.get("name") or ""
                        sym = row.get("trading_symbol", underlying)
                        try:
                            expiry = datetime.fromisoformat(expiry_str).date() if expiry_str else None
                        except ValueError:
                            expiry = None
                        try:
                            strike = float(strike_str) if strike_str else None
                        except ValueError:
                            strike = None
                        inst = Instrument(
                            symbol=underlying,
                            exchange=Exchange.NFO,
                            instrument_type=it,
                            lot_size=lot,
                            tick_size=tick,
                            expiry=expiry,
                            strike=strike,
                            underlying=underlying,
                            broker_keys={"upstox": instrument_key},
                        )
                    else:
                        continue
                    out.append(inst)
                except (ValueError, KeyError):
                    continue
        return out

    # --- Historical ---------------------------------------------------------

    async def historical_candles(
        self,
        instrument: Instrument,
        start: date,
        end: date,
        timeframe: str = "1d",
    ) -> list[Bar]:
        """Fetch historical OHLC bars from Upstox.

        Path: /historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}
        Intervals: "1minute", "30minute", "day", "week", "month"
        """
        instrument_key = instrument.broker_keys.get("upstox") if instrument.broker_keys else None
        if not instrument_key:
            raise BrokerError(f"No upstox instrument_key for {instrument.id}")
        interval = {
            "1m": "1minute",
            "30m": "30minute",
            "1d": "day",
            "1w": "week",
            "1mo": "month",
        }.get(timeframe, "day")
        path = f"/historical-candle/{urllib.parse.quote(instrument_key, safe='')}/{interval}/{end.isoformat()}/{start.isoformat()}"
        data = await self._request("GET", path)
        candles = data.get("data", {}).get("candles", [])
        out = []
        for row in candles:
            # Upstox candle format: [ts, open, high, low, close, volume, oi]
            ts_s, o, h, l, c, v, *rest = row
            ts = datetime.fromisoformat(ts_s).astimezone(IST)
            oi = rest[0] if rest else None
            out.append(Bar(
                instrument=instrument,
                ts=ts,
                timeframe=timeframe,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=int(v),
                open_interest=int(oi) if oi else None,
            ))
        out.sort(key=lambda b: b.ts)
        return out

    # --- Live data ----------------------------------------------------------

    async def subscribe_quotes(
        self,
        instruments: list[Instrument],
        on_quote: Callable[[LiveQuote], Awaitable[None]],
    ) -> None:
        """Subscribe to Upstox WebSocket market data feed.

        Upstox provides a Protobuf-encoded feed at
        wss://api.upstox.com/v3/feed/market-data-feed/

        For brevity, a simplified JSON feed is used here. The real implementation
        would unmarshal Protobuf via the upstox-python-sdk's MarketDataStreamer.
        """
        # NOTE: Requires `upstox-python-sdk` for Protobuf decoding. Stubbed here.
        try:
            import upstox_client  # type: ignore
            from upstox_client.feeder.market_data_streamer import MarketDataStreamerV3  # type: ignore
        except ImportError:
            raise BrokerError(
                "Upstox WebSocket requires `pip install upstox-python-sdk`. "
                "Install via `make install-broker`."
            )

        if not self.is_authenticated():
            raise BrokerAuthError("Upstox not authenticated")
        assert self._token is not None
        cfg = upstox_client.Configuration()
        cfg.access_token = self._token.access_token
        keys = [i.broker_keys.get("upstox") for i in instruments if i.broker_keys.get("upstox")]
        if not keys:
            raise BrokerError("No upstox instrument_keys for any subscribed instrument")
        streamer = MarketDataStreamerV3(cfg, keys, "full")
        # We adapt the SDK callback into our async on_quote
        loop = asyncio.get_running_loop()

        def _cb(message):  # noqa: ANN001
            # Decode minimal fields. Real impl would parse all OI/depth.
            for feed in getattr(message, "feeds", {}).items():
                key, payload = feed
                inst = next((i for i in instruments if i.broker_keys.get("upstox") == key), None)
                if inst is None:
                    continue
                ltp = float(getattr(payload.ltpc, "ltp", 0))
                ts_ms = getattr(payload.ltpc, "ltt", 0)
                ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=IST) if ts_ms else datetime.now(IST)
                quote = LiveQuote(instrument=inst, ts=ts, last_price=ltp)
                asyncio.run_coroutine_threadsafe(on_quote(quote), loop)

        streamer.on("message", _cb)
        await asyncio.to_thread(streamer.connect)

    async def unsubscribe(self, instruments: list[Instrument]) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # --- Orders -------------------------------------------------------------

    async def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResponse:
        instrument_key = req.instrument.broker_keys.get("upstox")
        if not instrument_key:
            raise BrokerError(f"No upstox instrument_key for {req.instrument.id}")
        body = {
            "quantity": req.quantity,
            "product": _PRODUCT_MAP.get(req.product, "D"),
            "validity": "DAY" if req.validity.value == "DAY" else "IOC",
            "price": req.price or 0,
            "tag": req.tag or "",
            "instrument_token": instrument_key,
            "order_type": _ORDER_TYPE_MAP.get(req.order_type, "MARKET"),
            "transaction_type": "BUY" if req.side == Side.BUY else "SELL",
            "disclosed_quantity": 0,
            "trigger_price": req.trigger_price or 0,
            "is_amo": False,
        }
        data = await self._request("POST", "/order/place", json=body)
        order_id = data.get("data", {}).get("order_id")
        if not order_id:
            raise BrokerError(f"No order_id in response: {data}")
        return BrokerOrderResponse(
            broker_order_id=order_id,
            client_order_id=req.client_order_id,
            status=data.get("status", "success"),
            accepted_at=datetime.now(IST),
            raw=data,
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        await self._request("DELETE", f"/order/cancel?order_id={broker_order_id}")

    async def modify_order(
        self,
        broker_order_id: str,
        *,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> BrokerOrderResponse:
        body: dict[str, Any] = {"order_id": broker_order_id}
        if quantity is not None:
            body["quantity"] = quantity
        if price is not None:
            body["price"] = price
        if trigger_price is not None:
            body["trigger_price"] = trigger_price
        data = await self._request("PUT", "/order/modify", json=body)
        return BrokerOrderResponse(
            broker_order_id=broker_order_id,
            client_order_id=None,
            status=data.get("status", "success"),
            accepted_at=datetime.now(IST),
            raw=data,
        )

    # --- Account ------------------------------------------------------------

    async def positions(self) -> list[BrokerPosition]:
        data = await self._request("GET", "/portfolio/short-term-positions")
        out = []
        for row in data.get("data", []):
            # Map back to our Instrument; we'd need the symbol master loaded for full fidelity.
            # Build a minimal Instrument from the response.
            sym = row.get("trading_symbol", "")
            ex = Exchange.NSE  # Best effort
            it_str = row.get("instrument_type", "EQ").upper()
            it = InstrumentType(it_str) if it_str in [t.value for t in InstrumentType] else InstrumentType.EQ
            inst = Instrument(symbol=sym, exchange=ex, instrument_type=it, lot_size=int(row.get("lot_size", 1)))
            out.append(BrokerPosition(
                instrument=inst,
                quantity=int(row.get("quantity", 0)),
                avg_price=float(row.get("average_price", 0)),
                last_price=float(row.get("last_price", 0)),
                realized_pnl=float(row.get("realised", 0)),
                unrealized_pnl=float(row.get("unrealised", 0)),
                product=ProductType.NRML,  # caller should map from row.product
            ))
        return out

    async def holdings(self) -> list[BrokerPosition]:
        data = await self._request("GET", "/portfolio/long-term-holdings")
        out = []
        for row in data.get("data", []):
            sym = row.get("trading_symbol", "")
            inst = Instrument(symbol=sym, exchange=Exchange.NSE, instrument_type=InstrumentType.EQ)
            out.append(BrokerPosition(
                instrument=inst,
                quantity=int(row.get("quantity", 0)),
                avg_price=float(row.get("average_price", 0)),
                last_price=float(row.get("last_price", 0)),
                realized_pnl=0.0,
                unrealized_pnl=float(row.get("pnl", 0)),
                product=ProductType.DELIVERY,
            ))
        return out

    async def funds(self) -> dict[str, float]:
        data = await self._request("GET", "/user/get-funds-and-margin")
        equity = data.get("data", {}).get("equity", {})
        return {
            "available_cash": float(equity.get("available_margin", 0)),
            "used_margin": float(equity.get("used_margin", 0)),
        }
