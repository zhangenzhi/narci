"""Real-time feature pipeline.

Single source of truth for all production features. Both nyx (offline
training) and echo (live inference) import this module — guarantees no
drift between training and inference.

Architecture:

  * Per-venue L2 reconstructors (CC, BJ, UM)
  * Per-venue trade-flow accumulators with multi-window decay
  * Cross-venue derived signals (basis, USDT/JPY)
  * Snapshot accessor — for OLS / LGB / GBDT
  * Sequence accessor — for GRU / LSTM / Transformer

Feature versioning: a `FEATURES_VERSION` constant + `FEATURE_NAMES` list.
Bump the version any time a feature is added/removed; AlphaModel
manifests record what version they trained against, and inference
refuses to run on a version mismatch.

Status: v1 — covers the 23 features used in the OLS research baseline
(test R² 12.5% on trade-target). L2-tier features (Tier 2: imbalance /
microprice) added in §3.3 of this module's docstring.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from data.l2_reconstruct import L2Reconstructor


# Bumped v1 → v2 on 2026-05-06: tier 2 placeholder values became real
# (cc_l2_top5_imb, cc_l2_imb_top1_5s, cc_l2_imb_top5_5s, cc_micro_dev_5s,
# bj_l2_top5_imb), and _PriceWindow / _TradeWindow stopped silently
# truncating UM 30s+ windows (maxlen=400 → time-bounded).
#
# Bumped v2 → v3 on 2026-05-07: removed two algebraically redundant
# columns flagged by nyx — basis_um_bps (= basis_bj_bps because we used
# bj_mid/um_mid as the USDT/JPY proxy, which is a tautology) and
# basis_bj_5s (= basis_bj_bps placeholder). 33 → 31 feature names. Will
# reintroduce basis_um_bps when an independent USDT/JPY ticker source is
# wired into the recorder layer.
#
# Bumped v3 → v4 on 2026-05-10: added UM signed trade flow features at
# four horizons (50ms / 100ms / 500ms / 5s). nyx 2026-05-10 lead-lag
# study on 10 days showed signed flow gives ~50ms more lead than mid
# (peak +25ms / 50% cliff +200ms vs +150ms for mid). Existing
# um_imb_1s / um_imb_30s_norm are imbalance ratios in [-1, +1];
# um_vol_5s is mid volatility (abs_log_return_sum); um_n_5s is count.
# None of these expose the raw signed quantity flow. flow() method on
# _TradeWindow already computes Σ(buys - sells) — these features just
# expose it at multiple horizons. 31 → 35 feature names.
#
# Models trained against vN must retrain against vN+1 — manifest required-
# version pin (narci_features_version_required) is enforced by
# load_alpha_model.
FEATURES_VERSION = "v5"


# ------------------------------------------------------------------ #
# Feature taxonomy — DO NOT change these names without bumping
# FEATURES_VERSION; downstream models pin to a specific list.
# ------------------------------------------------------------------ #


# Baseline (tested in OLS research, R² 12% on trade target)
BASELINE_FEATURES = [
    # UM trade-based
    "r_um", "r_um_2s", "r_um_5s", "r_um_10s",
    "um_imb_1s", "um_imb_30s_norm", "um_n_5s", "um_vol_5s",
    # UM signed trade flow (v4 — independent lead signal vs mid; ~50ms
    # extra horizon per nyx 2026-05-10 lead-lag study, peak +25ms /
    # 50% cliff +200ms). Σ(taker_buy_qty - taker_sell_qty) over window.
    "um_flow_50ms", "um_flow_100ms", "um_flow_500ms", "um_flow_5s",
    # BJ trade-based
    "r_bj", "bj_imb_1s", "bj_flow_5s",
    # CC own
    "r_cc_lag1", "r_cc_lag2", "cc_imb_1s",
    # Basis
    #   basis_bj_bps = log(cc_mid / bj_mid) * 1e4
    #     CC ↔ BJ cross-venue, both JPY-denominated.
    #   um_x_basis = r_um * basis_bj_bps (interaction).
    #   basis_um_bps = log(um_mid / bs_mid) * 1e4  (v5 2026-05-14)
    #     UM perp ↔ BS (Binance global spot) USDT-USDT basis. The
    #     classic crypto perp-spot premium signal (positive = perp
    #     premium = contango, generally bullish-leaning carry).
    #     STRICT: NaN whenever the BS book hasn't bootstrapped — most
    #     pre-2026-05-13 days are Vision-backfilled trade-only (Vision
    #     bookTicker not available — see deploy/donor/NARCI_DONOR_INTERFACE.md),
    #     so this is only finite on 2026-05-13+ live recording.
    #   basis_um_bps_trade_proxy = log(um_mid / bs_last_trade_price) * 1e4
    #     (v5 2026-05-14, addendum) Lossy fallback that lets the basis
    #     signal compute even on trade-only days. bs_last_trade alternates
    #     between bid and ask depending on which side the taker hit, so
    #     this has ~half-spread noise vs the strict mid version. nyx
    #     can use both: strict for clean signal where available, proxy
    #     when only aggTrades-backfilled days are in the training pool.
    "basis_bj_bps", "um_x_basis", "basis_um_bps", "basis_um_bps_trade_proxy",
]

# Tier 1 (hour-of-day + CC own flow)
TIER1_FEATURES = [
    "hour_sin", "hour_cos",
    "cc_flow_5s", "cc_flow_30s", "cc_imb_30s_norm",
]

# Tier 2 (CC L2 imbalance / microprice — gated by L2 reconstruction)
TIER2_FEATURES = [
    "cc_l2_top1_imb", "cc_l2_top5_imb",
    "cc_l2_imb_top1_5s", "cc_l2_imb_top5_5s",
    "cc_l2_micro_dev_bps", "cc_micro_dev_5s",
    "cc_l2_spread_bps",
    "bj_l2_top1_imb", "bj_l2_top5_imb", "l2_imb_diff",
]

FEATURE_NAMES = BASELINE_FEATURES + TIER1_FEATURES + TIER2_FEATURES


# ------------------------------------------------------------------ #
# Internal: per-venue trade flow accumulator with sliding windows
# ------------------------------------------------------------------ #


@dataclass
class _TradeWindow:
    """Tracks signed trade flow / counts / vol over a time-bounded history.

    No maxlen — bound by trim() on time, not count. UM peaks at >50 trades/sec
    so a 300s window can hold ~15k entries; a count-based maxlen would
    silently corrupt 30s+ window features."""
    history: deque = field(default_factory=deque)
    # Each entry: (ts_ms, signed_qty, abs_qty, side_sign)

    def push(self, ts_ms: int, signed_qty: float):
        # narci convention: side==2, qty > 0 = buyer maker (taker SELL),
        # qty < 0 = seller maker (taker BUY).
        # imbalance metric: + means aggressive buy pressure
        agg_buy = max(-signed_qty, 0.0)
        agg_sell = max(signed_qty, 0.0)
        self.history.append((ts_ms, signed_qty, agg_buy, agg_sell))

    def trim(self, now_ms: int, lookback_ms: int):
        cutoff = now_ms - lookback_ms
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

    def imbalance(self, now_ms: int, window_ms: int) -> float:
        """(buys - sells) / (buys + sells), normalized to [-1, 1]."""
        buys = sells = 0.0
        cutoff = now_ms - window_ms
        for ts, _, ab, asl in self.history:
            if ts < cutoff:
                continue
            buys += ab
            sells += asl
        total = buys + sells
        return (buys - sells) / total if total > 0 else 0.0

    def flow(self, now_ms: int, window_ms: int) -> float:
        """Signed net flow (buys - sells) raw."""
        net = 0.0
        cutoff = now_ms - window_ms
        for ts, _, ab, asl in self.history:
            if ts >= cutoff:
                net += ab - asl
        return net

    def count(self, now_ms: int, window_ms: int) -> int:
        cutoff = now_ms - window_ms
        return sum(1 for ts, _, _, _ in self.history if ts >= cutoff)


# ------------------------------------------------------------------ #
# Internal: rolling time-stamped scalar buffer (for r_um_*s windows)
# ------------------------------------------------------------------ #


@dataclass
class _PriceWindow:
    """Tracks last-trade price by ts; gives multi-window log returns.

    No maxlen — bound by trim() on time."""
    history: deque = field(default_factory=deque)
    # Each entry: (ts_ms, price)

    def push(self, ts_ms: int, price: float):
        self.history.append((ts_ms, price))

    def trim(self, now_ms: int, lookback_ms: int):
        cutoff = now_ms - lookback_ms
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

    def latest(self) -> Optional[tuple[int, float]]:
        return self.history[-1] if self.history else None

    def at_or_before(self, target_ms: int) -> Optional[float]:
        """Find price at or before target_ms; returns None if none."""
        last_p = None
        for ts, p in self.history:
            if ts <= target_ms:
                last_p = p
            else:
                break
        return last_p

    def log_return(self, now_ms: int, window_ms: int) -> float:
        """log(price[now] / price[now - window]) — NaN if either missing."""
        if not self.history:
            return float("nan")
        p_now = self.history[-1][1]
        p_then = self.at_or_before(now_ms - window_ms)
        if p_then is None or p_then <= 0 or p_now <= 0:
            return float("nan")
        return math.log(p_now / p_then)

    def abs_log_return_sum(self, now_ms: int, window_ms: int) -> float:
        """Sum |log_return| within window (volatility proxy)."""
        cutoff = now_ms - window_ms
        prev = None
        s = 0.0
        for ts, p in self.history:
            if ts < cutoff:
                prev = p; continue
            if prev is not None and prev > 0 and p > 0:
                s += abs(math.log(p / prev))
            prev = p
        return s


# ------------------------------------------------------------------ #
# Main builder
# ------------------------------------------------------------------ #


@dataclass
class _VenueState:
    """Per-venue rolling state: book + trade window + price window +
    book-derived metric history (top1/top5 imbalance + microprice
    deviation) for 5s rolling features."""
    book: L2Reconstructor = field(default_factory=lambda: L2Reconstructor(depth_limit=10))
    trades: _TradeWindow = field(default_factory=_TradeWindow)
    prices: _PriceWindow = field(default_factory=_PriceWindow)

    # Time-bounded history of book-derived metrics. Each entry:
    #   (ts_ms, top1_imb, top5_imb, micro_dev_bps)
    # Updated on every event that touches the book (sides 0,1,3,4).
    book_metric_history: deque = field(default_factory=deque)

    def __post_init__(self):
        self.book.reset()

    @classmethod
    def with_staleness(cls, book_staleness_seconds: float,
                       prune_snapshot_dust: bool = False,
                       incremental_ready_threshold: int = 5):
        """Construct a venue state whose L2Reconstructor accepts stale
        book reads up to N seconds (P1 fix 2026-05-08) and/or strips
        cross-side dust at snapshot batch close (P1-C fix 2026-05-08)
        and/or bootstraps is_ready from incrementals (P3 fix 2026-05-09)."""
        v = cls(book=L2Reconstructor(
            depth_limit=10,
            book_staleness_seconds=book_staleness_seconds,
            prune_snapshot_dust=prune_snapshot_dust,
            incremental_ready_threshold=incremental_ready_threshold,
        ))
        return v


class FeatureBuilder:
    """Real-time feature pipeline.

    Maintains per-venue rolling state. Caller updates by feeding events
    (`update_event`) and queries by `get_features()` or
    `get_feature_sequence()`.

    Convention:
      - Venue keys: "cc" (Coincheck BTC_JPY), "bj" (Binance JP BTCJPY),
        "um" (Binance UM BTCUSDT). Add more as needed.
      - All timestamps in ms.

    Multi-symbol support is planned but v1 is single-symbol per
    FeatureBuilder instance (instantiate one per trade pair).
    """

    def __init__(self, lookback_seconds: int = 300,
                 metric_sample_interval_ms: int = 500,
                 book_staleness_seconds: float = 0.0,
                 prune_snapshot_dust: bool = False,
                 incremental_ready_threshold: int = 5):
        """
        :param book_staleness_seconds: P1 fix 2026-05-08. When > 0, each
            venue's L2Reconstructor will fall back to its last valid
            top1/state snapshot during transient book invalidation
            (cross/dust/empty-side) up to this many seconds. Mitigates
            BJ "short-run NaN" pattern (143 runs/day, median 57 events
            ≈ a few seconds; staleness=10 catches all but the worst).
            Stale reads are flagged with `stale=True` in the returned
            dict; downstream FB features still treat them as valid
            numbers. Default 0 = strict (NaN on any invalid book).
        :param prune_snapshot_dust: P1-C fix 2026-05-08. The narci
            recorder writes its in-memory book as the snapshot batch,
            which carries dust whenever WS delete events were dropped.
            When True, strip cross-side dust (bids >= validated best_ask
            / asks <= validated best_bid) at the close of every snapshot
            batch. Composes cleanly with book_staleness_seconds (prune
            reduces failure rate; staleness catches the residual). Default
            False preserves old behavior.
        """
        self._lookback_ms = lookback_seconds * 1000
        # B0' fix (2026-05-06): throttle _record_book_metric per venue.
        # UM has 20k-200k levels (recorder doesn't delete stale levels) so
        # heapq.nlargest scan is still expensive; throttle bounds total
        # cost. 500ms gives 10 samples per 5s rolling window — adequate
        # for averaging and 10× fewer than 100ms.
        self._metric_sample_interval_ms = metric_sample_interval_ms
        self._book_staleness_seconds = book_staleness_seconds
        self._prune_snapshot_dust = prune_snapshot_dust
        self._incremental_ready_threshold = incremental_ready_threshold
        self._venues: dict[str, _VenueState] = {
            "cc": _VenueState.with_staleness(book_staleness_seconds, prune_snapshot_dust,
                                              incremental_ready_threshold),
            "bj": _VenueState.with_staleness(book_staleness_seconds, prune_snapshot_dust,
                                              incremental_ready_threshold),
            "um": _VenueState.with_staleness(book_staleness_seconds, prune_snapshot_dust,
                                              incremental_ready_threshold),
            # v5 2026-05-14: Binance global spot (USDT pairs) — feeds the
            # reintroduced basis_um_bps = log(um_mid / bs_mid) * 1e4 perp-
            # spot basis. BS recorder lives on AWS-SG, recording since
            # 2026-05-13 06:42 UTC. Historical L2 data 2026-03-06 → 04-17
            # is in cold tier; 04-17 → 05-09 is trade-only via Vision
            # backfill (basis NaN those days — only depth-bearing windows
            # produce finite basis).
            "bs": _VenueState.with_staleness(book_staleness_seconds, prune_snapshot_dust,
                                              incremental_ready_threshold),
        }
        self._last_metric_ts: dict[str, int] = {"cc": 0, "bj": 0, "um": 0, "bs": 0}
        # Snapshot of last get_features for sequence builder.
        self._snapshot_history: deque = deque(maxlen=400)
        # Latest seen ts (max across all venue events)
        self._last_ts_ms: int = 0

    # ---------------------------------------------------------------- #
    # Event ingestion
    # ---------------------------------------------------------------- #

    def update_event(self, venue: str, ts_ms: int, side: int,
                     price: float, qty: float) -> None:
        """Feed a single L2 / trade event for `venue`."""
        v = self._venues.get(venue)
        if v is None:
            return  # silent: unknown venue (don't crash on caller bugs)
        ts_ms = int(ts_ms)
        v.book.apply_event(ts_ms, side, price, qty)
        if side == 2:  # trade
            v.trades.push(ts_ms, qty)
            v.prices.push(ts_ms, price)
            v.trades.trim(ts_ms, self._lookback_ms)
            v.prices.trim(ts_ms, self._lookback_ms)
        elif side in (0, 1, 3, 4):
            # book changed → maybe snapshot top-5 metrics for 5s rolling
            # features. Throttled per-venue to avoid O(N log N) per event
            # on busy books (UM 1000+ levels × 1k events/sec = catastrophic).
            last = self._last_metric_ts.get(venue, 0)
            if ts_ms >= last + self._metric_sample_interval_ms:
                # P2 fix 2026-05-09: gate on book-has-both-sides BEFORE
                # touching the throttle. Previous logic ("only mark on
                # success") let snapshot batches — which share a single
                # ts across thousands of side=3/4 events — bypass throttle
                # entirely: each event found ts ≥ last+500ms, called
                # _record_book_metric, partial book returned False, last
                # stayed at 0, next event repeated. Profile on a 3-min
                # UM slice: 67,651 _record_book_metric calls vs ~720
                # expected (94x over) — 12x total wall reduction.
                #
                # The refined check is: only attempt + lock throttle if
                # both sides have at least one level. Pre-bootstrap (one
                # side cleared, other not yet refilled) is skipped without
                # consuming throttle so the FIRST sample after batch
                # completion still fires.
                book = v.book
                # P2.1 fix 2026-05-13: also gate on snapshot batch being
                # closed. Pre-fix, the first side=3 event in a fresh
                # snapshot batch fired _record_book_metric with only one
                # bid level applied (heapq nlargest yields the dust deep
                # bid as "best", micropx -> -2938 bps). The bad entry sat
                # in book_metric_history for 5s, poisoning cc_micro_dev_5s
                # to -1469 on the first ~2 trade samples after segment
                # start. _snapshot_batch_ts is set non-None throughout
                # side=3/4 application and cleared by the first 0/1/2
                # event after (which also runs prune_dust). Skip-without-
                # consuming-throttle is intentional — we want the very
                # next non-batch event to be the first sample.
                if (book.is_ready and book.bids and book.asks
                        and book._snapshot_batch_ts is None):
                    self._last_metric_ts[venue] = ts_ms
                    self._record_book_metric(v, ts_ms)
        if ts_ms > self._last_ts_ms:
            self._last_ts_ms = ts_ms

    def _rolling_book_metric(self, v: _VenueState, ts_ms: int,
                             window_ms: int, idx: int) -> float:
        """Mean of v.book_metric_history[idx] over [ts_ms - window_ms, ts_ms].
        idx: 1 = top1_imb, 2 = top5_imb, 3 = micro_dev_bps."""
        cutoff = ts_ms - window_ms
        s = 0.0
        n = 0
        for entry in v.book_metric_history:
            if entry[0] < cutoff:
                continue
            val = entry[idx]
            if val == val:  # not NaN
                s += val
                n += 1
        return s / n if n > 0 else float("nan")

    def _record_book_metric(self, v: _VenueState, ts_ms: int) -> bool:
        """Sample top-1 and top-5 imbalance + microprice deviation from
        current book state and push to history. Returns True on success
        (book ready, sample appended) or False if book not ready."""
        state = v.book.get_state(top_n=5)
        if state is None:
            return False
        top1_imb = state["imbalance_top1"]
        top5_imb = state["imbalance_top5"]
        mid = state["mid_price"]
        if mid > 0 and state["microprice"] > 0:
            micro_dev_bps = (state["microprice"] - mid) / mid * 10000.0
        else:
            micro_dev_bps = float("nan")
        v.book_metric_history.append((ts_ms, top1_imb, top5_imb, micro_dev_bps))
        # trim
        cutoff = ts_ms - self._lookback_ms
        while v.book_metric_history and v.book_metric_history[0][0] < cutoff:
            v.book_metric_history.popleft()
        return True

    # ---------------------------------------------------------------- #
    # Snapshot accessor
    # ---------------------------------------------------------------- #

    def get_features(self, ts_ms: Optional[int] = None) -> dict[str, float]:
        """Compute all features at the given timestamp (default: latest event).

        Returns dict[feature_name → value]. Value may be NaN if the
        underlying data is stale or unavailable. Caller decides what to
        do with NaN (typical: skip prediction, fall back to naive)."""
        if ts_ms is None:
            ts_ms = self._last_ts_ms
        nan = float("nan")

        cc = self._venues["cc"]
        bj = self._venues["bj"]
        um = self._venues["um"]
        bs = self._venues["bs"]   # v5: Binance global spot (USDT pairs)

        # Helper: short-name latest log returns
        r_um = um.prices.log_return(ts_ms, 1000)
        r_bj = bj.prices.log_return(ts_ms, 1000)
        r_cc = cc.prices.log_return(ts_ms, 1000)

        feats = {}

        # --- UM trade-based ---
        feats["r_um"]      = r_um
        feats["r_um_2s"]   = um.prices.log_return(ts_ms, 2000)
        feats["r_um_5s"]   = um.prices.log_return(ts_ms, 5000)
        feats["r_um_10s"]  = um.prices.log_return(ts_ms, 10_000)
        feats["um_imb_1s"]      = um.trades.imbalance(ts_ms, 1000)
        feats["um_imb_30s_norm"] = um.trades.imbalance(ts_ms, 30_000)
        feats["um_n_5s"]   = float(um.trades.count(ts_ms, 5000))
        feats["um_vol_5s"] = um.prices.abs_log_return_sum(ts_ms, 5000)
        # v4 signed trade flow at multiple horizons. Σ(taker_buy_qty -
        # taker_sell_qty) raw — positive = aggressive buy pressure.
        # Per nyx 2026-05-10 lead-lag study, signed flow gives ~50ms
        # more lead than mid; 50ms/100ms/500ms windows directly target
        # the +25ms peak / +200ms cliff zone identified in 10-day pool.
        feats["um_flow_50ms"]  = um.trades.flow(ts_ms, 50)
        feats["um_flow_100ms"] = um.trades.flow(ts_ms, 100)
        feats["um_flow_500ms"] = um.trades.flow(ts_ms, 500)
        feats["um_flow_5s"]    = um.trades.flow(ts_ms, 5000)

        # --- BJ trade-based ---
        feats["r_bj"] = r_bj
        feats["bj_imb_1s"]  = bj.trades.imbalance(ts_ms, 1000)
        feats["bj_flow_5s"] = bj.trades.flow(ts_ms, 5000)

        # --- CC own ---
        # r_cc_lag1: log_return at *previous* 1s vs 2s ago
        feats["r_cc_lag1"] = cc.prices.log_return(ts_ms - 1000, 1000)
        feats["r_cc_lag2"] = cc.prices.log_return(ts_ms - 2000, 1000)
        feats["cc_imb_1s"] = cc.trades.imbalance(ts_ms, 1000)

        # --- Basis (cross-venue) ---
        #   basis_bj_bps = log(CC_mid / BJ_mid) * 1e4   (cross-exchange,
        #     both JPY-denominated).
        #   um_x_basis = r_um × basis_bj_bps interaction.
        #   basis_um_bps = log(UM_mid / BS_mid) * 1e4   (v5 2026-05-14)
        #     UM perp vs Binance global spot USDT-USDT basis (classic
        #     crypto perp-spot premium). NaN when BS book isn't ready,
        #     which is the common case on pre-2026-05-13 days that were
        #     Vision-backfilled trade-only (no depth) and on 04-17 →
        #     05-12. Finite on 03-06 → 04-16 (full L2 historical) and
        #     2026-05-13+ (live recording).
        cc_top = cc.book.get_top1()
        bj_top = bj.book.get_top1()
        um_top = um.book.get_top1()
        bs_top = bs.book.get_top1()
        cc_mid = cc_top["mid_price"] if cc_top else nan
        bj_mid = bj_top["mid_price"] if bj_top else nan
        um_mid = um_top["mid_price"] if um_top else nan
        bs_mid = bs_top["mid_price"] if bs_top else nan

        if cc_mid > 0 and bj_mid > 0:
            feats["basis_bj_bps"] = math.log(cc_mid / bj_mid) * 10000.0
        else:
            feats["basis_bj_bps"] = nan

        feats["um_x_basis"] = (
            feats["r_um"] * feats["basis_bj_bps"]
            if not math.isnan(feats["r_um"]) and not math.isnan(feats["basis_bj_bps"])
            else nan
        )

        if um_mid > 0 and bs_mid > 0:
            feats["basis_um_bps"] = math.log(um_mid / bs_mid) * 10000.0
        else:
            feats["basis_um_bps"] = nan

        # Trade-proxy: when bs.book is absent (Vision-backfilled trade-
        # only days), substitute bs last_trade_price for bs_mid. UM mid
        # is always reliable (live recorder has full L2), so the bias
        # is only on the BS side — at most half-spread on BTCUSDT spot
        # (~0.5 bps), small compared to typical basis magnitudes.
        bs_last = bs.prices.latest()
        bs_trade_px = bs_last[1] if bs_last else nan
        if um_mid > 0 and bs_trade_px > 0:
            feats["basis_um_bps_trade_proxy"] = (
                math.log(um_mid / bs_trade_px) * 10000.0
            )
        else:
            feats["basis_um_bps_trade_proxy"] = nan

        # --- Tier 1: time-of-day (UTC) + CC flow ---
        # ts_ms / 1000 / 3600 mod 24
        h_utc = (ts_ms // 1000 % 86400) / 3600.0
        feats["hour_sin"] = math.sin(2 * math.pi * h_utc / 24.0)
        feats["hour_cos"] = math.cos(2 * math.pi * h_utc / 24.0)
        feats["cc_flow_5s"]  = cc.trades.flow(ts_ms, 5000)
        feats["cc_flow_30s"] = cc.trades.flow(ts_ms, 30_000)
        feats["cc_imb_30s_norm"] = cc.trades.imbalance(ts_ms, 30_000)

        # --- Tier 2: L2 imbalance + microprice (require book to be ready) ---
        # CC top1 — fast path
        feats["cc_l2_top1_imb"] = cc_top["imbalance_top1"] if cc_top else nan
        feats["cc_l2_spread_bps"] = cc_top["spread_bps"] if cc_top else nan
        feats["cc_l2_micro_dev_bps"] = (
            (cc_top["microprice"] - cc_top["mid_price"]) / cc_top["mid_price"] * 10000.0
            if cc_top and cc_top["mid_price"] > 0
            else nan
        )
        # CC top5 — needs full state
        cc_top5 = cc.book.get_state(top_n=5)
        feats["cc_l2_top5_imb"] = cc_top5["imbalance_top5"] if cc_top5 else nan
        # 5s rolling: average top1/top5 imbalance + micro_dev over last 5s
        feats["cc_l2_imb_top1_5s"] = self._rolling_book_metric(cc, ts_ms, 5000, idx=1)
        feats["cc_l2_imb_top5_5s"] = self._rolling_book_metric(cc, ts_ms, 5000, idx=2)
        feats["cc_micro_dev_5s"]   = self._rolling_book_metric(cc, ts_ms, 5000, idx=3)

        # BJ top1 + top5
        feats["bj_l2_top1_imb"] = bj_top["imbalance_top1"] if bj_top else nan
        bj_top5 = bj.book.get_state(top_n=5)
        feats["bj_l2_top5_imb"] = bj_top5["imbalance_top5"] if bj_top5 else nan

        # cross-venue diff
        feats["l2_imb_diff"] = (
            feats["cc_l2_top1_imb"] - feats["bj_l2_top1_imb"]
            if not math.isnan(feats["cc_l2_top1_imb"]) and not math.isnan(feats["bj_l2_top1_imb"])
            else nan
        )

        # Save snapshot for sequence access
        self._snapshot_history.append((ts_ms, feats))
        return feats

    # ---------------------------------------------------------------- #
    # Sequence accessor (for GRU / Transformer)
    # ---------------------------------------------------------------- #

    def get_feature_sequence(self, window_sec: int, step_sec: int) -> Optional[np.ndarray]:
        """Return shape (T, D) array of feature history.

        Walks back `window_sec` from latest, sampling at `step_sec`.
        Each row is FEATURE_NAMES order. Returns None if insufficient
        history."""
        if not self._snapshot_history:
            return None
        latest_ts = self._snapshot_history[-1][0]
        cutoff_ts = latest_ts - window_sec * 1000
        # Bin snapshots by step_sec. v1 simple: take latest snapshot per bin.
        step_ms = step_sec * 1000
        n_bins = window_sec // step_sec
        rows = []
        for i in range(n_bins, 0, -1):
            target_ts = latest_ts - (i - 1) * step_ms
            # find latest snapshot at or before target_ts
            best = None
            for ts, feats in self._snapshot_history:
                if ts <= target_ts:
                    best = (ts, feats)
                else:
                    break
            if best is None:
                return None  # insufficient history
            row = [best[1].get(name, float("nan")) for name in FEATURE_NAMES]
            rows.append(row)
        return np.array(rows, dtype=np.float64)

    # ---------------------------------------------------------------- #
    # Health
    # ---------------------------------------------------------------- #

    def is_healthy(self, max_stale_sec: float = 2.0,
                   require_venues: tuple[str, ...] = ("cc", "bj", "um")) -> bool:
        """Returns True if all required venues have data within max_stale_sec."""
        cutoff = self._last_ts_ms - max_stale_sec * 1000
        for v in require_venues:
            state = self._venues.get(v)
            if state is None:
                return False
            latest = state.prices.latest()
            if latest is None or latest[0] < cutoff:
                return False
        return True

    def stats(self) -> dict:
        return {
            "features_version": FEATURES_VERSION,
            "feature_count": len(FEATURE_NAMES),
            "lookback_ms": self._lookback_ms,
            "venues_state": {
                v: {
                    "book_ready": s.book.is_ready,
                    "trade_history_size": len(s.trades.history),
                    "price_history_size": len(s.prices.history),
                }
                for v, s in self._venues.items()
            },
            "snapshot_history_size": len(self._snapshot_history),
            "last_ts_ms": self._last_ts_ms,
        }
