"""AngelOne SmartAPI adapter — headless TOTP auth.

Why this exists alongside Upstox/Fyers: AngelOne SmartAPI is the only major
Indian retail broker with an OFFICIAL headless TOTP login endpoint. No browser,
no Selenium. One call to `generateSession(client_code, password, totp)` returns
a JWT access token.

Reference: https://smartapi.angelbroking.com/docs

Required credentials (in .env):
    ANGELONE_CLIENT_CODE   — your trading account ID (e.g. A123456)
    ANGELONE_PASSWORD      — trading password (NOT MPIN)
    ANGELONE_API_KEY       — from SmartAPI dashboard "Trading API" app
    ANGELONE_TOTP_SECRET   — base32 string from 2FA setup

The adapter computes the current TOTP at auth time (RFC 6238, 30s window). Token
remains valid for the trading day; we refresh on demand or at session reconnect.

This is the recommended adapter for fully autonomous use.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import struct
import time
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


_ANGELONE_API_BASE = "https://apiconnect.angelbroking.com"


# AngelOne enums
_ORDER_TYPE_MAP = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.SL: "STOPLOSS_LIMIT",
    OrderType.SL_M: "STOPLOSS_MARKET",
}

_PRODUCT_MAP = {
    ProductType.INTRADAY: "INTRADAY",
    ProductType.DELIVERY: "DELIVERY",
    ProductType.NRML: "CARRYFORWARD",
    ProductType.COVER: "BO",   # AngelOne doesn't separate CO/BO cleanly
    ProductType.BRACKET: "BO",
}

# Exchange mapping
_EXCHANGE_MAP = {
    Exchange.NSE: "NSE",
    Exchange.BSE: "BSE",
    Exchange.NFO: "NFO",
    Exchange.BFO: "BFO",
    Exchange.MCX: "MCX",
    Exchange.CDS: "CDS",
}


# ---------------------------------------------------------------------------
# TOTP — RFC 6238
# ---------------------------------------------------------------------------


def compute_totp(secret_b32: str, *, time_step: int = 30, digits: int = 6) -> str:
    """Compute current TOTP code from a base32 secret. Standard RFC 6238."""
    # Strip whitespace, pad if needed
    s = secret_b32.replace(" ", "").upper()
    pad = (8 - len(s) % 8) % 8
    s = s + ("=" * pad)
    key = base64.b32decode(s)
    counter = int(time.time()) // time_step
    counter_bytes = struct.pack(">Q", counter)
    digest = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{code:0{digits}d}"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AngelOneAdapter(BrokerAdapter):
    """Headless AngelOne SmartAPI integration.

    Usage:
        adapter = AngelOneAdapter(
            client_code="A123456",
            password="...",
            api_key="...",
            totp_secret="JBSWY3DPEHPK3PXP",
            token_store=store,
        )
        await adapter.connect()   # one-shot, no browser
        bars = await adapter.historical_candles(instrument, start, end)
    """

    name = "angelone"

    def __init__(
        self,
        *,
        client_code: str,
        password: str,
        api_key: str,
        totp_secret: str,
        token_store: TokenStore,
    ) -> None:
        self.client_code = client_code
        self.password = password
        self.api_key = api_key
        self.totp_secret = totp_secret
        self.token_store = token_store
        self._http = httpx.AsyncClient(timeout=30.0)
        self._token: Optional[StoredToken] = None
        self._refresh_token: Optional[str] = None
        self._feed_token: Optional[str] = None
        self._load_existing_token()

    def _load_existing_token(self) -> None:
        self._token = self.token_store.load(self.name)
        if self._token and self._token.raw:
            self._refresh_token = self._token.raw.get("refreshToken")  # type: ignore[union-attr]
            self._feed_token = self._token.raw.get("feedToken")        # type: ignore[union-attr]

    # --- Common headers (per AngelOne docs) ---------------------------------

    def _common_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": self.api_key,
        }

    def _auth_headers(self) -> dict[str, str]:
        if not self.is_authenticated():
            raise BrokerAuthError("AngelOne not authenticated — call connect()")
        assert self._token is not None
        return {
            **self._common_headers(),
            "Authorization": f"Bearer {self._token.access_token}",
        }

    # --- Auth ---------------------------------------------------------------

    def authorize_url(self) -> str:
        """N/A for AngelOne — returns a hint string."""
        return "AngelOne uses headless TOTP auth — call adapter.connect() instead"

    async def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        """Compatibility shim — AngelOne doesn't use OAuth code flow.

        `code` is ignored. Calls connect() to perform the real TOTP auth.
        """
        return await self.connect()

    async def connect(self) -> dict[str, Any]:
        """Headless login. No browser, no human."""
        totp = compute_totp(self.totp_secret)
        body = {
            "clientcode": self.client_code,
            "password": self.password,
            "totp": totp,
        }
        resp = await self._http.post(
            f"{_ANGELONE_API_BASE}/rest/auth/angelbroking/user/v1/loginByPassword",
            json=body,
            headers=self._common_headers(),
        )
        if resp.status_code != 200:
            raise BrokerAuthError(f"AngelOne login failed: {resp.status_code} {resp.text}")
        data = resp.json()
        if not data.get("status"):
            raise BrokerAuthError(f"AngelOne login error: {data.get('message')} (code={data.get('errorcode')})")

        d = data["data"]
        access_token = d["jwtToken"]
        refresh_token = d.get("refreshToken")
        feed_token = d.get("feedToken")

        # AngelOne tokens last for the trading day; treat as 24h.
        expires_at = (datetime.now(IST) + timedelta(hours=24)).astimezone(timezone.utc)
        self._refresh_token = refresh_token
        self._feed_token = feed_token
        self._token = StoredToken(
            broker=self.name,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            user_id=self.client_code,
            raw={"refreshToken": refresh_token, "feedToken": feed_token, "raw": d},
        )
        self.token_store.save(self._token)
        log.info("AngelOne authenticated as %s", self.client_code)
        return d

    async def refresh_token(self) -> dict[str, Any]:
        if not self._refresh_token:
            return await self.connect()
        body = {"refreshToken": self._refresh_token}
        resp = await self._http.post(
            f"{_ANGELONE_API_BASE}/rest/auth/angelbroking/jwt/v1/generateTokens",
            json=body,
            headers=self._common_headers(),
        )
        if resp.status_code != 200 or not resp.json().get("status"):
            log.warning("Refresh failed; falling back to fresh TOTP login")
            return await self.connect()
        data = resp.json()["data"]
        # Update stored token
        if self._token:
            self._token = StoredToken(
                broker=self.name,
                access_token=data["jwtToken"],
                refresh_token=data.get("refreshToken", self._refresh_token),
                expires_at=(datetime.now(IST) + timedelta(hours=24)).astimezone(timezone.utc),
                user_id=self._token.user_id,
                raw=self._token.raw,
            )
            self.token_store.save(self._token)
            self._refresh_token = self._token.refresh_token
        return data

    def is_authenticated(self) -> bool:
        if self._token is None:
            return False
        if self._token.expires_at and self._token.expires_at < datetime.now(timezone.utc):
            return False
        return True

    # --- Internal request ---------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{_ANGELONE_API_BASE}{path}"
        resp = await self._http.request(method, url, headers=self._auth_headers(), **kwargs)
        if resp.status_code == 429:
            raise RateLimitError(f"AngelOne rate limit on {path}")
        if resp.status_code in (401, 403):
            raise BrokerAuthError(f"AngelOne auth failed on {path}: {resp.text}")
        if resp.status_code >= 400:
            raise BrokerError(f"AngelOne {path} {resp.status_code}: {resp.text}")
        return resp.json()

    # --- Symbol master ------------------------------------------------------

    async def fetch_symbol_master(self) -> list[Instrument]:
        """Public JSON file — no auth needed.

        URL: https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json
        ~150k instruments.
        """
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        resp = await self._http.get(url, timeout=120.0)
        if resp.status_code != 200:
            raise BrokerError(f"Symbol master fetch failed: {resp.status_code}")
        rows = resp.json()
        out: list[Instrument] = []
        for row in rows:
            try:
                exch = (row.get("exch_seg") or "").upper()
                if exch not in _EXCHANGE_MAP.values():
                    continue
                sym = row.get("symbol") or row.get("name") or ""
                tick = float(row.get("tick_size", 0)) / 100.0 if row.get("tick_size") else 0.05
                lot = int(row.get("lotsize") or 1)
                instrument_type_str = (row.get("instrumenttype") or "").upper()
                expiry_str = row.get("expiry")
                strike_raw = row.get("strike")
                strike = (float(strike_raw) / 100.0) if strike_raw and float(strike_raw) > 0 else None
                expiry = None
                if expiry_str:
                    try:
                        # AngelOne uses DDMMMYYYY e.g. "12DEC2024"
                        expiry = datetime.strptime(expiry_str, "%d%b%Y").date()
                    except ValueError:
                        pass
                # Map types
                if instrument_type_str in ("OPTIDX", "OPTSTK"):
                    if "CE" in sym.upper():
                        it = InstrumentType.CE
                    elif "PE" in sym.upper():
                        it = InstrumentType.PE
                    else:
                        continue
                elif instrument_type_str in ("FUTIDX", "FUTSTK", "FUTCOM", "FUTCUR"):
                    it = InstrumentType.FUT
                elif instrument_type_str == "AMXIDX":
                    it = InstrumentType.INDEX
                elif exch in ("NSE", "BSE") and not instrument_type_str:
                    it = InstrumentType.EQ
                else:
                    continue
                # Underlying
                underlying = (row.get("name") or "").upper() if it.is_derivative else None
                inst = Instrument(
                    symbol=(underlying or sym).upper(),
                    exchange=Exchange[exch] if exch in Exchange.__members__ else Exchange.NSE,
                    instrument_type=it,
                    lot_size=lot,
                    tick_size=tick,
                    expiry=expiry,
                    strike=strike,
                    underlying=underlying,
                    broker_keys={"angelone": str(row.get("token", ""))},
                )
                out.append(inst)
            except (ValueError, KeyError, TypeError):
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
        token = instrument.broker_keys.get("angelone") if instrument.broker_keys else None
        if not token:
            raise BrokerError(f"No angelone token for {instrument.id}")
        interval = {
            "1m": "ONE_MINUTE",
            "3m": "THREE_MINUTE",
            "5m": "FIVE_MINUTE",
            "10m": "TEN_MINUTE",
            "15m": "FIFTEEN_MINUTE",
            "30m": "THIRTY_MINUTE",
            "60m": "ONE_HOUR",
            "1h": "ONE_HOUR",
            "1d": "ONE_DAY",
        }.get(timeframe, "ONE_DAY")
        body = {
            "exchange": _EXCHANGE_MAP.get(instrument.exchange, "NSE"),
            "symboltoken": token,
            "interval": interval,
            "fromdate": f"{start.isoformat()} 09:15",
            "todate": f"{end.isoformat()} 15:30",
        }
        data = await self._request(
            "POST",
            "/rest/secure/angelbroking/historical/v1/getCandleData",
            json=body,
        )
        candles = data.get("data") or []
        out: list[Bar] = []
        for row in candles:
            # Format: [ts_iso, open, high, low, close, volume]
            ts_str, o, h, l, c, v = row
            ts = datetime.fromisoformat(ts_str).astimezone(IST)
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

    # --- Live ticks ---------------------------------------------------------

    async def subscribe_quotes(
        self,
        instruments: list[Instrument],
        on_quote: Callable[[LiveQuote], Awaitable[None]],
    ) -> None:
        """Subscribe to AngelOne SmartWebSocketV2 live ticks.

        Requires the official `smartapi-python` package (installed via `make install-broker`).
        """
        try:
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2  # type: ignore
        except ImportError:
            raise BrokerError(
                "AngelOne WebSocket requires `pip install smartapi-python`. "
                "Run `make install-broker`."
            )
        if not self.is_authenticated():
            raise BrokerAuthError("AngelOne not authenticated")
        assert self._token is not None and self._feed_token is not None

        # Build subscription tokens. AngelOne wants {exchange_type, tokens[]}.
        # exchange_type codes: 1=NSE_CM, 2=NSE_FO, 3=BSE_CM, 4=BSE_FO, 5=MCX_FO, 7=NCX_FO, 13=CDE_FO
        ex_type_map = {Exchange.NSE: 1, Exchange.NFO: 2, Exchange.BSE: 3, Exchange.BFO: 4, Exchange.MCX: 5, Exchange.CDS: 13}
        groups: dict[int, list[str]] = {}
        for i in instruments:
            ex_t = ex_type_map.get(i.exchange)
            tok = i.broker_keys.get("angelone")
            if not ex_t or not tok:
                continue
            groups.setdefault(ex_t, []).append(tok)
        if not groups:
            raise BrokerError("No subscribable AngelOne tokens for any instrument")

        token_lookup = {(ex_type_map.get(i.exchange), i.broker_keys.get("angelone")): i for i in instruments}
        loop = asyncio.get_running_loop()

        ws = SmartWebSocketV2(
            auth_token=self._token.access_token,
            api_key=self.api_key,
            client_code=self.client_code,
            feed_token=self._feed_token,
        )

        def on_data(wsapp, message):  # noqa: ANN001
            try:
                ex_t = message.get("exchange_type")
                tok = str(message.get("token", ""))
                inst = token_lookup.get((ex_t, tok))
                if inst is None:
                    return
                ltp = float(message.get("last_traded_price", 0)) / 100.0  # AngelOne sends paise
                bid = best_buy_qty = best_sell_qty = best_buy = best_sell = None
                depth = message.get("best_5_buy_data") or []
                if depth:
                    best_buy = float(depth[0].get("price", 0)) / 100.0
                    best_buy_qty = int(depth[0].get("quantity", 0))
                sdepth = message.get("best_5_sell_data") or []
                if sdepth:
                    best_sell = float(sdepth[0].get("price", 0)) / 100.0
                    best_sell_qty = int(sdepth[0].get("quantity", 0))
                quote = LiveQuote(
                    instrument=inst,
                    ts=datetime.now(IST),
                    last_price=ltp,
                    bid=best_buy,
                    ask=best_sell,
                    bid_qty=best_buy_qty,
                    ask_qty=best_sell_qty,
                    volume=int(message.get("volume_trade_for_the_day", 0)),
                    open_interest=int(message.get("open_interest", 0)) if message.get("open_interest") else None,
                )
                asyncio.run_coroutine_threadsafe(on_quote(quote), loop)
            except Exception:
                log.exception("AngelOne quote handler error")

        def on_open(wsapp):  # noqa: ANN001
            for ex_t, toks in groups.items():
                ws.subscribe(
                    correlation_id="qb",
                    mode=2,  # 1=LTP, 2=Quote, 3=Snap, 4=Depth20
                    token_list=[{"exchangeType": ex_t, "tokens": toks}],
                )

        ws.on_open = on_open
        ws.on_data = on_data
        ws.on_error = lambda wsapp, error: log.error("AngelOne WS error: %s", error)
        ws.on_close = lambda wsapp: log.info("AngelOne WS closed")

        await asyncio.to_thread(ws.connect)

    async def unsubscribe(self, instruments: list[Instrument]) -> None:
        # SmartWebSocketV2 manages this internally
        pass

    # --- Orders -------------------------------------------------------------

    async def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResponse:
        token = req.instrument.broker_keys.get("angelone")
        if not token:
            raise BrokerError(f"No angelone token for {req.instrument.id}")
        body = {
            "variety": "NORMAL",
            "tradingsymbol": req.instrument.symbol,
            "symboltoken": token,
            "transactiontype": "BUY" if req.side == Side.BUY else "SELL",
            "exchange": _EXCHANGE_MAP.get(req.instrument.exchange, "NSE"),
            "ordertype": _ORDER_TYPE_MAP.get(req.order_type, "MARKET"),
            "producttype": _PRODUCT_MAP.get(req.product, "CARRYFORWARD"),
            "duration": "DAY" if req.validity.value == "DAY" else "IOC",
            "price": str(req.price or 0),
            "triggerprice": str(req.trigger_price or 0),
            "quantity": str(req.quantity),
        }
        data = await self._request("POST", "/rest/secure/angelbroking/order/v1/placeOrder", json=body)
        if not data.get("status"):
            raise BrokerError(f"AngelOne place_order failed: {data}")
        order_id = data["data"]["orderid"]
        return BrokerOrderResponse(
            broker_order_id=order_id,
            client_order_id=req.client_order_id,
            status="success",
            accepted_at=datetime.now(IST),
            raw=data,
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        body = {"variety": "NORMAL", "orderid": broker_order_id}
        await self._request("POST", "/rest/secure/angelbroking/order/v1/cancelOrder", json=body)

    async def modify_order(
        self,
        broker_order_id: str,
        *,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> BrokerOrderResponse:
        body: dict[str, Any] = {"variety": "NORMAL", "orderid": broker_order_id}
        if quantity is not None:
            body["quantity"] = str(quantity)
        if price is not None:
            body["price"] = str(price)
        if trigger_price is not None:
            body["triggerprice"] = str(trigger_price)
        data = await self._request("POST", "/rest/secure/angelbroking/order/v1/modifyOrder", json=body)
        return BrokerOrderResponse(
            broker_order_id=broker_order_id,
            client_order_id=None,
            status="success" if data.get("status") else "failed",
            accepted_at=datetime.now(IST),
            raw=data,
        )

    # --- Account ------------------------------------------------------------

    async def positions(self) -> list[BrokerPosition]:
        data = await self._request("GET", "/rest/secure/angelbroking/order/v1/getPosition")
        out: list[BrokerPosition] = []
        for row in data.get("data") or []:
            sym = row.get("tradingsymbol", "")
            ex = Exchange[row.get("exchange", "NSE").upper()] if row.get("exchange", "NSE").upper() in Exchange.__members__ else Exchange.NSE
            inst = Instrument(symbol=sym.split("-")[0], exchange=ex, instrument_type=InstrumentType.EQ)
            out.append(BrokerPosition(
                instrument=inst,
                quantity=int(row.get("netqty", 0)),
                avg_price=float(row.get("avgnetprice", 0) or 0),
                last_price=float(row.get("ltp", 0) or 0),
                realized_pnl=float(row.get("realised", 0) or 0),
                unrealized_pnl=float(row.get("unrealised", 0) or 0),
                product=ProductType.NRML,
            ))
        return out

    async def holdings(self) -> list[BrokerPosition]:
        data = await self._request("GET", "/rest/secure/angelbroking/portfolio/v1/getHolding")
        out: list[BrokerPosition] = []
        for row in (data.get("data") or {}).get("holdings", []):
            sym = row.get("tradingsymbol", "")
            inst = Instrument(symbol=sym, exchange=Exchange.NSE, instrument_type=InstrumentType.EQ)
            out.append(BrokerPosition(
                instrument=inst,
                quantity=int(row.get("quantity", 0)),
                avg_price=float(row.get("averageprice", 0) or 0),
                last_price=float(row.get("ltp", 0) or 0),
                realized_pnl=0.0,
                unrealized_pnl=float(row.get("profitandloss", 0) or 0),
                product=ProductType.DELIVERY,
            ))
        return out

    async def funds(self) -> dict[str, float]:
        data = await self._request("GET", "/rest/secure/angelbroking/user/v1/getRMS")
        d = data.get("data") or {}
        return {
            "available_cash": float(d.get("availablecash", 0) or 0),
            "used_margin": float(d.get("utiliseddebits", 0) or 0),
        }
