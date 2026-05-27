"""deploy shell 脚本静态检查(语法 + 关键内容)。

脚本会真动 AWS/docker/rclone,**绝不执行**。但 `bash -n` 只做语法解析、不运行 ——
能挡住"entrypoint.sh 一个语法错 → 容器起不来"这类低级但致命的错。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"

_SH = sorted(str(p) for p in DEPLOY.rglob("*.sh"))


@pytest.mark.skipif(not shutil.which("bash"), reason="bash 不可用")
@pytest.mark.parametrize("script", _SH, ids=[Path(p).relative_to(REPO).as_posix() for p in _SH])
def test_shell_syntax_ok(script):
    """bash -n:仅解析语法,不执行(不会触发任何 aws/docker 调用)。"""
    r = subprocess.run(["bash", "-n", script], capture_output=True, text=True)
    assert r.returncode == 0, f"{script} 语法错:\n{r.stderr}"


def test_found_shell_scripts():
    # 防御:若 glob 拿不到脚本(路径错),上面的 parametrize 会静默 0 例 —— 这里兜底
    assert len(_SH) >= 5, f"deploy 下 shell 脚本异常少: {_SH}"


def test_reco_aws_scripts_use_aws_cli():
    """reco/aws/ 下的运维脚本应真的调 aws cli(否则是空壳)。"""
    for p in (DEPLOY / "reco" / "aws").glob("*.sh"):
        assert "aws " in p.read_text(), f"{p.name} 未调用 aws cli"


def test_entrypoint_dispatches_record_and_shell_modes():
    txt = (DEPLOY / "entrypoint.sh").read_text()
    assert "record)" in txt and "shell)" in txt
    assert "supervisord" in txt  # record 模式起 supervisord(recorder + healthcheck)
