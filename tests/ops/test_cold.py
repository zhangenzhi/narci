"""ops cold-tier 落地解析/分类测试(fleet.parse_cold / classify_cold / cold_overall）。"""
from __future__ import annotations

from ops import fleet

COLD_STDOUT = """\
###COLD###
binance_jp/spot|20260526|81|0
binance/spot|20260526|90|0
binance/um_futures|20260525|60|0
gmo/spot|20260521|30|0
gmo/leverage||0|0
"""


def test_parse_cold():
    rows = {(r["exchange"], r["market"]): r for r in fleet.parse_cold(COLD_STDOUT)}
    assert rows[("binance_jp", "spot")]["latest_daily"] == "20260526"
    assert rows[("binance_jp", "spot")]["n_daily"] == 81
    assert rows[("gmo", "leverage")]["latest_daily"] is None     # 空 → None


def test_classify_cold_statuses():
    rows = fleet.parse_cold(COLD_STDOUT)
    out = {(r["exchange"], r["market"]): r["status"]
           for r in fleet.classify_cold(rows, "20260527", parked_venues={("gmo", "spot")})}
    assert out[("binance_jp", "spot")] == "ok"        # 昨日(0526)已落
    assert out[("binance", "um_futures")] == "lag"    # 0525,落后 1 天
    assert out[("gmo", "spot")] == "parked"           # AWS 容器 exited → 豁免
    assert out[("gmo", "leverage")] == "missing"      # 无 DAILY 且未 parked
    # 未传 parked 时 gmo/spot(0521,落后5天)= stale
    out2 = {(r["exchange"], r["market"]): r["status"]
            for r in fleet.classify_cold(rows, "20260527")}
    assert out2[("gmo", "spot")] == "stale"


def test_cold_overall():
    rows = fleet.classify_cold(fleet.parse_cold(COLD_STDOUT), "20260527",
                               parked_venues={("gmo", "spot"), ("gmo", "leverage")})
    # 全 active venue 都 ok/lag(gmo 豁免)→ 有 lag → AMBER
    assert fleet.cold_overall(rows) == "AMBER"
    # 全 ok 的场景
    allok = fleet.classify_cold(
        [{"exchange": "x", "market": "spot", "latest_daily": "20260526", "n_daily": 1, "n_gaps": 0}],
        "20260527")
    assert fleet.cold_overall(allok) == "GREEN"
