"""donor `binance_vision_push.sh` 推送后 prune 本地暂存 —— 行为 + 顺序回归测试。

背景(2026-06-09 事故):donor 下载 Binance Vision T-1 到 `official_validation`
后用 `rclone copy`(只上传不删本地)推 gdrive,且**无任何 cleanup**;下载又靠
「本地已存在跳过重复」—— 两者叠加 = 无界堆盘。实测攒到 5.6G,把 aws-sg 30G 根盘
顶到 93%,两个 binance recorder /health 因 `disk_usage>90%` 持续 503。
修复(42c4217)在 `rclone copy` 之后加了一段按 mtime 清暂存的 prune。

本测试守这个修复。`tests/deploy` 的铁律是「绝不执行脚本(脚本会真动 AWS)」——
这里**唯一的例外**,且安全:把脚本里两个外部副作用(`python main.py download`
和 `rclone copy`)全 stub 成 no-op(PYTHON=true + PATH 注入假 rclone),NARCI_DIR
指向 tmp,DRIVE_REMOTE 也指 tmp 本地路径做兜底 —— 脚本除了在 tmp 里跑 `find -delete`
外不碰任何网络/AWS/真实数据。跑**真脚本文件本身**(而非复制 find 命令)才能在
narci 误删/改坏 prune 表达式时真的失败 —— 静态「含 -delete」断言挡不住
`-mtime +1` 写成 `-mtime -1`(删反了)这类回归。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "reco" / "donor" / "binance_vision_push.sh"
DAY = 86400


def _touch(path: Path, age_days: float) -> None:
    """建文件并把 mtime/atime 设成 age_days 天前(用于驱动 `find -mtime`)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    t = time.time() - age_days * DAY
    os.utime(path, (t, t))


def _run_push(tmp: Path, retain_days: int) -> subprocess.CompletedProcess:
    """以全 stub 环境跑真脚本:python→true、rclone→no-op、目录全在 tmp。"""
    # 假 rclone:忽略所有参数直接 exit 0(脚本里的 `rclone copy ...` 变 no-op)
    bindir = tmp / "fakebin"
    bindir.mkdir()
    fake_rclone = bindir / "rclone"
    fake_rclone.write_text("#!/bin/sh\nexit 0\n")
    fake_rclone.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["NARCI_DIR"] = str(tmp)
    env["PYTHON"] = "true"  # `true main.py download ...` → 忽略参数,exit 0
    env["DRIVE_REMOTE"] = str(tmp / "fake_remote")  # 兜底:即便假 rclone 没命中也只落本地
    env["DONOR_RETAIN_DAYS"] = str(retain_days)
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


@pytest.mark.skipif(not shutil.which("bash"), reason="bash 不可用")
def test_prune_deletes_old_keeps_recent(tmp_path: Path):
    """retain=1d:>1 天的暂存被删,近 1 天的留下(核心防回归 —— 不再无界堆盘)。"""
    off = tmp_path / "replay_buffer" / "official_validation"
    old = off / "spot" / "aggTrades" / "BTCUSDT" / "BTCUSDT-2026-04-17.parquet"
    recent = off / "spot" / "aggTrades" / "BTCUSDT" / "BTCUSDT-2026-06-09.parquet"
    _touch(old, age_days=5)
    _touch(recent, age_days=0)

    r = _run_push(tmp_path, retain_days=1)

    assert r.returncode == 0, f"脚本非 0 退出:\n{r.stdout}\n{r.stderr}"
    assert not old.exists(), "旧暂存应被 prune 删除(无界堆盘 bug 回归)"
    assert recent.exists(), "近 retain 窗口内的暂存不应被删"


@pytest.mark.skipif(not shutil.which("bash"), reason="bash 不可用")
def test_prune_removes_emptied_dirs(tmp_path: Path):
    """删空一个 symbol 目录后,空目录本身也应被清(否则残留空目录树)。"""
    off = tmp_path / "replay_buffer" / "official_validation"
    stale_dir = off / "um_futures" / "aggTrades" / "OLDCOIN"
    _touch(stale_dir / "OLDCOIN-2026-04-17.parquet", age_days=10)
    # 另留一个近文件,确保 official_validation 根不被整棵删掉(模拟真实 retain 行为)
    _touch(off / "spot" / "aggTrades" / "BTCUSDT" / "BTCUSDT-2026-06-09.parquet", age_days=0)

    r = _run_push(tmp_path, retain_days=1)

    assert r.returncode == 0, f"脚本非 0 退出:\n{r.stdout}\n{r.stderr}"
    assert not stale_dir.exists(), "清空后的空目录应被 `-type d -empty -delete` 移除"
    assert off.exists(), "仍有近文件时 official_validation 根目录不应被删"


@pytest.mark.skipif(not shutil.which("bash"), reason="bash 不可用")
def test_prune_respects_retain_days_env(tmp_path: Path):
    """DONOR_RETAIN_DAYS 可调:retain=7 时 5 天前的文件应保留(env 覆盖生效)。"""
    off = tmp_path / "replay_buffer" / "official_validation"
    f = off / "spot" / "aggTrades" / "ETHUSDT" / "ETHUSDT-2026-06-04.parquet"
    _touch(f, age_days=5)

    r = _run_push(tmp_path, retain_days=7)

    assert r.returncode == 0, f"脚本非 0 退出:\n{r.stdout}\n{r.stderr}"
    assert f.exists(), "retain=7 时 5 天前的暂存应保留 —— retain 窗口未生效"


def test_prune_runs_after_push():
    """顺序铁律:prune 必须在 `rclone copy` **之后** —— 推之前删 = 丢未上传数据。"""
    txt = SCRIPT.read_text()
    i_push = txt.find("rclone copy")
    i_prune = txt.find("-mtime")
    assert i_push != -1, "脚本里找不到 rclone copy(推送步骤丢了?)"
    assert i_prune != -1, "脚本里找不到 prune 的 -mtime(清理步骤丢了 = 堆盘 bug 回归)"
    assert i_push < i_prune, "prune 出现在 rclone copy 之前 —— 会删掉尚未推送的暂存"
