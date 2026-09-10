"""Tests for services/backtest_instruments.py — instrument mapping and data sources."""

import sys
import os
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.backtest_instruments import (
    _parse_fut_expiry,
    _lookup_lot_size,
    _INTERVAL_TF,
)


class TestParseFutExpiry:
    def test_nifty_future(self):
        result = _parse_fut_expiry("NIFTY25AUG26FUT")
        assert result == date(2026, 8, 25)

    def test_banknifty_future(self):
        result = _parse_fut_expiry("BANKNIFTY25AUG26FUT")
        assert result == date(2026, 8, 25)

    def test_no_match_returns_none(self):
        assert _parse_fut_expiry("NIFTY") is None
        assert _parse_fut_expiry("RELIANCE") is None
        assert _parse_fut_expiry("") is None
        assert _parse_fut_expiry(None) is None

    def test_different_months(self):
        assert _parse_fut_expiry("NIFTY28JAN26FUT") == date(2026, 1, 28)
        assert _parse_fut_expiry("NIFTY28DEC26FUT") == date(2026, 12, 28)


class TestLookupLotSize:
    def test_nifty_fallback(self):
        lot = _lookup_lot_size("NIFTY25AUG26FUT", "NFO")
        assert lot == 65

    def test_banknifty_fallback(self):
        lot = _lookup_lot_size("BANKNIFTY25AUG26FUT", "NFO")
        assert lot == 30

    def test_finnifty_fallback(self):
        lot = _lookup_lot_size("FINNIFTY25AUG26FUT", "NFO")
        assert lot == 60

    def test_midcpnifty_fallback(self):
        lot = _lookup_lot_size("MIDCPNIFTY25AUG26FUT", "NFO")
        assert lot == 120

    def test_unknown_symbol_returns_1(self):
        lot = _lookup_lot_size("UNKNOWNSYMBOL", "NSE")
        assert lot == 1


class TestIntervalMapping:
    def test_daily_variants(self):
        assert _INTERVAL_TF["D"] == "1d"
        assert _INTERVAL_TF["1d"] == "1d"

    def test_intraday(self):
        assert _INTERVAL_TF["15m"] == "15m"
        assert _INTERVAL_TF["5m"] == "5m"
        assert _INTERVAL_TF["1h"] == "1h"
