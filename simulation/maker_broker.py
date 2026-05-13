"""MakerSimBroker v1 — maker-quoting simulator.

Drives a (paper) maker strategy through a stream of L2 events, modeling:

  * FIFO queue position (estimated from total qty at price level — no order id
    in narci raw data, so this is necessarily an approximation)
  * Per-trade fill consumption with queue eat-down
  * Spot-only inventory (>= 0)
  * Post-only emulation (reject crossing limits)
  * Cancel latency drawn from CalibrationParams.cancel_latency_p50_ms (v1
    uses constant; v2 will use distribution)

Output: three lists of dict events compatible with
`narci.calibration.schema.{DecisionEvent, FillEvent, CancelEvent}`. The
replay tool consumes these alongside echo's real logs.

What v1 does NOT do (Step 2+):

  * Adverse selection tracking (post-fill mid trajectory). Hooks present
    but populate fixed NaN; replay tool measures from L2 stream after the
    fact instead.
  * Walking the book on trade events. v1 only fills our orders at
    exact price = trade_price. If a market sweep crosses multiple
    levels, we model only the level we sit at.
  * Cancel-while-fill race. If a cancel and a fill arrive in the same
    ts, fill takes precedence (more conservative for PnL).

Caller pattern:

    broker = MakerSimBroker(params=priors.get_priors("coincheck", "BTC_JPY"),
                            symbol_spec=cc_spec)
    for ts, side, price, qty in raw_events:
        broker.apply_market_event(ts, side, price, qty)
        # strategy decides every K events / N seconds:
        cid = broker.place_limit(ts, gen_cid(), "BUY", 12_449_500, 0.005)
        broker.cancel(ts, cid)
    decisions, fills, cancels = broker.flush_results()
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from data.l2_reconstruct import L2Reconstructor
from backtest.symbol_spec import SymbolSpec
from calibration.priors import CalibrationParams


# Side codes (mirror narci recorder)
_BID = 0
_ASK = 1
_TRADE = 2
_BID_SNAP = 3
_ASK_SNAP = 4


@dataclass(slots=True)
class SimOrder:
    client_oid: str
    side: str                       # "BUY" | "SELL"
    price: float
    qty_total: float
    qty_remaining: float
    qty_filled: float
    place_ts: int                   # ns
    queue_ahead_qty: float          # remaining qty in front of us at our price level
    state: str                      # "ACTIVE" | "FILLED" | "CANCELLED"
    alpha_pred_bps: float = float("nan")
    alpha_features_hash: str = ""
    alpha_source: str = "naive"
    estimated_queue_position: int = -1
    # cancel lifecycle
    cancel_request_ts: int = 0
    cancel_ack_ts: int = 0
    cancel_reason: str = ""
    # mid snapshots for adverse-selection diagnostics (Step 2 fills these)
    mid_at_place: float = float("nan")
    inventory_before: float = 0.0


class MakerSimBroker:
    def __init__(
        self,
        params: CalibrationParams,
        symbol_spec: SymbolSpec,
        depth_limit: int = 20,
    ):
        self.params = params
        self.symbol_spec = symbol_spec
        self.symbol = params.symbol

        self.book = L2Reconstructor(depth_limit=depth_limit)
        self.book.reset()

        self.active_orders: dict[str, SimOrder] = {}
        # cid → ack_due_ts (ns); order is *still* in active_orders during this
        # window, only removed when ack arrives. Fills can still happen.
        self.pending_cancels: dict[str, int] = {}

        # Inventory accounting
        self.inventory: float = 0.0
        self.cash: float = 0.0

        # Output streams (dict events; schema-compatible)
        self.sim_decisions: list[dict] = []
        self.sim_fills: list[dict] = []
        self.sim_cancels: list[dict] = []

        self._cur_ts: int = 0   # last ts fed via apply_market_event

    # ---------------------------------------------------------------- #
    # Main event loop
    # ---------------------------------------------------------------- #

    def apply_market_event(self, ts: int, side: int, price: float, qty: float) -> None:
        """Feed a single L2 / trade event from the raw stream.

        Order of operations:
          1. Update internal book
          2. Process pending cancel acks that have come due
          3. On book updates: refresh queue_ahead_qty for our orders at
             affected levels
          4. On trade: attempt to consume queue + fill our matching orders
        """
        self._cur_ts = ts
        prev_qty_at_my_levels = self._snapshot_my_levels(side, price)

        self.book.apply_event(ts, side, price, qty)

        self._process_pending_cancels(ts)

        if side in (_BID, _ASK, _BID_SNAP, _ASK_SNAP):
            self._update_queues_after_book(side, price, qty, prev_qty_at_my_levels)

        if side == _TRADE:
            self._maybe_fill_on_trade(ts, price, qty)

    # ---------------------------------------------------------------- #
    # Order placement & cancellation
    # ---------------------------------------------------------------- #

    def place_limit(
        self,
        ts: int,
        client_oid: str,
        side: str,
        price: float,
        qty: float,
        *,
        alpha_pred_bps: float = float("nan"),
        alpha_features_hash: str = "",
        alpha_source: str = "naive",
        reason: str = "",
    ) -> str | None:
        """Place a maker (post-only) limit order. Returns client_oid on
        success, or None on rejection. Logs a DecisionEvent regardless."""
        side = side.upper()
        if side not in ("BUY", "SELL"):
            self._emit_reject(ts, client_oid, side, price, qty, "BAD_SIDE",
                              alpha_pred_bps, alpha_features_hash, alpha_source)
            return None

        # SymbolSpec validation (tick / lot / min_notional)
        err = self.symbol_spec.validate(price, qty)
        if err:
            self._emit_reject(ts, client_oid, side, price, qty, f"SPEC:{err}",
                              alpha_pred_bps, alpha_features_hash, alpha_source)
            return None

        # Spot-only inventory check (SELL needs base asset)
        if side == "SELL" and self.inventory + 1e-12 < qty:
            self._emit_reject(ts, client_oid, side, price, qty,
                              "INVENTORY_INSUFFICIENT",
                              alpha_pred_bps, alpha_features_hash, alpha_source)
            return None

        # Post-only: reject if would cross
        state = self.book.get_top1()
        if state is not None:
            if side == "BUY" and price >= state["best_ask"]:
                self._emit_reject(ts, client_oid, side, price, qty, "WOULD_CROSS_ASK",
                                  alpha_pred_bps, alpha_features_hash, alpha_source)
                return None
            if side == "SELL" and price <= state["best_bid"]:
                self._emit_reject(ts, client_oid, side, price, qty, "WOULD_CROSS_BID",
                                  alpha_pred_bps, alpha_features_hash, alpha_source)
                return None

        # Estimate initial queue position: assume joining the back.
        # Look up current cumulative qty at this price level.
        ahead_qty = self._book_qty_at_price(side, price)
        ahead_qty *= self.params.queue_scaling
        # Estimate position rank as a coarse "how many distinct orders ahead"
        # Heuristic: each unit of qty ≈ one order at typical top-1 size.
        if self.params.top1_size_median > 0:
            est_rank = int(ahead_qty / self.params.top1_size_median)
        else:
            est_rank = -1

        order = SimOrder(
            client_oid=client_oid,
            side=side, price=price,
            qty_total=qty, qty_remaining=qty, qty_filled=0.0,
            place_ts=ts, queue_ahead_qty=ahead_qty,
            state="ACTIVE",
            alpha_pred_bps=alpha_pred_bps,
            alpha_features_hash=alpha_features_hash,
            alpha_source=alpha_source,
            estimated_queue_position=est_rank,
            mid_at_place=state["mid_price"] if state else float("nan"),
            inventory_before=self.inventory,
        )
        self.active_orders[client_oid] = order
        self._emit_place(ts, order, state)
        return client_oid

    def cancel(self, ts: int, client_oid: str, reason: str = "STRATEGY_REPRICE") -> bool:
        """Issue a cancel. The cancel takes effect after a latency window
        (`params.cancel_latency_p50_ms`); fills can still happen during
        that window."""
        order = self.active_orders.get(client_oid)
        if order is None or order.state != "ACTIVE":
            return False
        if order.client_oid in self.pending_cancels:
            return False  # already in cancel pipeline
        order.cancel_request_ts = ts
        order.cancel_reason = reason
        latency_ns = int(self.params.cancel_latency_p50_ms * 1_000_000)
        self.pending_cancels[client_oid] = ts + latency_ns
        return True

    def record_reprice_trigger(
        self, ts: int, reason: str,
        alpha_pred_bps: float = float("nan"),
        alpha_features_hash: str = "",
        alpha_source: str = "naive",
    ) -> None:
        """Strategy calls this when its internal logic decided to reprice.
        Pure logging — broker doesn't use it for state."""
        state = self.book.get_top1()
        self.sim_decisions.append({
            "event_type": "REPRICE_TRIGGER",
            "ts_ns": ts, "ts_wall_ns": ts,
            "client_oid": "", "symbol": self.symbol,
            "mid_price": state["mid_price"] if state else float("nan"),
            "best_bid":  state["best_bid"]  if state else float("nan"),
            "best_ask":  state["best_ask"]  if state else float("nan"),
            "top1_bid_qty": state["bid_qty_top1"] if state else float("nan"),
            "top1_ask_qty": state["ask_qty_top1"] if state else float("nan"),
            "spread_bps":   state["spread_bps"]   if state else float("nan"),
            "alpha_pred_bps": alpha_pred_bps,
            "alpha_features_hash": alpha_features_hash,
            "alpha_source": alpha_source,
            "side": "", "price": float("nan"), "qty": float("nan"),
            "estimated_queue_position": -1,
            "estimated_queue_ahead_qty": float("nan"),
            "place_oid": "", "reason": reason,
        })

    # ---------------------------------------------------------------- #
    # Internals: queue & fills
    # ---------------------------------------------------------------- #

    def _book_qty_at_price(self, our_side: str, price: float) -> float:
        """Total qty currently sitting at (price) on our side. New entrant
        joins behind this much qty."""
        side_dict = self.book.bids if our_side == "BUY" else self.book.asks
        return side_dict.get(price, 0.0)

    def _snapshot_my_levels(self, evt_side: int, price: float) -> dict:
        """Capture qty at our-active-orders' levels BEFORE event applied.
        Used to compute `delta = before - after` for queue advancement."""
        snap = {}
        evt_is_bid = evt_side in (_BID, _BID_SNAP)
        evt_is_ask = evt_side in (_ASK, _ASK_SNAP)
        target_side = "BUY" if evt_is_bid else ("SELL" if evt_is_ask else None)
        if target_side is None:
            return snap
        for cid, o in self.active_orders.items():
            if o.side != target_side or o.price != price:
                continue
            book = self.book.bids if target_side == "BUY" else self.book.asks
            snap[cid] = book.get(price, 0.0)
        return snap

    def _update_queues_after_book(self, evt_side: int, price: float,
                                   evt_qty: float, prev_qtys: dict) -> None:
        """When level qty *decreases*, infer that someone in front of us
        cancelled or got filled → reduce our queue_ahead_qty.

        When level qty *increases*, assume new entrant goes BEHIND us
        (our queue_ahead_qty unchanged).

        This is the v1 approximation. It treats all qty deltas as 'in
        front of us' which over-credits us; v2 may add probabilistic
        attribution.
        """
        evt_is_bid = evt_side in (_BID, _BID_SNAP)
        evt_is_ask = evt_side in (_ASK, _ASK_SNAP)
        target_side = "BUY" if evt_is_bid else ("SELL" if evt_is_ask else None)
        if target_side is None:
            return
        book = self.book.bids if target_side == "BUY" else self.book.asks
        new_qty_at_price = book.get(price, 0.0)
        for cid, prev_qty in prev_qtys.items():
            order = self.active_orders.get(cid)
            if order is None or order.state != "ACTIVE":
                continue
            delta = prev_qty - new_qty_at_price
            if delta > 0:
                # someone in front of us went away
                order.queue_ahead_qty = max(0.0, order.queue_ahead_qty - delta)

    def _maybe_fill_on_trade(self, ts: int, trade_price: float, trade_qty: float) -> None:
        """A trade just printed. Determine which side of our orders is
        affected, eat through queues, and fill what's left.

        narci convention: trade_qty > 0 → buyer maker → aggressive seller hit
        a BID. So our BUY orders are eligible for fill. trade_qty < 0 →
        seller maker → aggressive buyer hit an ASK; our SELL orders eligible.

        Two fill cases, both physically required:

        Case 1 — same price (`order.price == trade_price`): standard queue
        consumption. The taker walked into our price level. Our position
        depends on queue_ahead_qty.

        Case 2 — price penetration (P3.1 fix 2026-05-13). The taker walked
        PAST our price to a worse level:
          - BUY  side: trade printed below our quote
          - SELL side: trade printed above our quote
        For the printer to print past us, all liquidity at our price had
        to be consumed first — including us. So our quote is necessarily
        filled at our own price (NOT trade_price), with queue_ahead_qty
        becoming irrelevant. This case is logically required regardless
        of counterfactual / improve-1-tick assumptions; it is a property
        of how a price gets penetrated in any orderbook. Without this
        path, simulator severely undercounts fills on `improve_1_tick`
        strategies (nyx 2026-05-13: 1 fill vs ~10-25 expected on 05-04).
        """
        if trade_qty > 0:
            target_side = "BUY"
            remaining = trade_qty
        elif trade_qty < 0:
            target_side = "SELL"
            remaining = abs(trade_qty)
        else:
            return

        # Iterate active orders matching the trade side.
        for cid in list(self.active_orders.keys()):
            if remaining <= 1e-12:
                break
            order = self.active_orders[cid]
            if order.state != "ACTIVE":
                continue
            if order.side != target_side:
                continue

            if order.price == trade_price:
                # Case 1: same price — work through queue.
                consumed = min(remaining, order.queue_ahead_qty)
                order.queue_ahead_qty -= consumed
                remaining -= consumed
                if remaining <= 1e-12 or order.queue_ahead_qty > 1e-12:
                    continue

                fill_qty = min(remaining, order.qty_remaining)
                if fill_qty <= 1e-12:
                    continue
                self._apply_fill(ts, order, fill_qty, trade_price)
                remaining -= fill_qty

            elif ((target_side == "BUY" and trade_price < order.price)
                  or (target_side == "SELL" and trade_price > order.price)):
                # Case 2: penetration — trade printed past our price.
                # Our quote is necessarily filled at our own price.
                # queue_ahead_qty is moot (entire level was consumed
                # to reach the penetrated print).
                fill_qty = min(remaining, order.qty_remaining)
                if fill_qty <= 1e-12:
                    continue
                self._apply_fill(ts, order, fill_qty, order.price)
                order.queue_ahead_qty = 0.0
                remaining -= fill_qty

            # Case 3: trade is in our favor (better than our quote) —
            # taker did not reach our level. No fill.

    def _apply_fill(self, ts: int, order: SimOrder, fill_qty: float,
                    fill_price: float) -> None:
        order.qty_filled += fill_qty
        order.qty_remaining -= fill_qty

        inv_before = self.inventory
        if order.side == "BUY":
            self.inventory += fill_qty
            self.cash -= fill_qty * fill_price
        else:
            self.inventory -= fill_qty
            self.cash += fill_qty * fill_price

        state = self.book.get_top1()
        mid_at_fill = state["mid_price"] if state else fill_price
        spread_bps_at_fill = state["spread_bps"] if state else float("nan")

        self.sim_fills.append({
            "ts_ns": ts, "ts_wall_ns": ts, "exchange_ts_ms": int(ts // 1_000_000),
            "client_oid": order.client_oid,
            "exchange_oid": f"sim-{order.client_oid}",
            "symbol": self.symbol, "side": order.side,
            "fill_price": fill_price, "fill_qty": fill_qty, "is_maker": True,
            "place_ts_ns": order.place_ts,
            "quote_age_ms": (ts - order.place_ts) / 1e6,
            "mid_at_place": order.mid_at_place,
            "mid_at_fill": mid_at_fill,
            "spread_at_fill_bps": spread_bps_at_fill,
            # Step-2 placeholders — replay tool fills these from L2 stream
            "mid_t_plus_500ms": float("nan"),
            "mid_t_plus_1s": float("nan"),
            "mid_t_plus_5s": float("nan"),
            "mid_t_plus_30s": float("nan"),
            "estimated_queue_at_place": order.estimated_queue_position,
            "estimated_queue_at_fill": 0,  # we just got filled → we were at front
            "inventory_before": inv_before,
            "inventory_after": self.inventory,
            "alpha_pred_bps_at_place": order.alpha_pred_bps,
            "alpha_realized_bps_post": float("nan"),
        })

        if order.qty_remaining <= 1e-12:
            order.state = "FILLED"
            # If this order is mid-cancel pipeline, leave it in active_orders
            # so _process_pending_cancels can emit FILLED_DURING_CANCEL with
            # full info. Otherwise remove now.
            if order.client_oid not in self.pending_cancels:
                self.active_orders.pop(order.client_oid, None)

    def _process_pending_cancels(self, ts: int) -> None:
        if not self.pending_cancels:
            return
        done = []
        for cid, ack_ts in self.pending_cancels.items():
            if ts < ack_ts:
                continue
            order = self.active_orders.get(cid)
            if order is None:
                # Already filled & removed during cancel window — emit FILLED_DURING_CANCEL
                done.append(cid)
                continue
            order.cancel_ack_ts = ack_ts
            qty_filled = order.qty_filled
            if qty_filled > 1e-12 and order.qty_remaining > 1e-12:
                final_state = "FILLED_DURING_CANCEL"
            elif order.qty_remaining < 1e-12:
                final_state = "FILLED_DURING_CANCEL"
            else:
                final_state = "CANCELLED"
            self._emit_cancel(order, ack_ts, final_state)
            order.state = "CANCELLED"
            self.active_orders.pop(cid, None)
            done.append(cid)
        for cid in done:
            self.pending_cancels.pop(cid, None)

    # ---------------------------------------------------------------- #
    # Event emitters (decisions / fills / cancels in dict form)
    # ---------------------------------------------------------------- #

    def _emit_place(self, ts: int, order: SimOrder, state: dict | None) -> None:
        self.sim_decisions.append({
            "event_type": "PLACE",
            "ts_ns": ts, "ts_wall_ns": ts,
            "client_oid": order.client_oid, "symbol": self.symbol,
            "mid_price": state["mid_price"] if state else float("nan"),
            "best_bid":  state["best_bid"]  if state else float("nan"),
            "best_ask":  state["best_ask"]  if state else float("nan"),
            "top1_bid_qty": state["bid_qty_top1"] if state else float("nan"),
            "top1_ask_qty": state["ask_qty_top1"] if state else float("nan"),
            "spread_bps":   state["spread_bps"]   if state else float("nan"),
            "alpha_pred_bps": order.alpha_pred_bps,
            "alpha_features_hash": order.alpha_features_hash,
            "alpha_source": order.alpha_source,
            "side": order.side, "price": order.price, "qty": order.qty_total,
            "estimated_queue_position": order.estimated_queue_position,
            "estimated_queue_ahead_qty": order.queue_ahead_qty,
            "place_oid": "", "reason": "",
        })

    def _emit_reject(self, ts: int, client_oid: str, side: str, price: float,
                     qty: float, reason: str,
                     alpha_pred_bps: float, alpha_features_hash: str,
                     alpha_source: str) -> None:
        state = self.book.get_top1()
        # Record as a "PLACE" decision but with reason marking REJECT, so
        # downstream can count rejects without a separate event type
        self.sim_decisions.append({
            "event_type": "PLACE_REJECT",
            "ts_ns": ts, "ts_wall_ns": ts,
            "client_oid": client_oid, "symbol": self.symbol,
            "mid_price": state["mid_price"] if state else float("nan"),
            "best_bid":  state["best_bid"]  if state else float("nan"),
            "best_ask":  state["best_ask"]  if state else float("nan"),
            "top1_bid_qty": state["bid_qty_top1"] if state else float("nan"),
            "top1_ask_qty": state["ask_qty_top1"] if state else float("nan"),
            "spread_bps":   state["spread_bps"]   if state else float("nan"),
            "alpha_pred_bps": alpha_pred_bps,
            "alpha_features_hash": alpha_features_hash,
            "alpha_source": alpha_source,
            "side": side, "price": price, "qty": qty,
            "estimated_queue_position": -1,
            "estimated_queue_ahead_qty": float("nan"),
            "place_oid": "", "reason": reason,
        })

    def _emit_cancel(self, order: SimOrder, ack_ts: int, final_state: str) -> None:
        state = self.book.get_top1()
        self.sim_cancels.append({
            "ts_ns_request": order.cancel_request_ts,
            "ts_wall_ns_request": order.cancel_request_ts,
            "ts_ns_ack": ack_ts,
            "cancel_latency_ms": (ack_ts - order.cancel_request_ts) / 1e6,
            "client_oid": order.client_oid,
            "exchange_oid": f"sim-{order.client_oid}",
            "symbol": self.symbol,
            "place_ts_ns": order.place_ts,
            "quote_age_at_cancel_ms": (order.cancel_request_ts - order.place_ts) / 1e6,
            "cancel_reason": order.cancel_reason,
            "final_state": final_state,
            "qty_filled_before_cancel": order.qty_filled,
            "mid_at_cancel": state["mid_price"] if state else float("nan"),
        })

    # ---------------------------------------------------------------- #
    # Output access
    # ---------------------------------------------------------------- #

    def flush_results(self) -> tuple[list, list, list]:
        """Return (decisions, fills, cancels) accumulated so far. Does NOT
        clear the buffers (call again to get same data; let consumer
        decide ownership)."""
        return self.sim_decisions, self.sim_fills, self.sim_cancels

    def stats(self) -> dict:
        return {
            "active_orders": len(self.active_orders),
            "pending_cancels": len(self.pending_cancels),
            "inventory": self.inventory,
            "cash": self.cash,
            "n_decisions": len(self.sim_decisions),
            "n_fills": len(self.sim_fills),
            "n_cancels": len(self.sim_cancels),
            "uncalibrated": self.params.UNCALIBRATED,
        }
