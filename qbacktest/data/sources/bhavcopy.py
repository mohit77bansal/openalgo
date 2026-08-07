"""NSE bhavcopy ingester — free EOD data for equity and F&O.

Bhavcopy is the official NSE end-of-day file. Equity bhavcopy contains OHLCV for
every NSE-listed equity. F&O bhavcopy contains OHLCV + open interest for every
option/future contract that traded that day.

URL conventions (subject to change — NSE rotates these every year or two):

    EQ bhavcopy (post-2024):
      https://archives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv

    FO bhavcopy (post-2024 unified format):
      https://archives.nseindia.com/products/content/derivatives/equities/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip

When NSE changes the URL format, update `EQ_BHAVCOPY_URL_TEMPLATE` and
`FO_BHAVCOPY_URL_TEMPLATE`.

NOTE: This module ships templates that work as of late 2024. NSE has historically
broken the URL conventions ~once per year. Logged in BLOCKERS.md.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable, Optional

import httpx
import pandas as pd

from qbacktest.core.calendar import IST
from qbacktest.core.types import Bar, Exchange, Instrument, InstrumentType
from qbacktest.data.io import bars_to_parquet
from qbacktest.data.store import DataStore

log = logging.getLogger(__name__)


EQ_BHAVCOPY_URL_TEMPLATE = (
    "https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
)

# NSE migrated FO bhavcopy to a new "unified" CSV in 2024.
# Pattern: https://archives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
FO_BHAVCOPY_URL_TEMPLATE = (
    "https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)

# Older format (pre-July-2024)
LEGACY_FO_BHAVCOPY_URL_TEMPLATE = (
    "https://archives.nseindia.com/content/historical/DERIVATIVES/{yyyy}/{mon}/fo{ddmonyyyy}bhav.csv.zip"
)


# NSE blocks browsers without realistic headers; we set a reasonable UA.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _format_url(template: str, d: date) -> str:
    return template.format(
        ddmmyyyy=d.strftime("%d%m%Y"),
        yyyymmdd=d.strftime("%Y%m%d"),
        yyyy=d.strftime("%Y"),
        mon=d.strftime("%b").upper(),
        ddmonyyyy=d.strftime("%d%b%Y").upper(),
    )


def download_bhavcopy_eq(d: date, *, dest: Path) -> Path:
    """Download equity bhavcopy CSV. Returns path to saved file."""
    url = _format_url(EQ_BHAVCOPY_URL_TEMPLATE, d)
    log.info("Downloading EQ bhavcopy %s from %s", d, url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers=_HEADERS, timeout=30.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    return dest


def download_bhavcopy_fo(d: date, *, dest: Path) -> Path:
    """Download FO bhavcopy ZIP, extract, save CSV. Returns path to CSV."""
    url = _format_url(FO_BHAVCOPY_URL_TEMPLATE, d)
    log.info("Downloading FO bhavcopy %s from %s", d, url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers=_HEADERS, timeout=30.0, follow_redirects=True) as client:
        try:
            resp = client.get(url)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Try legacy URL
            log.warning("New FO URL returned %s; trying legacy", e.response.status_code)
            resp = client.get(_format_url(LEGACY_FO_BHAVCOPY_URL_TEMPLATE, d))
            resp.raise_for_status()
        # Unzip
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
            if not csv_names:
                raise RuntimeError(f"No CSV in FO bhavcopy zip for {d}")
            with zf.open(csv_names[0]) as f:
                dest.write_bytes(f.read())
    return dest


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def parse_eq_bhavcopy(path: Path, *, trade_date: date | None = None) -> list[Bar]:
    """Parse EQ bhavcopy CSV → list[Bar].

    Modern columns: SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE,
                    LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY,
                    TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
    """
    bars: list[Bar] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        # Normalize column names (strip whitespace, uppercase)
        for row in reader:
            row = {k.strip().upper(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            series = row.get("SERIES", row.get(" SERIES", "")).strip()
            if series not in ("EQ", "BE"):  # only equity series
                continue
            symbol = row.get("SYMBOL", "").strip()
            if not symbol:
                continue
            try:
                ts_d = datetime.strptime(row.get("DATE1", "").strip(), "%d-%b-%Y").date() if row.get("DATE1") else trade_date
                if ts_d is None:
                    continue
                inst = Instrument(
                    symbol=symbol, exchange=Exchange.NSE, instrument_type=InstrumentType.EQ, lot_size=1,
                )
                bars.append(Bar(
                    instrument=inst,
                    ts=datetime.combine(ts_d, time(15, 30), tzinfo=IST),
                    timeframe="1d",
                    open=float(row.get("OPEN_PRICE", row.get("OPEN", 0)) or 0),
                    high=float(row.get("HIGH_PRICE", row.get("HIGH", 0)) or 0),
                    low=float(row.get("LOW_PRICE", row.get("LOW", 0)) or 0),
                    close=float(row.get("CLOSE_PRICE", row.get("CLOSE", 0)) or 0),
                    volume=int(float(row.get("TTL_TRD_QNTY", row.get("VOLUME", 0)) or 0)),
                ))
            except (ValueError, KeyError) as e:
                log.debug("Skipping bad row %s: %s", symbol, e)
                continue
    return bars


def parse_fo_bhavcopy(path: Path, *, trade_date: date | None = None) -> list[Bar]:
    """Parse FO bhavcopy CSV → list[Bar] (futures + options).

    Column layout differs by year. We handle the unified 2024+ schema:
        TradDt, BizDt, Sgmt, Src, FinInstrmTp, FinInstrmId, ISIN, TckrSymb,
        XpryDt, FininstrmActlXpryDt, StrkPric, OptnTp, OpnPric, HghPric, LwPric,
        ClsPric, LastPric, PrvsClsgPric, UndrlygPric, SttlmPric, OpnIntrst,
        ChngInOpnIntrst, TtlTradgVol, TtlTrfVal, TtlNbOfTxsExctd, SsnId,
        NewBrdLotQty, Rmks, Rsvd1...

    And the older 2018-2023 schema:
        INSTRUMENT, SYMBOL, EXPIRY_DT, STRIKE_PR, OPTION_TYP, OPEN, HIGH, LOW,
        CLOSE, SETTLE_PR, CONTRACTS, VAL_INLAKH, OPEN_INT, CHG_IN_OI, TIMESTAMP
    """
    bars: list[Bar] = []
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]

    # Detect schema
    new_schema = "FinInstrmTp" in df.columns or "TckrSymb" in df.columns
    legacy = "INSTRUMENT" in df.columns

    if new_schema:
        for _, r in df.iterrows():
            try:
                fi_type = str(r.get("FinInstrmTp", "")).strip().upper()
                if fi_type not in ("STO", "STF", "IDO", "IDF", "OPTSTK", "FUTSTK", "OPTIDX", "FUTIDX"):
                    continue
                symbol = str(r.get("TckrSymb", "")).strip()
                expiry = pd.to_datetime(r.get("XpryDt")).date() if pd.notna(r.get("XpryDt")) else None
                opt_type = str(r.get("OptnTp", "")).strip().upper()
                strike = float(r.get("StrkPric")) if pd.notna(r.get("StrkPric")) else None
                lot = int(r.get("NewBrdLotQty", 0) or 0)
                ts_d = pd.to_datetime(r.get("TradDt")).date() if pd.notna(r.get("TradDt")) else trade_date
                if ts_d is None or expiry is None:
                    continue

                if fi_type in ("IDF", "STF", "FUTIDX", "FUTSTK"):
                    inst_type = InstrumentType.FUT
                elif opt_type == "CE":
                    inst_type = InstrumentType.CE
                elif opt_type == "PE":
                    inst_type = InstrumentType.PE
                else:
                    continue

                inst = Instrument(
                    symbol=symbol,
                    exchange=Exchange.NFO,
                    instrument_type=inst_type,
                    lot_size=lot or 1,
                    expiry=expiry,
                    strike=strike,
                    underlying=symbol,
                )
                bars.append(Bar(
                    instrument=inst,
                    ts=datetime.combine(ts_d, time(15, 30), tzinfo=IST),
                    timeframe="1d",
                    open=float(r.get("OpnPric", 0) or 0),
                    high=float(r.get("HghPric", 0) or 0),
                    low=float(r.get("LwPric", 0) or 0),
                    close=float(r.get("ClsPric", 0) or 0),
                    volume=int(r.get("TtlTradgVol", 0) or 0),
                    open_interest=int(r.get("OpnIntrst", 0) or 0),
                ))
            except (ValueError, TypeError) as e:
                continue
    elif legacy:
        for _, r in df.iterrows():
            try:
                instrument_field = str(r.get("INSTRUMENT", "")).strip().upper()
                if instrument_field not in ("OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK"):
                    continue
                symbol = str(r.get("SYMBOL", "")).strip()
                expiry = datetime.strptime(str(r.get("EXPIRY_DT", "")).strip(), "%d-%b-%Y").date() if r.get("EXPIRY_DT") else None
                opt_type = str(r.get("OPTION_TYP", "")).strip().upper()
                strike = float(r.get("STRIKE_PR", 0)) or None
                ts_d = datetime.strptime(str(r.get("TIMESTAMP", "")).strip(), "%d-%b-%Y").date() if r.get("TIMESTAMP") else trade_date

                if not symbol or expiry is None or ts_d is None:
                    continue

                if instrument_field in ("FUTIDX", "FUTSTK"):
                    inst_type = InstrumentType.FUT
                elif opt_type == "CE":
                    inst_type = InstrumentType.CE
                elif opt_type == "PE":
                    inst_type = InstrumentType.PE
                else:
                    continue

                inst = Instrument(
                    symbol=symbol,
                    exchange=Exchange.NFO,
                    instrument_type=inst_type,
                    lot_size=1,
                    expiry=expiry,
                    strike=strike,
                    underlying=symbol,
                )
                bars.append(Bar(
                    instrument=inst,
                    ts=datetime.combine(ts_d, time(15, 30), tzinfo=IST),
                    timeframe="1d",
                    open=float(r.get("OPEN", 0) or 0),
                    high=float(r.get("HIGH", 0) or 0),
                    low=float(r.get("LOW", 0) or 0),
                    close=float(r.get("CLOSE", 0) or 0),
                    volume=int(r.get("CONTRACTS", 0) or 0),
                    open_interest=int(r.get("OPEN_INT", 0) or 0),
                ))
            except (ValueError, TypeError):
                continue
    else:
        log.warning("Unknown FO bhavcopy schema in %s; columns=%s", path, list(df.columns))

    return bars


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


@dataclass
class BhavcopySource:
    """Bhavcopy ingest pipeline.

    Workflow:
        src = BhavcopySource(store)
        src.ingest_eq(date(2024, 12, 5))   # downloads + parses + writes parquet
        src.ingest_fo(date(2024, 12, 5))
        src.ingest_range(start, end, segments=("EQ", "FO"))
    """

    store: DataStore

    def ingest_eq(self, d: date) -> int:
        csv_path = self.store.bhavcopy_path(d, "EQ")
        if not csv_path.exists():
            download_bhavcopy_eq(d, dest=csv_path)
        bars = parse_eq_bhavcopy(csv_path, trade_date=d)
        return self._write_bars(bars)

    def ingest_fo(self, d: date) -> int:
        csv_path = self.store.bhavcopy_path(d, "FO")
        if not csv_path.exists():
            download_bhavcopy_fo(d, dest=csv_path)
        bars = parse_fo_bhavcopy(csv_path, trade_date=d)
        return self._write_bars(bars)

    def ingest_range(
        self,
        start: date,
        end: date,
        segments: tuple[str, ...] = ("EQ", "FO"),
        calendar=None,
    ) -> dict[str, int]:
        from qbacktest.core.calendar import DEFAULT_CALENDAR
        cal = calendar or DEFAULT_CALENDAR
        out: dict[str, int] = {"EQ": 0, "FO": 0}
        for d in cal.trading_days_between(start, end):
            if "EQ" in segments:
                try:
                    out["EQ"] += self.ingest_eq(d)
                except Exception as e:
                    log.warning("EQ %s failed: %s", d, e)
            if "FO" in segments:
                try:
                    out["FO"] += self.ingest_fo(d)
                except Exception as e:
                    log.warning("FO %s failed: %s", d, e)
        return out

    def _write_bars(self, bars: list[Bar]) -> int:
        """Group bars by (instrument, date) and write one Parquet per group."""
        from collections import defaultdict
        groups: dict[tuple, list[Bar]] = defaultdict(list)
        for b in bars:
            key = (b.instrument.id, b.ts.date(), b.timeframe)
            groups[key].append(b)
        n = 0
        for (_inst_id, d, tf), grp in groups.items():
            if not grp:
                continue
            path = self.store.bar_path(grp[0].instrument, d, tf)
            n += bars_to_parquet(grp, path)
        return n
