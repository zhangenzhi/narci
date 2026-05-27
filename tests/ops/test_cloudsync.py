"""ops cloud-sync 推送腿日志解析/健康测试(fleet.parse_cloudsync / cloudsync_health)。

fixture = 真实 cloud-sync 容器日志格式(2026-05-27,docker logs --timestamps)。
"""
from __future__ import annotations

import datetime as dt

from ops import fleet

# 健康:三轮 syncing→done(rc=0),每轮 ~16min
CS_OK = """\
2026-05-27T05:06:52.6Z [cloud-sync] syncing...
2026-05-27T05:12:23.3Z 2026/05/27 NOTICE: foo.parquet: Duplicate object found - ignoring
2026-05-27T05:23:04.7Z [cloud-sync] done (rc=0)
2026-05-27T05:23:04.7Z [cloud-sync] sleeping 600s
2026-05-27T05:33:04.7Z [cloud-sync] syncing...
2026-05-27T05:49:53.5Z [cloud-sync] done (rc=0)
"""

NOW = dt.datetime(2026, 5, 27, 6, 0, 0, tzinfo=dt.timezone.utc).timestamp()


def test_parse_cloudsync_pairs_cycles():
    s = fleet.parse_cloudsync(CS_OK, now=NOW)
    assert s["n_cycles"] == 2
    assert s["last_rc"] == 0
    assert s["recent_rcs"] == [0, 0]
    assert s["in_progress"] is False
    # 第二轮 05:33:04 → 05:49:53 ≈ 1009s
    assert 1000 <= s["last_dur_sec"] <= 1020
    # 上次 done 05:49:53,now 06:00 → ~607s
    assert 600 <= s["last_done_age"] <= 615


def test_cloudsync_health_ok():
    assert fleet.cloudsync_health(fleet.parse_cloudsync(CS_OK, now=NOW)) == "ok"


def test_cloudsync_health_timeout_bad():
    s = fleet.parse_cloudsync(
        "2026-05-27T05:33:04.7Z [cloud-sync] syncing...\n"
        "2026-05-27T05:53:04.7Z [cloud-sync] done (rc=124)\n", now=NOW)
    assert s["last_rc"] == 124
    assert fleet.cloudsync_health(s) == "bad"      # rclone 超时卡住(2026-05-21 那类)


def test_cloudsync_health_stale():
    # 上次成功在很久前(>1h),且当前没在跑
    old = dt.datetime(2026, 5, 27, 3, 0, 0, tzinfo=dt.timezone.utc).timestamp()
    s = fleet.parse_cloudsync(
        "2026-05-27T02:40:00.0Z [cloud-sync] syncing...\n"
        "2026-05-27T02:55:00.0Z [cloud-sync] done (rc=0)\n", now=NOW)  # 3h+ 前
    assert fleet.cloudsync_health(s) == "stale"


def test_cloudsync_health_unknown_empty():
    assert fleet.cloudsync_health(fleet.parse_cloudsync("", now=NOW)) == "unknown"
