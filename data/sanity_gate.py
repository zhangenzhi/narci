"""Known-bad data window blacklist.

When recorder bugs / venue outages produced corrupt or missing data, the
days are recorded here so nyx training pipelines can drop them
automatically. Public API:

    from narci.data.sanity_gate import is_blacklisted, filter_days

Each entry is a (date_str_yyyymmdd, exchange, symbol) tuple. Use "*" for
exchange or symbol to match any.

Versioning: BLACKLIST_VERSION bumps when entries are added or removed —
nyx pipelines should pin the version they trained against in their
manifest.json.notes for reproducibility.
"""

from __future__ import annotations


BLACKLIST_VERSION = "v1.2"


# Known-bad windows. Each entry: (yyyymmdd, exchange, symbol, reason).
# Use "*" for wildcard.
_BLACKLIST: list[tuple[str, str, str, str]] = [
    # Binance UM aggTrade chain dead 04-24 to 04-28 (5 days). Recorder
    # subscribed to /stream which silently dropped aggTrade after the
    # 2026-04-23 endpoint split. Vision-backfilled via
    # `data.backfill_vision_trades` (commits 2654f93 + 553a1f2). Entries
    # remain blacklisted because: (a) live depth recording also had
    # quality issues during this window, and (b) Vision aggTrade
    # aggregation count differs from the recorder's live count, so the
    # signal density is non-uniform vs neighboring days.
    ("20260424", "binance_um", "*", "trades-dead recorder bug; Vision-backfilled"),
    ("20260425", "binance_um", "*", "trades-dead recorder bug; Vision-backfilled"),
    ("20260426", "binance_um", "*", "trades-dead recorder bug; Vision-backfilled"),
    ("20260427", "binance_um", "*", "trades-dead recorder bug; Vision-backfilled"),
    ("20260428", "binance_um", "*", "trades-dead recorder bug; Vision-backfilled"),

    # Binance JP BTCJPY 04-28: trades=2103 vs historical median ~37k (6%
    # of expected). Likely partial-day outage; also blacklist.
    ("20260428", "binance_jp", "BTCJPY",
     "trades count 2103 << median 37k (6% of expected); partial outage"),

    # Binance UM aggTrade dead again 05-06 + 05-07 (recorder running on
    # /market/stream which delivers ONLY aggTrade — depth went silent;
    # save_loop hung 27h waiting for depth alignment). Dual-WS fix
    # `4582d02` deployed mid-05-07, hence both days caught the bug.
    # Vision aggTrades backfilled via `data.backfill_vision_trades` on
    # 2026-05-10 (commit pending). Day-level blacklist because depth
    # incremental coverage is also degraded for these days (UM 05-06
    # total rows 56M vs typical 90-120M; 05-07 only 12M rows).
    ("20260506", "binance_um", "*",
     "trades-dead pre dual-WS fix; Vision-backfilled; depth coverage also low"),
    ("20260507", "binance_um", "*",
     "trades-dead pre dual-WS fix; Vision-backfilled; depth coverage 12% of normal"),

    # Sub-day issues — flagged for awareness, NOT blacklisted at
    # day-level. nyx training pipelines should treat these days as
    # usable but downweight if needed.
    #
    # 04-18: cross-day duplicates across symbols. Edge-dedup applied
    # downstream rather than blacklisting.
    # 04-23: trade content deficit ~30% across BTC/BNB on Binance Vision
    # (cross-val 2026-04-29). nyx treats as OOS test day.
    # 05-02: ALL three venues simultaneously low (UM 40%, BJ 47%,
    # CC 35% of typical trades). Looks like real low-volume weekend, not
    # a recorder bug. Keep usable.
    # 05-08: CC trades 17K (50% of normal); BJ + UM normal. Single-venue
    # partial outage on CC; usable but watch for asymmetric bias if
    # multi-venue training.
    # 05-08 → 05-09: CC depth (orderbook) WS channel silently died at
    # 2026-05-08 15:06:13 UTC and resumed at 2026-05-09 06:19:35 UTC
    # (≈ 15h gap). Trades kept flowing throughout. Side-effect: the
    # 05-09 CC daily file starts at 05-08 15:06 with 15h of trade-only
    # rows before depth resumes. nyx LOO training still gets ~17h of
    # full-feature samples per day; but a backtest sliced into [first_ts,
    # first_ts + 4h] on 05-09 lands entirely in the broken window and
    # produces 0 finite predictions (CC L2 features all NaN). The
    # backtest_alpha _multi_venue_anchor_ts fix (max first_ts) ameliorates
    # the slice anchor but doesn't restore depth in the broken window.
    # Not day-blacklisted because the remaining 17h is usable; comment-
    # flagged for awareness. Recorder side: investigate silent CC depth
    # channel death (likely needs subscribe-ack watchdog like UM dual-WS).
]


def is_blacklisted(yyyymmdd: str, exchange: str, symbol: str) -> bool:
    """Return True if this (date, exchange, symbol) tuple is on the
    known-bad list."""
    for d, ex, sym, _ in _BLACKLIST:
        if d != yyyymmdd:
            continue
        if ex != "*" and ex != exchange:
            continue
        if sym != "*" and sym != symbol:
            continue
        return True
    return False


def reason(yyyymmdd: str, exchange: str, symbol: str) -> str:
    """Return the documented reason if blacklisted, empty string otherwise."""
    for d, ex, sym, r in _BLACKLIST:
        if d == yyyymmdd and (ex == "*" or ex == exchange) \
                and (sym == "*" or sym == symbol):
            return r
    return ""


def filter_days(days: list[str], exchange: str, symbol: str) -> list[str]:
    """Return only the days NOT blacklisted for (exchange, symbol).
    Convenience for training-data loading."""
    return [d for d in days if not is_blacklisted(d, exchange, symbol)]


def all_entries() -> list[tuple[str, str, str, str]]:
    """Return a copy of the full blacklist (for inspection / audit)."""
    return list(_BLACKLIST)
