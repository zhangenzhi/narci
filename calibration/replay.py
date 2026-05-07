"""Calibration replay — drive narci's MakerSimBroker through an echo session
and compare simulator output against echo's real fills/cancels.

Entry point: `calibrate_session(echo_dir, narci_recorder_dir=None)`.

Returns a `CalibrationReport` which:

  * Counts fill match (sim vs real, with tolerance window)
  * Measures adverse selection from raw L2 stream (real ground truth)
  * Compares cancel latency distribution (sim const vs real measured)
  * Suggests v1.x parameter updates for `CalibrationParams`
  * Issues a verdict: HEALTHY / ACCEPT_WITH_TUNING / UNHEALTHY

See `docs/CALIBRATION_PROTOCOL.md` for the contract these reports satisfy.
"""

from __future__ import annotations

import dataclasses as dc
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pyarrow.parquet as pq

from .priors import CalibrationParams, get_priors
from .schema import SCHEMA_VERSION

# Imports below try `narci.X` first (PYTHONPATH=/lustre1/work/c30636/) and
# fall back to bare `X` (PYTHONPATH=/lustre1/work/c30636/narci/). This lets
# both echo and narci-internal callers import this module without
# environment hacks.
try:
    from narci.backtest.symbol_spec import SymbolSpec  # noqa: F401
    _NARCI_PREFIX = True
except ImportError:
    from backtest.symbol_spec import SymbolSpec        # noqa: F401
    _NARCI_PREFIX = False

log = logging.getLogger("narci.calibration.replay")


# ------------------------------------------------------------------ #
# Report dataclass
# ------------------------------------------------------------------ #


@dataclass
class FillMatchMetrics:
    real_count: int
    sim_count: int
    matched_count: int
    match_rate: float                 # matched / real_count
    timing_offset_p50_ms: float       # for matched, |sim_ts - real_ts| median
    timing_offset_p95_ms: float


@dataclass
class AdverseMetrics:
    real_mean_500ms_bps: float
    real_mean_1s_bps: float
    real_mean_5s_bps: float
    real_mean_30s_bps: float
    sim_mean_1s_bps: float            # what sim would expect
    delta_1s_bps: float               # real - sim


@dataclass
class CancelLatencyMetrics:
    real_p50_ms: float
    real_p95_ms: float
    real_p99_ms: float
    sim_p50_ms: float                 # const from priors
    delta_p50_ms: float


@dataclass
class CalibrationReport:
    session_id: str
    schema_version: str
    exchange: str
    symbol: str
    sample_count: int
    fill_metrics: FillMatchMetrics
    adverse_metrics: AdverseMetrics
    cancel_metrics: CancelLatencyMetrics
    queue_scaling_suggestion: float
    verdict: str                      # "HEALTHY" | "ACCEPT_WITH_TUNING" | "UNHEALTHY"
    issues: list[str] = field(default_factory=list)
    suggested_params: dict = field(default_factory=dict)


# ------------------------------------------------------------------ #
# Loaders
# ------------------------------------------------------------------ #


def _load_meta(echo_dir: Path) -> dict:
    p = echo_dir / "meta.json"
    if not p.exists():
        raise FileNotFoundError(f"missing {p}")
    with open(p) as f:
        return json.load(f)


def _load_event_dir(echo_dir: Path, kind: str) -> list[dict]:
    """Read all parquet shards in echo_dir/{kind}/, return list[dict] sorted
    by ts_ns (or ts_ns_request for cancels)."""
    d = echo_dir / kind
    if not d.exists():
        return []
    rows: list[dict] = []
    for p in sorted(d.glob("*.parquet")):
        tbl = pq.read_table(p)
        rows.extend(tbl.to_pylist())
    ts_key = "ts_ns_request" if kind == "cancels" else "ts_ns"
    rows.sort(key=lambda r: r.get(ts_key, 0))
    return rows


def _load_raw_l2(raw_dir: Path) -> list[tuple[int, int, float, float]]:
    """Read narci-format raw L2 parquet shards. Returns list of
    (ts_ms, side, price, qty), sorted by (ts_ms, -side) so snapshots
    (4 / 3) ingest before diffs (1 / 0) at the same timestamp."""
    if not raw_dir.exists():
        raise FileNotFoundError(f"raw L2 dir not found: {raw_dir}")
    events = []
    for p in sorted(raw_dir.glob("*.parquet")):
        tbl = pq.read_table(p)
        ts = tbl.column("timestamp").to_pylist()
        sd = tbl.column("side").to_pylist()
        pr = tbl.column("price").to_pylist()
        qt = tbl.column("quantity").to_pylist()
        for i in range(len(ts)):
            events.append((int(ts[i]), int(sd[i]), float(pr[i]), float(qt[i])))
    events.sort(key=lambda e: (e[0], -e[1]))
    return events


# ------------------------------------------------------------------ #
# Replay
# ------------------------------------------------------------------ #


def _replay(
    raw_l2_events: list[tuple[int, int, float, float]],
    decisions: list[dict],
    cancels: list[dict],
    params: CalibrationParams,
    symbol_spec,
):
    """Merge raw L2 stream with echo decisions/cancels by timestamp; drive
    the simulator through the merged stream so it makes the same choices
    echo did, then collect the simulator's view of fills.

    Echo's decision ts is in monotonic_ns; raw L2 ts is in ms (narci recorder
    convention). We convert both to the same unit (ns) for sorting."""
    if _NARCI_PREFIX:
        from narci.simulation import MakerSimBroker
    else:
        from simulation import MakerSimBroker

    broker = MakerSimBroker(params=params, symbol_spec=symbol_spec)

    # Merge events: tag each as ('L2', payload) | ('PLACE', d) | ('CANCEL', c)
    merged: list[tuple[int, str, object]] = []
    for ts_ms, side, price, qty in raw_l2_events:
        merged.append((ts_ms * 1_000_000, "L2", (side, price, qty)))
    for d in decisions:
        if d.get("event_type") == "PLACE":
            merged.append((int(d["ts_ns"]), "PLACE", d))
        elif d.get("event_type") == "REPRICE_TRIGGER":
            merged.append((int(d["ts_ns"]), "REPRICE", d))
        # PLACE_REJECT events are not replayed (broker re-validates)
    for c in cancels:
        merged.append((int(c["ts_ns_request"]), "CANCEL", c))

    # Stable sort: events at the same ts go in (L2 → PLACE → REPRICE → CANCEL) order
    type_order = {"L2": 0, "PLACE": 1, "REPRICE": 2, "CANCEL": 3}
    merged.sort(key=lambda x: (x[0], type_order[x[1]]))

    for ts_ns, kind, payload in merged:
        if kind == "L2":
            side, price, qty = payload
            broker.apply_market_event(ts_ns, side, price, qty)
        elif kind == "PLACE":
            d = payload
            broker.place_limit(
                ts_ns,
                d["client_oid"],
                d["side"],
                d["price"],
                d["qty"],
                alpha_pred_bps=d.get("alpha_pred_bps", float("nan")),
                alpha_features_hash=d.get("alpha_features_hash", ""),
                alpha_source=d.get("alpha_source", "naive"),
            )
        elif kind == "REPRICE":
            d = payload
            broker.record_reprice_trigger(
                ts_ns, reason=d.get("reason", ""),
                alpha_pred_bps=d.get("alpha_pred_bps", float("nan")),
                alpha_features_hash=d.get("alpha_features_hash", ""),
                alpha_source=d.get("alpha_source", "naive"),
            )
        elif kind == "CANCEL":
            c = payload
            broker.cancel(ts_ns, c["client_oid"], reason=c.get("cancel_reason", ""))

    # Drain pending cancels: feed one final no-op event far in the future
    # so any pending cancel acks fire.
    if merged:
        last_ts = merged[-1][0]
        broker.apply_market_event(last_ts + 60 * 1_000_000_000, 0, 0, 0)

    return broker


# ------------------------------------------------------------------ #
# Metric helpers
# ------------------------------------------------------------------ #


def _pair_fills(real: list[dict], sim: list[dict],
                tolerance_ms: float = 2000.0) -> tuple[list[tuple], int, int]:
    """Greedy nearest-neighbor pair real fills with sim fills.
    Match key: (client_oid OR side+price+qty within tolerance) AND ts_ns
    within tolerance_ms.

    Returns: (matched pairs, unmatched_real_count, unmatched_sim_count)."""
    tol_ns = int(tolerance_ms * 1_000_000)
    matched: list[tuple] = []
    sim_used = [False] * len(sim)
    unmatched_real = 0

    # First pass: match by client_oid (deterministic)
    sim_by_cid: dict[str, list[int]] = {}
    for i, s in enumerate(sim):
        sim_by_cid.setdefault(s.get("client_oid", ""), []).append(i)

    real_unmatched_idx: list[int] = []
    for ri, r in enumerate(real):
        cid = r.get("client_oid", "")
        candidates = sim_by_cid.get(cid, [])
        # find closest unused with ts within tolerance
        best = None
        for j in candidates:
            if sim_used[j]:
                continue
            dt = abs(r["ts_ns"] - sim[j]["ts_ns"])
            if dt <= tol_ns and (best is None or dt < best[0]):
                best = (dt, j)
        if best is not None:
            sim_used[best[1]] = True
            matched.append((r, sim[best[1]]))
        else:
            real_unmatched_idx.append(ri)

    # Second pass: for real fills without cid match, attempt timestamp-based
    for ri in real_unmatched_idx:
        r = real[ri]
        best = None
        for j, s in enumerate(sim):
            if sim_used[j]:
                continue
            if s.get("side") != r.get("side"):
                continue
            if abs(s.get("fill_price", 0) - r.get("fill_price", 0)) > 1e-6:
                continue
            dt = abs(r["ts_ns"] - s["ts_ns"])
            if dt <= tol_ns and (best is None or dt < best[0]):
                best = (dt, j)
        if best is not None:
            sim_used[best[1]] = True
            matched.append((r, sim[best[1]]))
        else:
            unmatched_real += 1

    unmatched_sim = sum(1 for u in sim_used if not u)
    return matched, unmatched_real, unmatched_sim


def _compute_adverse_from_l2(
    fills: list[dict],
    raw_l2_events: list[tuple[int, int, float, float]],
) -> dict[str, float]:
    """For each real fill, look up the mid 500ms / 1s / 5s / 30s after
    fill_ts using the raw L2 event stream as ground truth, then compute:

        adverse_at_offset = (mid_offset - mid_at_fill) × fill_side_sign
            (positive = bad for maker — mid moved away from us)

    Where fill_side_sign:
        +1 if BUY (we're long; mid going down is bad)  →  use -delta
        -1 if SELL (we're short; mid going up is bad)  →  use -delta
    Actually simpler: adverse = -delta_log_return × fill_side
    """
    if not raw_l2_events or not fills:
        return {"500ms": float("nan"), "1s": float("nan"),
                "5s": float("nan"),   "30s": float("nan")}

    # Build a lookup: replay book up to each fill's ts, record mid at each
    # offset by stepping forward through future events.
    if _NARCI_PREFIX:
        from narci.data.l2_reconstruct import L2Reconstructor
    else:
        from data.l2_reconstruct import L2Reconstructor

    rec = L2Reconstructor(depth_limit=5)
    rec.reset()

    # Convert L2 timestamps to ns to align with fill ts
    # raw events are (ts_ms, side, price, qty)
    ev_ts_ns = [e[0] * 1_000_000 for e in raw_l2_events]

    # Sort fills by ts for forward sweep
    sorted_fills = sorted(fills, key=lambda f: f["ts_ns"])

    # Precompute event index per offset target — we'll do a linear sweep
    offsets_ns = {"500ms": 500_000_000, "1s": 1_000_000_000,
                  "5s": 5_000_000_000, "30s": 30_000_000_000}
    sums = {k: 0.0 for k in offsets_ns}
    counts = {k: 0 for k in offsets_ns}

    fill_idx = 0
    fill_targets: list[tuple[int, dict, dict]] = []  # (target_ts, fill, offset_state)
    # We'll do two passes:
    #   pass A: walk events linearly, advance the book; whenever we cross
    #           a fill's "fill_ts + offset", grab mid for that offset
    # To do this in one loop, build per-fill schedule:
    # Each fill has 4 (offset_ns, fill_ref) entries.
    schedule: list[tuple[int, dict, str]] = []
    for f in sorted_fills:
        for k, off_ns in offsets_ns.items():
            schedule.append((f["ts_ns"] + off_ns, f, k))
    schedule.sort(key=lambda x: x[0])
    sched_idx = 0

    # Replay raw L2 and collect mids at scheduled times
    fill_mids_at_offset: dict[int, dict[str, float]] = {}  # id(fill) → {k: mid}
    for k, e in enumerate(raw_l2_events):
        ts_ms, side, price, qty = e
        ts_ns = ts_ms * 1_000_000
        # Before applying, fire any scheduled probes whose target_ts <= current ts_ns
        while sched_idx < len(schedule) and schedule[sched_idx][0] <= ts_ns:
            target_ts, fill_ref, off_key = schedule[sched_idx]
            st = rec.get_state(top_n=1)
            if st is not None:
                fill_mids_at_offset.setdefault(id(fill_ref), {})[off_key] = st["mid_price"]
            sched_idx += 1
        rec.apply_event(ts_ns, side, price, qty)
    # Drain remaining scheduled probes (shouldn't usually happen — means session ended early)
    while sched_idx < len(schedule):
        target_ts, fill_ref, off_key = schedule[sched_idx]
        st = rec.get_state(top_n=1)
        if st is not None:
            fill_mids_at_offset.setdefault(id(fill_ref), {})[off_key] = st["mid_price"]
        sched_idx += 1

    import math
    for f in sorted_fills:
        mids = fill_mids_at_offset.get(id(f), {})
        side_sign = 1 if f["side"] == "BUY" else -1
        mid_at_fill = f.get("mid_at_fill", 0)
        if mid_at_fill <= 0:
            continue
        for k in offsets_ns:
            mid_off = mids.get(k)
            if mid_off is None or mid_off <= 0:
                continue
            log_return = math.log(mid_off / mid_at_fill)
            # Adverse for maker: mid moves AGAINST our position
            # BUY filled (we're long) → mid going down is bad → adverse = -delta × +1
            # SELL filled (we're short) → mid going up is bad → adverse = -delta × -1
            adverse_bps = -log_return * side_sign * 10000
            sums[k] += adverse_bps
            counts[k] += 1

    return {k: (sums[k] / counts[k] if counts[k] else float("nan")) for k in offsets_ns}


# ------------------------------------------------------------------ #
# Main entry
# ------------------------------------------------------------------ #


def calibrate_session(
    echo_session_dir: str | Path,
    narci_recorder_dir: Optional[str | Path] = None,
    *,
    params: Optional[CalibrationParams] = None,
    symbol_spec=None,
    fill_match_tolerance_ms: float = 2000.0,
) -> CalibrationReport:
    """Replay an echo session through narci's simulator and compare.

    echo_session_dir:    directory created by EchoLogWriter; contains
                         meta.json, decisions/, fills/, cancels/, raw_l2/.
    narci_recorder_dir:  optional fallback path if echo's raw_l2/ is missing.
    params:              CalibrationParams to use for sim; defaults to priors.
    symbol_spec:         SymbolSpec for tick/lot validation; defaults best-effort.

    Reads meta.json, drives MakerSimBroker through the merged event stream,
    measures adverse from L2 ground-truth, and emits a CalibrationReport.
    """
    echo_dir = Path(echo_session_dir)
    meta = _load_meta(echo_dir)

    if meta.get("schema_version") != SCHEMA_VERSION:
        log.warning("schema_version mismatch (session=%s, code=%s)",
                    meta.get("schema_version"), SCHEMA_VERSION)

    # Load events
    decisions = _load_event_dir(echo_dir, "decisions")
    fills = _load_event_dir(echo_dir, "fills")
    cancels = _load_event_dir(echo_dir, "cancels")

    # Raw L2: prefer echo's own (sub-ms aligned)
    raw_l2_dir = echo_dir / "raw_l2"
    raw_used = "echo"
    if not raw_l2_dir.exists() or not list(raw_l2_dir.glob("*.parquet")):
        if narci_recorder_dir is None:
            raise FileNotFoundError(
                f"echo's raw_l2/ is empty and no narci_recorder_dir provided "
                f"(checked {raw_l2_dir})")
        raw_l2_dir = Path(narci_recorder_dir)
        raw_used = "narci_recorder"
    raw_l2 = _load_raw_l2(raw_l2_dir)
    log.info("raw L2: %d events from %s", len(raw_l2), raw_used)

    # Params + spec
    exchange = meta.get("exchange", "unknown")
    trade_symbols = meta.get("trade_symbols", [])
    symbol = trade_symbols[0] if trade_symbols else meta.get("symbols", ["UNKNOWN"])[0]

    if params is None:
        params = get_priors(exchange, symbol)
    if symbol_spec is None:
        if _NARCI_PREFIX:
            from narci.backtest.symbol_spec import SymbolSpec
        else:
            from backtest.symbol_spec import SymbolSpec
        symbol_spec = SymbolSpec(symbol=symbol, tick_size=1.0, lot_size=1e-8,
                                 min_notional=0.0)

    # Replay
    log.info("replaying %d L2 + %d decisions + %d cancels",
             len(raw_l2), len(decisions), len(cancels))
    broker = _replay(raw_l2, decisions, cancels, params, symbol_spec)
    sim_decisions, sim_fills, sim_cancels = broker.flush_results()

    # Metrics
    matched, _u_real, _u_sim = _pair_fills(fills, sim_fills, fill_match_tolerance_ms)
    real_count = len(fills)
    sim_count = len(sim_fills)
    matched_count = len(matched)
    match_rate = (matched_count / real_count) if real_count > 0 else 0.0

    if matched:
        offsets_ms = sorted(abs(r["ts_ns"] - s["ts_ns"]) / 1e6 for r, s in matched)
        p50 = offsets_ms[len(offsets_ms) // 2]
        p95 = offsets_ms[int(len(offsets_ms) * 0.95)]
    else:
        p50 = p95 = float("nan")

    fill_metrics = FillMatchMetrics(
        real_count=real_count, sim_count=sim_count,
        matched_count=matched_count, match_rate=match_rate,
        timing_offset_p50_ms=p50, timing_offset_p95_ms=p95,
    )

    # Adverse selection ground truth from L2
    adv = _compute_adverse_from_l2(fills, raw_l2)
    sim_adv_1s = params.adverse_baseline_1s_bps
    adverse_metrics = AdverseMetrics(
        real_mean_500ms_bps=adv["500ms"],
        real_mean_1s_bps=adv["1s"],
        real_mean_5s_bps=adv["5s"],
        real_mean_30s_bps=adv["30s"],
        sim_mean_1s_bps=sim_adv_1s,
        delta_1s_bps=(adv["1s"] - sim_adv_1s) if not _isnan(adv["1s"]) else float("nan"),
    )

    # Cancel latency metrics
    real_lats = [c["cancel_latency_ms"] for c in cancels if c.get("cancel_latency_ms", 0) > 0]
    if real_lats:
        real_lats.sort()
        rp50 = real_lats[len(real_lats) // 2]
        rp95 = real_lats[int(len(real_lats) * 0.95)]
        rp99 = real_lats[int(len(real_lats) * 0.99)] if len(real_lats) > 100 else rp95
    else:
        rp50 = rp95 = rp99 = float("nan")
    sim_p50 = params.cancel_latency_p50_ms
    cancel_metrics = CancelLatencyMetrics(
        real_p50_ms=rp50, real_p95_ms=rp95, real_p99_ms=rp99,
        sim_p50_ms=sim_p50,
        delta_p50_ms=(rp50 - sim_p50) if not _isnan(rp50) else float("nan"),
    )

    # Queue scaling suggestion: ratio of real fills count to sim fills count.
    # If sim under-fills (fewer sim fills than real), our queue model
    # over-estimates ahead_qty; suggest scaling < 1. Inverse otherwise.
    if sim_count > 0 and real_count > 0:
        queue_scaling = real_count / sim_count
    else:
        queue_scaling = 1.0

    # Verdict
    issues: list[str] = []
    if real_count > 0 and match_rate < 0.7:
        issues.append(f"fill_match_rate {match_rate:.2f} < 0.7")
    if real_count > 5 and (sim_count < real_count * 0.5 or sim_count > real_count * 2.0):
        issues.append(f"fill count ratio sim/real = {sim_count}/{real_count}")
    if not _isnan(adverse_metrics.delta_1s_bps) and abs(adverse_metrics.delta_1s_bps) > 0.3:
        issues.append(f"adverse_1s delta = {adverse_metrics.delta_1s_bps:.2f} bps")
    if not _isnan(cancel_metrics.delta_p50_ms) and abs(cancel_metrics.delta_p50_ms) > sim_p50 * 0.5:
        issues.append(f"cancel_latency delta = {cancel_metrics.delta_p50_ms:.0f} ms")

    if not issues:
        verdict = "HEALTHY"
    elif len(issues) <= 2:
        verdict = "ACCEPT_WITH_TUNING"
    else:
        verdict = "UNHEALTHY"

    # Suggested params (only emit fields where we have data)
    suggested: dict = {}
    if not _isnan(adv["500ms"]):
        suggested["adverse_baseline_500ms_bps"] = round(adv["500ms"], 3)
    if not _isnan(adv["1s"]):
        suggested["adverse_baseline_1s_bps"] = round(adv["1s"], 3)
    if not _isnan(adv["5s"]):
        suggested["adverse_baseline_5s_bps"] = round(adv["5s"], 3)
    if not _isnan(adv["30s"]):
        suggested["adverse_baseline_30s_bps"] = round(adv["30s"], 3)
    if not _isnan(rp50):
        suggested["cancel_latency_p50_ms"] = round(rp50, 1)
    if not _isnan(rp95):
        suggested["cancel_latency_p95_ms"] = round(rp95, 1)
    if abs(queue_scaling - 1.0) > 0.05:
        suggested["queue_scaling"] = round(queue_scaling * params.queue_scaling, 3)

    report = CalibrationReport(
        session_id=meta.get("session_id", "unknown"),
        schema_version=meta.get("schema_version", SCHEMA_VERSION),
        exchange=exchange, symbol=symbol,
        sample_count=real_count,
        fill_metrics=fill_metrics,
        adverse_metrics=adverse_metrics,
        cancel_metrics=cancel_metrics,
        queue_scaling_suggestion=queue_scaling,
        verdict=verdict, issues=issues,
        suggested_params=suggested,
    )
    return report


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


def _isnan(x: float) -> bool:
    return x != x  # NaN != NaN


def report_to_json(report: CalibrationReport) -> str:
    """Serialize a CalibrationReport to indented JSON."""
    def _conv(o):
        if dc.is_dataclass(o) and not isinstance(o, type):
            return dc.asdict(o)
        if isinstance(o, float) and _isnan(o):
            return None
        return str(o)
    d = dc.asdict(report)
    return json.dumps(d, indent=2, default=_conv)
