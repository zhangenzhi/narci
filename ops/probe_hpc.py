"""probe_hpc — ssh lustre1 探 cold-tier 落地(只读)。

cold-tier 在 HPC(lustre1),reco 物理直连可 ssh。本探针对每个 venue 目录
(cold/{exchange}/{market}/)取最新 DAILY 日期 + DAILY/GAPS 文件数,用于"昨日逐 venue
落地检测"。天级 cadence,ssh `find/ls` 很便宜。

防引号:远端脚本 base64 后 `echo <b64> | base64 --decode | bash`。
扩展点(future cold↔官方对账):远端脚本可加读 *_GAPS_*.json 内容 / 行数,不改通道。
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import time

from ops import config, fleet


class HpcError(RuntimeError):
    pass


def _remote_script(cold_path: str) -> str:
    return f"""set +e
COLD={cold_path}
[ -d "$COLD" ] || {{ echo "###ERR### cold path not found: $COLD"; exit 0; }}
echo '###COLD###'
for d in $(find "$COLD" -mindepth 2 -maxdepth 2 -type d | sort); do
  rel=${{d#$COLD/}}
  latest=$(ls "$d"/*_DAILY*.parquet 2>/dev/null | sed -E 's/.*_([0-9]{{8}})_DAILY.*/\\1/' | sort | tail -1)
  ndaily=$(ls "$d"/*_DAILY*.parquet 2>/dev/null | wc -l | tr -d ' ')
  ngaps=$(ls "$d"/*_GAPS_*.json 2>/dev/null | wc -l | tr -d ' ')
  echo "$rel|$latest|$ndaily|$ngaps"
done
"""


def probe_cold() -> dict:
    """ssh lustre 探 cold 落地;返回 {rows, error, ts}（error 非抛，便于 GUI 降级）。"""
    res: dict = {"rows": [], "error": None, "ts": time.time()}
    target = config.lustre_target()
    if not target:
        res["error"] = "未配置 NARCI_LUSTRE_SSH（deploy/reco/.env）"
        return res
    if not shutil.which("ssh"):
        res["error"] = "ssh 未安装"
        return res

    script = _remote_script(config.lustre_cold_path())
    b64 = base64.b64encode(script.encode()).decode()
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
           "-o", "StrictHostKeyChecking=accept-new"]
    key = config.lustre_ssh_key()
    if key:
        cmd += ["-i", key]
    cmd += [target, f"echo {b64} | base64 --decode | bash"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired:
        res["error"] = "ssh lustre 超时(>40s)"
        return res
    if r.returncode != 0:
        res["error"] = f"ssh rc={r.returncode}: {r.stderr.strip()[:160]}"
        return res
    if "###ERR###" in r.stdout:
        res["error"] = r.stdout.split("###ERR###", 1)[1].strip()[:160]
        return res
    res["rows"] = fleet.parse_cold(r.stdout)
    return res
