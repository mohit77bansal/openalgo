"""Black-Scholes pricing and Greeks for European options.

All Indian listed index/stock options are European-style — no early exercise — so
Black-Scholes (and Black 76 for futures options) is correct. American models are
intentionally not included.

Public API:
    bs_price(S, K, t, r, sigma, option_type)
    bs_greeks(S, K, t, r, sigma, option_type) → Greeks
    implied_volatility(price, S, K, t, r, option_type) → sigma

Vectorized variants (all *_v) accept numpy arrays.
"""

from qbacktest.greeks.black_scholes import (
    Greeks,
    bs_greeks,
    bs_greeks_v,
    bs_price,
    bs_price_v,
    implied_volatility,
    implied_volatility_v,
)

__all__ = [
    "Greeks",
    "bs_greeks",
    "bs_greeks_v",
    "bs_price",
    "bs_price_v",
    "implied_volatility",
    "implied_volatility_v",
]
