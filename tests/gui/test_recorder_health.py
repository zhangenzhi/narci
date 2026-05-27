"""录制健康数据层测试(analytics/gui/recorder_health.py)。"""
from __future__ import annotations

import json
import os
import time

from analytics.gui import recorder_health as rh


def _touch(path, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    if mtime is not None:
        os.utime(path, (mtime, mtime))


# ----------------------------- freshness ----------------------------- #

def test_freshness_fresh_vs_stale(tmp_path):
    now = 1_000_000.0
    rt = tmp_path / "realtime"
    # 新鲜(age 10s)
    _touch(str(rt / "binance" / "um_futures" / "l2" / "BTCUSDT_RAW_20260517_120000.parquet"), now - 10)
    # 陈旧(age 1000s > 300)
    _touch(str(rt / "coincheck" / "spot" / "l2" / "BTC_JPY_RAW_20260517_120000.parquet"), now - 1000)
    rows = rh.scan_freshness(str(rt), now=now, stale_sec=300)
    by = {(r["exchange"], r["symbol"]): r for r in rows}
    assert by[("binance", "BTCUSDT")]["status"] == "fresh"
    assert by[("coincheck", "BTC_JPY")]["status"] == "stale"
    # 陈旧的排在前(便于一眼看见)
    assert rows[0]["status"] == "stale"


def test_freshness_counts_shards_and_takes_latest(tmp_path):
    now = 1_000_000.0
    d = tmp_path / "realtime" / "binance" / "spot" / "l2"
    _touch(str(d / "ETHUSDT_RAW_20260517_120000.parquet"), now - 500)
    _touch(str(d / "ETHUSDT_RAW_20260517_121000.parquet"), now - 30)   # 最新
    r = rh.scan_freshness(str(tmp_path / "realtime"), now=now, stale_sec=300)[0]
    assert r["n_shards"] == 2
    assert r["age_sec"] == 30.0 and r["status"] == "fresh"


def test_freshness_excludes_daily(tmp_path):
    now = 1_000_000.0
    d = tmp_path / "realtime" / "binance" / "spot" / "l2"
    _touch(str(d / "ETHUSDT_RAW_20260517_DAILY.parquet"), now - 5)  # 应被忽略
    assert rh.scan_freshness(str(tmp_path / "realtime"), now=now) == []


def test_freshness_missing_root():
    assert rh.scan_freshness("/no/such/dir") == []


# ----------------------------- gap reports ----------------------------- #

def test_gap_reports_parsed(tmp_path):
    cold = tmp_path / "cold" / "coincheck" / "spot"
    os.makedirs(cold)
    rep = {"symbol": "BTC_JPY", "date": "2026-05-17", "total_gaps": 2,
           "streams": {"depth": {"stats": {"coverage_pct": 87.5, "n_gaps": 2, "max_gap_sec": 120.0}},
                       "trade": {"stats": {"coverage_pct": 99.0, "n_gaps": 0, "max_gap_sec": 0.0}}}}
    (cold / "BTC_JPY_GAPS_20260517.json").write_text(json.dumps(rep))
    rows = rh.scan_gap_reports(str(tmp_path / "cold"))
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "BTC_JPY" and r["date"] == "2026-05-17"
    assert r["depth_coverage_pct"] == 87.5 and r["depth_max_gap_sec"] == 120.0
    assert r["trade_n_gaps"] == 0


def test_gap_reports_missing_root():
    assert rh.scan_gap_reports("/no/such") == []


# ----------------------------- wal backlog ----------------------------- #

def test_wal_backlog_counts(tmp_path):
    w = tmp_path / "wal" / "binance" / "um_futures" / "l2"
    for seq in range(3):
        _touch(str(w / f"BTCUSDT_{seq:06d}.segwal"))
    _touch(str(w / "ETHUSDT_000000.segwal"))
    rows = rh.scan_wal_backlog(str(tmp_path / "wal"))
    by = {r["symbol"]: r["pending_segments"] for r in rows}
    assert by["BTCUSDT"] == 3 and by["ETHUSDT"] == 1
    assert rows[0]["symbol"] == "BTCUSDT"  # 多的排前


def test_wal_backlog_empty(tmp_path):
    assert rh.scan_wal_backlog(str(tmp_path / "wal")) == []


# ----------------------------- summary ----------------------------- #

def test_health_summary():
    fresh = [{"exchange": "binance", "market": "spot", "symbol": "BTCUSDT", "status": "fresh"}]
    stale = fresh + [{"exchange": "coincheck", "market": "spot", "symbol": "BTC_JPY", "status": "stale"}]
    assert rh.health_summary([])["overall"] == "EMPTY"
    assert rh.health_summary(fresh)["overall"] == "GREEN"
    s = rh.health_summary(stale)
    assert s["overall"] == "RED" and s["n_stale"] == 1
    assert "coincheck/spot/BTC_JPY" in s["stale"]
