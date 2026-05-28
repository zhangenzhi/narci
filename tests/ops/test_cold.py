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


def test_parse_cold_with_days_csv():
    """2026-05 加的扩展字段:days_csv + bad_csv(向后兼容老 4 字段)。"""
    stdout = ("###COLD###\n"
              "binance/spot|20260527|3|0|20260525,20260526,20260527|\n"
              "gmo/spot|20260521|1|0|20260521|20260520\n")
    rows = {(r["exchange"], r["market"]): r for r in fleet.parse_cold(stdout)}
    assert rows[("binance", "spot")]["days"] == {"20260525", "20260526", "20260527"}
    assert rows[("binance", "spot")]["bad"] == set()
    assert rows[("gmo", "spot")]["bad"] == {"20260520"}


def test_classify_cold_history_red_lag_parked_corrupted():
    """每天的状态映射:落=1 / 真 gap=0 / lag(今/昨)=? / parked=p / corrupted=x。"""
    rows = [{"exchange": "x", "market": "spot",
             "days": {"20260527", "20260525", "20260520"},
             "bad": {"20260524"}},
            {"exchange": "gmo", "market": "spot",
             "days": {"20260520"}, "bad": set()}]
    out = {(r["exchange"], r["market"]): r["history"]
           for r in fleet.classify_cold_history(
               rows, "20260528", parked_venues={("gmo", "spot")}, n_days=10)}
    # 10 天 0519→0528,today=0528(offset 0 missing=lag)、昨日=0527 已落
    assert out[("x", "spot")] == "01000x101?", out[("x", "spot")]
    # gmo:有 0520 一天有 DAILY,其余皆 parked('p')
    assert out[("gmo", "spot")] == "p1pppppppp", out[("gmo", "spot")]


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
