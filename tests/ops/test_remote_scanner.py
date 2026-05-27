"""probe_aws._REMOTE_SCANNER 行为测试 + 与 analytics/gui/recorder_health 的 parity。

内嵌扫描器是 recorder_health.scan_freshness/scan_wal_backlog 的复刻（recorder 镜像
.dockerignore 剥了 analytics/，容器内 import 不到，只能内嵌）。这里钉死两者行为一致，
防复刻漂移。
"""
from __future__ import annotations

import json
import subprocess
import sys

from ops.probe_aws import _REMOTE_SCANNER
from analytics.gui.recorder_health import scan_freshness, scan_wal_backlog


def _make_rb(tmp_path):
    rt = tmp_path / "realtime" / "binance" / "spot" / "l2"
    rt.mkdir(parents=True)
    (rt / "BTCJPY_RAW_20260527_010000.parquet").write_bytes(b"x")
    (rt / "BTCJPY_RAW_20260527_010600.parquet").write_bytes(b"x")
    (rt / "ETHJPY_RAW_20260527_010000.parquet").write_bytes(b"x")
    (rt / "BTCJPY_DAILY.parquet").write_bytes(b"x")          # DAILY 应被排除
    wal = tmp_path / "wal" / "binance" / "spot" / "l2"
    wal.mkdir(parents=True)
    (wal / "BTCJPY_000001.segwal").write_bytes(b"x")
    (wal / "BTCJPY_000002.segwal").write_bytes(b"x")
    return tmp_path


def _run_scanner(rb) -> dict:
    r = subprocess.run([sys.executable, "-"], input=_REMOTE_SCANNER,
                       capture_output=True, text=True, env={"RB": str(rb)})
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_remote_scanner_basic(tmp_path):
    rb = _make_rb(tmp_path)
    out = _run_scanner(rb)
    fr = {r["symbol"]: r for r in out["freshness"]}
    assert set(fr) == {"BTCJPY", "ETHJPY"}                   # DAILY 不计
    assert fr["BTCJPY"]["n_shards"] == 2
    assert fr["BTCJPY"]["exchange"] == "binance" and fr["BTCJPY"]["market"] == "spot"
    wal = {r["symbol"]: r["pending_segments"] for r in out["wal"]}
    assert wal == {"BTCJPY": 2}


def test_parity_with_recorder_health(tmp_path):
    rb = _make_rb(tmp_path)
    out = _run_scanner(rb)
    ref_f = scan_freshness(str(rb / "realtime"))
    ref_w = scan_wal_backlog(str(rb / "wal"))
    # 比对 (exchange,market,symbol,n_shards) 集合一致
    key = lambda r: (r["exchange"], r["market"], r["symbol"], r["n_shards"])
    assert {key(r) for r in out["freshness"]} == {key(r) for r in ref_f}
    wkey = lambda r: (r["exchange"], r["market"], r["symbol"], r["pending_segments"])
    assert {wkey(r) for r in out["wal"]} == {wkey(r) for r in ref_w}
