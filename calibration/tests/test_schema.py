"""Schema sanity tests.

Run from narci repo root:
    python -m pytest calibration/tests/test_schema.py -v
or just:
    python -m calibration.tests.test_schema
"""

from __future__ import annotations

import dataclasses as dc
import json
import math
import os
import sys
import tempfile
import time

import pyarrow as pa
import pyarrow.parquet as pq

from calibration import schema as S


def _build_decision() -> S.DecisionEvent:
    ts_now = time.monotonic_ns()
    ts_wall = time.time_ns()
    return S.DecisionEvent(
        ts_ns=ts_now, ts_wall_ns=ts_wall, event_type="PLACE",
        client_oid="echo-abc", symbol="BTC_JPY",
        mid_price=12_450_000.0, best_bid=12_449_000.0, best_ask=12_451_000.0,
        top1_bid_qty=0.02, top1_ask_qty=0.018, spread_bps=2.0,
        alpha_pred_bps=0.45, alpha_features_hash="dead" * 8, alpha_source="ols_v1",
        side="BUY", price=12_449_500.0, qty=0.005,
        estimated_queue_position=2, estimated_queue_ahead_qty=0.04,
    )


def _build_fill() -> S.FillEvent:
    ts_now = time.monotonic_ns()
    ts_wall = time.time_ns()
    return S.FillEvent(
        ts_ns=ts_now, ts_wall_ns=ts_wall,
        exchange_ts_ms=int(ts_wall / 1e6),
        client_oid="echo-abc", exchange_oid="cc-12345",
        symbol="BTC_JPY", side="BUY",
        fill_price=12_449_500.0, fill_qty=0.005, is_maker=True,
        place_ts_ns=ts_now - 50_000_000, quote_age_ms=50.0,
        mid_at_place=12_450_000.0, mid_at_fill=12_450_200.0,
        spread_at_fill_bps=2.1,
        mid_t_plus_500ms=12_450_300.0, mid_t_plus_1s=12_450_500.0,
        mid_t_plus_5s=12_450_800.0, mid_t_plus_30s=12_451_200.0,
        estimated_queue_at_place=2, estimated_queue_at_fill=0,
        inventory_before=0.01, inventory_after=0.015,
        alpha_pred_bps_at_place=0.45,
        alpha_realized_bps_post=math.log(12_450_500 / 12_450_200) * 1e4,
    )


def _build_cancel() -> S.CancelEvent:
    ts_now = time.monotonic_ns()
    ts_wall = time.time_ns()
    return S.CancelEvent(
        ts_ns_request=ts_now, ts_wall_ns_request=ts_wall,
        ts_ns_ack=ts_now + 100_000_000, cancel_latency_ms=100.0,
        client_oid="echo-def", exchange_oid="cc-67890", symbol="BTC_JPY",
        place_ts_ns=ts_now - 10_000_000_000, quote_age_at_cancel_ms=10_100.0,
        cancel_reason="STRATEGY_REPRICE", final_state="CANCELLED",
        qty_filled_before_cancel=0.0, mid_at_cancel=12_450_000.0,
    )


def test_schema_version_constant():
    assert isinstance(S.SCHEMA_VERSION, str)
    assert S.SCHEMA_VERSION


def test_empty_tables_round_trip():
    for kind, sch in S.SCHEMAS.items():
        tbl = S.to_arrow_table([], sch)
        assert tbl.num_rows == 0
        assert tbl.schema.equals(sch), f"empty table schema drift on {kind}"


def test_decision_event_validate_pass():
    ev = _build_decision()
    assert S.validate_event(ev, S.DecisionEvent.PARQUET_SCHEMA) == []


def test_fill_event_validate_pass():
    ev = _build_fill()
    assert S.validate_event(ev, S.FillEvent.PARQUET_SCHEMA) == []


def test_cancel_event_validate_pass():
    ev = _build_cancel()
    assert S.validate_event(ev, S.CancelEvent.PARQUET_SCHEMA) == []


def test_validate_catches_missing_field():
    """Stripping a field from the dataclass instance is detected."""
    ev = _build_decision()
    # delete via slots: set to a sentinel — slots dataclass with `del` is ok
    # Use a dict-backed substitute to ensure the helper actually triggers.
    class FakeEv:
        pass
    fake = FakeEv()
    for f in S.DecisionEvent.PARQUET_SCHEMA:
        if f.name == "mid_price":
            continue  # skip this one to trigger the check
        setattr(fake, f.name, getattr(ev, f.name))
    errs = S.validate_event(fake, S.DecisionEvent.PARQUET_SCHEMA)
    assert any("mid_price" in e for e in errs), errs


def test_validate_catches_none_in_required():
    ev = _build_decision()
    object.__setattr__(ev, "mid_price", None)  # bypass slots
    errs = S.validate_event(ev, S.DecisionEvent.PARQUET_SCHEMA)
    assert any("mid_price" in e for e in errs), errs


def test_decision_arrow_table_round_trip():
    ev = _build_decision()
    tbl = S.to_arrow_table([ev], S.DecisionEvent.PARQUET_SCHEMA)
    assert tbl.num_rows == 1
    assert tbl.schema.equals(S.DecisionEvent.PARQUET_SCHEMA)
    row = tbl.to_pylist()[0]
    assert row["client_oid"] == "echo-abc"
    assert row["mid_price"] == 12_450_000.0


def test_parquet_round_trip_preserves_schema():
    ev = _build_fill()
    tbl = S.to_arrow_table([ev], S.FillEvent.PARQUET_SCHEMA)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.parquet")
        pq.write_table(tbl, p)
        rt = pq.read_table(p)
        assert rt.schema.equals(S.FillEvent.PARQUET_SCHEMA)
        assert rt.num_rows == 1


def test_session_meta_json_serializable():
    sm = S.SessionMeta(
        session_id="20260506-100000-coincheck-btc_jpy-naive_v1",
        schema_version=S.SCHEMA_VERSION,
        start_wall_ns=time.time_ns(), end_wall_ns=0,
        strategy_class="echo.strategies.maker_naive.NaiveMakerStrategy",
        strategy_config_hash="abc123",
        exchange="coincheck", symbols=["BTC_JPY", "BTCUSDT", "BTCJPY"],
        trade_symbols=["BTC_JPY"],
        hostname="ip-10-0-1-23", az="ap-northeast-1a",
        instance_type="c7g.large", ntp_source="169.254.169.123",
        python_version="3.13.x", echo_git_sha="abcdef0",
        narci_git_sha="7c54677",
    )
    blob = json.dumps(dc.asdict(sm))
    parsed = json.loads(blob)
    assert parsed["schema_version"] == S.SCHEMA_VERSION
    assert parsed["trade_symbols"] == ["BTC_JPY"]


if __name__ == "__main__":
    # Allow `python -m calibration.tests.test_schema` for the impatient.
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
            failed += 1
    print()
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(failed)
