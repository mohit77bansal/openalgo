"""Domain exceptions."""


class QBacktestError(Exception):
    """Base."""


class DataNotAvailableError(QBacktestError):
    """Data for the requested instrument/range is not available locally or upstream."""


class InvalidOrderError(QBacktestError):
    """Order failed pre-trade validation."""


class InsufficientMarginError(InvalidOrderError):
    """Position would breach margin limits."""


class FreezeQuantityError(InvalidOrderError):
    """Order exceeds NSE freeze quantity (auto-split should have handled this)."""


class CircuitLimitError(InvalidOrderError):
    """Limit price is outside daily circuit band."""


class BrokerError(QBacktestError):
    """Broker API rejected or errored."""


class BrokerAuthError(BrokerError):
    """Auth/token problem with broker."""


class RateLimitError(BrokerError):
    """Broker rate limit hit."""


class StrategyError(QBacktestError):
    """User strategy code raised."""
