"""Priors module tests."""

from __future__ import annotations

import dataclasses as dc
import sys

from calibration import priors as P


def test_priors_version_set():
    assert isinstance(P.PRIORS_VERSION, str)
    assert P.PRIORS_VERSION.startswith("v")


def test_known_pairs_resolve():
    cc_btc = P.get_priors("coincheck", "BTC_JPY")
    assert cc_btc.exchange == "coincheck"
    assert cc_btc.symbol == "BTC_JPY"
    assert cc_btc.UNCALIBRATED is True
    assert cc_btc.spread_median_bps == 2.0


def test_eth_has_different_spread_than_btc():
    btc = P.get_priors("coincheck", "BTC_JPY")
    eth = P.get_priors("coincheck", "ETH_JPY")
    assert eth.spread_median_bps > btc.spread_median_bps
    assert eth.trade_arrival_rate_per_sec < btc.trade_arrival_rate_per_sec


def test_unknown_pair_returns_fallback():
    p = P.get_priors("kraken", "XRPUSD")
    assert p.UNCALIBRATED is True
    assert p.exchange == "kraken"
    assert p.symbol == "XRPUSD"
    assert "NO PRIOR" in p.notes


def test_get_priors_returns_copy():
    """Mutating returned params shouldn't affect default for next call."""
    p1 = P.get_priors("coincheck", "BTC_JPY")
    p1.adverse_baseline_1s_bps = 999.0
    p2 = P.get_priors("coincheck", "BTC_JPY")
    assert p2.adverse_baseline_1s_bps == 0.55


def test_tag_result_prepends_when_uncalibrated():
    p = P.get_priors("coincheck", "BTC_JPY")
    out = P.tag_result("daily PnL: $100", p)
    assert out.startswith("[⚠ UNCAL]")


def test_tag_result_passes_when_calibrated():
    p = P.get_priors("coincheck", "BTC_JPY")
    p.UNCALIBRATED = False
    out = P.tag_result("daily PnL: $100", p)
    assert not out.startswith("[")


def test_assert_calibrated_raises_on_priors():
    p = P.get_priors("coincheck", "BTC_JPY")
    try:
        P.assert_calibrated(p)
        raise AssertionError("should have raised")
    except RuntimeError as e:
        assert "uncalibrated" in str(e).lower()


def test_all_defaults_iterable():
    items = list(P.all_defaults())
    assert len(items) >= 3
    assert all(isinstance(p, P.CalibrationParams) for p in items)


def test_calibration_params_dataclass_serializable():
    p = P.get_priors("coincheck", "BTC_JPY")
    d = dc.asdict(p)
    assert d["exchange"] == "coincheck"
    assert d["UNCALIBRATED"] is True


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
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(failed)
