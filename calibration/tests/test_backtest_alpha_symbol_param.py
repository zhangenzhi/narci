"""Regression test for backtest_alpha symbol parameterization (echo D10).

Pre-fix `backtest_alpha._stream_days` was hardcoded to BTC venue parquet
paths via module-level VENUE_SOURCES. Calling
`backtest_alpha_model(..., symbol="ETH_JPY")` silently opened BTC files
(broker had ETH priors but the event stream was BTC events) — predictions
were garbage.

Post-fix `VENUE_SOURCES_BY_SYMBOL` map + `symbol`/`venue_symbol` threaded
through `_stream_days` / `_multi_venue_*` so a non-BTC binding routes to
its own cold-tier parquet universe.

These tests don't run the full backtest engine (too slow + needs binding
weights present); they verify the routing primitives directly.
"""

from __future__ import annotations

import unittest

from simulation.backtest_alpha import (
    VENUE_SOURCES, VENUE_SOURCES_BY_SYMBOL,
    _multi_venue_first_tss, _multi_venue_anchor_ts, _stream_days,
)


class TestVenueRouting(unittest.TestCase):

    def test_venue_sources_map_has_btc_and_eth(self):
        self.assertIn("BTC_JPY", VENUE_SOURCES_BY_SYMBOL)
        self.assertIn("ETH_JPY", VENUE_SOURCES_BY_SYMBOL)
        # 4 venues each
        self.assertEqual(len(VENUE_SOURCES_BY_SYMBOL["BTC_JPY"]), 4)
        self.assertEqual(len(VENUE_SOURCES_BY_SYMBOL["ETH_JPY"]), 4)
        # ETH CC entry must point at ETH_JPY symbol (not BTC_JPY)
        cc_eth = next(v for v in VENUE_SOURCES_BY_SYMBOL["ETH_JPY"]
                       if v[3] == "cc")
        self.assertEqual(cc_eth[2], "ETH_JPY")

    def test_legacy_alias_is_btc(self):
        """Backward compat: bare `VENUE_SOURCES` import still points at BTC."""
        self.assertEqual(VENUE_SOURCES, VENUE_SOURCES_BY_SYMBOL["BTC_JPY"])

    def test_first_tss_routes_by_symbol(self):
        """Smoke against actual cold tier: ETH and BTC return ETH_JPY /
        BTC_JPY first-event timestamps respectively. Day 20260517 has cold
        tier for both per recent compact runs.

        Data-dependent: cold tier lives outside the repo. Skip (not fail)
        when 20260517 isn't present — mirrors
        test_stream_days_yields_eth_events_for_eth_symbol's guard."""
        btc = _multi_venue_first_tss("20260517", symbol="BTC_JPY")
        eth = _multi_venue_first_tss("20260517", symbol="ETH_JPY")
        if "cc" not in btc or "cc" not in eth:
            self.skipTest(f"cold tier 0517 missing cc venue; btc={btc} eth={eth}")
        # Each should have at least the `cc` venue (CC is the smallest /
        # most-likely-available file)
        self.assertIn("cc", btc, f"BTC should find cc venue; got {btc}")
        self.assertIn("cc", eth, f"ETH should find cc venue; got {eth}")
        # The first-event timestamps are NOT identical between BTC and ETH
        # (different markets, different first events). If they were equal
        # something is wrong with venue routing.
        self.assertNotEqual(btc["cc"], eth["cc"],
            f"BTC and ETH cc first_ts unexpectedly equal: btc={btc['cc']} "
            f"eth={eth['cc']}; venue routing likely broken (both reading "
            f"same parquet?)")

    def test_anchor_ts_routes_by_symbol(self):
        # Data-dependent (see test_first_tss_routes_by_symbol): skip when
        # cold tier 20260517 isn't present in this checkout.
        anchor_btc = _multi_venue_anchor_ts("20260517", symbol="BTC_JPY")
        anchor_eth = _multi_venue_anchor_ts("20260517", symbol="ETH_JPY")
        if anchor_btc is None or anchor_eth is None:
            self.skipTest("cold tier 0517 absent → anchor ts None")
        self.assertIsNotNone(anchor_btc)
        self.assertIsNotNone(anchor_eth)

    def test_stream_days_yields_eth_events_for_eth_symbol(self):
        """Pull first ~50 events from each universe and verify ETH stream
        prices are in ETH ballpark (~350k JPY) while BTC stream is in BTC
        ballpark (~10M JPY) — distinct enough to prove routing."""
        # 0.1h = 6 min; ETH has ~5× lower trade frequency than BTC, this
        # is enough to clear ~15 CC trades for both symbols.
        btc_iter = _stream_days(["20260517"], max_hours=0.1, symbol="BTC_JPY")
        eth_iter = _stream_days(["20260517"], max_hours=0.1, symbol="ETH_JPY")

        btc_prices = []
        eth_prices = []
        for ts_ms, _neg, venue, side, price, qty in btc_iter:
            if venue == "cc" and side == 2:
                btc_prices.append(price)
            if len(btc_prices) >= 10:
                break
        for ts_ms, _neg, venue, side, price, qty in eth_iter:
            if venue == "cc" and side == 2:
                eth_prices.append(price)
            if len(eth_prices) >= 10:
                break

        if not btc_prices or not eth_prices:
            self.skipTest("cold tier 0517 missing one or both symbols")

        btc_mid = sum(btc_prices) / len(btc_prices)
        eth_mid = sum(eth_prices) / len(eth_prices)
        # ETH JPY price << BTC JPY price by a factor of ~30. As a loose
        # sanity ETH should be < BTC / 5.
        self.assertLess(eth_mid, btc_mid / 5,
            f"venue routing likely broken: btc_mid={btc_mid:.0f}, "
            f"eth_mid={eth_mid:.0f}. ETH should be ~30× smaller than BTC.")


if __name__ == "__main__":
    unittest.main()
