"""L2Reconstructor streaming-mode tests.

Verifies:
  1. Streaming apply_event matches batch process_dataframe final state
  2. get_state() returns sensible derived metrics
  3. Reset / period_volumes work as expected
  4. Snapshot batch consistency flag
"""

from __future__ import annotations

import sys

import pandas as pd
import numpy as np

from data.l2_reconstruct import L2Reconstructor


def _make_events():
    """Build a small synthetic event stream covering all 5 sides."""
    events = []
    ts = 1_000_000  # ms
    # initial snapshot
    for p in (100, 99, 98):
        events.append((ts, 3, p, 0.5))
    for p in (101, 102, 103):
        events.append((ts, 4, p, 0.4))
    ts += 1
    # bid update: add level
    events.append((ts, 0, 99.5, 0.3))
    ts += 1
    # ask update: shrink existing
    events.append((ts, 1, 101, 0.1))
    ts += 1
    # trade: aggressive buy (negative qty per narci convention)
    events.append((ts, 2, 101, -0.05))
    ts += 1
    # bid delete
    events.append((ts, 0, 98, 0.0))
    ts += 1
    # ask add
    events.append((ts, 1, 100.5, 0.2))
    ts += 1
    # trade: aggressive sell
    events.append((ts, 2, 99.5, 0.04))
    return events


def test_streaming_state_consistency():
    """Trace expected final state after all events:
       bids: {100: 0.5 (snap), 99.5: 0.3 (added), 99: 0.5 (snap)}    [98 deleted]
       asks: {100.5: 0.2 (added), 101: 0.1 (shrunk), 102: 0.4, 103: 0.4}
       Trades: ts=3 qty=-0.05 → seller maker → period_taker_sell_vol += 0.05
                ts=6 qty=0.04 → buyer maker  → period_taker_buy_vol += 0.04
    """
    events = _make_events()
    rec = L2Reconstructor(depth_limit=5)
    rec.reset()
    for ts, side, price, qty in events:
        rec.apply_event(ts, side, price, qty)
    state = rec.get_state(top_n=3)
    assert state is not None
    assert state["best_bid"] == 100, state["best_bid"]
    assert state["best_ask"] == 100.5, state["best_ask"]
    assert state["spread"] == 0.5, state["spread"]
    # narci's existing convention (process_dataframe): qty>0 → period_taker_buy_vol;
    # qty<0 → period_taker_sell_vol. Field names are historically counter-intuitive
    # but we preserve them.
    assert state["taker_buy_vol"] == 0.04, state["taker_buy_vol"]
    assert state["taker_sell_vol"] == 0.05, state["taker_sell_vol"]


def test_streaming_matches_batch():
    """apply_event sequence should produce the same final book as
    process_dataframe."""
    events = _make_events()
    df = pd.DataFrame(events, columns=["timestamp", "side", "price", "quantity"])
    df = df.sort_values(["timestamp", "side"], ascending=[True, False]).reset_index(drop=True)

    # Batch
    rec_batch = L2Reconstructor(depth_limit=5)
    _ = rec_batch.process_dataframe(df.copy(), sample_interval_ms=None)
    # Stream
    rec_stream, _ = L2Reconstructor.replay_dataframe(df.copy(), depth_limit=5)

    # Compare bids / asks dicts
    assert rec_batch.bids == rec_stream.bids, (rec_batch.bids, rec_stream.bids)
    assert rec_batch.asks == rec_stream.asks, (rec_batch.asks, rec_stream.asks)


def test_get_state_returns_none_before_snapshot():
    rec = L2Reconstructor()
    rec.reset()
    assert rec.get_state() is None
    rec.apply_event(1, 0, 100, 0.5)  # bid update without snapshot
    rec.apply_event(2, 1, 101, 0.4)
    assert rec.get_state() is None  # is_ready False


def test_reset_clears_state():
    rec = L2Reconstructor()
    rec.reset()
    rec.apply_event(1, 3, 100, 0.5)
    rec.apply_event(1, 4, 101, 0.5)
    rec.apply_event(2, 2, 100.5, -0.1)
    assert rec.is_ready is True
    rec.reset()
    assert rec.is_ready is False
    assert rec.bids == {}
    assert rec.asks == {}
    assert rec.period_taker_buy_vol == 0.0
    assert rec.period_taker_sell_vol == 0.0


def test_reset_period_volumes_zeros_only_volumes():
    rec = L2Reconstructor()
    rec.reset()
    rec.apply_event(1, 3, 100, 0.5)
    rec.apply_event(1, 4, 101, 0.5)
    rec.apply_event(2, 2, 100, -0.1)   # qty<0 → period_taker_sell_vol
    rec.apply_event(2, 2, 100, 0.05)   # qty>0 → period_taker_buy_vol
    assert rec.period_taker_buy_vol == 0.05
    assert rec.period_taker_sell_vol == 0.1
    rec.reset_period_volumes()
    assert rec.period_taker_buy_vol == 0.0
    assert rec.period_taker_sell_vol == 0.0
    # Book preserved
    assert rec.bids == {100: 0.5}
    assert rec.asks == {101: 0.5}


def test_qty_zero_deletes_level():
    rec = L2Reconstructor()
    rec.reset()
    rec.apply_event(1, 3, 100, 0.5)
    rec.apply_event(1, 4, 101, 0.5)
    rec.apply_event(2, 0, 100, 0.0)  # delete bid
    assert rec.bids == {}
    rec.apply_event(3, 1, 101, 0.0)  # delete ask
    assert rec.asks == {}


def test_microprice_makes_sense():
    rec = L2Reconstructor()
    rec.reset()
    rec.apply_event(1, 3, 100, 0.6)  # bid 100 @ 0.6
    rec.apply_event(1, 4, 101, 0.4)  # ask 101 @ 0.4
    state = rec.get_state(top_n=1)
    # microprice = (100 * 0.4 + 101 * 0.6) / (0.6 + 0.4) = (40 + 60.6) / 1.0 = 100.6
    assert abs(state["microprice"] - 100.6) < 1e-9
    assert state["mid_price"] == 100.5
    # ask side has more pressure (less qty), so microprice > mid
    assert state["microprice"] > state["mid_price"]


def test_imbalance_signs():
    rec = L2Reconstructor()
    rec.reset()
    rec.apply_event(1, 3, 100, 0.8)
    rec.apply_event(1, 4, 101, 0.2)
    state = rec.get_state(top_n=1)
    # bid >> ask, imb_top1 positive
    assert state["imbalance_top1"] > 0


def test_top1_dust_cross_recovery():
    """Regression for B0 (nyx 2026-05-06): when recorder retains stale
    dust bid above current ask (price never deleted), get_top1 must walk
    past dust instead of returning None forever."""
    rec = L2Reconstructor()
    rec.reset()
    # Snapshot: dust bid at high price + dust ask just above
    rec.apply_event(1000, 3, 1_000_000, 1e-5)   # dust bid
    rec.apply_event(1000, 4, 1_000_001, 1e-5)   # dust ask
    # Real market moves down — new bid + new ask both BELOW the dust
    rec.apply_event(2000, 0, 999_000, 0.5)      # real bid (high qty)
    rec.apply_event(2000, 1, 999_500, 0.5)      # real ask (high qty)

    state = rec.get_top1()
    assert state is not None, "dust must not break top1"
    # Should pick the highest non-crossed pair: real bid 999000 vs real ask 999500
    assert state["best_bid"] == 999_000, state["best_bid"]
    assert state["best_ask"] == 999_500, state["best_ask"]


def test_top1_recovers_after_dust_resolution_caches_correctly():
    """After resolving past dust, subsequent get_top1 calls without book
    changes are O(1) (cache holds the resolved values)."""
    rec = L2Reconstructor()
    rec.reset()
    rec.apply_event(1000, 3, 1_000_000, 1e-5)
    rec.apply_event(1000, 4, 1_000_001, 1e-5)
    rec.apply_event(2000, 0, 999_000, 0.5)
    rec.apply_event(2000, 1, 999_500, 0.5)

    s1 = rec.get_top1()
    # Internal cache should be the cleaned values now
    assert rec._best_bid_p == 999_000
    assert rec._best_ask_p == 999_500
    s2 = rec.get_top1()
    assert s1 == s2


def test_new_snapshot_batch_clears_stale_levels():
    """A new snapshot batch (side=3 at new ts) should atomically wipe
    stale bid levels from prior snapshots/updates. Prevents dust
    accumulation that broke basis_* features in segment-parallel runs."""
    rec = L2Reconstructor()
    rec.reset()
    # First snapshot at ts=1000: bids 100, 99
    rec.apply_event(1000, 3, 100, 0.5)
    rec.apply_event(1000, 3, 99, 0.3)
    rec.apply_event(1000, 4, 101, 0.4)
    rec.apply_event(1000, 4, 102, 0.2)
    # Second snapshot at ts=2000: bids 95, 94 (no 100/99). Old bids must vanish.
    rec.apply_event(2000, 3, 95, 0.5)
    rec.apply_event(2000, 3, 94, 0.3)
    assert set(rec.bids.keys()) == {95, 94}, rec.bids
    assert rec._best_bid_p == 95
    # Asks side should NOT be cleared (no new ask snapshot at ts=2000)
    assert set(rec.asks.keys()) == {101, 102}


def test_same_ts_snapshot_levels_accumulate_within_batch():
    """Multiple side=3 events at the same ts are part of one batch and
    must NOT each trigger a clear."""
    rec = L2Reconstructor()
    rec.reset()
    rec.apply_event(1000, 3, 100, 0.5)
    rec.apply_event(1000, 3, 99, 0.3)
    rec.apply_event(1000, 3, 98, 0.2)
    assert set(rec.bids.keys()) == {100, 99, 98}


def test_snapshot_clear_drops_dust_when_market_moves_down():
    """Reproduce dust scenario: snapshot 1 at high price, market falls,
    snapshot 2 at lower price. Old high-price levels must be dropped so
    get_top1 doesn't return crossed/None."""
    rec = L2Reconstructor()
    rec.reset()
    # Snap 1 — high price book
    rec.apply_event(1000, 3, 1_000_000, 0.5)
    rec.apply_event(1000, 4, 1_000_001, 0.5)
    state1 = rec.get_top1()
    assert state1["best_bid"] == 1_000_000
    # Snap 2 — market lower, no overlap with old levels
    rec.apply_event(2000, 3, 999_000, 0.5)
    rec.apply_event(2000, 4, 999_500, 0.5)
    state2 = rec.get_top1()
    assert state2["best_bid"] == 999_000, state2
    assert state2["best_ask"] == 999_500
    # Old dust must be wiped
    assert 1_000_000 not in rec.bids
    assert 1_000_001 not in rec.asks


def test_top1_returns_none_when_no_non_crossing_pair():
    """If every bid >= every ask (pathological), get_top1 returns None."""
    rec = L2Reconstructor()
    rec.reset()
    rec.apply_event(1000, 3, 1000, 0.5)
    rec.apply_event(1000, 4, 999, 0.5)   # all bids > all asks
    state = rec.get_top1()
    assert state is None


def test_replay_dataframe_round_trip():
    events = _make_events()
    df = pd.DataFrame(events, columns=["timestamp", "side", "price", "quantity"])
    df = df.sort_values(["timestamp", "side"], ascending=[True, False])
    rec, state = L2Reconstructor.replay_dataframe(df, depth_limit=10)
    assert state is not None
    assert state["best_bid"] == 100, state["best_bid"]
    assert state["best_ask"] == 100.5, state["best_ask"]


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
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print()
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(failed)
