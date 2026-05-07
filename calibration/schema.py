"""Calibration log schema.

This is the **contract** between echo (writes) and narci (reads).
Field names, dtypes and ordering must match `docs/CALIBRATION_PROTOCOL.md`
§2.2-2.5 byte-for-byte. Any change requires a `SCHEMA_VERSION` bump and a
test fixture refresh.

Three event types:

  - DecisionEvent  PLACE / CANCEL / REPRICE_TRIGGER decisions emitted by
                   the strategy. Logged at decision time on the echo host.
  - FillEvent      Generated when an exchange execution_report arrives.
  - CancelEvent    Generated when a cancel ack arrives (or terminal state
                   for the order: REJECTED, FILLED_DURING_CANCEL).

All three include both:
  - `ts_ns`       monotonic_ns (internal ordering, immune to NTP)
  - `ts_wall_ns`  time_ns (cross-host alignment via AWS Time Sync)

Plus exchange-side timestamps where applicable.

Echo writes one parquet shard per save_interval; the calibration replay
loads all shards in a session window.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import ClassVar

import pyarrow as pa


# Bump this when any field changes type, name, or ordering.
SCHEMA_VERSION = "v1.1"


# ------------------------------------------------------------------ #
# Raw L2 stream — same 4-col format as narci.data.l2_recorder.L2Recorder
# Echo's in-process L2 sidecar writes parquet with this schema so
# narci's calibration replay can ingest it the same way it ingests
# narci recorder data.
#
# Side encoding (must match narci recorder + ABC contract):
#   0 = bid update         (qty == 0 → delete level)
#   1 = ask update
#   2 = aggTrade           (signed qty: > 0 buyer maker, < 0 seller maker)
#   3 = bid snapshot       (sets is_ready in the reconstructor)
#   4 = ask snapshot
# ------------------------------------------------------------------ #


RAW_L2_SCHEMA: pa.Schema = pa.schema([
    pa.field("timestamp", pa.int64(),  nullable=False),  # ms since epoch
    pa.field("side",      pa.int8(),   nullable=False),  # 0..4
    pa.field("price",     pa.float64(), nullable=False),
    pa.field("quantity",  pa.float64(), nullable=False),
])


# Recommended file naming for echo's in-process recorder:
#   {session_dir}/raw_l2/{SYMBOL}_RAW_{YYYYMMDD}_{HHMMSS}.parquet
# Snapshot injection (~once per save_interval) ensures every shard is
# self-contained: a full bid/ask book in side=3/4 rows at the start of
# the shard, followed by side=0/1/2 incremental updates. This matches
# narci recorder's behavior and lets calibrate_session bootstrap from
# any single shard.
RAW_L2_FILENAME_TEMPLATE = "{symbol}_RAW_{date}_{time}.parquet"


# ------------------------------------------------------------------ #
# DecisionEvent — strategy-level place/cancel/reprice intent
# ------------------------------------------------------------------ #


@dataclass(slots=True)
class DecisionEvent:
    # --- always populated ---
    ts_ns: int                              # monotonic_ns at decision time
    ts_wall_ns: int                         # time_ns at decision time
    event_type: str                         # "PLACE" | "CANCEL" | "REPRICE_TRIGGER"
    client_oid: str
    symbol: str

    # --- market state snapshot at decision time ---
    mid_price: float
    best_bid: float
    best_ask: float
    top1_bid_qty: float
    top1_ask_qty: float
    spread_bps: float

    # --- alpha provenance ---
    alpha_pred_bps: float                   # NaN if no signal active
    alpha_features_hash: str                # hex; "" if no signal active
    alpha_source: str                       # "naive" | "ols_v1" | "ols_v1_fallback" | etc.

    # --- intent (for PLACE; partly null for others) ---
    side: str                               # "BUY" | "SELL" | "" for REPRICE_TRIGGER
    price: float                            # quote price (NaN for non-PLACE)
    qty: float                              # quote size (NaN for non-PLACE)
    estimated_queue_position: int           # rank in queue at insert time (0 = front); -1 if unknown
    estimated_queue_ahead_qty: float        # cumulative qty before us; NaN if unknown

    # --- linkage ---
    place_oid: str = ""                     # for CANCEL: which PLACE this cancels
    reason: str = ""                        # for REPRICE_TRIGGER: "MID_DRIFT" | "ALPHA_FLIP" | "POST_FILL" | "STALE_TIMEOUT"

    # parquet schema mirror; keep in sync with field defs above
    PARQUET_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        pa.field("ts_ns", pa.int64(), nullable=False),
        pa.field("ts_wall_ns", pa.int64(), nullable=False),
        pa.field("event_type", pa.string(), nullable=False),
        pa.field("client_oid", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("mid_price", pa.float64(), nullable=False),
        pa.field("best_bid", pa.float64(), nullable=False),
        pa.field("best_ask", pa.float64(), nullable=False),
        pa.field("top1_bid_qty", pa.float64(), nullable=False),
        pa.field("top1_ask_qty", pa.float64(), nullable=False),
        pa.field("spread_bps", pa.float64(), nullable=False),
        pa.field("alpha_pred_bps", pa.float64(), nullable=True),
        pa.field("alpha_features_hash", pa.string(), nullable=False),
        pa.field("alpha_source", pa.string(), nullable=False),
        pa.field("side", pa.string(), nullable=False),
        pa.field("price", pa.float64(), nullable=True),
        pa.field("qty", pa.float64(), nullable=True),
        pa.field("estimated_queue_position", pa.int32(), nullable=False),
        pa.field("estimated_queue_ahead_qty", pa.float64(), nullable=True),
        pa.field("place_oid", pa.string(), nullable=False),
        pa.field("reason", pa.string(), nullable=False),
    ])


# ------------------------------------------------------------------ #
# FillEvent — execution_report from exchange (maker fill, taker fill,
# or partial fill of an existing order)
# ------------------------------------------------------------------ #


@dataclass(slots=True)
class FillEvent:
    # --- timestamps ---
    ts_ns: int                              # monotonic_ns when echo received the report
    ts_wall_ns: int                         # time_ns at same instant
    exchange_ts_ms: int                     # exchange-side fill time (ms)

    # --- identity ---
    client_oid: str
    exchange_oid: str
    symbol: str
    side: str                               # "BUY" | "SELL"

    # --- fill payload ---
    fill_price: float
    fill_qty: float
    is_maker: bool                          # CC always True; other venues vary

    # --- linkage to the originating PLACE ---
    place_ts_ns: int                        # PLACE event ts_ns
    quote_age_ms: float                     # (fill_ts_ns - place_ts_ns) / 1e6

    # --- market state snapshot at place vs at fill ---
    mid_at_place: float
    mid_at_fill: float
    spread_at_fill_bps: float

    # --- post-fill mid trajectory (for adverse selection metric) ---
    # Echo populates these by polling its own L2 book at the given offsets.
    # NaN if echo session ends before the offset is reached.
    mid_t_plus_500ms: float
    mid_t_plus_1s: float
    mid_t_plus_5s: float
    mid_t_plus_30s: float

    # --- queue model diagnostics ---
    estimated_queue_at_place: int           # rank when we placed (0=front, -1 unknown)
    estimated_queue_at_fill: int            # rank moments before fill (0/1 typical, -1 unknown)

    # --- inventory accounting ---
    inventory_before: float                 # base asset qty before this fill
    inventory_after: float                  # after fill (pos + or - depending on side)

    # --- alpha replay ---
    alpha_pred_bps_at_place: float          # alpha at PLACE time (NaN if naive)
    alpha_realized_bps_post: float          # log(mid_t_plus_1s / mid_at_fill) * 1e4

    PARQUET_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        pa.field("ts_ns", pa.int64(), nullable=False),
        pa.field("ts_wall_ns", pa.int64(), nullable=False),
        pa.field("exchange_ts_ms", pa.int64(), nullable=False),
        pa.field("client_oid", pa.string(), nullable=False),
        pa.field("exchange_oid", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("side", pa.string(), nullable=False),
        pa.field("fill_price", pa.float64(), nullable=False),
        pa.field("fill_qty", pa.float64(), nullable=False),
        pa.field("is_maker", pa.bool_(), nullable=False),
        pa.field("place_ts_ns", pa.int64(), nullable=False),
        pa.field("quote_age_ms", pa.float64(), nullable=False),
        pa.field("mid_at_place", pa.float64(), nullable=False),
        pa.field("mid_at_fill", pa.float64(), nullable=False),
        pa.field("spread_at_fill_bps", pa.float64(), nullable=False),
        pa.field("mid_t_plus_500ms", pa.float64(), nullable=True),
        pa.field("mid_t_plus_1s", pa.float64(), nullable=True),
        pa.field("mid_t_plus_5s", pa.float64(), nullable=True),
        pa.field("mid_t_plus_30s", pa.float64(), nullable=True),
        pa.field("estimated_queue_at_place", pa.int32(), nullable=False),
        pa.field("estimated_queue_at_fill", pa.int32(), nullable=False),
        pa.field("inventory_before", pa.float64(), nullable=False),
        pa.field("inventory_after", pa.float64(), nullable=False),
        pa.field("alpha_pred_bps_at_place", pa.float64(), nullable=True),
        pa.field("alpha_realized_bps_post", pa.float64(), nullable=True),
    ])


# ------------------------------------------------------------------ #
# CancelEvent — cancel ack from exchange (or terminal state inferred)
# ------------------------------------------------------------------ #


@dataclass(slots=True)
class CancelEvent:
    # --- timestamps ---
    ts_ns_request: int                      # echo emitted DELETE
    ts_wall_ns_request: int                 # wall-clock equivalent
    ts_ns_ack: int                          # echo got cancel response (or final terminal)
    cancel_latency_ms: float                # (ts_ns_ack - ts_ns_request) / 1e6

    # --- identity ---
    client_oid: str
    exchange_oid: str
    symbol: str

    # --- linkage ---
    place_ts_ns: int                        # original PLACE ts_ns
    quote_age_at_cancel_ms: float           # (ts_ns_request - place_ts_ns) / 1e6

    # --- reason / outcome ---
    cancel_reason: str                      # "STRATEGY_REPRICE" | "RISK_LIMIT" | "KILL_SWITCH" | "INVENTORY"
    final_state: str                        # "CANCELLED" | "FILLED_DURING_CANCEL" | "REJECTED"
    qty_filled_before_cancel: float         # 0.0 normally; > 0 if partial fill happened first

    # --- market state snapshot ---
    mid_at_cancel: float

    PARQUET_SCHEMA: ClassVar[pa.Schema] = pa.schema([
        pa.field("ts_ns_request", pa.int64(), nullable=False),
        pa.field("ts_wall_ns_request", pa.int64(), nullable=False),
        pa.field("ts_ns_ack", pa.int64(), nullable=False),
        pa.field("cancel_latency_ms", pa.float64(), nullable=False),
        pa.field("client_oid", pa.string(), nullable=False),
        pa.field("exchange_oid", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("place_ts_ns", pa.int64(), nullable=False),
        pa.field("quote_age_at_cancel_ms", pa.float64(), nullable=False),
        pa.field("cancel_reason", pa.string(), nullable=False),
        pa.field("final_state", pa.string(), nullable=False),
        pa.field("qty_filled_before_cancel", pa.float64(), nullable=False),
        pa.field("mid_at_cancel", pa.float64(), nullable=False),
    ])


# ------------------------------------------------------------------ #
# Session metadata — written once per session as JSON
# ------------------------------------------------------------------ #


@dataclass
class SessionMeta:
    session_id: str                         # YYYYMMDD-HHMMSS-{exchange}-{symbol}-{strategy_v}
    schema_version: str                     # SCHEMA_VERSION at write time
    start_wall_ns: int
    end_wall_ns: int                        # 0 if still running
    strategy_class: str                     # dotted import path
    strategy_config_hash: str               # sha256 of strategy yaml
    exchange: str                           # "coincheck" | "binance_jp" | ...
    symbols: list[str]                      # everything echo subscribed to
    trade_symbols: list[str]                # subset echo actually placed orders on

    # host/runtime info — for cross-host jitter diagnosis
    hostname: str
    az: str                                 # "ap-northeast-1a"
    instance_type: str                      # "c7g.large"
    ntp_source: str                         # "169.254.169.123" expected
    python_version: str
    echo_git_sha: str
    narci_git_sha: str

    # narci recorder linkage — echo leaves these empty; narci's
    # calibrate_session resolves them by time-window match against
    # narci/replay_buffer/realtime/.
    narci_recorder_session_id: str = ""
    narci_recorder_window_start_ns: int = 0
    narci_recorder_window_end_ns: int = 0

    # Lifecycle status (added v1.1 per echo feedback 3.3):
    #   "running"   — written on startup; end_wall_ns == 0
    #   "completed" — written on graceful shutdown
    #   "crashed"   — written by external watchdog (or detected by
    #                 narci replay if last log shard is fresher than
    #                 meta and end_wall_ns == 0 + status == "running")
    status: str = "running"


# ------------------------------------------------------------------ #
# Helpers: build pyarrow Table from dataclass list
# ------------------------------------------------------------------ #


def to_arrow_table(events: list, schema: pa.Schema) -> pa.Table:
    """Convert a list of dataclass instances to a pyarrow Table.
    Empty list -> empty table with the given schema."""
    if not events:
        return pa.Table.from_pydict(
            {f.name: [] for f in schema},
            schema=schema,
        )
    cols: dict[str, list] = {f.name: [] for f in schema}
    for ev in events:
        for f in schema:
            cols[f.name].append(getattr(ev, f.name))
    return pa.Table.from_pydict(cols, schema=schema)


def validate_event(ev, schema: pa.Schema) -> list[str]:
    """Lightweight runtime validation: every schema field exists on `ev`,
    and required (non-nullable) fields aren't None.

    Returns a list of error strings; empty list = pass."""
    errors = []
    for f in schema:
        if not hasattr(ev, f.name):
            errors.append(f"missing field: {f.name}")
            continue
        v = getattr(ev, f.name)
        if v is None and not f.nullable:
            errors.append(f"non-nullable field {f.name} is None")
    return errors


# Convenience: ordered dict of event_type → schema
SCHEMAS = {
    "decisions": DecisionEvent.PARQUET_SCHEMA,
    "fills": FillEvent.PARQUET_SCHEMA,
    "cancels": CancelEvent.PARQUET_SCHEMA,
}

# Map from event class to its schema (writers use this).
SCHEMA_FOR = {
    DecisionEvent: DecisionEvent.PARQUET_SCHEMA,
    FillEvent: FillEvent.PARQUET_SCHEMA,
    CancelEvent: CancelEvent.PARQUET_SCHEMA,
}
