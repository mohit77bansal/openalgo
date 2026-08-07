"""Indian-market feasibility scorer.

Given a StrategySpec extracted from a paper (likely US/European), score how
easily it transplants to the Indian retail context.

Scoring dimensions (each 0-1):
- data_score: do we have / can we cheaply get the data inputs?
- capacity_score: can we deploy meaningful capital before hitting illiquidity?
- cost_score: does the edge survive Indian costs (STT-on-sell, brokerage, etc)?
- regulatory_score: SEBI/NSE rules allow the construction?
- short_borrow_score: if short-selling needed, is borrow available?

Rule-based — transparent, conservative. Pluggable LLM scorer for nuanced calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from qbacktest.research.types import (
    FeasibilityScore,
    StrategyAssetClass,
    StrategyMarketType,
    StrategySpec,
)


def score_indian_feasibility(spec: StrategySpec) -> FeasibilityScore:
    blockers: list[str] = []
    notes: list[str] = []

    # --- Data score ---
    data_score = 1.0
    if spec.requires_microstructure_data:
        data_score *= 0.2  # tick-level / order book — not retail-affordable
        notes.append("microstructure data not available to retail in India")
    if spec.requires_intraday_data and spec.asset_class == StrategyAssetClass.OPTIONS:
        data_score *= 0.6  # historical options intraday is expensive
        notes.append("intraday options history depth-limited via free brokers")
    if spec.requires_options_data:
        data_score *= 0.85  # OK via Upstox/AngelOne but rate-limited

    # --- Capacity score ---
    capacity_score = 0.85
    hp = spec.holding_period.lower()
    if "intraday" in hp:
        capacity_score *= 0.7  # intraday needs liquidity, harder for retail
        notes.append("intraday strategies face NSE freeze-quantity + slippage")
    if spec.asset_class == StrategyAssetClass.OPTIONS and "intraday" in hp:
        capacity_score *= 0.7
    elif spec.asset_class == StrategyAssetClass.FX:
        capacity_score *= 0.5
        notes.append("FX retail in India has narrow currency-pair set vs research universes")

    # --- Cost score ---
    # India has STT-on-sell (esp. options), 18% GST on brokerage+exchange fees,
    # and stamp duty on buy. Strategies depending on tiny edges per trade fail.
    cost_score = 1.0
    claimed_sharpe = spec.claimed_sharpe or 0
    if claimed_sharpe and claimed_sharpe < 1.0:
        cost_score *= 0.5
        notes.append("low claimed Sharpe — costs likely eat the edge in India")
    if spec.asset_class == StrategyAssetClass.OPTIONS:
        cost_score *= 0.85  # options: post-Oct-2024 STT hike + brokerage/leg
        notes.append("options STT hiked Oct 2024 — re-validate edge with current costs")
    if "intraday" in hp:
        cost_score *= 0.85  # intraday equity STT 0.025% sell side

    # --- Regulatory score ---
    regulatory_score = 1.0
    if spec.requires_short_selling and spec.asset_class == StrategyAssetClass.EQUITY:
        # Equity short selling for retail in India is INTRADAY-only via SLB scheme.
        # Overnight short of cash equity: not allowed for retail directly.
        regulatory_score *= 0.4
        blockers.append("equity short overnight not allowed for retail (SLB only intraday/limited)")
    if spec.requires_microstructure_data:
        regulatory_score *= 0.9  # not a hard blocker, just data
    if "high-frequency" in spec.signal_summary.lower() or "milliseconds" in spec.signal_summary.lower():
        regulatory_score *= 0.5
        blockers.append("co-location / HFT requires SEBI algo registration + exchange membership")

    # --- Short-borrow score ---
    short_borrow_score = 1.0
    if spec.requires_short_selling:
        if spec.asset_class == StrategyAssetClass.EQUITY:
            short_borrow_score = 0.3
        elif spec.asset_class in (StrategyAssetClass.OPTIONS, StrategyAssetClass.FUTURES):
            # Short F&O is fine
            short_borrow_score = 0.95
        else:
            short_borrow_score = 0.6

    # Market-type bonus / penalty
    mt = spec.market_type
    if mt == StrategyMarketType.US:
        notes.append("strategy targeted US market — Indian transplant requires re-validation on local data")
    elif mt == StrategyMarketType.INDIAN:
        notes.append("paper targets Indian market — direct transplant likely")
        capacity_score *= 1.05
        regulatory_score *= 1.05
    elif mt == StrategyMarketType.EMERGING:
        notes.append("emerging-market scope — likely transplants to India with minor calibration")

    # Clip to [0, 1]
    data_score = min(1.0, max(0.0, data_score))
    capacity_score = min(1.0, max(0.0, capacity_score))
    cost_score = min(1.0, max(0.0, cost_score))
    regulatory_score = min(1.0, max(0.0, regulatory_score))
    short_borrow_score = min(1.0, max(0.0, short_borrow_score))

    overall = (
        0.20 * data_score
        + 0.20 * capacity_score
        + 0.20 * cost_score
        + 0.25 * regulatory_score
        + 0.15 * short_borrow_score
    )

    return FeasibilityScore(
        overall_score=overall,
        data_score=data_score,
        capacity_score=capacity_score,
        cost_score=cost_score,
        regulatory_score=regulatory_score,
        short_borrow_score=short_borrow_score,
        blockers=tuple(blockers),
        notes=" · ".join(notes),
    )
