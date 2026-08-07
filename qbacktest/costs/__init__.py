"""Indian-market trading cost model.

Computes per-trade costs to brokerage-statement accuracy:
  - Brokerage (broker-specific tier)
  - STT/CTT (asymmetric — applies on sell-side for options)
  - Exchange transaction charges (NSE/BSE-specific)
  - SEBI turnover fee
  - GST (18% on brokerage + transaction + SEBI)
  - Stamp duty (state-specific, on buy-side only)

Output is a CostBreakdown for full transparency and a single `total` for engine use.
"""

from qbacktest.costs.costs import (
    BrokerageScheme,
    CostBreakdown,
    CostModel,
    DEFAULT_COST_MODELS,
    fyers_cost_model,
    upstox_cost_model,
    zerodha_cost_model,
    zero_brokerage_model,
)

__all__ = [
    "BrokerageScheme",
    "CostBreakdown",
    "CostModel",
    "DEFAULT_COST_MODELS",
    "fyers_cost_model",
    "upstox_cost_model",
    "zerodha_cost_model",
    "zero_brokerage_model",
]
