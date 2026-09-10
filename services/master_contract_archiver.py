"""
Daily BANKNIFTY master contract archiver.

Archives all BANKNIFTY option symbol-to-token mappings from the master
contract DB (SQLite symtoken table) to daily JSON files, and adds
current-week ATM +/- N strikes (CE + PE) to the Historify download
queue for 1m data ingestion.

Usage:
    .venv/bin/python -m services.master_contract_archiver
    .venv/bin/python -m services.master_contract_archiver --spot 51200
    .venv/bin/python -m services.master_contract_archiver --strikes 10
"""

import json
import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: ensure project root is on sys.path so `database.*`, `utils.*`
# imports resolve when invoked via `python -m services.master_contract_archiver`.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

from database.symbol import SymToken, db_session
from database.historify_db import bulk_add_to_watchlist, init_database
from utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ARCHIVE_DIR = Path(_PROJECT_ROOT) / "data" / "master_contract_archive"
UNDERLYING = "BANKNIFTY"
EXCHANGE = "NFO"
DEFAULT_STRIKE_STEP = 100  # BANKNIFTY strike gap


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------


def query_banknifty_options():
    """Return all BANKNIFTY CE and PE option rows from the symtoken table."""
    rows = (
        db_session.query(SymToken)
        .filter(
            SymToken.name == UNDERLYING,
            SymToken.exchange == EXCHANGE,
            SymToken.instrumenttype.in_(["CE", "PE"]),
        )
        .order_by(SymToken.expiry, SymToken.strike, SymToken.instrumenttype)
        .all()
    )
    return rows


def _serialize(row):
    """Convert a SymToken row to a plain dict for JSON archival."""
    return {
        "symbol": row.symbol,
        "token": row.token,
        "exchange": row.exchange,
        "lotsize": row.lotsize,
        "strike": row.strike,
        "expiry": row.expiry,
        "option_type": row.instrumenttype,
    }


def save_archive(options, archive_date=None):
    """Write BANKNIFTY options to ``data/master_contract_archive/YYYY-MM-DD.json``.

    Returns (filepath, record_count).
    """
    archive_date = archive_date or date.today()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    filepath = ARCHIVE_DIR / f"{archive_date.isoformat()}.json"
    records = [_serialize(opt) for opt in options]

    payload = {
        "date": archive_date.isoformat(),
        "underlying": UNDERLYING,
        "exchange": EXCHANGE,
        "count": len(records),
        "options": records,
    }
    with open(filepath, "w") as fh:
        json.dump(payload, fh, indent=2)

    return filepath, len(records)


# ---------------------------------------------------------------------------
# Watchlist helpers (Historify download queue)
# ---------------------------------------------------------------------------


def _parse_expiry(exp_str):
    """Parse an expiry string like '14-AUG-26' into a date object."""
    for fmt in ("%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(exp_str, fmt).date()
        except ValueError:
            continue
    return None


def find_current_week_expiry(options):
    """Return the nearest future expiry date (current week's BANKNIFTY expiry)."""
    today = date.today()
    future_expiries = set()
    for opt in options:
        if not opt.expiry:
            continue
        exp_date = _parse_expiry(opt.expiry)
        if exp_date and exp_date >= today:
            future_expiries.add(exp_date)

    if not future_expiries:
        return None
    return min(future_expiries)


def _detect_strike_step(strikes):
    """Auto-detect the strike step from a sorted list of strikes.

    Looks at the most common gap between consecutive strikes.
    Falls back to DEFAULT_STRIKE_STEP if detection fails.
    """
    if len(strikes) < 2:
        return DEFAULT_STRIKE_STEP

    gaps = [round(strikes[i + 1] - strikes[i]) for i in range(len(strikes) - 1)]
    # Filter out zero gaps (duplicates)
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return DEFAULT_STRIKE_STEP

    most_common_gap = Counter(gaps).most_common(1)[0][0]
    return most_common_gap


def determine_atm_strike(options, expiry_str, spot=None):
    """Determine the ATM strike for a given expiry.

    If *spot* is provided, rounds to the nearest strike step.
    Otherwise uses the median of available strikes as a reasonable
    approximation (the master contract is roughly symmetric around ATM).
    """
    strikes = sorted(
        {opt.strike for opt in options if opt.expiry == expiry_str and opt.strike and opt.strike > 0}
    )
    if not strikes:
        return None, DEFAULT_STRIKE_STEP

    step = _detect_strike_step(strikes)

    if spot is not None and spot > 0:
        atm = round(spot / step) * step
        return atm, step

    # Fallback: median strike
    mid_idx = len(strikes) // 2
    return strikes[mid_idx], step


def add_atm_strikes_to_watchlist(options, num_strikes=5, spot=None):
    """Add current-week ATM +/- *num_strikes* (CE + PE) to the Historify watchlist.

    Returns the number of symbols actually added.
    """
    # Ensure Historify DB tables exist
    init_database()

    expiry_date = find_current_week_expiry(options)
    if expiry_date is None:
        logger.warning("No current or future expiry found for %s", UNDERLYING)
        return 0, None, None

    expiry_str = expiry_date.strftime("%d-%b-%y").upper()
    atm_strike, step = determine_atm_strike(options, expiry_str, spot)
    if atm_strike is None:
        logger.warning("Could not determine ATM strike for expiry %s", expiry_str)
        return 0, None, None

    logger.info("ATM strike: %.0f  step: %d  expiry: %s", atm_strike, step, expiry_str)

    # Build target strike set
    target_strikes = {atm_strike + offset * step for offset in range(-num_strikes, num_strikes + 1)}

    # Collect matching option symbols
    symbols_to_add = []
    for opt in options:
        if opt.expiry == expiry_str and opt.strike in target_strikes and opt.instrumenttype in (
            "CE",
            "PE",
        ):
            symbols_to_add.append(
                {
                    "symbol": opt.symbol,
                    "exchange": opt.exchange,
                    "display_name": f"{UNDERLYING} {opt.strike:.0f} {opt.instrumenttype} {expiry_str}",
                }
            )

    if not symbols_to_add:
        logger.warning("No matching option symbols found for ATM +/- %d range", num_strikes)
        return 0, atm_strike, expiry_str

    added, skipped, failed = bulk_add_to_watchlist(symbols_to_add)
    logger.info(
        "Watchlist update: %d added, %d already present, %d failed",
        added,
        skipped,
        len(failed),
    )
    if failed:
        for f in failed:
            logger.warning("  failed: %s — %s", f.get("symbol"), f.get("error"))

    return added, atm_strike, expiry_str


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run(spot=None, num_strikes=5):
    """Run the full archiver pipeline: archive to JSON + add to watchlist."""
    logger.info("=== BANKNIFTY Master Contract Archiver ===")

    options = query_banknifty_options()
    if not options:
        msg = (
            "No BANKNIFTY options found in master contract DB. "
            "Has the master contract been downloaded today? "
            "(Download it via the OpenAlgo UI or broker API first.)"
        )
        logger.error(msg)
        print(f"\nERROR: {msg}")
        sys.exit(1)

    # --- Step 1: archive all BANKNIFTY options to JSON ---
    filepath, count = save_archive(options)
    logger.info("Archived %d BANKNIFTY options to %s", count, filepath)

    # --- Step 2: add ATM +/- N strikes to Historify watchlist ---
    added, atm_strike, expiry_str = add_atm_strikes_to_watchlist(
        options, num_strikes=num_strikes, spot=spot
    )

    # --- Summary ---
    print(f"\n{'=' * 60}")
    print(f"  BANKNIFTY Master Contract Archive — {date.today().isoformat()}")
    print(f"{'=' * 60}")
    print(f"  Archive file : {filepath}")
    print(f"  Options saved: {count}")
    if atm_strike is not None:
        print(f"  ATM strike   : {atm_strike:.0f}  (expiry {expiry_str})")
        print(f"  Watchlist    : {added} symbols added for 1m data download")
        spot_note = f"(from --spot {spot})" if spot else "(median-strike estimate)"
        print(f"  Spot basis   : {spot_note}")
    else:
        print("  Watchlist    : skipped (no future expiry found)")
    print(f"{'=' * 60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Archive BANKNIFTY option master contracts to daily JSON and "
            "queue ATM +/- N strikes for Historify 1m data download."
        ),
    )
    parser.add_argument(
        "--spot",
        type=float,
        default=None,
        help="BANKNIFTY spot price for ATM calculation. "
        "If omitted, the median available strike is used as an approximation.",
    )
    parser.add_argument(
        "--strikes",
        type=int,
        default=5,
        help="Number of strikes above and below ATM to add to watchlist (default: 5).",
    )
    args = parser.parse_args()
    run(spot=args.spot, num_strikes=args.strikes)
