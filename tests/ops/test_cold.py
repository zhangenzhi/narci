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
    """每天的状态映射:落=1 / 真 gap=0 / lag(今/昨)=? / parked=p / corrupted=x
    / 上线前=-。"""
    rows = [{"exchange": "x", "market": "spot",
             "days": {"20260527", "20260525", "20260520"},
             "bad": {"20260524"}},
            {"exchange": "gmo", "market": "spot",
             "days": {"20260520"}, "bad": set()}]
    out = {(r["exchange"], r["market"]): r["history"]
           for r in fleet.classify_cold_history(
               rows, "20260528", parked_venues={("gmo", "spot")}, n_days=10)}
    # 10 天 0519→0528,today=0528(offset 0 missing=lag)、昨日=0527 已落。
    # 0519 早于该 venue 最早 DAILY(0520)→ '-' 上线前(非 gap),不再算红。
    assert out[("x", "spot")] == "-1000x101?", out[("x", "spot")]
    # gmo:有 0520 一天有 DAILY,其余皆 parked('p')(parked 优先于 '-')
    assert out[("gmo", "spot")] == "p1pppppppp", out[("gmo", "spot")]


def test_classify_cold_history_new_venue_no_false_gaps():
    """新接入 venue:上线前的窗口段是 '-'(非 gap),不被刷红 —— 修复 dashboard
    把 bitbank/bitflyer 这类近期上线 venue 整窗算"缺 N 天"的虚高。"""
    # venue 仅最近 3 天有 DAILY(0526/0527/0528),窗口 10 天。
    rows = [{"exchange": "bitbank", "market": "spot",
             "days": {"20260526", "20260527", "20260528"}, "bad": set()}]
    out = fleet.classify_cold_history(rows, "20260528", n_days=10)
    h = out[0]["history"]
    # 0519→0525 = 上线前(7 个 '-'),0526/0527/0528 = 已落('1')
    assert h == "-------111", h
    assert h.count("0") == 0, "上线前不应产生真 gap(红)"
    assert h.count("-") == 7
    # 完全无 DAILY 的 venue:无 floor,保持红/lag(让真正全缺仍显红)
    empty = fleet.classify_cold_history(
        [{"exchange": "dead", "market": "spot", "days": set(), "bad": set()}],
        "20260528", n_days=10)[0]["history"]
    assert "0" in empty and empty.count("-") == 0, empty


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
