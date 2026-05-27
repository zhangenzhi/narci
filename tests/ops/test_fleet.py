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


def test_summarize_red_on_stale_or_bad():
    ps = fleet.parse_ps(PS_TEXT)
    fresh = fleet.classify_freshness(
        [{"exchange": "b", "symbol": "Y", "last_shard_ts": 0}], now=10**9)  # very stale
    summ = fleet.summarize(ps, fresh)
    assert summ["overall"] == "RED"                 # event-publisher restarting + stale
    assert "narci-event-publisher" in summ["bad_containers"]
    assert summ["n_parked"] == 1                    # gmo exited


def test_summarize_green_all_healthy():
    ps = fleet.parse_ps(
        "narci-recorder-a|Up 1 hour (healthy)|img\nnarci-recorder-b|Up 1 hour (healthy)|img")
    fresh = fleet.classify_freshness(
        [{"exchange": "x", "symbol": "S", "last_shard_ts": 10**9 - 30}], now=10**9)
    assert fleet.summarize(ps, fresh)["overall"] == "GREEN"
