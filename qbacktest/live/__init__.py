"""Live and paper trading.

The live engine reuses BacktestEngine core; only the data source and order sink
change. This eliminates simulation-to-live drift.

Public API:
    PaperEngine - paper trading: live data, simulated fills
    LiveEngine  - real broker order routing
    RiskLimits  - max-loss, max-position, kill switch
    LiveDataSource - adapts a broker WebSocket feed to the engine's DataSource
"""

from qbacktest.live.chain_scanner import ChainScannerWorker
from qbacktest.live.live_data import LiveDataSource
from qbacktest.live.paper import PaperEngine
from qbacktest.live.live_engine import LiveEngine
from qbacktest.live.risk import RiskLimits

__all__ = [
    "ChainScannerWorker",
    "LiveDataSource",
    "LiveEngine",
    "PaperEngine",
    "RiskLimits",
]
