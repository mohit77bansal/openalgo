"""Historical data layer: Parquet storage + DuckDB queries + ingest from NSE/Upstox.

Layout on disk:
    data/
      parquet/
        bars/
          NSE/EQ/{symbol}/{year}/{month}.parquet
          NFO/FUT/{underlying}/{expiry}/{date}.parquet
          NFO/OPT/{underlying}/{expiry}/{strike}/{ce_or_pe}/{date}.parquet
        ticks/   (later)
      duckdb/
        catalog.duckdb   (catalog views over parquet)
      raw/
        bhavcopy/{date}.csv

Public API:
    DataStore           - filesystem layout + read/write helpers
    DuckDBStore         - in-memory DuckDB with Parquet views
    DataAvailability    - "what data do I have for X over Y?"
    bars_to_parquet, parquet_to_bars
"""

from qbacktest.data.store import DataStore
from qbacktest.data.duckdb_store import DuckDBStore
from qbacktest.data.availability import DataAvailability
from qbacktest.data.io import bars_to_parquet, parquet_to_bars
from qbacktest.data.chain_builder import ChainBuilder

__all__ = [
    "ChainBuilder",
    "DataAvailability",
    "DataStore",
    "DuckDBStore",
    "bars_to_parquet",
    "parquet_to_bars",
]
