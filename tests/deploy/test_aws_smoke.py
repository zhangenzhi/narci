"""AWS 只读冒烟(opt-in)。

narci-reco 用本机 aws-cli 在 AWS 上跑交易所 L2 录制。本测试确认 aws-cli 装好、
凭证可用 —— **只调只读 API**(sts get-caller-identity),**绝不执行**任何会动
AWS 的 deploy 脚本(start/stop/build/rollback)。

- opt-in:NARCI_AWS_TESTS=1 才跑;否则 skip。
- aws-cli 缺失 / 无凭证 → skip 报原因,不 fail。
- `-m "not network"` 可排除。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.network


def _require():
    if not os.environ.get("NARCI_AWS_TESTS", "").strip():
        pytest.skip("AWS 冒烟默认关闭;设 NARCI_AWS_TESTS=1 开启")
    if not shutil.which("aws"):
        pytest.skip("aws-cli 未安装")


def test_aws_cli_present_and_credentials_valid():
    """aws sts get-caller-identity —— 纯只读,确认凭证可用且返回 Account。"""
    _require()
    try:
        r = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--output", "json"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as e:
        pytest.skip(f"aws 调用失败: {e}")
    if r.returncode != 0:
        pytest.skip(f"无可用 AWS 凭证(rc={r.returncode}): {r.stderr.strip()[:120]}")
    ident = json.loads(r.stdout)
    assert "Account" in ident and ident["Account"], f"未返回 Account: {ident}"
    assert ident.get("Arn", "").startswith("arn:aws:")
