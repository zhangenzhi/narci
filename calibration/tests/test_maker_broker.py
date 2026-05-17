"""MakerSimBroker v1 unit tests.

Covers: place / cancel / fill via trade event / spec validation /
post-only emulation / spot-only inventory / queue eat-down.
"""

from __future__ import annotations

import sys

from backtest.symbol_spec import SymbolSpec
from calibration import priors as P
from simulation import MakerSimBroker


def _make_broker(*, post_only=True, top1_size=0.02):
    spec = SymbolSpec(symbol="BTC_JPY", tick_size=1.0, lot_size=1e-8, min_notional=0.0)
    params = P.get_priors("coincheck", "BTC_JPY")
    params.top1_size_median = top1_size
    params.cancel_latency_p50_ms = 100.0  # explicit
    return MakerSimBroker(params=params, symbol_spec=spec)


def _seed_book(broker, ts=1_000_000_000):
    """Inject a basic snapshot so book is ready."""
    # Bid side
    for p, q in [(100, 0.5), (99, 0.5), (98, 0.5)]:
        broker.apply_market_event(ts, 3, p, q)
    # Ask side
    for p, q in [(101, 0.5), (102, 0.5), (103, 0.5)]:
        broker.apply_market_event(ts, 4, p, q)
    return ts + 1_000_000  # ns


def test_book_seed_makes_state_ready():
    b = _make_broker()
    _seed_book(b)
    state = b.book.get_state(top_n=3)
    assert state is not None
    assert state["best_bid"] == 100
    assert state["best_ask"] == 101


def test_place_then_filled_by_trade():
    b = _make_broker()
    ts = _seed_book(b)
    # Place a BUY at price=99 (existing level, joins behind 0.5)
    cid = b.place_limit(ts, "echo-1", "BUY", 99, 0.1)
    assert cid == "echo-1"
    assert "echo-1" in b.active_orders
    o = b.active_orders["echo-1"]
    # Queue ahead = 0.5 (existing 99 level qty); top1_size=0.02 → est rank = 25
    assert o.queue_ahead_qty == 0.5
    assert o.estimated_queue_position == 25

    # Trade comes that consumes 0.4 at price 99 (qty>0 = aggressive seller hit bid)
    ts += 1_000_000
    b.apply_market_event(ts, 2, 99, 0.4)
    # Our queue: was 0.5 ahead, consumed 0.4 → 0.1 ahead, no fill yet
    assert abs(o.queue_ahead_qty - 0.1) < 1e-9, o.queue_ahead_qty
    assert o.qty_filled == 0
    assert b.inventory == 0

    # Another trade consumes another 0.2 — first 0.1 finishes queue, then 0.1 of us fills
    ts += 1_000_000
    b.apply_market_event(ts, 2, 99, 0.2)
    assert abs(o.queue_ahead_qty) < 1e-9
    assert abs(o.qty_filled - 0.1) < 1e-9
    assert abs(b.inventory - 0.1) < 1e-9
    assert b.cash == -0.1 * 99
    assert "echo-1" not in b.active_orders  # fully filled
    assert len(b.sim_fills) == 1


def test_post_only_rejects_cross():
    b = _make_broker()
    _seed_book(b)
    # BUY at price 101 = best_ask → would cross (taker). Must reject.
    cid = b.place_limit(1_001_000_000, "echo-2", "BUY", 101, 0.05)
    assert cid is None
    assert "echo-2" not in b.active_orders
    # The reject should have logged a PLACE_REJECT decision with WOULD_CROSS_ASK
    rejects = [d for d in b.sim_decisions if d["event_type"] == "PLACE_REJECT"]
    assert len(rejects) == 1
    assert "WOULD_CROSS_ASK" in rejects[0]["reason"]


def test_spot_only_inventory_blocks_short_sell():
    b = _make_broker()
    _seed_book(b)
    cid = b.place_limit(1_001_000_000, "echo-3", "SELL", 102, 0.005)
    assert cid is None
    rejects = [d for d in b.sim_decisions if d["event_type"] == "PLACE_REJECT"]
    assert len(rejects) == 1
    assert "INVENTORY_INSUFFICIENT" in rejects[0]["reason"]


def test_sell_after_buy_works():
    b = _make_broker()
    ts = _seed_book(b)
    # Build inventory by buying via trade fill
    b.place_limit(ts, "echo-4", "BUY", 99, 0.05)
    ts += 1_000_000
    b.apply_market_event(ts, 2, 99, 1.0)  # huge sell sweep, fills us after 0.5 queue
    assert b.inventory > 0
    # Now we can place a SELL
    ts += 1_000_000
    cid = b.place_limit(ts, "echo-5", "SELL", 102, 0.05)
    assert cid == "echo-5"


def test_concurrent_sells_reserve_inventory():
    """echo 2026-05-17 #6: two concurrent SELLs at the boundary used to
    both pass the place check (no reservation), then both fill and drive
    inventory negative (-0.001 BTC observed on 0510). Fix reserves
    qty_remaining of every ACTIVE SELL before admitting a new one."""
    b = _make_broker()
    ts = _seed_book(b)
    # Build inventory of exactly 0.001 BTC at price 99
    b.place_limit(ts, "buy-1", "BUY", 99, 0.001)
    ts += 1_000_000
    # Sweep enough volume to fill the 0.5 queue ahead + our 0.001
    b.apply_market_event(ts, 2, 99, 0.6)
    assert abs(b.inventory - 0.001) < 1e-12, b.inventory

    # First SELL of 0.001 — should pass (inventory == qty exactly)
    ts += 1_000_000
    cid1 = b.place_limit(ts, "sell-1", "SELL", 102, 0.001)
    assert cid1 == "sell-1", "first SELL at boundary must pass"
    assert "sell-1" in b.active_orders

    # Second concurrent SELL of 0.001 — MUST reject; without
    # reservation it used to pass and later drive inventory negative.
    ts += 1_000_000
    cid2 = b.place_limit(ts, "sell-2", "SELL", 103, 0.001)
    assert cid2 is None, (
        "second SELL must be rejected: inventory already fully reserved "
        "by sell-1. Pre-fix this slipped through and the eventual fills "
        "drove inventory to -0.001 (echo 0510 incident).")
    rejects = [d for d in b.sim_decisions if d["event_type"] == "PLACE_REJECT"]
    # Should include exactly one INVENTORY_INSUFFICIENT for sell-2.
    inv_rejects = [r for r in rejects if "INVENTORY_INSUFFICIENT" in r["reason"]]
    assert len(inv_rejects) == 1
    assert inv_rejects[0]["client_oid"] == "sell-2"

    # After sell-1 cancels, sell-2 (or a new SELL) becomes admissible.
    ts += 1_000_000
    b.cancel(ts, "sell-1")
    # cancel ack lands after 100ms (cancel_latency_p50_ms=100). Advance.
    ts += 200_000_000  # 200ms in ns
    # Trigger _process_pending_cancels by stepping an arbitrary event
    b.apply_market_event(ts, 0, 99, 0.5)  # bid update (no fill possible)
    assert "sell-1" not in b.active_orders, "sell-1 should be cancelled now"
    # Now a fresh SELL should be admissible — inventory unreserved.
    ts += 1_000_000
    cid3 = b.place_limit(ts, "sell-3", "SELL", 102, 0.001)
    assert cid3 == "sell-3", (
        "after sell-1 cancel ack, inventory unreserved; sell-3 must pass")


def test_cancel_with_latency():
    b = _make_broker()
    ts = _seed_book(b)
    cid = b.place_limit(ts, "echo-6", "BUY", 99, 0.05)
    ts += 1_000_000
    ok = b.cancel(ts, "echo-6")
    assert ok
    assert "echo-6" in b.active_orders   # still active until ack
    assert "echo-6" in b.pending_cancels

    # Below ack time
    ts_pending = ts + int(50e6)  # 50 ms — under 100 ms p50
    b.apply_market_event(ts_pending, 0, 95, 0.1)
    assert "echo-6" in b.active_orders

    # Past ack time
    ts_after = ts + int(150e6)  # 150 ms — past p50
    b.apply_market_event(ts_after, 0, 95, 0.0)
    assert "echo-6" not in b.active_orders
    assert "echo-6" not in b.pending_cancels
    assert len(b.sim_cancels) == 1
    cxl = b.sim_cancels[0]
    assert cxl["final_state"] == "CANCELLED"
    assert abs(cxl["cancel_latency_ms"] - 100.0) < 1e-6


def test_filled_during_cancel():
    b = _make_broker()
    ts = _seed_book(b)
    cid = b.place_limit(ts, "echo-7", "BUY", 99, 0.05)
    o = b.active_orders["echo-7"]
    # First eat the 0.5 queue ahead (full sweep)
    ts += 1_000_000
    b.apply_market_event(ts, 2, 99, 0.5)
    assert abs(o.queue_ahead_qty) < 1e-9
    # Issue cancel — order enters cancel pipeline
    ts += 1_000_000
    b.cancel(ts, "echo-7")
    # Trade hits us BEFORE cancel ack
    ts += 1_000_000
    b.apply_market_event(ts, 2, 99, 0.05)
    # Order is fully filled but still tracked in active_orders so the
    # imminent cancel-ack can emit FILLED_DURING_CANCEL with full info
    assert "echo-7" in b.active_orders
    assert b.active_orders["echo-7"].state == "FILLED"
    assert b.sim_fills, "should have one fill"

    # Now process cancel ack — should record FILLED_DURING_CANCEL and finally
    # remove the order
    ts += int(200e6)  # past p50
    b.apply_market_event(ts, 0, 95, 0.1)
    assert "echo-7" not in b.active_orders
    assert b.sim_cancels
    assert b.sim_cancels[0]["final_state"] == "FILLED_DURING_CANCEL"


def test_cancel_unknown_oid_returns_false():
    b = _make_broker()
    _seed_book(b)
    assert b.cancel(2_000_000_000, "no-such-oid") is False


def test_double_cancel_returns_false():
    b = _make_broker()
    ts = _seed_book(b)
    b.place_limit(ts, "echo-8", "BUY", 99, 0.05)
    ts += 1_000_000
    assert b.cancel(ts, "echo-8") is True
    assert b.cancel(ts, "echo-8") is False  # already in pipeline


def test_book_qty_increase_does_not_advance_queue():
    """When level qty *grows*, our queue_ahead_qty must NOT decrease.
    (New entrants go behind us, not in front.)"""
    b = _make_broker()
    ts = _seed_book(b)
    cid = b.place_limit(ts, "echo-9", "BUY", 99, 0.05)
    o = b.active_orders["echo-9"]
    initial_ahead = o.queue_ahead_qty
    ts += 1_000_000
    # Someone adds 1.0 BTC at price 99 (level grows from 0.5 to 1.5)
    b.apply_market_event(ts, 0, 99, 1.5)
    # Queue ahead should be unchanged
    assert abs(o.queue_ahead_qty - initial_ahead) < 1e-9


def test_book_qty_decrease_advances_queue():
    """When level qty shrinks (cancel ahead of us), queue_ahead reduces."""
    b = _make_broker()
    ts = _seed_book(b)
    cid = b.place_limit(ts, "echo-10", "BUY", 99, 0.05)
    o = b.active_orders["echo-10"]
    initial_ahead = o.queue_ahead_qty
    assert initial_ahead == 0.5
    ts += 1_000_000
    # Level qty drops from 0.5 → 0.2 (someone cancelled 0.3)
    b.apply_market_event(ts, 0, 99, 0.2)
    # Our queue_ahead should drop by 0.3
    assert abs(o.queue_ahead_qty - 0.2) < 1e-9


def test_stats_reports_uncalibrated():
    b = _make_broker()
    s = b.stats()
    assert s["uncalibrated"] is True
    assert s["active_orders"] == 0
    assert s["n_fills"] == 0


def test_validate_rejects_bad_tick():
    b = _make_broker()
    _seed_book(b)
    # tick_size=1.0; price 99.5 not aligned
    cid = b.place_limit(1_001_000_000, "echo-11", "BUY", 99.5, 0.05)
    assert cid is None
    assert any("SPEC" in d.get("reason", "") for d in b.sim_decisions)


def test_min_notional_enforced():
    spec = SymbolSpec(symbol="BTC_JPY", tick_size=1.0, lot_size=1e-8, min_notional=10000.0)
    params = P.get_priors("coincheck", "BTC_JPY")
    b = MakerSimBroker(params=params, symbol_spec=spec)
    _seed_book(b)
    # 99 * 0.001 = 99 << 10000
    cid = b.place_limit(1_001_000_000, "echo-12", "BUY", 99, 0.001)
    assert cid is None
    assert any("min_notional" in d.get("reason", "") for d in b.sim_decisions)


# ---------------------------------------------------------------- #
# P3.1 fix 2026-05-13 — price-penetration fill
# ---------------------------------------------------------------- #

def test_buy_filled_by_penetrating_trade_below_quote():
    """nyx P3.1 (2026-05-13): a sell-taker walking through the bid stack
    must fill our BUY before the trade print reaches a lower price.

    Setup: BUY quote at 100. A trade prints at 99 (below us, qty>0 =
    aggressive seller).
    Required: order filled at OUR price 100 (NOT 99), inventory +qty.
    Pre-fix: order skipped (strict price == trade_price), 0 fills."""
    b = _make_broker()
    ts = _seed_book(b)
    cid = b.place_limit(ts, "p3-buy", "BUY", 100, 0.05)
    assert cid == "p3-buy"

    # Penetrating sell-taker trade: trade_qty>0 (buyer maker = seller hit
    # the bid stack), price 99 < our BUY price 100.
    ts += 1_000_000
    b.apply_market_event(ts, 2, 99, 1.0)  # large sweep

    assert "p3-buy" not in b.active_orders, "BUY should be filled by penetration"
    assert b.inventory == 0.05
    # Fill price = our quote price, not the lower trade print.
    assert b.cash == -0.05 * 100, f"expected fill at 100, cash={b.cash}"
    assert len(b.sim_fills) == 1
    fill = b.sim_fills[0]
    assert fill["fill_price"] == 100, f"fill price should be quote price 100, got {fill['fill_price']}"


def test_sell_filled_by_penetrating_trade_above_quote():
    """Symmetric case: SELL quote at 101, trade prints at 102 (above us).
    Required: filled at 101 (our price)."""
    b = _make_broker()
    ts = _seed_book(b)
    # Need inventory to sell. Buy first.
    b.place_limit(ts, "p3-buyseed", "BUY", 100, 0.1)
    ts += 1_000_000
    b.apply_market_event(ts, 2, 99, 1.0)   # penetration fills the buy
    assert b.inventory == 0.1

    ts += 1_000_000
    cid = b.place_limit(ts, "p3-sell", "SELL", 101, 0.05)
    assert cid == "p3-sell"

    # Penetrating buy-taker: trade_qty<0, price 102 > our SELL 101.
    ts += 1_000_000
    b.apply_market_event(ts, 2, 102, -1.0)

    assert "p3-sell" not in b.active_orders
    assert abs(b.inventory - 0.05) < 1e-9
    fill = [f for f in b.sim_fills if f["client_oid"] == "p3-sell"][0]
    assert fill["fill_price"] == 101, f"sell fill at quote 101 (not trade 102), got {fill['fill_price']}"


def test_no_fill_when_trade_in_our_favor():
    """The opposite direction must NOT fill. BUY at 100, trade prints
    at 101 (above us). No bid-side liquidity was consumed at our level,
    so we are not filled.

    Equivalent: a buy-taker walked into the ask side; that does not
    interact with bids."""
    b = _make_broker()
    ts = _seed_book(b)
    cid = b.place_limit(ts, "p3-favor", "BUY", 100, 0.05)
    assert cid == "p3-favor"

    ts += 1_000_000
    # trade_qty<0 (seller maker = buyer hit ask), price 101 — touches ask
    # side, not our BUY. Even if we conjured a buyer-maker trade at 101
    # (trade_qty>0 at 101 above our BUY), it would still not fill us
    # because 101 > 100 means our level wasn't touched.
    b.apply_market_event(ts, 2, 101, 1.0)

    assert "p3-favor" in b.active_orders, "BUY should not fill on trade above us"
    assert b.inventory == 0


def test_penetration_does_not_double_fill_with_remaining_quantity():
    """A single penetrating trade with massive qty fills our order once,
    not multiple times. qty_remaining tracking still correct."""
    b = _make_broker()
    ts = _seed_book(b)
    cid = b.place_limit(ts, "p3-once", "BUY", 100, 0.03)

    ts += 1_000_000
    # Huge sweep penetrates well past us
    b.apply_market_event(ts, 2, 98, 10.0)

    assert b.inventory == 0.03  # exactly our order size, no double-fill
    fills_for_us = [f for f in b.sim_fills if f["client_oid"] == "p3-once"]
    assert len(fills_for_us) == 1
    assert fills_for_us[0]["fill_qty"] == 0.03


def test_same_price_queue_path_unchanged():
    """The pre-existing same-price queue eat-down still works after the
    P3.1 fix (regression check for case 1 path)."""
    b = _make_broker()
    ts = _seed_book(b)
    cid = b.place_limit(ts, "p3-queue", "BUY", 99, 0.1)
    o = b.active_orders["p3-queue"]
    assert o.queue_ahead_qty == 0.5

    # Consume 0.4 at same price — queue 0.5 → 0.1, no fill.
    ts += 1_000_000
    b.apply_market_event(ts, 2, 99, 0.4)
    assert abs(o.queue_ahead_qty - 0.1) < 1e-9
    assert "p3-queue" in b.active_orders

    # Another 0.2 finishes queue then 0.1 fills.
    ts += 1_000_000
    b.apply_market_event(ts, 2, 99, 0.2)
    assert "p3-queue" not in b.active_orders
    assert abs(b.inventory - 0.1) < 1e-9


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
