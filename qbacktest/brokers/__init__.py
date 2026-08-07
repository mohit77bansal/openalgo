"""Broker adapters — Upstox v2, Fyers v3, AngelOne SmartAPI.

All adapters implement the `BrokerAdapter` protocol so the live engine and data
ingest pipeline are broker-agnostic.

Public API:
    BrokerAdapter      - abstract base class
    UpstoxAdapter      - Upstox v2 (OAuth, browser-required)
    FyersAdapter       - Fyers v3 (OAuth, browser-required)
    AngelOneAdapter    - AngelOne SmartAPI (TOTP, headless — recommended)
    TokenStore         - encrypted at-rest storage of OAuth tokens
"""

from qbacktest.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResponse,
    BrokerPosition,
    LiveQuote,
)
from qbacktest.brokers.token_store import TokenStore

__all__ = [
    "BrokerAdapter",
    "BrokerOrderRequest",
    "BrokerOrderResponse",
    "BrokerPosition",
    "LiveQuote",
    "TokenStore",
]
