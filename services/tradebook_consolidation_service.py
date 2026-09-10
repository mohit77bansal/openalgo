"""Consolidate broker tradebook fills into round-trips — ALL math in the backend.

The broker tradebook lists each fill as a separate BUY/SELL leg with no P&L.
This service pairs them (FIFO, per symbol) into closed round-trips and computes
gross P&L, fees, net P&L, and return % using the SAME ``qbacktest`` cost model
the backtester uses — so live and backtested economics can never drift. It also
returns a ledger (initial capital vs the broker's actual balance), which is the
exact realised-P&L truth because the broker's balance already nets every charge.

Design note: fees here are a MODEL estimate (documented Indian rates). The
ledger is the ground truth — when they disagree, trust the ledger.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

from qbacktest.costs import CostModel, zerodha_cost_model
from qbacktest.core.types import Exchange, InstrumentType, ProductType, Side
from services.funds_service import get_funds
from services.tradebook_service import get_tradebook
from utils.logging import get_logger

logger = get_logger(__name__)

_COST = CostModel(brokerage=zerodha_cost_model())

_EXCHANGE_MAP = {
    "NSE": Exchange.NSE, "BSE": Exchange.BSE, "NFO": Exchange.NFO,
    "BFO": Exchange.BFO, "MCX": Exchange.MCX, "CDS": Exchange.CDS,
}
_PRODUCT_MAP = {
    "MIS": ProductType.INTRADAY, "CNC": ProductType.DELIVERY, "NRML": ProductType.NRML,
}


def _instrument_type(symbol: str, exchange: str) -> InstrumentType:
    sym = (symbol or "").upper()
    if (exchange or "").upper() in ("NFO", "BFO", "MCX", "CDS"):
        if sym.endswith("CE"):
            return InstrumentType.CE
        if sym.endswith("PE"):
            return InstrumentType.PE
        return InstrumentType.FUT
    return InstrumentType.EQ


def _parse_date(ts: str) -> date:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime((ts or "")[:19], fmt).date()
        except (ValueError, TypeError):
            continue
    return datetime.now().date()


def _parse_ts(ts: str) -> float:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime((ts or "")[:19], fmt).timestamp()
        except (ValueError, TypeError):
            continue
    return 0.0


_ANGEL_PRODUCT = {"MIS": "INTRADAY", "CNC": "DELIVERY", "NRML": "CARRYFORWARD"}
_charges_cache: dict[tuple, float] = {}


def _angel_actual_charges(
    auth_token: str, symbol: str, exchange: str, product: str,
    buy_price: float, sell_price: float, qty: int,
) -> float | None:
    """Exact round-trip charges from AngelOne's estimateCharges API (the same
    numbers the broker app shows). Returns None on any failure so the caller
    falls back to the model. Cached per (symbol, prices, qty)."""
    import os

    import httpx

    from database.token_db import get_token

    key = (symbol, exchange, product, round(buy_price, 2), round(sell_price, 2), qty)
    if key in _charges_cache:
        return _charges_cache[key]
    try:
        token = get_token(symbol, exchange)
        if not token:
            return None
        prod = _ANGEL_PRODUCT.get((product or "").upper(), "CARRYFORWARD")
        headers = {
            "Authorization": f"Bearer {auth_token}", "Content-Type": "application/json",
            "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
            "X-ClientLocalIP": "1", "X-ClientPublicIP": "1", "X-MACAddress": "1",
            "X-PrivateKey": os.getenv("BROKER_API_KEY", ""),
        }
        payload = {"orders": [
            {"product_type": prod, "transaction_type": "BUY", "quantity": str(qty),
             "price": str(buy_price), "exchange": exchange, "symbol_name": symbol, "token": str(token)},
            {"product_type": prod, "transaction_type": "SELL", "quantity": str(qty),
             "price": str(sell_price), "exchange": exchange, "symbol_name": symbol, "token": str(token)},
        ]}
        r = httpx.post(
            "https://apiconnect.angelone.in/rest/secure/angelbroking/brokerage/v1/estimateCharges",
            headers=headers, json=payload, timeout=15,
        )
        d = r.json()
        if d.get("status") and d.get("data", {}).get("summary"):
            total = round(float(d["data"]["summary"]["total_charges"]), 2)
            _charges_cache[key] = total
            return total
    except Exception:
        logger.debug("estimateCharges failed for %s", symbol, exc_info=True)
    return None


def _fill_cost(symbol, exchange, product, side, price, qty, trade_date) -> float:
    """One fill's cost via the authoritative model. 0.0 on any mapping failure."""
    try:
        cb = _COST.compute_fill(
            exchange=_EXCHANGE_MAP.get((exchange or "").upper(), Exchange.NSE),
            instrument_type=_instrument_type(symbol, exchange),
            product=_PRODUCT_MAP.get((product or "").upper(), ProductType.NRML),
            side=Side.BUY if side == "BUY" else Side.SELL,
            quantity=int(qty),
            price=float(price),
            trade_date=trade_date,
        )
        return round(cb.total, 2)
    except Exception:
        logger.exception("Cost calc failed for %s/%s", symbol, exchange)
        return 0.0


def _match_group(fills: list[dict], fees_fn=None) -> list[dict]:
    """FIFO-match one symbol+product group's chronologically-sorted fills.

    ``fees_fn(symbol, exchange, product, buy_price, sell_price, qty) -> float|None``
    supplies the broker's ACTUAL round-trip charges when available; on None we
    fall back to the model estimate.
    """
    trips: list[dict] = []
    longs: list[dict] = []   # open buys awaiting a sell
    shorts: list[dict] = []  # open sells awaiting a buy
    first = fills[0]
    lot_size = int(float(first.get("lotsize") or 1)) or 1
    is_lot = _instrument_type(first.get("symbol", ""), first.get("exchange", "")) in (
        InstrumentType.CE, InstrumentType.PE, InstrumentType.FUT
    )

    def emit(opn: dict, close_price: float, close_time: str, qty: int, direction: str) -> None:
        buy_price = opn["price"] if direction == "LONG" else close_price
        sell_price = close_price if direction == "LONG" else opn["price"]
        gross = (sell_price - buy_price) * qty
        sym, exch, prod = first["symbol"], first["exchange"], first.get("product", "")

        # Prefer the broker's ACTUAL charges; fall back to the model estimate.
        fees = None
        fees_source = "model"
        if fees_fn is not None:
            actual = fees_fn(sym, exch, prod, buy_price, sell_price, qty)
            if actual is not None:
                fees, fees_source = actual, "broker"
        if fees is None:
            d = _parse_date(close_time)
            fees = (
                _fill_cost(sym, exch, prod, "BUY", buy_price, qty, d)
                + _fill_cost(sym, exch, prod, "SELL", sell_price, qty, d)
            )

        net = gross - fees
        entry_price = buy_price if direction == "LONG" else sell_price
        exit_price = sell_price if direction == "LONG" else buy_price
        entry_notional = entry_price * qty
        trips.append({
            "symbol": sym, "exchange": exch, "product": prod, "direction": direction,
            "qty": qty, "lot_size": lot_size,
            "lots": round(qty / lot_size, 2) if is_lot else None,
            "entry_price": round(entry_price, 2), "exit_price": round(exit_price, 2),
            "gross_pnl": round(gross, 2), "fees": round(fees, 2), "net_pnl": round(net, 2),
            "net_pct": round((net / entry_notional) * 100, 2) if entry_notional > 0 else 0.0,
            "fees_source": fees_source,
            "entry_time": opn["time"], "exit_time": close_time,
        })

    for f in fills:
        qty = int(float(f.get("quantity") or 0))
        price = float(f.get("average_price") or 0)
        if qty <= 0 or price <= 0:
            continue
        if f.get("action") == "BUY":
            while qty > 0 and shorts:
                s = shorts[0]
                m = min(qty, s["qty"])
                emit(s, price, f.get("timestamp", ""), m, "SHORT")
                s["qty"] -= m
                qty -= m
                if s["qty"] == 0:
                    shorts.pop(0)
            if qty > 0:
                longs.append({"qty": qty, "price": price, "time": f.get("timestamp", "")})
        else:
            while qty > 0 and longs:
                l = longs[0]
                m = min(qty, l["qty"])
                emit(l, price, f.get("timestamp", ""), m, "LONG")
                l["qty"] -= m
                qty -= m
                if l["qty"] == 0:
                    longs.pop(0)
            if qty > 0:
                shorts.append({"qty": qty, "price": price, "time": f.get("timestamp", "")})
    return trips


def consolidate_trades(trades: list[dict], fees_fn=None) -> list[dict]:
    """Group fills by symbol+exchange+product, FIFO-match into round-trips."""
    groups: dict[str, list[dict]] = {}
    for t in trades:
        key = f"{t.get('symbol')}|{t.get('exchange')}|{t.get('product')}"
        groups.setdefault(key, []).append(t)
    trips: list[dict] = []
    for arr in groups.values():
        trips.extend(
            _match_group(sorted(arr, key=lambda x: _parse_ts(x.get("timestamp", ""))), fees_fn)
        )
    trips.sort(key=lambda r: _parse_ts(r["exit_time"]), reverse=True)
    return trips


def _ledger(api_key: str) -> dict[str, Any]:
    """Initial capital vs the broker's actual balance = exact realised P&L."""
    initial = float(os.getenv("INITIAL_CAPITAL", "10000"))
    success, resp, _ = get_funds(api_key=api_key)
    data = resp.get("data", {}) if success and isinstance(resp, dict) else {}
    # OpenAlgo normalises broker funds to availablecash; keep raw net if present.
    current = None
    for k in ("net", "availablecash", "availablecash_net", "balance"):
        v = data.get(k)
        if v is not None:
            try:
                current = float(v)
                break
            except (TypeError, ValueError):
                continue
    net_pnl = (current - initial) if current is not None else None
    return {
        "initial_capital": round(initial, 2),
        "current_balance": round(current, 2) if current is not None else None,
        "net_pnl": round(net_pnl, 2) if net_pnl is not None else None,
        "net_pct": round((net_pnl / initial) * 100, 2) if (net_pnl is not None and initial) else None,
        "is_loss": (net_pnl < 0) if net_pnl is not None else None,
    }


def get_tradebook_analysis(api_key: str) -> tuple[bool, dict[str, Any], int]:
    """Round-trips (with model fees) + ledger (broker-actual P&L). Backend-only math."""
    success, resp, status = get_tradebook(api_key=api_key)
    if not success:
        return False, resp, status
    data = resp.get("data", []) if isinstance(resp, dict) else []
    trades = data if isinstance(data, list) else data.get("trades", []) if isinstance(data, dict) else []

    # Build a broker-actual-charges callback (AngelOne) so live fees match the
    # broker exactly; falls back to the model for other brokers or on failure.
    fees_fn = None
    try:
        from database.auth_db import get_auth_token_broker
        auth_token, broker = get_auth_token_broker(api_key)
        if auth_token and broker == "angel":
            fees_fn = lambda sym, exch, prod, bp, sp, q: _angel_actual_charges(  # noqa: E731
                auth_token, sym, exch, prod, bp, sp, q
            )
    except Exception:
        logger.debug("Could not build broker charges callback", exc_info=True)

    roundtrips = consolidate_trades(trades, fees_fn)
    totals = {
        "count": len(roundtrips),
        "gross_pnl": round(sum(r["gross_pnl"] for r in roundtrips), 2),
        "fees": round(sum(r["fees"] for r in roundtrips), 2),
        "net_pnl": round(sum(r["net_pnl"] for r in roundtrips), 2),
        "wins": sum(1 for r in roundtrips if r["net_pnl"] > 0),
        "win_rate": round(
            100 * sum(1 for r in roundtrips if r["net_pnl"] > 0) / len(roundtrips), 1
        ) if roundtrips else 0.0,
    }
    return True, {
        "status": "success",
        "data": {"roundtrips": roundtrips, "totals": totals, "ledger": _ledger(api_key)},
    }, 200
