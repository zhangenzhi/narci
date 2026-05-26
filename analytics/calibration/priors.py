"""v1 simulator priors.

Until echo runs the first calibration session, we have no real data to fit
the simulator parameters from. This module ships **prior estimates** based on
the cross-venue analysis narci already did (see notebooks under research/),
plus published market microstructure rules-of-thumb.

Any backtest result computed with `UNCALIBRATED=True` priors must be tagged
with `[⚠ UNCAL]` in its output. The replay pipeline strips this tag once
calibration data exists.

Sources for the v1 numbers (CC BTC_JPY, 4 days of cold tier data):
  - σ_mid_1s = 0.76 bps                    (measured)
  - spread median = 2.07 bps                (measured from L2 snapshot)
  - top-1 size median = 0.02 BTC            (measured)
  - trade rate = 0.35 per second             (measured)
  - adverse 1s mean baseline ≈ 0.55 bps     (rough; literature + intuition)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

# Priors version — bump when defaults change (so caller can detect stale).
PRIORS_VERSION = "v1.0"


@dataclass(slots=True)
class CalibrationParams:
    """One symbol's simulator parameters.

    Per-venue/symbol; instantiate with priors then mutate via fitting.
    """

    # ---- identity ----
    exchange: str
    symbol: str

    # ---- adverse selection (bps; positive = bad for maker) ----
    # E[ post_fill_mid_move × fill_side ] — measured in bps
    adverse_baseline_500ms_bps: float = 0.30
    adverse_baseline_1s_bps:    float = 0.55
    adverse_baseline_5s_bps:    float = 0.45
    adverse_baseline_30s_bps:   float = 0.20    # mean reverts somewhat

    # ---- queue model ----
    queue_scaling: float = 1.0          # multiplier on inferred ahead_qty (1.0 = take inferred at face)
    queue_arrival_top_of_book: bool = True   # initial queue position assumed at very back

    # ---- cancel latency (ms) — applied as gaussian-ish jitter ----
    cancel_latency_p50_ms: float = 200.0
    cancel_latency_p95_ms: float = 500.0
    cancel_latency_p99_ms: float = 1500.0

    # ---- fill probability per second when at top-of-book (heuristic) ----
    # used only as fallback when no real trade-arrival rate available
    fill_probability_per_sec_at_top: float = 0.05   # 5%/sec implies ~150 fills/hour

    # ---- spread / trade microstructure baseline (for sanity check) ----
    spread_median_bps: float = 2.0
    sigma_mid_1s_bps: float = 0.76
    trade_arrival_rate_per_sec: float = 0.35
    top1_size_median: float = 0.02

    # ---- post-only / fee model ----
    fee_maker_bps: float = 0.0          # CC = 0
    fee_taker_bps: float = 0.0          # CC = 0
    post_only_supported: bool = False   # CC: emulated by caller

    # ---- meta ----
    UNCALIBRATED: bool = True
    last_calibrated_session: str = ""    # session_id of latest calibration that touched this
    notes: str = ""


# ---------------------------------------------------------------- #
# Default params per (exchange, symbol)
# ---------------------------------------------------------------- #


COINCHECK_BTC_JPY = CalibrationParams(
    exchange="coincheck",
    symbol="BTC_JPY",
    notes=(
        "v1 priors derived from narci cold tier 4-day analysis (2026-04-20..23). "
        "Adverse baseline is rough — literature + 0.55 bps lit-style estimate. "
        "Replace with real measurements after first echo session."
    ),
)

COINCHECK_ETH_JPY = replace(
    COINCHECK_BTC_JPY,
    symbol="ETH_JPY",
    # ETH on CC: spread ~10 bps, lower flow; needs different priors
    adverse_baseline_1s_bps=0.40,
    adverse_baseline_5s_bps=0.35,
    spread_median_bps=10.0,
    sigma_mid_1s_bps=1.5,                 # higher relative vol on smaller pair
    trade_arrival_rate_per_sec=0.05,
    top1_size_median=0.5,                 # ETH units
    fill_probability_per_sec_at_top=0.02,
    notes="v1 priors; CC ETH liquidity ~1/4 of BTC, much wider spread.",
)

# Binance JP (BTCJPY spot — has fees, different microstructure)
BINANCE_JP_BTCJPY = CalibrationParams(
    exchange="binance_jp",
    symbol="BTCJPY",
    adverse_baseline_500ms_bps=0.40,
    adverse_baseline_1s_bps=0.65,        # binance cross-venue MM are faster
    adverse_baseline_5s_bps=0.50,
    spread_median_bps=1.0,                # tighter than CC
    sigma_mid_1s_bps=0.7,
    trade_arrival_rate_per_sec=0.6,
    top1_size_median=0.018,
    fee_maker_bps=10.0,                   # PLACEHOLDER — verify with binance JP fee schedule
    fee_taker_bps=10.0,
    post_only_supported=True,
    notes="Fees are placeholders — confirm before any non-zero-fee analysis.",
)


# Index keyed by (exchange, symbol) for quick lookup
_DEFAULTS: dict[tuple[str, str], CalibrationParams] = {
    ("coincheck", "BTC_JPY"):  COINCHECK_BTC_JPY,
    ("coincheck", "ETH_JPY"):  COINCHECK_ETH_JPY,
    ("binance_jp", "BTCJPY"):  BINANCE_JP_BTCJPY,
}


def get_priors(exchange: str, symbol: str) -> CalibrationParams:
    """Return v1 priors for (exchange, symbol). Falls back to a CC BTC_JPY
    template with renamed symbol if exact match missing — explicit warning
    via UNCALIBRATED flag stays True."""
    key = (exchange.lower(), symbol.upper())
    if key in _DEFAULTS:
        return replace(_DEFAULTS[key])
    # fallback: clone CC BTC defaults, relabel
    fallback = replace(
        COINCHECK_BTC_JPY,
        exchange=exchange,
        symbol=symbol,
        notes=f"NO PRIOR FOR {exchange}/{symbol} — using CC BTC_JPY template; very rough.",
    )
    return fallback


def all_defaults() -> Iterable[CalibrationParams]:
    """Yield a copy of every preset."""
    for p in _DEFAULTS.values():
        yield replace(p)


# ---------------------------------------------------------------- #
# UNCALIBRATED tagging helpers
# ---------------------------------------------------------------- #


def tag_result(result_str: str, params: CalibrationParams | Iterable[CalibrationParams]) -> str:
    """Prepend [⚠ UNCAL] to result_str if any of params is uncalibrated.
    Use this on every backtest report / printed PnL."""
    if isinstance(params, CalibrationParams):
        params = [params]
    if any(p.UNCALIBRATED for p in params):
        return "[⚠ UNCAL] " + result_str
    return result_str


def assert_calibrated(*params: CalibrationParams) -> None:
    """Raise if any param set is still uncalibrated. Use this in production-
    sensitive code paths that should refuse to run on priors only."""
    bad = [p for p in params if p.UNCALIBRATED]
    if bad:
        names = ", ".join(f"{p.exchange}/{p.symbol}" for p in bad)
        raise RuntimeError(
            f"refusing to run on uncalibrated priors: {names}. "
            "Run a calibration session first or pass --allow-uncalibrated."
        )
