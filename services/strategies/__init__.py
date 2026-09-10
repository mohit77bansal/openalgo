"""Strategy package — 26 strategies grouped by theme.

Importing this package populates ``STRATEGY_REGISTRY`` and exposes the
public helpers that ``backtest_service.py`` needs.
"""

from ._registry import STRATEGY_REGISTRY, _make_strategy, _register, list_strategies

# Side-effect imports: each module registers its strategies at import time.
from . import trend_following  # noqa: F401
from . import mean_reversion   # noqa: F401
from . import momentum         # noqa: F401
from . import volatility       # noqa: F401
from . import pattern          # noqa: F401
from . import hybrid                # noqa: F401
from . import options_directional   # noqa: F401
from . import options_strategies    # noqa: F401
from . import options_scalping      # noqa: F401
from . import options_scalping2     # noqa: F401
from . import options_research      # noqa: F401
from . import options_premium_native  # noqa: F401
from . import options_premium_native2  # noqa: F401

# Strategies removed from the product: non-premium-native options that produce
# ZERO trades on REAL premium data (they only ever "worked" on simulated
# Black-Scholes premium, which we no longer use). Popped from the registry so
# they vanish from the backtest list, the backtester, and activation.
# NOTE: donchian_options_directional and options_orb_research are deliberately
# NOT here — they are wired to live strategies; removing them would break the
# live setup. Ask before deleting those.
REMOVED_STRATEGIES = {
    "options_straddle_breakout", "options_momentum_scalp", "options_orb",
    "options_mean_reversion", "options_expiry_fade", "options_iv_crush",
    "options_scalp_momentum", "options_scalp_spread", "options_scalp_reversal",
    "options_scalp_opening", "options_scalp_gamma", "options_vwap_pullback",
    "options_ema_crossover_scalp", "options_rsi_extreme", "options_bb_reversion",
    "donchian_options_weekly",
}
for _removed_key in REMOVED_STRATEGIES:
    STRATEGY_REGISTRY.pop(_removed_key, None)

__all__ = [
    "STRATEGY_REGISTRY",
    "list_strategies",
    "_make_strategy",
    "_register",
    "REMOVED_STRATEGIES",
]
