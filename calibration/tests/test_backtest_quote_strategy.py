"""Unit tests for _compute_quote_price — the maker-price decision used
by backtest_alpha_model. Covers both strategies and the cross-spread
fallback. Per nyx 2026-05-13 OOS sweep, improve_1_tick is the binding
production strategy for CC echo."""

from __future__ import annotations

import pytest

from simulation.backtest_alpha import (
    QUOTE_STRATEGIES,
    _compute_quote_price,
    backtest_alpha_model,
)


def test_join_back_buy_posts_at_best_bid():
    p = _compute_quote_price("BUY", 11_777_000.0, 11_779_000.0,
                             "join_back", tick_size=1.0)
    assert p == 11_777_000.0


def test_join_back_sell_posts_at_best_ask():
    p = _compute_quote_price("SELL", 11_777_000.0, 11_779_000.0,
                             "join_back", tick_size=1.0)
    assert p == 11_779_000.0


def test_improve_1_tick_buy_posts_inside_spread():
    p = _compute_quote_price("BUY", 11_777_000.0, 11_779_000.0,
                             "improve_1_tick", tick_size=1.0)
    assert p == 11_777_001.0


def test_improve_1_tick_sell_posts_inside_spread():
    p = _compute_quote_price("SELL", 11_777_000.0, 11_779_000.0,
                             "improve_1_tick", tick_size=1.0)
    assert p == 11_778_999.0


def test_improve_1_tick_falls_back_when_spread_is_one_tick_buy():
    """Spread == tick → improving would land at best_ask (cross). Must
    fall back to best_bid, otherwise place_limit would reject the order
    as WOULD_CROSS_ASK."""
    p = _compute_quote_price("BUY", 11_777_000.0, 11_777_001.0,
                             "improve_1_tick", tick_size=1.0)
    assert p == 11_777_000.0


def test_improve_1_tick_falls_back_when_spread_is_one_tick_sell():
    p = _compute_quote_price("SELL", 11_777_000.0, 11_777_001.0,
                             "improve_1_tick", tick_size=1.0)
    assert p == 11_777_001.0


def test_improve_1_tick_respects_non_unit_tick():
    """tick_size != 1.0 (e.g. some exchanges use 0.5 / 5 / 10)."""
    p_buy = _compute_quote_price("BUY", 100.0, 110.0,
                                  "improve_1_tick", tick_size=5.0)
    assert p_buy == 105.0
    p_sell = _compute_quote_price("SELL", 100.0, 110.0,
                                   "improve_1_tick", tick_size=5.0)
    assert p_sell == 105.0


def test_unknown_strategy_raises():
    with pytest.raises(ValueError, match="unknown quote_strategy"):
        _compute_quote_price("BUY", 100.0, 101.0,
                             "front_of_book", tick_size=1.0)


def test_backtest_rejects_unknown_strategy_early():
    """The top-level backtest entry point also validates so a typo'd
    --quote-strategy fails fast instead of mid-replay."""
    with pytest.raises(ValueError, match="quote_strategy"):
        backtest_alpha_model(
            model_path="/dev/null",
            days=["20260420"],
            quote_strategy="improvee_1_tick",
        )


def test_quote_strategies_constant_is_complete():
    """If we add a new strategy literal, the constant must include it."""
    assert "join_back" in QUOTE_STRATEGIES
    assert "improve_1_tick" in QUOTE_STRATEGIES


if __name__ == "__main__":
    import sys
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
