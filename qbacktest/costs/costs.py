"""Indian-market cost calculation.

Critical realities (do not paper over):

1. **STT on options is on SELL-side PREMIUM** — not notional, not buy-side.
   STT rate (effective Oct 2024 — SEBI revised upward):
     Options sell premium: 0.10% (was 0.0625% pre-Oct-2024)
     Futures sell:         0.02% (was 0.0125% pre-Oct-2024)
     Equity intraday sell: 0.025%
     Equity delivery (both sides): 0.10%

2. **Stamp duty is on BUY-side only** (post 2020 unified rules):
     Equity delivery: 0.015%
     Equity intraday: 0.003%
     Futures:         0.002%
     Options:         0.003%

3. **Exchange transaction charges** differ NSE vs BSE and revised periodically.
   Current values (NSE, post Oct 2023 revision):
     Equity delivery:    0.00297%
     Equity intraday:    0.00297%
     Futures:            0.00173%
     Options on premium: 0.03503%
   BSE differs slightly; we encode separately.

4. **SEBI turnover fee:** ₹10 per crore (0.0001%) on transaction value.

5. **GST:** 18% on (brokerage + exchange charges + SEBI fee).

6. **Brokerage**: Most discount brokers (Zerodha, Upstox, Fyers) charge:
   - ₹0 for equity delivery
   - ₹20 OR 0.03% (whichever is lower) for intraday equity & all F&O
   We model this with a `BrokerageScheme`.

References:
  - Zerodha brokerage calculator: https://zerodha.com/brokerage-calculator/
  - Upstox: https://upstox.com/brokerage-charges/
  - Fyers: https://fyers.in/charges/

We test against published values to ensure ±₹0.50 accuracy per typical trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from qbacktest.core.types import Exchange, InstrumentType, ProductType, Side


# ---------------------------------------------------------------------------
# Cost breakdown
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Per-trade cost breakdown for transparency and reconciliation.

    All values are in INR.
    """
    brokerage: float = 0.0
    stt: float = 0.0
    ctt: float = 0.0
    exchange_txn: float = 0.0
    sebi_fee: float = 0.0
    gst: float = 0.0
    stamp_duty: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.brokerage
            + self.stt
            + self.ctt
            + self.exchange_txn
            + self.sebi_fee
            + self.gst
            + self.stamp_duty
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "brokerage": self.brokerage,
            "stt": self.stt,
            "ctt": self.ctt,
            "exchange_txn": self.exchange_txn,
            "sebi_fee": self.sebi_fee,
            "gst": self.gst,
            "stamp_duty": self.stamp_duty,
            "total": self.total,
        }


# ---------------------------------------------------------------------------
# STT/CTT/transaction tables
# ---------------------------------------------------------------------------

# Securities Transaction Tax (NSE/BSE equity & equity F&O).
# Rates effective Oct 1, 2024 (SEBI revised upward via Finance Bill 2024).
# Earlier rates apply for pre-Oct-2024 backtests — we date-bound below.
@dataclass(frozen=True)
class STTSchedule:
    effective_from: date
    eq_delivery_both_sides: float       # both buy + sell
    eq_intraday_sell: float
    futures_sell: float
    options_sell_premium: float

STT_HISTORY: list[STTSchedule] = [
    STTSchedule(
        effective_from=date(2010, 1, 1),
        eq_delivery_both_sides=0.001,    # 0.1%
        eq_intraday_sell=0.00025,        # 0.025%
        futures_sell=0.000125,           # 0.0125%
        options_sell_premium=0.000625,   # 0.0625%
    ),
    STTSchedule(
        effective_from=date(2024, 10, 1),
        eq_delivery_both_sides=0.001,    # unchanged
        eq_intraday_sell=0.00025,        # unchanged
        futures_sell=0.0002,             # 0.02%
        options_sell_premium=0.001,      # 0.10%
    ),
]

# Commodity Transaction Tax (MCX). Not used in v1 since we focus on NSE/BSE.
CTT_FUTURES_SELL = 0.0001    # 0.01%
CTT_OPTIONS_SELL_PREMIUM = 0.0005  # 0.05%


# Exchange transaction charges (% of turnover for equity, % of premium for options)
@dataclass(frozen=True)
class ExchangeTxnSchedule:
    effective_from: date
    eq_delivery: float
    eq_intraday: float
    futures: float
    options_premium: float

NSE_EXCHANGE_TXN_HISTORY: list[ExchangeTxnSchedule] = [
    # Pre-Oct-2023 rates (approximate)
    ExchangeTxnSchedule(
        effective_from=date(2010, 1, 1),
        eq_delivery=0.0000345,     # 0.00345%
        eq_intraday=0.0000345,
        futures=0.0000020,         # 0.0002%
        options_premium=0.00053,   # 0.053%
    ),
    # Post Oct 2023 (NSE revised)
    ExchangeTxnSchedule(
        effective_from=date(2023, 10, 1),
        eq_delivery=0.0000297,     # 0.00297%
        eq_intraday=0.0000297,
        futures=0.0000173,         # 0.00173%
        options_premium=0.0003503, # 0.03503%
    ),
]

BSE_EXCHANGE_TXN_HISTORY: list[ExchangeTxnSchedule] = [
    ExchangeTxnSchedule(
        effective_from=date(2010, 1, 1),
        eq_delivery=0.0000375,     # 0.00375%
        eq_intraday=0.0000375,
        futures=0.0000275,
        options_premium=0.000325,
    ),
]


# Stamp duty (post-July 2020 unified rates; on BUY side only)
STAMP_DUTY_EQ_DELIVERY = 0.00015     # 0.015%
STAMP_DUTY_EQ_INTRADAY = 0.00003     # 0.003%
STAMP_DUTY_FUTURES = 0.00002         # 0.002%
STAMP_DUTY_OPTIONS = 0.00003         # 0.003%

SEBI_TURNOVER_FEE = 0.000001         # ₹10 per crore = 0.0001% = 1e-6
GST_RATE = 0.18                      # 18%


def _resolve_stt(d: date) -> STTSchedule:
    active = STT_HISTORY[0]
    for sched in STT_HISTORY:
        if d >= sched.effective_from:
            active = sched
        else:
            break
    return active


def _resolve_exchange_txn(exchange: Exchange, d: date) -> ExchangeTxnSchedule:
    history = NSE_EXCHANGE_TXN_HISTORY if exchange in (Exchange.NSE, Exchange.NFO) else BSE_EXCHANGE_TXN_HISTORY
    active = history[0]
    for sched in history:
        if d >= sched.effective_from:
            active = sched
        else:
            break
    return active


# ---------------------------------------------------------------------------
# Brokerage schemes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrokerageScheme:
    """Per-broker pricing for one trade leg.

    Most Indian discount brokers charge:
        equity_delivery: ₹0
        per_order_max:   ₹20
        per_order_pct:   0.03% (3 bps)
        actual fee = min(per_order_max, per_order_pct * turnover)
    """
    name: str
    eq_delivery_per_order: float = 0.0
    eq_intraday_per_order_max: float = 20.0
    eq_intraday_per_order_pct: float = 0.0003
    futures_per_order_max: float = 20.0
    futures_per_order_pct: float = 0.0003
    options_per_order_flat: float = 20.0          # most brokers: flat ₹20 per order on options

    def compute(
        self,
        instrument_type: InstrumentType,
        product: ProductType,
        turnover: float,
    ) -> float:
        if instrument_type == InstrumentType.EQ:
            if product == ProductType.DELIVERY:
                return self.eq_delivery_per_order
            return min(self.eq_intraday_per_order_max, self.eq_intraday_per_order_pct * turnover)
        if instrument_type == InstrumentType.FUT:
            return min(self.futures_per_order_max, self.futures_per_order_pct * turnover)
        if instrument_type in (InstrumentType.CE, InstrumentType.PE):
            return self.options_per_order_flat
        # Index — no brokerage (you can't trade an index directly)
        return 0.0


def zerodha_cost_model() -> BrokerageScheme:
    return BrokerageScheme(
        name="Zerodha",
        eq_delivery_per_order=0.0,
        eq_intraday_per_order_max=20.0,
        eq_intraday_per_order_pct=0.0003,
        futures_per_order_max=20.0,
        futures_per_order_pct=0.0003,
        options_per_order_flat=20.0,
    )


def upstox_cost_model() -> BrokerageScheme:
    # Upstox: ₹20 per order for eq intraday/futures (no % cap currently),
    # ₹20 per order for options. Eq delivery ₹0.
    return BrokerageScheme(
        name="Upstox",
        eq_delivery_per_order=0.0,
        eq_intraday_per_order_max=20.0,
        eq_intraday_per_order_pct=0.0005,  # 0.05% — not as aggressive
        futures_per_order_max=20.0,
        futures_per_order_pct=0.0005,
        options_per_order_flat=20.0,
    )


def fyers_cost_model() -> BrokerageScheme:
    # Fyers: ₹20 or 0.03%, options ₹20.
    return BrokerageScheme(
        name="Fyers",
        eq_delivery_per_order=0.0,
        eq_intraday_per_order_max=20.0,
        eq_intraday_per_order_pct=0.0003,
        futures_per_order_max=20.0,
        futures_per_order_pct=0.0003,
        options_per_order_flat=20.0,
    )


def zero_brokerage_model() -> BrokerageScheme:
    """Useful for pre-cost analysis."""
    return BrokerageScheme(
        name="Zero",
        eq_delivery_per_order=0.0,
        eq_intraday_per_order_max=0.0,
        eq_intraday_per_order_pct=0.0,
        futures_per_order_max=0.0,
        futures_per_order_pct=0.0,
        options_per_order_flat=0.0,
    )


# ---------------------------------------------------------------------------
# Cost model — orchestrates everything
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostModel:
    """Computes the full cost breakdown for a single fill (trade leg).

    Use:
        model = CostModel(brokerage=zerodha_cost_model())
        cost = model.compute_fill(
            exchange=Exchange.NFO,
            instrument_type=InstrumentType.CE,
            product=ProductType.NRML,
            side=Side.SELL,
            quantity=50,
            price=125.50,
            trade_date=date(2024, 12, 5),
        )
    """
    brokerage: BrokerageScheme
    slippage_bps: float = 0.0   # additional % of price applied to fill price (positive = worse)
    options_slippage_per_lot: float = 0.0   # absolute paise/rupees per lot (illiquid OTM penalty)

    def compute_fill(
        self,
        *,
        exchange: Exchange,
        instrument_type: InstrumentType,
        product: ProductType,
        side: Side,
        quantity: int,
        price: float,
        trade_date: date,
    ) -> CostBreakdown:
        """Compute costs for one fill."""
        turnover = price * quantity
        if turnover <= 0:
            return CostBreakdown()

        stt_sched = _resolve_stt(trade_date)
        exch_sched = _resolve_exchange_txn(exchange, trade_date)

        # Brokerage
        brokerage = self.brokerage.compute(instrument_type, product, turnover)

        # STT
        stt = 0.0
        if instrument_type == InstrumentType.EQ:
            if product == ProductType.DELIVERY:
                stt = stt_sched.eq_delivery_both_sides * turnover
            elif side == Side.SELL:
                stt = stt_sched.eq_intraday_sell * turnover
        elif instrument_type == InstrumentType.FUT:
            if side == Side.SELL:
                stt = stt_sched.futures_sell * turnover
        elif instrument_type in (InstrumentType.CE, InstrumentType.PE):
            if side == Side.SELL:
                stt = stt_sched.options_sell_premium * turnover

        # Exchange txn (always charged)
        if instrument_type == InstrumentType.EQ:
            if product == ProductType.DELIVERY:
                exchange_txn = exch_sched.eq_delivery * turnover
            else:
                exchange_txn = exch_sched.eq_intraday * turnover
        elif instrument_type == InstrumentType.FUT:
            exchange_txn = exch_sched.futures * turnover
        elif instrument_type in (InstrumentType.CE, InstrumentType.PE):
            exchange_txn = exch_sched.options_premium * turnover
        else:
            exchange_txn = 0.0

        # SEBI fee
        sebi_fee = SEBI_TURNOVER_FEE * turnover

        # GST: 18% on (brokerage + exchange + SEBI)
        gst = GST_RATE * (brokerage + exchange_txn + sebi_fee)

        # Stamp duty — buy side only
        stamp_duty = 0.0
        if side == Side.BUY:
            if instrument_type == InstrumentType.EQ:
                stamp_duty = (
                    STAMP_DUTY_EQ_DELIVERY if product == ProductType.DELIVERY else STAMP_DUTY_EQ_INTRADAY
                ) * turnover
            elif instrument_type == InstrumentType.FUT:
                stamp_duty = STAMP_DUTY_FUTURES * turnover
            elif instrument_type in (InstrumentType.CE, InstrumentType.PE):
                stamp_duty = STAMP_DUTY_OPTIONS * turnover

        return CostBreakdown(
            brokerage=round(brokerage, 4),
            stt=round(stt, 4),
            exchange_txn=round(exchange_txn, 4),
            sebi_fee=round(sebi_fee, 4),
            gst=round(gst, 4),
            stamp_duty=round(stamp_duty, 4),
        )

    def apply_slippage(self, *, side: Side, price: float, quantity: int, lot_size: int) -> float:
        """Return adjusted fill price after slippage. Slippage hurts the trader.

        For BUY: adjusted = price * (1 + bps) + per_lot_penalty
        For SELL: adjusted = price * (1 - bps) - per_lot_penalty

        Per-lot penalty captures illiquid OTM spreads.
        """
        bps_adj = price * self.slippage_bps / 10000.0
        lots = max(1, quantity // max(lot_size, 1))
        per_lot = self.options_slippage_per_lot * lots
        if side == Side.BUY:
            return price + bps_adj + per_lot
        return price - bps_adj - per_lot


DEFAULT_COST_MODELS: dict[str, CostModel] = {
    "zerodha": CostModel(brokerage=zerodha_cost_model()),
    "upstox": CostModel(brokerage=upstox_cost_model()),
    "fyers": CostModel(brokerage=fyers_cost_model()),
    "zero": CostModel(brokerage=zero_brokerage_model()),
}
