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
from pathlib import Path

import pytest

pytestmark = pytest.mark.network

REPO = Path(__file__).resolve().parents[2]
RECO_ENV = REPO / "deploy" / "reco" / ".env"


def _require():
    if not os.environ.get("NARCI_AWS_TESTS", "").strip():
        pytest.skip("AWS 冒烟默认关闭;设 NARCI_AWS_TESTS=1 开启")
    if not shutil.which("aws"):
        pytest.skip("aws-cli 未安装")


def _env(key: str) -> str | None:
    """先看进程环境,再回退 deploy/reco/.env(部署参数的实际所在,gitignored)。"""
    v = os.environ.get(key, "").strip()
    if v:
        return v
    if RECO_ENV.is_file():
        for line in RECO_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, val = line.partition("=")
            if k.strip() == key:
                return val.strip().strip('"').strip("'") or None
    return None


def _aws_json(args: list[str]):
    try:
        r = subprocess.run(["aws", *args, "--output", "json"],
                           capture_output=True, text=True, timeout=25)
    except Exception as e:
        pytest.skip(f"aws 调用失败: {e}")
    if r.returncode != 0:
        pytest.skip(f"aws {args[0]} {args[1]} rc={r.returncode}: {r.stderr.strip()[:140]}")
    return json.loads(r.stdout) if r.stdout.strip() else None


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


def test_sg_instance_iam_role_resolvable():
    """aws-sg 实例的 IAM 角色可解析 —— donor 的 cloudwatch:PutMetricData 要授到**这个实例
    角色**(不是 narci-reco-policy.json 那个控制面策略)。部署时实测角色 = narci-recorder-ssm。

    把这条固化:IAM apply 的目标 = SG 实例 InstanceProfile 绑定的 role,避免每次现查。
    只读 describe-instances / get-instance-profile。需 NARCI_SG_INSTANCE_ID(进程环境或
    deploy/reco/.env)。
    """
    _require()
    iid = _env("NARCI_SG_INSTANCE_ID")
    if not iid:
        pytest.skip("未配置 NARCI_SG_INSTANCE_ID(进程环境 / deploy/reco/.env)")
    region = _env("AWS_REGION_SG") or "ap-southeast-1"
    d = _aws_json(["ec2", "describe-instances", "--instance-ids", iid, "--region", region,
                   "--query", "Reservations[].Instances[].IamInstanceProfile.Arn"])
    assert d and d[0], f"SG 实例 {iid} 未绑定 InstanceProfile → donor 无角色可授 PutMetricData"
    profile_name = d[0].rsplit("/", 1)[-1]
    roles = _aws_json(["iam", "get-instance-profile", "--instance-profile-name", profile_name,
                       "--query", "InstanceProfile.Roles[].RoleName"])
    assert roles and roles[0], f"InstanceProfile {profile_name} 未绑定 role"


def test_rclone_remote_configured():
    """cloud-sync 推送的 rclone 远端已配置且是 remote:path 形态 —— wal/ 排除验证
    (rclone lsf <remote> --include 'wal/**' 应为空)针对的就是这个 remote。部署时实测
    = gdrive:/narci_raw。需 NARCI_RCLONE_REMOTE(进程环境或 deploy/reco/.env;含 host .env 时
    本测试在控制机可能 skip)。"""
    remote = _env("NARCI_RCLONE_REMOTE")
    if not remote:
        pytest.skip("未配置 NARCI_RCLONE_REMOTE → wal/ 排除验证的目标 remote 未知")
    assert ":" in remote, f"rclone 远端应为 remote:path 形态,实得: {remote!r}"
