"""Tests for L2Recorder._refresh_book_via_rest (P1-A fix 2026-05-08).

The save_loop's REST refresh path replaces the in-memory book with a
fresh REST snapshot every save_interval to evict dust from missed WS
delete events. These tests cover:

  1. Successful refresh atomically swaps book + last_update_id +
     resets is_initialized/stream_aligned for the WS re-alignment
     handshake.
  2. Failed refresh (timeout/network) restores legacy state so the
     existing in-memory book continues to work.
  3. State-machine guards (is_initialized=False during await) block
     concurrent depth events from mutating the about-to-be-replaced
     book — concurrent events queue in pre_align_buffer instead.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

import pytest

from data.l2_recorder import L2Recorder


class _StubAdapter:
    """Minimal ExchangeAdapter implementing just what L2Recorder needs."""
    name = "test"
    market_type = "spot"

    def __init__(self, snapshot_data, parse_id=12345):
        self._snapshot = snapshot_data
        self._parse_id = parse_id
        self.fetch_count = 0
        self.fetch_should_raise = None  # set to an exception to simulate failure
        self.fetch_delay_sec = 0.0

    def ws_url(self, symbols, interval_ms=100):
        return "wss://example.invalid/test"

    def ws_urls(self, symbols, interval_ms=100):
        return [self.ws_url(symbols, interval_ms)]

    def subscribe_messages(self, symbols):
        return []

    async def fetch_snapshot(self, symbol):
        self.fetch_count += 1
        if self.fetch_delay_sec > 0:
            await asyncio.sleep(self.fetch_delay_sec)
        if self.fetch_should_raise:
            raise self.fetch_should_raise
        return dict(self._snapshot)

    def parse_snapshot(self, data):
        return self._parse_id, []

    def parse_message(self, msg):
        return None, None, None

    def standardize_event(self, event_type, data, now_ms=None):
        return []

    def needs_alignment(self):
        return True

    def try_align(self, snapshot_update_id, event):
        return True

    def get_update_id(self, event):
        return 0

    def to_native(self, std):
        return std.lower()

    def to_std(self, native):
        return native.upper()


@pytest.fixture
def recorder():
    """Build a recorder with stub adapter, no config file required."""
    tmpdir = tempfile.mkdtemp(prefix="narci_test_")
    fresh_snapshot = {
        "bids": [["100.5", "1.0"], ["100.0", "2.0"]],
        "asks": [["101.0", "1.5"], ["101.5", "0.5"]],
    }
    adapter = _StubAdapter(fresh_snapshot, parse_id=99999)

    # Bypass _load_config by passing an explicit path that doesn't exist;
    # _load_config returns {} on missing file, then config-driven defaults
    # apply. Use the symbol/adapter overrides.
    rec = L2Recorder(config_path="/nonexistent.yaml", symbol="btcjpy",
                     adapter=adapter)
    rec.save_dir = tmpdir
    rec.snapshot_refresh_on_save = True
    yield rec, adapter, tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_refresh_swaps_book_atomically(recorder):
    """Successful REST returns; new bids/asks installed, last_update_id
    set, is_initialized flipped True, stream_aligned still False so
    existing alignment logic kicks in on next WS event."""
    rec, adapter, _ = recorder

    # Pre-state: stale in-memory book with dust
    rec.orderbooks["btcjpy"] = {"bids": {500.0: 0.1}, "asks": {50.0: 0.2}}
    rec.last_update_ids["btcjpy"] = 1
    rec.is_initialized["btcjpy"] = True
    rec.stream_aligned["btcjpy"] = True

    asyncio.run(rec._refresh_book_via_rest("btcjpy"))

    assert adapter.fetch_count == 1
    bids = rec.orderbooks["btcjpy"]["bids"]
    asks = rec.orderbooks["btcjpy"]["asks"]
    assert bids == {100.5: 1.0, 100.0: 2.0}, bids
    assert asks == {101.0: 1.5, 101.5: 0.5}, asks
    # No dust survived
    assert 500.0 not in bids
    assert 50.0 not in asks
    # last_update_id refreshed
    assert rec.last_update_ids["btcjpy"] == 99999
    # State machine: ready for WS re-align
    assert rec.is_initialized["btcjpy"] is True
    assert rec.stream_aligned["btcjpy"] is False
    assert rec.pre_align_buffer["btcjpy"] == []


def test_refresh_failure_restores_legacy_state(recorder):
    """REST fetch raises → in-memory book preserved, is_initialized +
    stream_aligned restored to True (legacy behavior). The existing
    book is dust-laden but still usable."""
    rec, adapter, _ = recorder
    adapter.fetch_should_raise = TimeoutError("simulated REST timeout")

    rec.orderbooks["btcjpy"] = {"bids": {99.0: 1.0}, "asks": {102.0: 1.0}}
    rec.last_update_ids["btcjpy"] = 42
    rec.is_initialized["btcjpy"] = True
    rec.stream_aligned["btcjpy"] = True

    asyncio.run(rec._refresh_book_via_rest("btcjpy"))

    # Book unchanged
    assert rec.orderbooks["btcjpy"]["bids"] == {99.0: 1.0}
    assert rec.orderbooks["btcjpy"]["asks"] == {102.0: 1.0}
    assert rec.last_update_ids["btcjpy"] == 42
    # State restored — recorder keeps recording with stale book
    assert rec.is_initialized["btcjpy"] is True
    assert rec.stream_aligned["btcjpy"] is True


def test_refresh_clears_pre_align_before_await(recorder):
    """During the REST round-trip, concurrent _handle_depth callbacks
    push events to pre_align_buffer (not the about-to-be-replaced book).
    Verify the buffer is reset at the START of refresh so we don't keep
    pre-refresh stale events."""
    rec, adapter, _ = recorder
    rec.pre_align_buffer["btcjpy"] = [{"stale": "data1"}, {"stale": "data2"}]

    # Slow the fetch down enough to observe state mid-flight
    adapter.fetch_delay_sec = 0.05

    async def race():
        # Schedule the refresh, then mid-flight verify is_initialized=False
        # and pre_align_buffer was cleared.
        task = asyncio.create_task(rec._refresh_book_via_rest("btcjpy"))
        await asyncio.sleep(0.01)  # let the refresh start, hit the await
        assert rec.is_initialized["btcjpy"] is False
        assert rec.pre_align_buffer["btcjpy"] == []
        # Simulate a depth event arriving while we're waiting on REST.
        # In production _handle_depth would call this:
        rec.pre_align_buffer["btcjpy"].append({"during_fetch": "data"})
        await task
        # After refresh completes, the during-fetch event remains queued
        # for re-alignment by the existing logic.
        assert rec.pre_align_buffer["btcjpy"] == [{"during_fetch": "data"}]
        assert rec.is_initialized["btcjpy"] is True

    asyncio.run(race())


def test_snapshot_refresh_on_save_default_off():
    """Default constructor (no config, no flag) preserves legacy
    behavior: snapshot_refresh_on_save=False."""
    adapter = _StubAdapter({"bids": [], "asks": []})
    tmpdir = tempfile.mkdtemp(prefix="narci_test_")
    rec = L2Recorder(config_path="/nonexistent.yaml", symbol="btcjpy",
                     adapter=adapter)
    rec.save_dir = tmpdir
    try:
        assert rec.snapshot_refresh_on_save is False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
