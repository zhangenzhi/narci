"""Unit tests for bookticker_to_narci conversion."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from data.backfill_vision_bookticker import bookticker_to_narci


def _write_vision_parquet(rows: list[dict]) -> Path:
    """Helper: write a synthetic Vision bookTicker parquet."""
    tmpdir = tempfile.mkdtemp(prefix="narci_test_bt_")
    path = Path(tmpdir) / "BTCUSDT-bookTicker-2026-04-20.parquet"
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)
    return path


def test_simple_two_row_conversion():
    """Two bookTicker rows → 4 narci rows (2 bids, 2 asks), interleaved
    bid-then-ask at each ts."""
    path = _write_vision_parquet([
        {"update_id": 1, "best_bid_price": 100.0, "best_bid_qty": 1.5,
         "best_ask_price": 101.0, "best_ask_qty": 2.0,
         "transaction_time": 1_700_000_000_000, "event_time": 1_700_000_000_001},
        {"update_id": 2, "best_bid_price": 100.5, "best_bid_qty": 1.0,
         "best_ask_price": 101.5, "best_ask_qty": 1.8,
         "transaction_time": 1_700_000_000_500, "event_time": 1_700_000_000_502},
    ])
    out = bookticker_to_narci(path)
    assert len(out) == 4
    # Row 0: bid at first ts
    assert out.iloc[0].timestamp == 1_700_000_000_001
    assert out.iloc[0].side == 3
    assert out.iloc[0].price == 100.0
    assert out.iloc[0].quantity == 1.5
    # Row 1: ask at first ts
    assert out.iloc[1].timestamp == 1_700_000_000_001
    assert out.iloc[1].side == 4
    assert out.iloc[1].price == 101.0
    assert out.iloc[1].quantity == 2.0
    # Row 2: bid at second ts
    assert out.iloc[2].timestamp == 1_700_000_000_502
    assert out.iloc[2].side == 3
    assert out.iloc[2].price == 100.5
    # Row 3: ask at second ts
    assert out.iloc[3].side == 4
    assert out.iloc[3].price == 101.5


def test_microsecond_event_time_normalised_to_ms():
    """Some Vision bookTicker files use microsecond event_time. The
    converter must detect and downscale to ms so timestamps match the
    rest of the narci pipeline."""
    # Microsecond ts: 1.7e15 range
    us_ts = 1_700_000_000_001_234   # ~1.7e15, microseconds
    path = _write_vision_parquet([
        {"update_id": 1, "best_bid_price": 100.0, "best_bid_qty": 1.0,
         "best_ask_price": 101.0, "best_ask_qty": 1.0,
         "transaction_time": us_ts, "event_time": us_ts},
    ])
    out = bookticker_to_narci(path)
    # Expected: us_ts // 1000 = 1_700_000_000_001
    assert int(out.iloc[0].timestamp) == us_ts // 1000


def test_camelcase_column_names_accepted():
    """Some older Vision dumps use bestBidPrice etc. (camelCase). The
    converter normalises to snake_case."""
    path = _write_vision_parquet([
        {"u": 1, "bestBidPrice": 100.0, "bestBidQty": 1.0,
         "bestAskPrice": 101.0, "bestAskQty": 1.0,
         "event_time": 1_700_000_000_001},
    ])
    out = bookticker_to_narci(path)
    assert len(out) == 2
    assert out.iloc[0].price == 100.0
    assert out.iloc[1].price == 101.0


def test_fallback_to_transaction_time_when_event_time_missing():
    """Some dumps only have transaction_time. Converter falls back."""
    path = _write_vision_parquet([
        {"update_id": 1, "best_bid_price": 100.0, "best_bid_qty": 1.0,
         "best_ask_price": 101.0, "best_ask_qty": 1.0,
         "transaction_time": 1_700_000_000_001},
    ])
    out = bookticker_to_narci(path)
    assert len(out) == 2
    assert int(out.iloc[0].timestamp) == 1_700_000_000_001


def test_missing_columns_raises_clear_error():
    path = _write_vision_parquet([
        {"update_id": 1, "best_bid_price": 100.0, "event_time": 0},
    ])
    try:
        bookticker_to_narci(path)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "best_ask_price" in str(e) or "missing columns" in str(e)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
