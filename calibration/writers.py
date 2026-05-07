"""Echo session log writer.

Echo's trading loop calls into this; writer buffers events in memory and
flushes shards to parquet on a timer or buffer cap.

API contract (echo imports from here):

    from narci.calibration.writers import EchoLogWriter
    from narci.calibration.schema import DecisionEvent, FillEvent, CancelEvent

    writer = EchoLogWriter(
        session_id="20260506-100000-coincheck-btc_jpy-naive_v1",
        base_dir="/var/lib/echo/logs",
        flush_interval_sec=60,
    )
    writer.write_session_meta(meta)              # called once on startup
    asyncio.create_task(writer.run_flush_loop()) # background task

    # in trading loop:
    writer.write_decision(decision_ev)
    writer.write_fill(fill_ev)
    writer.write_cancel(cancel_ev)

    # on shutdown:
    await writer.close()                          # final flush + meta update

Design choices:

  * **Schema validated on write** — bad events are dropped + logged, not raised.
    Echo's hot path must never crash because of a logger bug.
  * **Atomic file writes** — write to .tmp, then rename. Reader (narci replay)
    can scan a directory mid-session without seeing torn parquets.
  * **One shard per (kind, symbol, flush)** — keeps shards small + parallel-readable.
    Filename: `{symbol}_{KIND}_{YYYYMMDD}_{HHMMSS}.parquet`
  * **Single-threaded asyncio assumption** — no locking. If you call
    `write_*` from another thread, expect data races.
  * **Disk-full / I/O errors** are logged + buffer kept; next successful flush
    eventually catches up. We do NOT drop events on I/O failure.
"""

from __future__ import annotations

import dataclasses as dc
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

import pyarrow.parquet as pq

from .schema import (
    SCHEMA_FOR,
    SCHEMA_VERSION,
    CancelEvent,
    DecisionEvent,
    FillEvent,
    SessionMeta,
    to_arrow_table,
    validate_event,
)

log = logging.getLogger("narci.calibration.writers")

E = TypeVar("E", DecisionEvent, FillEvent, CancelEvent)

_KIND_BY_TYPE: dict[type, tuple[str, str]] = {
    DecisionEvent: ("decisions", "DEC"),
    FillEvent:     ("fills",     "FILL"),
    CancelEvent:   ("cancels",   "CXL"),
}


class EchoLogWriter:
    def __init__(
        self,
        session_id: str,
        base_dir: str | Path,
        *,
        flush_interval_sec: float = 60.0,
        max_buffer_events: int = 10_000,
        validate_on_write: bool = True,
    ):
        self.session_id = session_id
        self.session_dir = Path(base_dir) / session_id
        self.flush_interval_sec = flush_interval_sec
        self.max_buffer_events = max_buffer_events
        self.validate_on_write = validate_on_write

        # Layout subdirs
        for kind, _ in _KIND_BY_TYPE.values():
            (self.session_dir / kind).mkdir(parents=True, exist_ok=True)
        # raw_l2 dir is created by L2Recorder; we just touch its parent.
        (self.session_dir / "raw_l2").mkdir(parents=True, exist_ok=True)

        # Buffers (kind → list[event])
        self._buffers: dict[str, list] = {kind: [] for kind, _ in _KIND_BY_TYPE.values()}
        self._dropped: dict[str, int] = {kind: 0 for kind, _ in _KIND_BY_TYPE.values()}
        self._written: dict[str, int] = {kind: 0 for kind, _ in _KIND_BY_TYPE.values()}

        self._closed = False
        self._meta: SessionMeta | None = None

    # ------------------------------------------------------------------ #
    # Session meta
    # ------------------------------------------------------------------ #

    def write_session_meta(self, meta: SessionMeta) -> None:
        """Persist `meta.json`. Called once on startup, may be called again on
        shutdown to update `end_wall_ns`."""
        if meta.schema_version != SCHEMA_VERSION:
            log.warning("session schema_version %s != writer SCHEMA_VERSION %s",
                        meta.schema_version, SCHEMA_VERSION)
        self._meta = meta
        path = self.session_dir / "meta.json"
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(dc.asdict(meta), f, indent=2)
        os.replace(tmp, path)

    # ------------------------------------------------------------------ #
    # Event writes
    # ------------------------------------------------------------------ #

    def write_decision(self, ev: DecisionEvent) -> None:
        self._enqueue(ev)

    def write_fill(self, ev: FillEvent) -> None:
        self._enqueue(ev)

    def write_cancel(self, ev: CancelEvent) -> None:
        self._enqueue(ev)

    def _enqueue(self, ev) -> None:
        if self._closed:
            log.warning("writer closed; dropping %s", type(ev).__name__)
            return
        meta = _KIND_BY_TYPE.get(type(ev))
        if meta is None:
            log.error("unknown event type %s; dropping", type(ev))
            return
        kind, _ = meta
        if self.validate_on_write:
            errors = validate_event(ev, SCHEMA_FOR[type(ev)])
            if errors:
                self._dropped[kind] += 1
                log.error("schema validation failed for %s: %s", kind, errors)
                return
        self._buffers[kind].append(ev)

        if len(self._buffers[kind]) >= self.max_buffer_events:
            # Synchronous flush of just this kind (cheap; buffer cap rarely hit
            # if flush_interval is short enough).
            self._flush_kind(kind)

    # ------------------------------------------------------------------ #
    # Flush
    # ------------------------------------------------------------------ #

    async def run_flush_loop(self) -> None:
        """Background task: periodically flush all kinds. Stops when close()
        is called."""
        import asyncio  # local import keeps schema-only consumers async-free
        while not self._closed:
            try:
                await asyncio.sleep(self.flush_interval_sec)
            except asyncio.CancelledError:
                break
            try:
                self.flush()
            except Exception:  # noqa: BLE001
                log.exception("flush loop error (continuing)")

    def flush(self) -> None:
        """Flush all buffered events to parquet shards."""
        for kind in self._buffers:
            self._flush_kind(kind)

    def _flush_kind(self, kind: str) -> None:
        events = self._buffers[kind]
        if not events:
            return

        # Group by symbol so each shard corresponds to one symbol.
        by_symbol: dict[str, list] = {}
        for ev in events:
            sym = getattr(ev, "symbol", "_unknown")
            by_symbol.setdefault(sym, []).append(ev)

        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        kind_dir = self.session_dir / kind
        _, fn_kind = next(v for k, v in _KIND_BY_TYPE.items() if v[0] == kind)

        wrote_any = False
        for sym, evs in by_symbol.items():
            schema = SCHEMA_FOR[type(evs[0])]
            tbl = to_arrow_table(evs, schema)
            fname = f"{sym}_{fn_kind}_{ts_str}_{len(evs)}.parquet"
            path = kind_dir / fname
            tmp = path.with_suffix(".parquet.tmp")
            try:
                pq.write_table(tbl, tmp)
                os.replace(tmp, path)
                self._written[kind] += len(evs)
                wrote_any = True
            except Exception:  # noqa: BLE001
                log.exception("failed to write %s; keeping %d events in buffer",
                              path, len(evs))
                # Restore: events were taken out below only if write succeeded.
                # We'll still drain the buffer once at the end on success path.
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                # Do NOT clear buffer for this kind — try again next flush
                # (note: we re-enter the buffer below only when wrote_any, so
                #  events stay)
                return  # bail; remaining symbols retry next cycle

        # Only clear buffer if every per-symbol write succeeded.
        if wrote_any:
            self._buffers[kind].clear()

    # ------------------------------------------------------------------ #
    # Close
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """Final flush + update meta with end_wall_ns. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self.flush()
        except Exception:  # noqa: BLE001
            log.exception("final flush error")
        if self._meta is not None:
            try:
                self._meta.end_wall_ns = time.time_ns()
                self._meta.status = "completed"
                self.write_session_meta(self._meta)
            except Exception:  # noqa: BLE001
                log.exception("final meta write error")

    # ------------------------------------------------------------------ #
    # Stats — surfaced to monitor / metrics
    # ------------------------------------------------------------------ #

    def stats(self) -> dict:
        return {
            "session_id": self.session_id,
            "schema_version": SCHEMA_VERSION,
            "buffered": {k: len(v) for k, v in self._buffers.items()},
            "written":  dict(self._written),
            "dropped":  dict(self._dropped),
            "closed":   self._closed,
        }
