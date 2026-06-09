"""ops.fleet 纯解析器测试。fixture = 本会话真实探针输出（2026-05-27 SG/JP）。"""
from __future__ import annotations

from ops import fleet

# 真实 docker compose ps 片段（SG redeploy 后 + 一个 PARKED 的 gmo + 一个崩环示例）
PS_TEXT = """\
narci-cloud-sync|Up 2 minutes|rclone/rclone:latest
narci-event-publisher|Restarting (1) 18 seconds ago|narci-event-publisher
narci-recorder-binance-spot|Up 2 minutes (healthy)|narci-recorder-binance-spot
narci-recorder-binance-umfut|Up 2 minutes (healthy)|narci-recorder-binance-umfut
narci-recorder-gmo-spot|Exited (0) 6 days ago|narci-recorder-gmo-spot
"""

FULL_STDOUT = """\
###COMMIT###
1bc2cdf
fix(gui): dashboard root_dir 上溯两级
###PS###
narci-recorder-binance-spot|Up 2 minutes (healthy)|narci-recorder-binance-spot
narci-recorder-gmo-spot|Exited (0) 6 days ago|narci-recorder-gmo-spot
###SCAN###
{"freshness": [{"exchange": "binance", "market": "spot", "symbol": "BTCJPY", "last_shard_ts": 1000000.0, "n_shards": 5}], "wal": [{"exchange": "binance", "market": "spot", "symbol": "BTCJPY", "pending_segments": 12}]}
###HEALTH###
port=8079 code=200
port=8080 code=200
"""


def test_split_sections():
    s = fleet.split_sections(FULL_STDOUT)
    assert set(s) == {"COMMIT", "PS", "SCAN", "HEALTH"}
    assert s["COMMIT"].startswith("1bc2cdf")


def test_parse_commit():
    c = fleet.parse_commit("1bc2cdf\nfix(gui): xxx")
    assert c == {"sha": "1bc2cdf", "subject": "fix(gui): xxx"}


def test_parse_ps_states():
    rows = fleet.parse_ps(PS_TEXT)
    by = {r["name"]: r["state"] for r in rows}
    assert by["narci-recorder-binance-spot"] == "healthy"
    assert by["narci-event-publisher"] == "restarting"
    assert by["narci-recorder-gmo-spot"] == "exited"     # PARKED
    assert by["narci-cloud-sync"] == "up"                 # 无 healthcheck


def test_parse_health():
    h = fleet.parse_health("port=8079 code=200\nport=8080 code=503")
    assert h[0] == {"port": 8079, "code": "200", "ok": True}
    assert h[1]["ok"] is False


def test_classify_freshness_fresh_stale_parked():
    now = 1_000_000.0
    rows = [
        {"exchange": "a", "symbol": "X", "last_shard_ts": now - 60},        # fresh
        {"exchange": "b", "symbol": "Y", "last_shard_ts": now - 2000},      # stale (>1500)
        {"exchange": "gmo", "symbol": "Z", "last_shard_ts": now - 8 * 86400},  # parked (>7d)
    ]
    out = {r["symbol"]: r["status"] for r in fleet.classify_freshness(rows, now=now)}
    assert out == {"X": "fresh", "Y": "stale", "Z": "parked"}


def test_parked_venues_from_exited():
    ps = fleet.parse_ps(
        "narci-recorder-gmo-spot|Exited (0) 6 days ago|img\n"
        "narci-recorder-coincheck|Up 1 hour (healthy)|img")
    assert fleet.parked_venues(ps) == {("gmo", "spot")}


def test_parked_venue_exempts_stale_false_positive():
    """GMO 5/21 park 后第 6 天:老 shard < 7d 本会判 stale,但容器 exited → 应豁免为 parked。"""
    now = 1_000_000.0
    rows = [{"exchange": "gmo", "market": "spot", "symbol": "BTC",
             "last_shard_ts": now - 6 * 86400}]   # 6 天前,<7d parked_sec
    # 不传 parked_venues → 误判 stale
    assert fleet.classify_freshness(rows, now=now)[0]["status"] == "stale"
    # 传入(容器 exited)→ 豁免 parked,不再 stale
    out = fleet.classify_freshness(rows, now=now, parked_venues={("gmo", "spot")})
    assert out[0]["status"] == "parked"
    assert fleet.summarize([], out)["overall"] != "RED"


def test_summarize_red_on_stale_or_bad():
    ps = fleet.parse_ps(PS_TEXT)
    fresh = fleet.classify_freshness(
        [{"exchange": "b", "symbol": "Y", "last_shard_ts": 0}], now=10**9)  # very stale
    summ = fleet.summarize(ps, fresh)
    assert summ["overall"] == "RED"                 # event-publisher restarting + stale
    assert "narci-event-publisher" in summ["bad_containers"]
    assert summ["n_parked"] == 1                    # gmo exited


def test_container_venue_mapping():
    assert fleet.container_venue("narci-recorder-binance-umfut") == ("binance", "um_futures")
    assert fleet.container_venue("narci-recorder-coincheck") == ("coincheck", "spot")
    assert fleet.container_venue("narci-recorder-bitflyer-fx") == ("bitflyer", "fx")
    # binance-jp 写到 exchange=binance_jp(不是 binance)—— "jp" 是 exchange 变体
    assert fleet.container_venue("narci-recorder-binance-jp") == ("binance_jp", "spot")
    assert fleet.container_venue("narci-recorder-binance-spot") == ("binance", "spot")
    assert fleet.container_venue("narci-cloud-sync") == (None, None)
    # 未登记 venue 走启发式兜底
    assert fleet.container_venue("narci-recorder-kraken-spot") == ("kraken", "spot")


def test_tile_health():
    fresh_stale = [{"exchange": "binance", "market": "um_futures", "symbol": "B", "status": "stale"}]
    fresh_ok = [{"exchange": "binance", "market": "um_futures", "symbol": "B", "status": "fresh"}]
    umfut = {"name": "narci-recorder-binance-umfut", "state": "healthy"}
    assert fleet.tile_health(umfut, fresh_ok) == "ok"
    assert fleet.tile_health(umfut, fresh_stale) == "bad"   # 容器健康但 venue 静默死
    assert fleet.tile_health({"name": "narci-recorder-gmo-spot", "state": "exited"}, []) == "parked"
    assert fleet.tile_health({"name": "narci-event-publisher", "state": "restarting"}, []) == "bad"
    assert fleet.tile_health({"name": "narci-cloud-sync", "state": "up"}, []) == "ok"


def test_summarize_splits_down_and_degraded():
    """restarting=宕机(down)、unhealthy=降级(degraded) 分开统计;n_bad 仍是合计。"""
    ps = fleet.parse_ps(
        "narci-recorder-coincheck|Up 3 hours (unhealthy)|img\n"
        "narci-event-publisher|Restarting (2) 5 seconds ago|img\n"
        "narci-recorder-binance-spot|Up 3 hours (healthy)|img")
    summ = fleet.summarize(ps, [])
    assert summ["n_down"] == 1 and summ["down_containers"] == ["narci-event-publisher"]
    assert summ["n_degraded"] == 1 and summ["degraded_containers"] == ["narci-recorder-coincheck"]
    assert summ["n_bad"] == 2                                   # 合计,向后兼容
    assert set(summ["bad_containers"]) == {"narci-event-publisher", "narci-recorder-coincheck"}
    assert summ["n_up"] == 1                                    # unhealthy 不计入 up
    assert summ["overall"] == "RED"                            # 有 restarting → RED


def test_summarize_degraded_only_is_degraded_not_red():
    """只有 unhealthy(在跑·探针红)、无 stale/无 restarting → DEGRADED 而非 RED。

    这正是 coincheck "Up (unhealthy)" 被误读成"崩了"的场景:容器在录,只是
    /health 503,应显"降级"不应显"需要关注(崩)"。"""
    ps = fleet.parse_ps(
        "narci-recorder-coincheck|Up 3 hours (unhealthy)|img\n"
        "narci-recorder-binance-spot|Up 3 hours (healthy)|img")
    # freshness 全新鲜(无 stale)
    fresh = fleet.classify_freshness(
        [{"exchange": "coincheck", "symbol": "btc_jpy", "last_shard_ts": 10**9 - 30}],
        now=10**9)
    summ = fleet.summarize(ps, fresh)
    assert summ["overall"] == "DEGRADED", summ
    assert summ["n_down"] == 0
    assert summ["n_degraded"] == 1
    assert summ["degraded_containers"] == ["narci-recorder-coincheck"]


def test_summarize_green_all_healthy():
    ps = fleet.parse_ps(
        "narci-recorder-a|Up 1 hour (healthy)|img\nnarci-recorder-b|Up 1 hour (healthy)|img")
    fresh = fleet.classify_freshness(
        [{"exchange": "x", "symbol": "S", "last_shard_ts": 10**9 - 30}], now=10**9)
    assert fleet.summarize(ps, fresh)["overall"] == "GREEN"
