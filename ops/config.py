"""ops 配置:从 deploy/reco/.env(gitignored)读 fleet 连接参数。

不在代码里硬编码实例ID/region/lustre target —— 全从环境或 deploy/reco/.env 取,
复用 tests/deploy/test_aws_smoke.py 里同款 _env 解析。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RECO_ENV = REPO / "deploy" / "reco" / ".env"

# compose profile：决定 docker compose 认得哪些 profile-gated 服务(同 recorder_restart.sh）
_PROFILES = {"jp": "tokyo,tokyo-extra", "sg": "global"}


def _env(key: str, default: str | None = None) -> str | None:
    """先看进程环境，再回退 deploy/reco/.env。"""
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
                return val.strip().strip('"').strip("'") or default
    return default


@dataclass(frozen=True)
class FleetConfig:
    name: str                 # jp | sg
    instance_id: str
    region: str
    profiles: str             # COMPOSE_PROFILES
    health_ports: list[int]   # 各 recorder /health 端口
    deploy_path: str          # 主机上 narci repo 路径


def fleet_config(name: str) -> FleetConfig:
    """组一个 fleet 的配置；缺关键值抛 KeyError（GUI 侧捕获给提示）。"""
    name = name.lower()
    if name not in _PROFILES:
        raise KeyError(f"未知 fleet: {name}（应为 jp|sg）")
    suffix = name.upper()
    iid = _env(f"NARCI_{suffix}_INSTANCE_ID")
    if not iid:
        raise KeyError(f"缺 NARCI_{suffix}_INSTANCE_ID（进程环境或 deploy/reco/.env）")
    region = _env(f"AWS_REGION_{suffix}") or ("ap-northeast-1" if name == "jp" else "ap-southeast-1")
    ports_raw = _env(f"NARCI_{suffix}_HEALTH_PORTS") or ""
    ports = [int(p) for p in ports_raw.replace(",", " ").split() if p.strip().isdigit()]
    deploy_path = _env("NARCI_DEPLOY_PATH") or "/home/ec2-user/narci"
    return FleetConfig(name, iid, region, _PROFILES[name], ports, deploy_path)


def lustre_target() -> str | None:
    """lustre1 的 ssh target（user@host 或 ssh config 别名），cold 通道用；未配则 None。"""
    return _env("NARCI_LUSTRE_SSH")


def lustre_ssh_key() -> str | None:
    """lustre ssh 私钥路径（~ 展开）；未配则 None（走默认 key / ssh config）。"""
    k = _env("NARCI_LUSTRE_SSH_KEY")
    return os.path.expanduser(k) if k else None


def lustre_cold_path() -> str:
    return _env("NARCI_LUSTRE_COLD_PATH") or "/lustre1/work/c30636/narci/replay_buffer/cold"


def available_fleets() -> list[str]:
    return [f for f in _PROFILES if _env(f"NARCI_{f.upper()}_INSTANCE_ID")]
