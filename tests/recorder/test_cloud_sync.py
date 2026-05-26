"""CloudSyncDaemon._sync_once 测试(rclone 命令构造 + 返回码处理)。

云同步是 IO 外壳:测可控的边界 —— rclone 命令怎么拼、各 returncode/异常如何
归类、本地目录缺失时不调 rclone。subprocess 全 mock,不跑真 rclone。
"""
from __future__ import annotations

import subprocess
import pytest

from recorder.cloud_sync import CloudSyncDaemon


def _daemon():
    return CloudSyncDaemon(local_dir="/tmp/narci_x", remote="gdrive:/narci_raw",
                           interval=300, transfers=4)


def test_missing_local_dir_skips_without_calling_rclone(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr("os.path.exists", lambda p: False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    assert _daemon()._sync_once() is False
    assert called["n"] == 0  # 目录不存在 → 根本不调 rclone


def test_rclone_command_construction(monkeypatch):
    captured = {}

    class _R:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["timeout"] = kw.get("timeout")
        return _R()

    monkeypatch.setattr("os.path.exists", lambda p: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _daemon()._sync_once() is True
    cmd = captured["cmd"]
    assert cmd[:2] == ["rclone", "copy"]
    assert "/tmp/narci_x" in cmd and "gdrive:/narci_raw" in cmd
    assert "--transfers" in cmd and "4" in cmd
    assert "--no-traverse" in cmd
    assert captured["timeout"] == 300  # 用 interval 作超时


def test_nonzero_returncode_is_false(monkeypatch):
    class _R:
        returncode = 1
        stderr = b"some rclone error"
    monkeypatch.setattr("os.path.exists", lambda p: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    assert _daemon()._sync_once() is False


def test_timeout_is_false(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="rclone", timeout=300)
    monkeypatch.setattr("os.path.exists", lambda p: True)
    monkeypatch.setattr(subprocess, "run", boom)
    assert _daemon()._sync_once() is False


def test_rclone_not_installed_is_false(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("rclone")
    monkeypatch.setattr("os.path.exists", lambda p: True)
    monkeypatch.setattr(subprocess, "run", boom)
    assert _daemon()._sync_once() is False  # rclone 缺失不崩,优雅返回 False
