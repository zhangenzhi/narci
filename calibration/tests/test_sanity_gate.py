"""sanity_gate tests."""

from __future__ import annotations

import sys

from data.sanity_gate import (
    BLACKLIST_VERSION,
    all_entries,
    filter_days,
    is_blacklisted,
    reason,
)


def test_version_constant():
    assert isinstance(BLACKLIST_VERSION, str)
    assert BLACKLIST_VERSION


def test_um_trades_dead_window():
    for d in ("20260424", "20260425", "20260426", "20260427", "20260428"):
        assert is_blacklisted(d, "binance_um", "BTCUSDT"), d
        assert is_blacklisted(d, "binance_um", "ETHUSDT"), d   # wildcard symbol
        assert "trades-dead" in reason(d, "binance_um", "BTCUSDT")


def test_um_trades_dead_05_06_07_blacklist():
    """v1.2 (2026-05-10): 0506+0507 UM also caught the recorder bug
    (running on /market/stream which dropped depth; dual-WS fix
    deployed mid-0507). Vision-backfilled but day-level blacklisted
    because depth coverage is also degraded for these days."""
    for d in ("20260506", "20260507"):
        assert is_blacklisted(d, "binance_um", "BTCUSDT"), d
        assert is_blacklisted(d, "binance_um", "ETHUSDT"), d
        r = reason(d, "binance_um", "BTCUSDT")
        assert "Vision-backfilled" in r
        assert "dual-WS" in r


def test_um_05_08_NOT_blacklisted_only_flagged():
    """0508 partial CC outage is documented in comments only, not
    blacklisted. UM/BJ on 0508 are normal, CC trades ~50% of normal."""
    assert not is_blacklisted("20260508", "binance_um", "BTCUSDT")
    assert not is_blacklisted("20260508", "coincheck", "BTC_JPY")
    assert not is_blacklisted("20260508", "binance_jp", "BTCJPY")


def test_um_05_02_NOT_blacklisted_low_volume_weekend():
    """0502 low across all 3 venues is documented as real low-volume
    weekend (not a recorder bug). Keep usable."""
    assert not is_blacklisted("20260502", "binance_um", "BTCUSDT")
    assert not is_blacklisted("20260502", "coincheck", "BTC_JPY")
    assert not is_blacklisted("20260502", "binance_jp", "BTCJPY")


def test_bj_partial_outage():
    assert is_blacklisted("20260428", "binance_jp", "BTCJPY")
    assert "outage" in reason("20260428", "binance_jp", "BTCJPY")


def test_outside_blacklist_returns_false():
    assert not is_blacklisted("20260423", "binance_um", "BTCUSDT")
    assert not is_blacklisted("20260423", "coincheck", "BTC_JPY")
    assert not is_blacklisted("20260601", "binance_um", "BTCUSDT")


def test_wildcard_exchange_does_not_match_other_exchanges():
    """UM blacklist should NOT affect coincheck or BJ on same date."""
    assert not is_blacklisted("20260424", "coincheck", "BTC_JPY")
    # NB: the wildcard is only on symbol within UM, not across exchanges
    assert not is_blacklisted("20260424", "binance_jp", "BTCJPY")


def test_filter_days_drops_blacklisted():
    days = ["20260423", "20260424", "20260425", "20260429"]
    kept = filter_days(days, "binance_um", "BTCUSDT")
    assert kept == ["20260423", "20260429"]


def test_filter_days_passes_through_unrelated():
    days = ["20260424", "20260425", "20260426"]
    kept = filter_days(days, "coincheck", "BTC_JPY")
    assert kept == days  # CC not affected by UM blacklist


def test_all_entries_returns_copy():
    e1 = all_entries()
    e1.append(("forever", "test", "test", "tampered"))
    e2 = all_entries()
    assert ("forever", "test", "test", "tampered") not in e2


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
