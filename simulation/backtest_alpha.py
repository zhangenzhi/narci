"""backtest_alpha_model — replay an AlphaModel over historical cold tier
events and compute PnL / Sharpe / fill metrics via MakerSimBroker.

Pipeline:

    cold tier events (3 venues, time-merged)
        │
        ├─→ FeatureBuilder.update_event(...)           [for predictions]
        │
        └─→ MakerSimBroker.apply_market_event(...)     [for fills, CC only]

At every CC trade event, the strategy:
  1. Calls model.predict(fb) → alpha in bps
  2. Cancels any existing live quote (one-quote policy, v1)
  3. If |alpha| > threshold, places a maker quote on the side aligned
     with alpha sign, using `quote_strategy`:
       "join_back"     — alpha > 0 → BUY at best_bid (back of queue)
                         alpha < 0 → SELL at best_ask
       "improve_1_tick"— alpha > 0 → BUY at best_bid + tick
                         alpha < 0 → SELL at best_ask - tick
                         (falls back to join_back if would cross)
  4. Quote auto-cancels after quote_lifetime_sec if unfilled

After replay, PnL is computed from broker fills:
  realized PnL = Σ (fill_price × signed_size) plus mark-to-market on
  any leftover inventory at session end (priced at last observed mid).

This is a v1 reference backtest — single-symbol, single side at a time,
no inventory-aware sizing. Echo will eventually wrap a richer strategy.
"""
from __future__ import annotations

import heapq
import math
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from backtest.symbol_spec import SymbolSpec
from calibration.alpha_models import AlphaModel, load_alpha_model
from calibration.priors import get_priors
from features import FeatureBuilder

from .maker_broker import MakerSimBroker


COLD = Path("/lustre1/work/c30636/narci/replay_buffer/cold")

# Match research/segmented_replay.py
VENUE_SOURCES = [
    ("coincheck",   "spot",       "BTC_JPY", "cc"),
    ("binance_jp",  "spot",       "BTCJPY",  "bj"),
    ("binance",     "um_futures", "BTCUSDT", "um"),
]

QUOTE_STRATEGIES = ("join_back", "improve_1_tick")


def _compute_quote_price(
    side: str, best_bid: float, best_ask: float,
    strategy: str, tick_size: float,
) -> float:
    """Decide the limit price for a post-only maker quote.

    join_back     : post at the current best (BUY @ bid, SELL @ ask).
                    Joins the back of the existing queue.
    improve_1_tick: post one tick inside the spread (BUY @ bid+tick,
                    SELL @ ask-tick). Falls back to join_back when the
                    spread is already one tick wide and improving would
                    cross the book.

    nyx's 2026-05-13 sweep showed improve_1_tick lifts 05-04 OOS edge
    from +2.17 → +16.21 bps because front-of-queue dodges adverse
    selection. Production binding strategy for CC echo.
    """
    if strategy == "join_back":
        return best_bid if side == "BUY" else best_ask
    if strategy == "improve_1_tick":
        if side == "BUY":
            improved = best_bid + tick_size
            return improved if improved < best_ask else best_bid
        else:  # SELL
            improved = best_ask - tick_size
            return improved if improved > best_bid else best_ask
    raise ValueError(
        f"unknown quote_strategy {strategy!r}; expected one of {QUOTE_STRATEGIES}"
    )


def _stream_cold_file(path: Path, venue: str,
                       end_ts_ms: int | None = None):
    """Yield (ts_ms, neg_side, venue, side, price, qty) tuples.

    Reads numpy arrays once (faster than to_pylist) then iterates.
    end_ts_ms: if set, drop rows with ts >= end_ts_ms (saves work on
    the tail when max_hours truncation is requested)."""
    if end_ts_ms is not None:
        tbl = pq.read_table(str(path),
                            filters=[("timestamp", "<", end_ts_ms)])
    else:
        tbl = pq.read_table(str(path))
    ts = np.asarray(tbl.column("timestamp").to_numpy(zero_copy_only=False),
                    dtype=np.int64)
    sd = np.asarray(tbl.column("side").to_numpy(zero_copy_only=False),
                    dtype=np.int8)
    pr = np.asarray(tbl.column("price").to_numpy(zero_copy_only=False),
                    dtype=np.float64)
    qt = np.asarray(tbl.column("quantity").to_numpy(zero_copy_only=False),
                    dtype=np.float64)
    for i in range(ts.size):
        s = int(sd[i])
        yield (int(ts[i]), -s, venue, s, float(pr[i]), float(qt[i]))


def _multi_venue_first_tss(day: str) -> dict[str, int]:
    """Return {venue: first_event_ts} across VENUE_SOURCES daily files."""
    out: dict[str, int] = {}
    for exchange, market, sym, venue in VENUE_SOURCES:
        p = COLD / exchange / market / f"{sym}_RAW_{day}_DAILY.parquet"
        if not p.exists():
            p = COLD / f"{sym}_RAW_{day}_DAILY.parquet"
        if p.exists():
            tbl = pq.ParquetFile(str(p)).read_row_group(
                0, columns=["timestamp"])
            out[venue] = int(tbl.column("timestamp")[0].as_py())
    return out


def _multi_venue_anchor_ts(day: str) -> int | None:
    """Return the latest first-event timestamp across all VENUE_SOURCES
    daily files for `day`, or None if no daily files exist.

    This is the earliest time at which all venues are simultaneously
    alive — anchoring a max_hours slice here ensures the window contains
    multi-venue data rather than just one venue's pre-rollover tail.

    Day-skew can be ~hours when one venue's recorder restarted mid-day
    (the new shard's filename takes the next UTC day, so the resulting
    daily compaction starts well before midnight): on 2026-05-09 the CC
    daily file began 2026-05-08 15:06 while BJ started 2026-05-09 00:57
    — anchoring on CC's first_ts cut a max_hours=4 window entirely
    before BJ/UM had any data, producing 0 predictions."""
    first_tss = _multi_venue_first_tss(day)
    return max(first_tss.values()) if first_tss else None


def _stream_days(days: list[str], max_hours: float | None = None):
    iters = []
    end_ts_ms: int | None = None
    if max_hours is not None and days:
        first_tss = _multi_venue_first_tss(days[0])
        if first_tss:
            anchor_ts = max(first_tss.values())
            end_ts_ms = anchor_ts + int(max_hours * 3_600_000)
            # Warn when venues' first_ts span > 1h — usually means one
            # venue's recorder restarted mid-day and the corresponding
            # daily file is skewed. The slice may contain stretches of
            # one-venue-only events that produce all-NaN predictions
            # until the lagging venue catches up.
            span_ms = anchor_ts - min(first_tss.values())
            if span_ms > 3_600_000:
                import datetime as _dt
                _fmt = lambda t: _dt.datetime.fromtimestamp(
                    t/1000, tz=_dt.timezone.utc).strftime("%H:%M:%S")
                detail = ", ".join(f"{v}={_fmt(t)}" for v, t in
                                   sorted(first_tss.items()))
                print(f"[warn] day {days[0]}: venue first_ts span "
                      f"{span_ms/3_600_000:.1f}h (anchor={_fmt(anchor_ts)}). "
                      f"Predictions before the lagging venue starts will "
                      f"be NaN. Details: {detail}", flush=True)
    for d in days:
        for exchange, market, sym, venue in VENUE_SOURCES:
            path = COLD / exchange / market / f"{sym}_RAW_{d}_DAILY.parquet"
            if not path.exists():
                path = COLD / f"{sym}_RAW_{d}_DAILY.parquet"
            if path.exists():
                iters.append(_stream_cold_file(path, venue, end_ts_ms))
    yield from heapq.merge(*iters)


def _make_default_symbol_spec(symbol: str, exchange: str) -> SymbolSpec:
    """Reasonable defaults so callers don't have to pass it for the
    common (CC BTC_JPY / BJ BTCJPY) cases."""
    if (exchange, symbol) == ("coincheck", "BTC_JPY"):
        return SymbolSpec(symbol=symbol, tick_size=1.0,
                           lot_size=1e-8, min_notional=500.0)
    if (exchange, symbol) == ("coincheck", "ETH_JPY"):
        return SymbolSpec(symbol=symbol, tick_size=1.0,
                           lot_size=1e-7, min_notional=500.0)
    if (exchange, symbol) == ("binance_jp", "BTCJPY"):
        return SymbolSpec(symbol=symbol, tick_size=1.0,
                           lot_size=1e-5, min_notional=1000.0)
    raise ValueError(
        f"No default SymbolSpec for {exchange}/{symbol}; pass symbol_spec=...")


def backtest_alpha_model(
    model_path: str | Path,
    days: list[str],
    *,
    symbol: str = "BTC_JPY",
    exchange: str = "coincheck",
    symbol_spec: SymbolSpec | None = None,
    params=None,
    quote_size: float = 0.001,            # base-asset units
    alpha_threshold_bps: float = 1.0,
    quote_lifetime_sec: float = 5.0,
    lookback_seconds: int = 300,
    warmup_seconds: int = 300,            # skip quote placement during warmup
    max_hours: float | None = None,       # smoke knob; None = full day(s)
    quote_strategy: str = "join_back",    # "join_back" | "improve_1_tick"
    verbose: bool = True,
) -> dict:
    """Backtest an AlphaModel by streaming events through narci's
    simulator and computing PnL + Sharpe + fill metrics.

    :param model_path: dir containing manifest.json + weights.npz
    :param days: list of YYYYMMDD strings
    :param symbol: trading symbol (driven by broker; defaults to CC BTC_JPY)
    :param exchange: broker exchange; "coincheck" runs MakerSimBroker spot
    :param symbol_spec: tick/lot/min_notional. Inferred for common pairs.
    :param params: CalibrationParams. Falls back to priors.get_priors(...)
    :param quote_size: order qty per quote
    :param alpha_threshold_bps: |alpha| below this → no quote (stay flat)
    :param quote_lifetime_sec: cancel unfilled quote after this many sec
    :param lookback_seconds: FeatureBuilder lookback
    :param quote_strategy: "join_back" (default, conservative — joins
        the back of the queue at the current best) or "improve_1_tick"
        (posts one tick inside the spread; falls back to join_back when
        spread is already one tick wide). Per nyx 2026-05-13 sweep,
        improve_1_tick is the production-binding strategy for CC echo
        (+11.3x PnL vs join_back on 05-04).

    :return: dict with daily_pnl, sharpe, fills, decisions, cancels,
             plus aggregate stats.
    """
    if quote_strategy not in QUOTE_STRATEGIES:
        raise ValueError(
            f"quote_strategy must be one of {QUOTE_STRATEGIES}, got {quote_strategy!r}"
        )
    model: AlphaModel = load_alpha_model(Path(model_path))
    if symbol_spec is None:
        symbol_spec = _make_default_symbol_spec(symbol, exchange)
    if params is None:
        params = get_priors(exchange, symbol)

    fb = FeatureBuilder(lookback_seconds=lookback_seconds)
    broker = MakerSimBroker(params=params, symbol_spec=symbol_spec)

    # Strategy state. MakerSimBroker uses NANOSECONDS internally (its
    # cancel_latency_p50_ms is converted to ns for time-arith). Cold tier
    # timestamps are MILLISECONDS. We pass ts_ns to the broker, ts_ms to
    # FeatureBuilder.
    live_oid: str | None = None
    live_oid_expires_at_ns: int = 0
    quote_lifetime_ns = int(quote_lifetime_sec * 1_000_000_000)
    n_predictions = 0
    n_quotes_placed = 0
    n_predictions_during_warmup = 0
    warmup_end_ts_ms: int | None = None
    warmup_done = False

    t0 = time.time()
    n_events = 0
    last_print_evt = 0
    for ts_ms, _neg, venue, side, price, qty in _stream_days(days, max_hours):
        n_events += 1
        ts_ns = ts_ms * 1_000_000

        # First event: anchor warmup window. After warmup_seconds, the
        # FeatureBuilder rolling buffers (5s/30s/300s) have a full
        # history and predictions become trustworthy. Quotes during
        # warmup are skipped to avoid burning fee/inventory on bootstrap
        # noise.
        if warmup_end_ts_ms is None:
            warmup_end_ts_ms = ts_ms + warmup_seconds * 1000
        if not warmup_done and ts_ms >= warmup_end_ts_ms:
            warmup_done = True
            if verbose:
                print(f"  [warmup-done] ts={ts_ms}  predictions during warmup="
                      f"{n_predictions_during_warmup}", flush=True)

        # Feed FeatureBuilder always (predictions need cross-venue context)
        fb.update_event(venue, ts_ms, side, price, qty)

        # Broker simulates fills only for the CC venue (we trade there)
        if venue == "cc":
            broker.apply_market_event(ts_ns, side, price, qty)

        # Cancel expired quote (TTL lapse)
        if live_oid is not None and ts_ns >= live_oid_expires_at_ns:
            broker.cancel(ts_ns, live_oid, reason="QUOTE_TTL_EXPIRED")
            live_oid = None

        # On CC trade, predict + (re)quote
        if venue == "cc" and side == 2:
            try:
                alpha_bps = model.predict(fb)
            except Exception:
                continue
            if not math.isfinite(alpha_bps):
                continue
            n_predictions += 1

            # Skip quote placement during warmup (still record predictions
            # for diagnostic).
            if not warmup_done:
                n_predictions_during_warmup += 1
                continue

            # Decide
            if abs(alpha_bps) < alpha_threshold_bps:
                continue   # below threshold, no quote

            top1 = broker.book.get_top1()
            if top1 is None:
                continue
            best_bid = top1["best_bid"]
            best_ask = top1["best_ask"]

            # Cancel previous live quote before placing new (one-quote policy)
            if live_oid is not None:
                broker.cancel(ts_ns, live_oid, reason="STRATEGY_REPRICE")
                live_oid = None

            quote_side = "BUY" if alpha_bps > 0 else "SELL"
            quote_price = _compute_quote_price(
                quote_side, best_bid, best_ask,
                quote_strategy, symbol_spec.tick_size,
            )

            client_oid = f"bt_{ts_ms}_{n_quotes_placed}"
            placed = broker.place_limit(
                ts_ns, client_oid, quote_side, quote_price, quote_size,
                alpha_pred_bps=alpha_bps, alpha_source="alpha_model",
                reason=f"pred={alpha_bps:+.2f}bps",
            )
            if placed is not None:
                live_oid = placed
                live_oid_expires_at_ns = ts_ns + quote_lifetime_ns
                n_quotes_placed += 1

        if verbose and n_events - last_print_evt > 1_000_000:
            elapsed = time.time() - t0
            print(f"  streamed {n_events:>11,} events in {elapsed:>6.1f}s "
                  f"({n_events/elapsed:>9,.0f}/sec)  "
                  f"predictions={n_predictions:,} quotes={n_quotes_placed:,}",
                  flush=True)
            last_print_evt = n_events

    # Final cancel of any dangling order
    if live_oid is not None:
        broker.cancel(broker._cur_ts, live_oid, reason="SESSION_END")

    decisions, fills, cancels = broker.flush_results()
    bstats = broker.stats()

    # Compute PnL: realized cash + inventory at last mid
    last_top1 = broker.book.get_top1()
    last_mid = ((last_top1["best_bid"] + last_top1["best_ask"]) / 2
                if last_top1 else 0.0)
    realized_pnl = broker.cash + broker.inventory * last_mid
    edge_per_trade_bps = float("nan")
    if fills:
        # rough "edge per fill" = realized PnL / total notional / n_fills
        notional = sum(abs(f["fill_price"] * f["fill_qty"]) for f in fills)
        if notional > 0:
            edge_per_trade_bps = realized_pnl / notional * 1e4

    elapsed = time.time() - t0
    report = {
        "model_path": str(model_path),
        "days": days,
        "symbol": symbol,
        "exchange": exchange,
        "n_events": n_events,
        "n_predictions": n_predictions,
        "n_predictions_during_warmup": n_predictions_during_warmup,
        "n_quotes_placed": n_quotes_placed,
        "n_fills": len(fills),
        "n_cancels": len(cancels),
        "fill_rate": (len(fills) / n_quotes_placed) if n_quotes_placed else 0.0,
        "realized_pnl_quote": realized_pnl,           # in quote currency (JPY)
        "ending_inventory": broker.inventory,
        "ending_cash": broker.cash,
        "edge_per_fill_bps": edge_per_trade_bps,
        "broker_stats": bstats,
        "wall_sec": elapsed,
        "alpha_threshold_bps": alpha_threshold_bps,
        "quote_size": quote_size,
        "quote_lifetime_sec": quote_lifetime_sec,
        "quote_strategy": quote_strategy,
        # Raw events for downstream analysis
        "decisions": decisions,
        "fills": fills,
        "cancels": cancels,
    }

    if verbose:
        print()
        print(f"=== Backtest report ===")
        print(f"  model:        {model_path}")
        print(f"  days:         {days}")
        print(f"  events:       {n_events:,}  ({n_events/elapsed:,.0f}/sec)")
        print(f"  predictions:  {n_predictions:,}")
        print(f"  quotes:       {n_quotes_placed:,}")
        print(f"  fills:        {len(fills):,}  "
              f"(fill rate: {report['fill_rate']*100:.2f}%)")
        print(f"  realized PnL: {realized_pnl:>+12.4f} {symbol_spec.symbol[-3:]}")
        print(f"  inventory:    {broker.inventory:>+12.6f}")
        print(f"  edge/fill:    {edge_per_trade_bps:>+8.3f} bps")
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="model dir path")
    ap.add_argument("--days", default="20260423",
                    help="comma-separated YYYYMMDD")
    ap.add_argument("--threshold-bps", type=float, default=1.0)
    ap.add_argument("--quote-size", type=float, default=0.001)
    ap.add_argument("--quote-lifetime", type=float, default=5.0)
    ap.add_argument("--hours", type=float, default=None,
                    help="only replay first N hours (smoke)")
    ap.add_argument("--warmup-sec", type=int, default=300,
                    help="skip quoting during first N seconds (FB bootstrap)")
    ap.add_argument("--quote-strategy", choices=QUOTE_STRATEGIES,
                    default="join_back",
                    help="maker price placement: 'join_back' joins the "
                         "current best (back of queue); 'improve_1_tick' "
                         "posts one tick inside the spread for front-of-"
                         "queue priority (per nyx 2026-05-13, +11.3x PnL).")
    args = ap.parse_args()
    days = args.days.split(",")
    backtest_alpha_model(
        model_path=args.model,
        days=days,
        alpha_threshold_bps=args.threshold_bps,
        quote_size=args.quote_size,
        quote_lifetime_sec=args.quote_lifetime,
        warmup_seconds=args.warmup_sec,
        max_hours=args.hours,
        quote_strategy=args.quote_strategy,
    )
