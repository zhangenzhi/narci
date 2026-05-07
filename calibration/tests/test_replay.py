"""End-to-end calibration replay test.

Builds a synthetic echo session with deterministic logs + raw L2, runs
calibrate_session, asserts the report shape + reasonable metrics.

Run:
    cd /lustre1/work/c30636/narci
    python -m calibration.tests.test_replay
"""

from __future__ import annotations

import dataclasses as dc
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from calibration import EchoLogWriter
from calibration import schema as S
from calibration.replay import calibrate_session, report_to_json


def _build_synthetic_l2(out_dir: Path, start_ms: int = 1_700_000_000_000):
    """Write a tiny narci-format raw L2 parquet covering a few seconds.

    Layout:
      ts=start: snapshots at bid 99/98/97 ×0.5, ask 101/102/103 ×0.5
      ts+0.5s: trade qty=0.4 at price 99 (eats bid queue)
      ts+1.0s: trade qty=-0.3 at price 101 (eats ask queue)
      ts+1.5s: bid update qty=0 at 99 (level deletion)
      ts+2.0s: snapshot refresh
      ts+30s: another trade for adverse window probe
    """
    ts = start_ms
    rows = []
    for p, q in [(99, 0.5), (98, 0.5), (97, 0.5)]:
        rows.append((ts, 3, p, q))
    for p, q in [(101, 0.5), (102, 0.5), (103, 0.5)]:
        rows.append((ts, 4, p, q))
    rows.append((ts + 500, 2, 99, 0.4))
    rows.append((ts + 1000, 2, 101, -0.3))
    rows.append((ts + 1500, 0, 99, 0.0))
    # snapshot at t+2s — same prices, fresh
    for p, q in [(99.5, 0.4), (98, 0.5), (97, 0.5)]:
        rows.append((ts + 2000, 3, p, q))
    for p, q in [(101, 0.4), (102, 0.5), (103, 0.5)]:
        rows.append((ts + 2000, 4, p, q))
    # Late events to give us a 30s window probe target
    for i in range(1, 32):
        rows.append((ts + i * 1000, 0, 99.5, 0.4))  # tick level alive
    out_dir.mkdir(parents=True, exist_ok=True)
    # Write as a single parquet shard
    schema = pa.schema([
        pa.field("timestamp", pa.int64()),
        pa.field("side", pa.int64()),
        pa.field("price", pa.float64()),
        pa.field("quantity", pa.float64()),
    ])
    cols = {
        "timestamp": [r[0] for r in rows],
        "side":      [r[1] for r in rows],
        "price":     [r[2] for r in rows],
        "quantity":  [r[3] for r in rows],
    }
    tbl = pa.Table.from_pydict(cols, schema=schema)
    pq.write_table(tbl, out_dir / "BTC_JPY_RAW_20260506_120000.parquet")


def _build_synthetic_session(tmp: Path) -> str:
    """Write a synthetic echo session: 1 PLACE, 1 FILL, 1 CANCEL."""
    sid = "20260506-120000-coincheck-btcjpy-naive_v1"
    w = EchoLogWriter(session_id=sid, base_dir=tmp)

    # session meta
    meta = S.SessionMeta(
        session_id=sid, schema_version=S.SCHEMA_VERSION,
        start_wall_ns=time.time_ns(), end_wall_ns=0,
        strategy_class="echo.strategies.NaiveMaker",
        strategy_config_hash="mock",
        exchange="coincheck",
        symbols=["BTC_JPY"],
        trade_symbols=["BTC_JPY"],
        hostname="testhost", az="ap-northeast-1a",
        instance_type="c7g.large", ntp_source="169.254.169.123",
        python_version="3.13", echo_git_sha="d00d", narci_git_sha="cafe",
    )
    w.write_session_meta(meta)

    # synthetic raw_l2 inside the session dir
    _build_synthetic_l2(Path(tmp) / sid / "raw_l2",
                       start_ms=1_700_000_000_000)

    # Echo decisions: PLACE BUY 99 right after snapshot
    # ts_ns aligned with raw L2 ts (which is in ms; ns = ms * 1e6)
    base_ns = 1_700_000_000_000 * 1_000_000  # snapshot ts_ms × 1e6
    place_ts = base_ns + 100 * 1_000_000     # 100 ms after snapshot
    dec = S.DecisionEvent(
        ts_ns=place_ts, ts_wall_ns=place_ts, event_type="PLACE",
        client_oid="echo-A", symbol="BTC_JPY",
        mid_price=100.0, best_bid=99.0, best_ask=101.0,
        top1_bid_qty=0.5, top1_ask_qty=0.5, spread_bps=200.0,
        alpha_pred_bps=float("nan"), alpha_features_hash="",
        alpha_source="naive",
        side="BUY", price=99.0, qty=0.05,
        estimated_queue_position=25, estimated_queue_ahead_qty=0.5,
    )
    w.write_decision(dec)

    # FillEvent: caused by the trade at ts+500ms eating queue + filling us
    # In broker logic: trade qty=0.4 eats 0.4 of queue (we had 0.5 ahead),
    # remaining queue 0.1. So we DON'T fill on first trade. Strategy
    # would need another trade. For test simplicity, pretend the second
    # trade at +1.5s (we synthesize the level deletion which then
    # reduces our queue) fills us.
    # We log a fill at ts+1.6s indicating echo got 0.05 BTC at price 99.
    fill_ts = base_ns + 1_600_000_000   # +1.6s
    fil = S.FillEvent(
        ts_ns=fill_ts, ts_wall_ns=fill_ts,
        exchange_ts_ms=fill_ts // 1_000_000,
        client_oid="echo-A", exchange_oid="cc-1",
        symbol="BTC_JPY", side="BUY",
        fill_price=99.0, fill_qty=0.05, is_maker=True,
        place_ts_ns=place_ts, quote_age_ms=1500.0,
        mid_at_place=100.0, mid_at_fill=100.0, spread_at_fill_bps=200.0,
        mid_t_plus_500ms=100.0, mid_t_plus_1s=100.5,
        mid_t_plus_5s=101.0, mid_t_plus_30s=99.0,
        estimated_queue_at_place=25, estimated_queue_at_fill=0,
        inventory_before=0.0, inventory_after=0.05,
        alpha_pred_bps_at_place=float("nan"),
        alpha_realized_bps_post=0.0,
    )
    w.write_fill(fil)

    # CancelEvent: cancel a different (fictional) order
    cxl_req_ts = base_ns + 5_000_000_000
    cxl_ack_ts = cxl_req_ts + 187_000_000  # 187 ms latency
    cxl = S.CancelEvent(
        ts_ns_request=cxl_req_ts, ts_wall_ns_request=cxl_req_ts,
        ts_ns_ack=cxl_ack_ts, cancel_latency_ms=187.0,
        client_oid="echo-B", exchange_oid="cc-2", symbol="BTC_JPY",
        place_ts_ns=cxl_req_ts - 10_000_000_000,
        quote_age_at_cancel_ms=10_000.0,
        cancel_reason="STRATEGY_REPRICE", final_state="CANCELLED",
        qty_filled_before_cancel=0.0, mid_at_cancel=100.0,
    )
    w.write_cancel(cxl)
    w.flush()

    return sid


def test_calibrate_session_basic():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _build_synthetic_session(Path(tmp))
        session_dir = Path(tmp) / sid

        report = calibrate_session(session_dir)

        # Shape
        assert report.session_id == sid
        assert report.exchange == "coincheck"
        assert report.symbol == "BTC_JPY"
        assert report.sample_count == 1   # one fill in the session
        assert report.fill_metrics.real_count == 1
        # Sim may or may not match (synthetic data is contrived); just
        # check structure
        assert report.fill_metrics.match_rate >= 0.0
        assert report.cancel_metrics.real_p50_ms == 187.0
        assert report.verdict in ("HEALTHY", "ACCEPT_WITH_TUNING", "UNHEALTHY")


def test_calibrate_session_json_serializable():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _build_synthetic_session(Path(tmp))
        report = calibrate_session(Path(tmp) / sid)
        s = report_to_json(report)
        d = json.loads(s)
        assert d["session_id"] == sid
        assert "fill_metrics" in d
        assert "adverse_metrics" in d
        assert "cancel_metrics" in d


def test_calibrate_session_missing_meta_raises():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "empty_session"
        d.mkdir()
        try:
            calibrate_session(d)
            assert False, "should have raised"
        except FileNotFoundError:
            pass


def test_calibrate_session_no_raw_l2_falls_back():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _build_synthetic_session(Path(tmp))
        session_dir = Path(tmp) / sid
        # Wipe echo's raw_l2/
        for f in (session_dir / "raw_l2").glob("*.parquet"):
            f.unlink()
        # Provide narci_recorder_dir as fallback
        narci_dir = Path(tmp) / "narci_recorder"
        _build_synthetic_l2(narci_dir)
        report = calibrate_session(session_dir, narci_recorder_dir=narci_dir)
        assert report.session_id == sid


def test_calibrate_session_no_l2_no_fallback_raises():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _build_synthetic_session(Path(tmp))
        session_dir = Path(tmp) / sid
        for f in (session_dir / "raw_l2").glob("*.parquet"):
            f.unlink()
        try:
            calibrate_session(session_dir)
            assert False, "should have raised"
        except FileNotFoundError:
            pass


def test_pair_fills_helper():
    """Test the matching helper directly with crafted lists."""
    from calibration.replay import _pair_fills
    real = [
        {"ts_ns": 1_000_000_000, "client_oid": "A", "side": "BUY",
         "fill_price": 100.0},
        {"ts_ns": 2_000_000_000, "client_oid": "B", "side": "SELL",
         "fill_price": 101.0},
    ]
    sim = [
        {"ts_ns": 1_001_000_000, "client_oid": "A", "side": "BUY",
         "fill_price": 100.0},
        {"ts_ns": 2_500_000_000, "client_oid": "B", "side": "SELL",
         "fill_price": 101.0},
    ]
    matched, ur, us = _pair_fills(real, sim, tolerance_ms=2000.0)
    assert len(matched) == 2
    assert ur == 0 and us == 0


def test_pair_fills_unmatched_when_far_in_time():
    from calibration.replay import _pair_fills
    real = [{"ts_ns": 1_000_000_000, "client_oid": "A", "side": "BUY",
             "fill_price": 100.0}]
    sim = [{"ts_ns": 10_000_000_000, "client_oid": "A", "side": "BUY",
            "fill_price": 100.0}]
    matched, ur, us = _pair_fills(real, sim, tolerance_ms=2000.0)
    assert len(matched) == 0
    assert ur == 1 and us == 1


if __name__ == "__main__":
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print()
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(failed)
