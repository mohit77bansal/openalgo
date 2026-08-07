"""Fyers v3 broker adapter.

Reference: https://myapi.fyers.in/docsv3

OAuth flow:
    1. authorize_url() returns the Fyers login URL with `client_id` & `redirect_uri`.
    2. After login, Fyers redirects with `auth_code`.
    3. App POSTs auth_code + appIdHash → access_token (~24h validity).

Symbol convention differs from Upstox:
    Upstox: instrument_key = "NSE_FO|123456"
    Fyers:  symbol = "NSE:NIFTY24DEC25000CE"
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
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
from qbacktest.brokers.rate_limit import fyers_limits
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


_FYERS_API_BASE = "https://api-t1.fyers.in/api/v3"
_FYERS_AUTHORIZE = "https://api-t1.fyers.in/api/v3/generate-authcode"
_FYERS_TOKEN = "https://api-t1.fyers.in/api/v3/validate-authcode"


# Fyers numeric order-type codes
# 1: limit, 2: market, 3: stop (SL-M), 4: stoplimit (SL)
_ORDER_TYPE_MAP = {
    OrderType.LIMIT: 1,
    OrderType.MARKET: 2,
    OrderType.SL_M: 3,
    OrderType.SL: 4,
}

# 1: BUY, -1: SELL
_SIDE_MAP = {Side.BUY: 1, Side.SELL: -1}

# Product types
# CNC, INTRADAY, MARGIN, CO, BO
_PRODUCT_MAP = {
    ProductType.DELIVERY: "CNC",
    ProductType.INTRADAY: "INTRADAY",
    ProductType.NRML: "MARGIN",
    ProductType.COVER: "CO",
    ProductType.BRACKET: "BO",
}


def _fyers_app_id_hash(app_id: str, secret_key: str) -> str:
    """SHA-256 of app_id:secret_key — required at token exchange."""
    return hashlib.sha256(f"{app_id}:{secret_key}".encode("utf-8")).hexdigest()


def _fyers_symbol(instrument: Instrument) -> str:
    """Translate our canonical id to Fyers' symbol format.

    Equity:  NSE:RELIANCE-EQ
    Index:   NSE:NIFTY50-INDEX
    Future:  NSE:NIFTY24DECFUT
    Option:  NSE:NIFTY24DEC25000CE
    """
    if instrument.instrument_type == InstrumentType.EQ:
        return f"{instrument.exchange.value}:{instrument.symbol}-EQ"
    if instrument.instrument_type == InstrumentType.INDEX:
        return f"{instrument.exchange.value}:{instrument.symbol}-INDEX"
    if instrument.instrument_type == InstrumentType.FUT:
        assert instrument.expiry
        # Fyers monthly format: YYDDMMM (e.g. NIFTY24DECFUT for Dec 2024 monthly)
        # Weekly: YYDDMMMSTRIKE format... but Fyers uses different scheme for weeklies.
        return f"{instrument.exchange.value}:{instrument.symbol}{instrument.expiry:%y%b}FUT".upper()
    # Option
    assert instrument.expiry and instrument.strike is not None
    strike_part = f"{int(instrument.strike)}" if float(instrument.strike).is_integer() else f"{instrument.strike:g}"
    return f"{instrument.exchange.value}:{instrument.symbol}{instrument.expiry:%y%b}{strike_part}{instrument.instrument_type.value}".upper()


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class FyersAdapter(BrokerAdapter):
    name = "fyers"

    def __init__(
        self,
        *,
        client_id: str,
        secret_key: str,
        redirect_uri: str,
        token_store: TokenStore,
    ) -> None:
        self.client_id = client_id
        self.secret_key = secret_key
        self.redirect_uri = redirect_uri
        self.token_store = token_store
        self.rate_limiter = fyers_limits()
        self._http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
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
            "state": "qb",
        }
        return f"{_FYERS_AUTHORIZE}?{urllib.parse.urlencode(params)}"

    async def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        body = {
            "grant_type": "authorization_code",
            "appIdHash": _fyers_app_id_hash(self.client_id, self.secret_key),
            "code": code,
        }
        await self.rate_limiter.acquire()
        resp = await self._http.post(_FYERS_TOKEN, json=body)
        if resp.status_code != 200:
            raise BrokerAuthError(f"Fyers token exchange failed: {resp.status_code} {resp.text}")
        data = resp.json()
        if data.get("s") != "ok":
            raise BrokerAuthError(f"Fyers token error: {data}")
        access_token = data.get("access_token")
        # Fyers tokens generally last until next day's market open.
        expires_at = (datetime.now(IST) + timedelta(hours=24)).astimezone(timezone.utc)
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
        # Fyers v3 has refresh tokens but practical use is limited; treat as needing re-login.
        raise BrokerAuthError("Fyers refresh not implemented in v0.1; user must re-login")

    def is_authenticated(self) -> bool:
        if self._token is None:
            return False
        if self._token.expires_at and self._token.expires_at < datetime.now(timezone.utc):
            return False
        return True

    def _auth_headers(self) -> dict[str, str]:
        if not self.is_authenticated():
            raise BrokerAuthError("Fyers token absent or expired")
        assert self._token is not None
        return {
            # Fyers expects "Authorization: <client_id>:<token>"
            "Authorization": f"{self.client_id}:{self._token.access_token}",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        await self.rate_limiter.acquire()
        url = f"{_FYERS_API_BASE}{path}"
        resp = await self._http.request(method, url, headers=self._auth_headers(), **kwargs)
        if resp.status_code == 429:
            raise RateLimitError(f"Fyers rate limit on {path}")
        if resp.status_code in (401, 403):
            raise BrokerAuthError(f"Fyers auth failed: {resp.text}")
        if resp.status_code >= 400:
            raise BrokerError(f"Fyers {path} {resp.status_code}: {resp.text}")
        return resp.json()

    # --- Symbol master ------------------------------------------------------

    async def fetch_symbol_master(self) -> list[Instrument]:
        """Fetch Fyers' symbol master (CSV).

        Fyers publishes per-segment files at:
          https://public.fyers.in/sym_details/NSE_FO_sym_master.csv
          https://public.fyers.in/sym_details/NSE_CM_sym_master.csv
        """
        urls = [
            ("NSE_CM", "https://public.fyers.in/sym_details/NSE_CM.csv"),
            ("NSE_FO", "https://public.fyers.in/sym_details/NSE_FO.csv"),
            ("BSE_CM", "https://public.fyers.in/sym_details/BSE_CM.csv"),
            ("BSE_FO", "https://public.fyers.in/sym_details/BSE_FO.csv"),
        ]
        out: list[Instrument] = []
        for seg, url in urls:
            await self.rate_limiter.acquire()
            try:
                resp = await self._http.get(url)
                if resp.status_code != 200:
                    log.warning("Fyers %s symbol master failed: %s", seg, resp.status_code)
                    continue
            except httpx.RequestError as e:
                log.warning("Fyers %s download error: %s", seg, e)
                continue
            import csv
            import io
            reader = csv.reader(io.StringIO(resp.text))
            for row in reader:
                if not row or row[0].startswith("Fytoken"):
                    continue
                try:
                    # Fyers schema (varies): [Fytoken, Symbol, ExchSeg, ..., LotSize, TickSize, ..., Expiry, Strike, ...]
                    fy_symbol = row[1]
                    lot = int(float(row[3])) if len(row) > 3 and row[3] else 1
                    # We can't reliably parse all fields without per-file schema.
                    # For now, store as a raw lookup table and extract on demand.
                    inst = Instrument(
                        symbol=fy_symbol.split(":")[-1].rstrip("CE").rstrip("PE")[:50],
                        exchange=Exchange.NSE if "NSE" in seg else Exchange.BSE,
                        instrument_type=InstrumentType.EQ,  # placeholder — TODO refine
                        lot_size=lot,
                        broker_keys={"fyers": fy_symbol},
                    )
                    out.append(inst)
                except (ValueError, IndexError):
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
        """Fyers historical OHLC.

        Endpoint: /data/history
        Resolution: "1", "5", "15", "30", "60", "D"
        """
        resolution = {
            "1m": "1",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "60m": "60",
            "1h": "60",
            "1d": "D",
        }.get(timeframe, "D")
        symbol = instrument.broker_keys.get("fyers") or _fyers_symbol(instrument)
        params = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": start.isoformat(),
            "range_to": end.isoformat(),
            "cont_flag": "1",
        }
        data = await self._request("GET", "/data/history", params=params)
        candles = data.get("candles", [])
        out: list[Bar] = []
        for row in candles:
            ts_epoch, o, h, l, c, v = row[:6]
            ts = datetime.fromtimestamp(ts_epoch, tz=IST)
            out.append(Bar(
                instrument=instrument,
                ts=ts,
                timeframe=timeframe,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=int(v),
            ))
        out.sort(key=lambda b: b.ts)
        return out

    # --- Live data ----------------------------------------------------------

    async def subscribe_quotes(
        self,
        instruments: list[Instrument],
        on_quote: Callable[[LiveQuote], Awaitable[None]],
    ) -> None:
        """Subscribe to Fyers WebSocket data feed.

        Requires `fyers-apiv3` SDK for proper Protobuf decoding.
        """
        try:
            from fyers_apiv3.FyersWebsocket import data_ws  # type: ignore
        except ImportError:
            raise BrokerError(
                "Fyers WebSocket requires `pip install fyers-apiv3`. "
                "Install via `make install-broker`."
            )

        if not self.is_authenticated():
            raise BrokerAuthError("Fyers not authenticated")
        assert self._token is not None
        symbols = [i.broker_keys.get("fyers") or _fyers_symbol(i) for i in instruments]
        loop = asyncio.get_running_loop()

        def _on_message(msg):  # noqa: ANN001
            try:
                sym = msg.get("symbol")
                inst = next((i for i in instruments if (i.broker_keys.get("fyers") or _fyers_symbol(i)) == sym), None)
                if inst is None:
                    return
                ts = datetime.fromtimestamp(msg.get("timestamp", 0), tz=IST)
                quote = LiveQuote(
                    instrument=inst,
                    ts=ts,
                    last_price=float(msg.get("ltp", 0)),
                    bid=float(msg.get("bid_price")) if msg.get("bid_price") else None,
                    ask=float(msg.get("ask_price")) if msg.get("ask_price") else None,
                    bid_qty=int(msg.get("bid_size")) if msg.get("bid_size") else None,
                    ask_qty=int(msg.get("ask_size")) if msg.get("ask_size") else None,
                    volume=int(msg.get("volume", 0)),
                    open_interest=int(msg.get("oi", 0)) if msg.get("oi") else None,
                )
                asyncio.run_coroutine_threadsafe(on_quote(quote), loop)
            except Exception as e:
                log.exception("Quote callback error: %s", e)

        ws = data_ws.FyersDataSocket(
            access_token=f"{self.client_id}:{self._token.access_token}",
            log_path="",
            litemode=False,
            write_to_file=False,
            reconnect=True,
            on_connect=lambda: ws.subscribe(symbols=symbols, data_type="SymbolUpdate"),
            on_close=lambda *_: log.info("Fyers WS closed"),
            on_error=lambda e: log.error("Fyers WS error: %s", e),
            on_message=_on_message,
        )
        await asyncio.to_thread(ws.connect)

    async def unsubscribe(self, instruments: list[Instrument]) -> None:
        # SDK manages this internally; in production we'd hold the ws reference.
        pass

    # --- Orders -------------------------------------------------------------

    async def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResponse:
        symbol = req.instrument.broker_keys.get("fyers") or _fyers_symbol(req.instrument)
        body = {
            "symbol": symbol,
            "qty": req.quantity,
            "type": _ORDER_TYPE_MAP.get(req.order_type, 2),
            "side": _SIDE_MAP.get(req.side, 1),
            "productType": _PRODUCT_MAP.get(req.product, "MARGIN"),
            "limitPrice": req.price or 0,
            "stopPrice": req.trigger_price or 0,
            "validity": "DAY" if req.validity.value == "DAY" else "IOC",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderTag": req.tag or "",
        }
        data = await self._request("POST", "/orders/sync", json=body)
        if data.get("s") != "ok":
            raise BrokerError(f"Fyers place_order error: {data}")
        return BrokerOrderResponse(
            broker_order_id=str(data.get("id", "")),
            client_order_id=req.client_order_id,
            status=data.get("s", "ok"),
            accepted_at=datetime.now(IST),
            raw=data,
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        await self._request("DELETE", "/orders/sync", json={"id": broker_order_id})

    async def modify_order(
        self,
        broker_order_id: str,
        *,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> BrokerOrderResponse:
        body: dict[str, Any] = {"id": broker_order_id}
        if quantity is not None:
            body["qty"] = quantity
        if price is not None:
            body["limitPrice"] = price
        if trigger_price is not None:
            body["stopPrice"] = trigger_price
        data = await self._request("PATCH", "/orders/sync", json=body)
        return BrokerOrderResponse(
            broker_order_id=broker_order_id,
            client_order_id=None,
            status=data.get("s", "ok"),
            accepted_at=datetime.now(IST),
            raw=data,
        )

    # --- Account ------------------------------------------------------------

    async def positions(self) -> list[BrokerPosition]:
        data = await self._request("GET", "/positions")
        out = []
        for row in data.get("netPositions", []):
            sym = row.get("symbol", "")
            inst = Instrument(symbol=sym.split(":")[-1], exchange=Exchange.NSE, instrument_type=InstrumentType.EQ)
            out.append(BrokerPosition(
                instrument=inst,
                quantity=int(row.get("netQty", 0)),
                avg_price=float(row.get("avgPrice", 0)),
                last_price=float(row.get("ltp", 0)),
                realized_pnl=float(row.get("realized_profit", 0)),
                unrealized_pnl=float(row.get("unrealized_profit", 0)),
                product=ProductType.NRML,
            ))
        return out

    async def holdings(self) -> list[BrokerPosition]:
        data = await self._request("GET", "/holdings")
        out = []
        for row in data.get("holdings", []):
            sym = row.get("symbol", "")
            inst = Instrument(symbol=sym.split(":")[-1], exchange=Exchange.NSE, instrument_type=InstrumentType.EQ)
            out.append(BrokerPosition(
                instrument=inst,
                quantity=int(row.get("quantity", 0)),
                avg_price=float(row.get("costPrice", 0)),
                last_price=float(row.get("ltp", 0)),
                realized_pnl=0.0,
                unrealized_pnl=float(row.get("pl", 0)),
                product=ProductType.DELIVERY,
            ))
        return out

    async def funds(self) -> dict[str, float]:
        data = await self._request("GET", "/funds")
        funds = data.get("fund_limit", [])
        avail = next((f.get("equityAmount", 0) for f in funds if f.get("title") == "Available Balance"), 0)
        used = next((f.get("equityAmount", 0) for f in funds if f.get("title") == "Utilized Amount"), 0)
        return {"available_cash": float(avail), "used_margin": float(used)}
