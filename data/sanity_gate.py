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


BLACKLIST_VERSION = "v1.1"


# Known-bad windows. Each entry: (yyyymmdd, exchange, symbol, reason).
# Use "*" for wildcard.
_BLACKLIST: list[tuple[str, str, str, str]] = [
    # Binance UM aggTrade chain dead 04-24 to 04-28 (5 days).
    # Discovered by nyx 2026-05-06 cross-validation; was previously
    # documented as 04-24~26 in NARCI_INTEGRATION.md but the actual
    # window is wider.
    ("20260424", "binance_um", "*", "trades-dead (recorder bug, all UM symbols)"),
    ("20260425", "binance_um", "*", "trades-dead (recorder bug)"),
    ("20260426", "binance_um", "*", "trades-dead (recorder bug)"),
    ("20260427", "binance_um", "*", "trades-dead (recorder bug)"),
    ("20260428", "binance_um", "*", "trades-dead (recorder bug)"),

    # Binance JP BTCJPY 04-28: trades=2103 vs historical median ~37k (6%
    # of expected). Likely partial-day outage; also blacklist.
    ("20260428", "binance_jp", "BTCJPY",
     "trades count 2103 << median 37k (6% of expected); partial outage"),

    # Earlier sub-day issues (from cross-val analysis 2026-04-29):
    # 04-23: trade content deficit ~30% across BTC/BNB on Binance Vision.
    # Not blacklisted at day-level (still usable) but flagged for awareness.
    # 04-18: cross-day duplicates across symbols. Edge-dedup applied
    # downstream rather than blacklisting.
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
