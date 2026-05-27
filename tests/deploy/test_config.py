"""deploy 配置静态验证(ECS task def / IAM 策略 / supervisord / 跨配置一致)。

deploy/ 多是脚本+配置,**不能执行**(会真动 AWS/docker)。这里只做静态校验 ——
配置 typo / IAM 越权 / 端口不一致都会让线上部署坏掉或埋安全隐患,静态测试就能挡。
"""
from __future__ import annotations

import json
import configparser
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"


# ============================ ECS task definition ============================ #

def _ecs():
    return json.loads((DEPLOY / "ecs-task-definition.json").read_text())


def test_ecs_task_def_valid_and_fargate():
    d = _ecs()
    assert d["family"] == "narci-recorder"
    assert "FARGATE" in d["requiresCompatibilities"]
    assert d["networkMode"] == "awsvpc"


def test_ecs_recorder_container_shape():
    d = _ecs()
    recorder = next(c for c in d["containerDefinitions"] if c["name"] == "recorder")
    assert recorder["essential"] is True
    assert recorder["command"] == ["record"]          # 与 Dockerfile CMD 一致
    ports = [p["containerPort"] for p in recorder["portMappings"]]
    assert 8079 in ports
    # healthcheck 探 /health
    hc = " ".join(recorder["healthCheck"]["command"])
    assert "/health" in hc and "curl" in hc
    # 数据卷挂到 /app/replay_buffer
    assert any(m["containerPath"] == "/app/replay_buffer" for m in recorder["mountPoints"])


def test_ecs_efs_volume_present():
    d = _ecs()
    vol = next(v for v in d["volumes"] if v["name"] == "narci-data")
    assert "efsVolumeConfiguration" in vol


def test_ecs_port_matches_dockerfile_and_supervisord():
    """8079 在 ECS / Dockerfile EXPOSE 三处必须一致 —— 端口漂移 = healthcheck 探不到。"""
    d = _ecs()
    port = d["containerDefinitions"][0]["portMappings"][0]["containerPort"]
    dockerfile = (REPO / "Dockerfile").read_text()
    assert f"EXPOSE {port}" in dockerfile, f"Dockerfile EXPOSE 与 ECS 端口({port})不一致"


# ============================ IAM 策略(最小权限)============================ #

def _iam():
    return json.loads((DEPLOY / "reco" / "aws" / "iam" / "narci-reco-policy.json").read_text())


def test_iam_policy_well_formed():
    p = _iam()
    assert p["Version"] == "2012-10-17"
    for st in p["Statement"]:
        assert "Sid" in st and "Effect" in st and "Action" in st
        assert st["Effect"] in ("Allow", "Deny")


def test_iam_no_full_wildcard_action():
    """不得出现 Action:"*"(全权限);单个 action 末尾通配如 ec2:Describe* 仍允许。"""
    for st in _iam()["Statement"]:
        actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        assert "*" not in actions, f"语句 {st.get('Sid')} 含全通配 Action:*"


def test_iam_instance_lifecycle_is_tag_gated():
    """Start/Stop/Reboot/Modify 这类破坏性操作必须用 ResourceTag 限定(只动 narci 的机器)。"""
    p = _iam()
    dangerous = {"ec2:StartInstances", "ec2:StopInstances", "ec2:RebootInstances",
                 "ec2:ModifyInstanceAttribute"}
    for st in p["Statement"]:
        acts = set(st["Action"] if isinstance(st["Action"], list) else [st["Action"]])
        if acts & dangerous:
            cond = json.dumps(st.get("Condition", {}))
            assert "ResourceTag/Project" in cond, \
                f"生命周期语句 {st.get('Sid')} 缺少 ResourceTag 限定 → 可动任意 EC2"


def test_iam_secrets_scoped_not_wildcard():
    """secretsmanager 读权限必须限定到 narci/* ARN,不能 Resource:*。"""
    for st in _iam()["Statement"]:
        acts = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        if any(a.startswith("secretsmanager:") for a in acts):
            res = st["Resource"]
            res_list = res if isinstance(res, list) else [res]
            assert all(r != "*" for r in res_list), "secrets 不应 Resource:*"
            assert any("secret:narci/" in r for r in res_list), "secrets 应限 narci/* 前缀"


def test_iam_cloudwatch_put_present_and_namespace_scoped():
    """donor check_health.sh 发 Narci/Donor/DonorHealthy 需 PutMetricData。

    部署阻塞守卫(2026-05-27,见 docs/INTERFACE_NARCI_RECO.md #3):
    - **必须存在**:缺了 donor 的 put-metric-data 静默 no-op → Alarm 拿不到数据,
      donor 挂了没人知道。
    - **必须 namespace 收窄**:PutMetricData 不支持 resource 级限制,只能用
      cloudwatch:namespace 条件键限到 Narci/Donor,否则等于全账户随便发指标。
    """
    p = _iam()
    put_stmts = [
        st for st in p["Statement"]
        if "cloudwatch:PutMetricData" in (
            st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        )
    ]
    assert put_stmts, "IAM 缺 cloudwatch:PutMetricData → donor 心跳指标发不出去"
    for st in put_stmts:
        cond = st.get("Condition", {})
        ns = cond.get("StringEquals", {}).get("cloudwatch:namespace")
        assert ns == "Narci/Donor", \
            f"PutMetricData 语句 {st.get('Sid')} 未用 namespace 条件收窄到 Narci/Donor"


# ===================== cloud-sync(rclone sidecar / WAL 隔离)===================== #

def _cloud_sync_script() -> str:
    """提取 docker-compose cloud-sync 服务的 sh 脚本(command: [-c, <script>])。"""
    compose = yaml.safe_load((REPO / "docker-compose.yaml").read_text())
    cmd = compose["services"]["cloud-sync"]["command"]
    # 形如 ["-c", "<多行脚本>"];取脚本体
    return cmd[cmd.index("-c") + 1]


def test_cloud_sync_excludes_wal_tree():
    """rclone copy /data 必须排除 wal/**(段式 WAL 中间态,不该上 gdrive)。

    部署阻塞守卫(2026-05-27,见 docs/INTERFACE_NARCI_RECO.md #1):sidecar copy 的是
    整个 replay_buffer 根(/data),replay_buffer/wal/*.segwal 是 realtime/ 的平级
    兄弟。少了 --exclude 'wal/**' 就会把秒级短命段 + .tmp 整盘推上远端 → 浪费传输 +
    污染 gdrive。段被 save_loop 合并成 RAW_*.parquet 后即删,远端只该有最终 parquet。
    """
    script = _cloud_sync_script()
    assert "rclone copy /data" in script, "cloud-sync 应整盘 copy /data(改了请同步本测试)"
    copy_idx = script.index("rclone copy /data")
    tail = script[copy_idx:]
    assert "--exclude 'wal/**'" in tail, "cloud-sync 缺 --exclude 'wal/**' → WAL 段会被推上远端"


# ============================ supervisord ============================ #

def _supervisord():
    cp = configparser.ConfigParser()
    cp.read(DEPLOY / "supervisord.conf")
    return cp


def test_supervisord_runs_recorder_and_healthcheck():
    cp = _supervisord()
    assert "program:recorder" in cp
    assert "program:healthcheck" in cp
    assert "main.py record" in cp["program:recorder"]["command"]
    assert "deploy/healthcheck.py" in cp["program:healthcheck"]["command"]


def test_supervisord_autorestart_on():
    cp = _supervisord()
    # 录制器崩了要自动拉起(配合 recorder 的熔断 os._exit → supervisord 重启)
    assert cp["program:recorder"]["autorestart"] == "true"
    assert cp["program:healthcheck"]["autorestart"] == "true"
    assert int(cp["program:recorder"]["startretries"]) >= 10
