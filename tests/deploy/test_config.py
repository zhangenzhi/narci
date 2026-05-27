"""deploy 配置静态验证(ECS task def / IAM 策略 / supervisord / 跨配置一致)。

deploy/ 多是脚本+配置,**不能执行**(会真动 AWS/docker)。这里只做静态校验 ——
配置 typo / IAM 越权 / 端口不一致都会让线上部署坏掉或埋安全隐患,静态测试就能挡。
"""
from __future__ import annotations

import json
import configparser
from pathlib import Path

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
