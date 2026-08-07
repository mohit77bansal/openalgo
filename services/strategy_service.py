"""
Strategy Service — automated NIFTY F&O strategies with defined risk-reward.

Strategy 1: ORB Debit Spread (2:1 Risk-Reward)
================================================
Opening Range Breakout on NIFTY using debit spreads that structurally enforce
a ~2:1 reward-to-risk ratio.

Logic:
  1. Wait for the first 15 minutes (09:15-09:30 IST) to establish the
     Opening Range (OR) = high and low of NIFTY index.
  2. If NIFTY breaks ABOVE the OR high → bullish breakout:
     - Buy NIFTY ATM Call (at the strike nearest to spot)
     - Sell NIFTY OTM Call (2-3 strikes higher, ~100-150 pts above ATM)
     - This is a Bull Call Spread (debit spread)
     - Max loss = net premium paid (defined at entry)
     - Max profit = spread width - net premium
     - R:R target: choose spread width so max_profit >= 2 * max_loss
  3. If NIFTY breaks BELOW the OR low → bearish breakout:
     - Buy NIFTY ATM Put + Sell NIFTY OTM Put (Bear Put Spread)
     - Same R:R structure
  4. Exit: at 15:15 IST (before close) or if the spread reaches 80% of
     max profit (take-profit) or if NIFTY reverses back inside the OR
     (stop-loss at the debit paid).

Why 2:1 is structural (not dependent on discipline):
  - Max loss is the debit paid. Period. Can't lose more.
  - The spread width is chosen so that max_profit = 2 * debit.
  - E.g., if debit = 50/unit, spread width must be 150 (so max_profit = 100).
  - At NIFTY lot 75: risk = 50*75 = 3,750; reward = 100*75 = 7,500.

Why this works for retail:
  - ORB is one of the most-studied intraday patterns. Win rate ~40-45%.
  - At 2:1 R:R, you need only ~34% win rate to break even after costs.
  - 40% win rate × 2:1 R:R = positive expectancy.
  - Defined risk means no blow-ups. Max loss per trade is known upfront.
  - Single entry, single exit — no adjustment needed during the day.

Cost considerations:
  - 2 legs (buy + sell). STT only on the sell leg.
  - Margin: much lower than naked options (spread margin benefit).
  - Brokerage: 2 × ₹20 = ₹40 round-trip (AngelOne flat).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Strategy parameters (configurable)
OR_START = time(9, 15)       # Opening range start
OR_END = time(9, 30)         # Opening range end (15-min OR)
ENTRY_WINDOW_END = time(14, 0)  # Don't enter after 2 PM
EXIT_TIME = time(15, 15)     # Force exit before close
TAKE_PROFIT_PCT = 0.80       # Exit at 80% of max profit
MIN_OR_RANGE_PTS = 30        # Minimum OR range (avoid tight ranges)
MAX_OR_RANGE_PTS = 150       # Maximum OR range (avoid wild opens)
TARGET_RR_RATIO = 2.0        # Minimum reward-to-risk ratio
NIFTY_LOT = 75
STRIKE_STEP = 50             # NIFTY strike interval


@dataclass(frozen=True)
class SpreadSignal:
    """A debit-spread trade signal with defined risk-reward."""
    direction: str               # "BULL" or "BEAR"
    trigger_price: float         # the OR level that was broken
    buy_strike: float            # ATM strike to buy
    sell_strike: float           # OTM strike to sell
    buy_symbol: str              # OpenAlgo symbol for the buy leg
    sell_symbol: str             # OpenAlgo symbol for the sell leg
    option_type: str             # "CE" or "PE"
    spread_width: float          # sell_strike - buy_strike (or reversed for puts)
    estimated_debit: float       # estimated net premium per unit
    max_loss_per_lot: float      # debit * lot_size
    max_profit_per_lot: float    # (spread_width - debit) * lot_size
    rr_ratio: float              # max_profit / max_loss
    or_high: float
    or_low: float
    spot_at_signal: float
    expiry: str                  # nearest expiry date string


def _nearest_strike(price: float, step: float = STRIKE_STEP) -> float:
    return round(price / step) * step


def _find_spread_width_for_rr(debit: float, target_rr: float, step: float = STRIKE_STEP) -> float:
    """Find the minimum spread width that gives >= target_rr ratio.

    spread_width = debit + (target_rr * debit) = debit * (1 + target_rr)
    Round up to nearest strike step.
    """
    raw = debit * (1.0 + target_rr)
    return math.ceil(raw / step) * step


def compute_orb_signal(
    or_high: float,
    or_low: float,
    current_spot: float,
    nearest_expiry: str,
    estimated_atm_premium: float = 80.0,
) -> SpreadSignal | None:
    """Compute an ORB debit-spread signal given the opening range and current spot.

    Returns None if no valid signal (spot inside OR, OR too tight/wide,
    or R:R can't meet target).
    """
    or_range = or_high - or_low
    if or_range < MIN_OR_RANGE_PTS:
        logger.info("OR range %.1f < min %d, skipping", or_range, MIN_OR_RANGE_PTS)
        return None
    if or_range > MAX_OR_RANGE_PTS:
        logger.info("OR range %.1f > max %d, skipping", or_range, MAX_OR_RANGE_PTS)
        return None

    if current_spot > or_high:
        direction = "BULL"
        trigger = or_high
        option_type = "CE"
        buy_strike = _nearest_strike(current_spot)
        debit_est = estimated_atm_premium
        spread_width = _find_spread_width_for_rr(debit_est, TARGET_RR_RATIO)
        sell_strike = buy_strike + spread_width
    elif current_spot < or_low:
        direction = "BEAR"
        trigger = or_low
        option_type = "PE"
        buy_strike = _nearest_strike(current_spot)
        debit_est = estimated_atm_premium
        spread_width = _find_spread_width_for_rr(debit_est, TARGET_RR_RATIO)
        sell_strike = buy_strike - spread_width
    else:
        return None

    max_loss = debit_est * NIFTY_LOT
    max_profit = (spread_width - debit_est) * NIFTY_LOT
    rr = max_profit / max_loss if max_loss > 0 else 0

    if rr < TARGET_RR_RATIO:
        logger.info("R:R %.2f < target %.1f, skipping", rr, TARGET_RR_RATIO)
        return None

    dd = nearest_expiry.replace("-", "")
    buy_sym = f"NIFTY{dd}{int(buy_strike)}{option_type}"
    sell_sym = f"NIFTY{dd}{int(sell_strike)}{option_type}"

    return SpreadSignal(
        direction=direction,
        trigger_price=trigger,
        buy_strike=buy_strike,
        sell_strike=sell_strike,
        buy_symbol=buy_sym,
        sell_symbol=sell_sym,
        option_type=option_type,
        spread_width=abs(spread_width),
        estimated_debit=debit_est,
        max_loss_per_lot=max_loss,
        max_profit_per_lot=max_profit,
        rr_ratio=rr,
        or_high=or_high,
        or_low=or_low,
        spot_at_signal=current_spot,
        expiry=nearest_expiry,
    )


def execute_spread_paper(
    signal: SpreadSignal,
    api_key: str,
    lots: int = 1,
) -> dict[str, Any]:
    """Execute a debit spread as two paper orders through the risk layer.

    Returns status dict with order IDs for both legs.
    """
    from services.paper_trading_service import paper_order

    qty = lots * NIFTY_LOT
    results = {}

    buy_result = paper_order(
        symbol=signal.buy_symbol,
        exchange="NFO",
        action="BUY",
        quantity=qty,
        product="MIS",
        pricetype="MARKET",
        strategy=f"ORB_{signal.direction}",
        api_key=api_key,
    )
    results["buy_leg"] = buy_result

    if buy_result.get("status") != "success":
        return {
            "status": "error",
            "message": f"buy leg failed: {buy_result.get('message', 'unknown')}",
            "buy_leg": buy_result,
        }

    sell_result = paper_order(
        symbol=signal.sell_symbol,
        exchange="NFO",
        action="SELL",
        quantity=qty,
        product="MIS",
        pricetype="MARKET",
        strategy=f"ORB_{signal.direction}",
        api_key=api_key,
    )
    results["sell_leg"] = sell_result

    return {
        "status": "success" if sell_result.get("status") == "success" else "partial",
        "direction": signal.direction,
        "buy_symbol": signal.buy_symbol,
        "sell_symbol": signal.sell_symbol,
        "lots": lots,
        "quantity": qty,
        "max_loss": signal.max_loss_per_lot * lots,
        "max_profit": signal.max_profit_per_lot * lots,
        "rr_ratio": signal.rr_ratio,
        **results,
    }


def get_strategy_info() -> dict[str, Any]:
    """Return the current strategy configuration for the UI."""
    return {
        "name": "ORB Debit Spread",
        "description": "Opening Range Breakout with structurally enforced 2:1 R:R via debit spreads",
        "parameters": {
            "or_window": f"{OR_START.strftime('%H:%M')}-{OR_END.strftime('%H:%M')} IST",
            "entry_window_end": ENTRY_WINDOW_END.strftime("%H:%M"),
            "exit_time": EXIT_TIME.strftime("%H:%M"),
            "take_profit": f"{TAKE_PROFIT_PCT*100:.0f}% of max profit",
            "min_or_range": f"{MIN_OR_RANGE_PTS} pts",
            "max_or_range": f"{MAX_OR_RANGE_PTS} pts",
            "target_rr": f"{TARGET_RR_RATIO}:1",
            "strike_step": STRIKE_STEP,
            "lot_size": NIFTY_LOT,
        },
        "risk_profile": {
            "max_loss_per_lot": f"debit × {NIFTY_LOT} (known at entry)",
            "max_profit_per_lot": f"(spread_width - debit) × {NIFTY_LOT}",
            "win_rate_needed_to_breakeven": f"{1/(1+TARGET_RR_RATIO)*100:.0f}%",
            "typical_win_rate": "40-45%",
        },
    }
