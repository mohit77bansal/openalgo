"""DuckDB query layer over the Parquet store.

DuckDB reads Parquet directly (no copy), prunes partitions via filename, and
supports SQL queries over millions of rows in milliseconds. This is the engine's
hot path for ATM-strike lookups, options chain reconstruction, and walk-forward
optimization.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

import duckdb
import pandas as pd

from qbacktest.data.store import DataStore


class DuckDBStore:
    """In-process DuckDB connection with views over the Parquet store."""

    def __init__(self, store: DataStore) -> None:
        self.store = store
        self.conn = duckdb.connect(database=":memory:")
        self._register_views()

    def _register_views(self) -> None:
        # Globs for each instrument-type partition. DuckDB will lazily prune.
        self.conn.execute(f"""
            CREATE OR REPLACE VIEW v_bars_eq AS
            SELECT * FROM read_parquet('{self.store.parquet_root}/bars/eq/**/*.parquet', hive_partitioning=false, union_by_name=true)
        """) if any(self.store.parquet_root.joinpath("bars/eq").rglob("*.parquet")) else self.conn.execute(
            "CREATE OR REPLACE VIEW v_bars_eq AS SELECT NULL::TIMESTAMP AS ts, NULL::VARCHAR AS symbol WHERE 1=0"
        )
        self.conn.execute(f"""
            CREATE OR REPLACE VIEW v_bars_fut AS
            SELECT * FROM read_parquet('{self.store.parquet_root}/bars/fut/**/*.parquet', hive_partitioning=false, union_by_name=true)
        """) if any(self.store.parquet_root.joinpath("bars/fut").rglob("*.parquet")) else self.conn.execute(
            "CREATE OR REPLACE VIEW v_bars_fut AS SELECT NULL::TIMESTAMP AS ts WHERE 1=0"
        )
        self.conn.execute(f"""
            CREATE OR REPLACE VIEW v_bars_opt AS
            SELECT * FROM read_parquet('{self.store.parquet_root}/bars/opt/**/*.parquet', hive_partitioning=false, union_by_name=true)
        """) if any(self.store.parquet_root.joinpath("bars/opt").rglob("*.parquet")) else self.conn.execute(
            "CREATE OR REPLACE VIEW v_bars_opt AS SELECT NULL::TIMESTAMP AS ts WHERE 1=0"
        )
        self.conn.execute(f"""
            CREATE OR REPLACE VIEW v_bars_index AS
            SELECT * FROM read_parquet('{self.store.parquet_root}/bars/index/**/*.parquet', hive_partitioning=false, union_by_name=true)
        """) if any(self.store.parquet_root.joinpath("bars/index").rglob("*.parquet")) else self.conn.execute(
            "CREATE OR REPLACE VIEW v_bars_index AS SELECT NULL::TIMESTAMP AS ts WHERE 1=0"
        )

    def refresh_views(self) -> None:
        """Re-register views after new Parquet files are written."""
        self._register_views()

    def query(self, sql: str, **params: Any) -> pd.DataFrame:
        return self.conn.execute(sql, params).fetchdf()

    # --- Common queries ---

    def equity_bars(self, symbol: str, start: date, end: date, timeframe: str = "1d") -> pd.DataFrame:
        return self.query(
            """
            SELECT ts, symbol, open, high, low, close, volume
            FROM v_bars_eq
            WHERE symbol = $sym AND ts::DATE BETWEEN $start AND $end AND timeframe = $tf
            ORDER BY ts
            """,
            sym=symbol.upper(), start=start, end=end, tf=timeframe,
        )

    def index_bars(self, symbol: str, start: date, end: date, timeframe: str = "1d") -> pd.DataFrame:
        return self.query(
            """
            SELECT ts, symbol, open, high, low, close
            FROM v_bars_index
            WHERE symbol = $sym AND ts::DATE BETWEEN $start AND $end AND timeframe = $tf
            ORDER BY ts
            """,
            sym=symbol.upper(), start=start, end=end, tf=timeframe,
        )

    def option_chain_at(self, underlying: str, expiry: date, ts: datetime) -> pd.DataFrame:
        """Snapshot of the option chain at a moment in time.

        Returns one row per (strike, CE/PE) with the latest close at or before `ts`.
        """
        return self.query(
            """
            WITH latest AS (
                SELECT strike, instrument_type,
                       last_value(close ORDER BY ts) AS last_close,
                       last_value(open_interest ORDER BY ts) AS last_oi,
                       last_value(volume ORDER BY ts) AS last_vol
                FROM v_bars_opt
                WHERE underlying = $u AND expiry = $e AND ts <= $ts
                GROUP BY strike, instrument_type
            )
            SELECT * FROM latest ORDER BY strike, instrument_type
            """,
            u=underlying.upper(), e=expiry, ts=ts,
        )

    def atm_option_strikes(
        self,
        underlying: str,
        expiry: date,
        ts: datetime,
        spot: float,
        n_each_side: int = 5,
    ) -> pd.DataFrame:
        """ATM ± N strikes (CE + PE) at `ts`.

        Useful for delta-hedging or chain-walking strategies.
        """
        chain = self.option_chain_at(underlying, expiry, ts)
        if chain.empty:
            return chain
        chain["dist"] = (chain["strike"] - spot).abs()
        # Get the n_each_side strikes nearest each side
        strikes = sorted(chain["strike"].unique())
        if not strikes:
            return chain
        below = sorted([s for s in strikes if s <= spot])[-n_each_side:]
        above = sorted([s for s in strikes if s > spot])[:n_each_side]
        keep = set(below + above)
        return chain[chain["strike"].isin(keep)].sort_values(["strike", "instrument_type"])
