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
    """lustre 侧只读扫描:对每 venue 输出 ex/mkt|latest|n_daily|n_gaps|days_csv|bad_csv。

    days_csv = 该 venue 已有 DAILY 的日期集合(近 120 天,YYYYMMDD 逗号串);
    bad_csv  = 含 .corrupted 后缀的损坏 DAILY 日期集合。
    用 Python heredoc(quoted)避免 bash 跟 Python 字符串互相干扰;COLD 路径走 env。
    """
    return f"""set +e
export COLD={cold_path}
[ -d "$COLD" ] || {{ echo "###ERR### cold path not found: $COLD"; exit 0; }}
echo '###COLD###'
python3 - <<'PYEOF'
import os, re, datetime
COLD = os.environ["COLD"]
RAW_DAILY = re.compile(r"_RAW_(\\d{{8}})_DAILY\\.parquet$")
RAW_BAD   = re.compile(r"_RAW_(\\d{{8}})_DAILY\\.parquet\\.corrupted$")
cutoff = (datetime.date.today() - datetime.timedelta(days=120)).strftime("%Y%m%d")
try:
    exchanges = sorted(os.listdir(COLD))
except OSError:
    exchanges = []
for ex in exchanges:
    ex_p = os.path.join(COLD, ex)
    if not os.path.isdir(ex_p):
        continue
    for mkt in sorted(os.listdir(ex_p)):
        mkt_p = os.path.join(ex_p, mkt)
        if not os.path.isdir(mkt_p):
            continue
        try:
            files = os.listdir(mkt_p)
        except OSError:
            continue
        days, bad = set(), set()
        ngaps = 0
        for fn in files:
            mb = RAW_BAD.search(fn)
            if mb and mb.group(1) >= cutoff:
                bad.add(mb.group(1)); continue
            md = RAW_DAILY.search(fn)
            if md and md.group(1) >= cutoff:
                days.add(md.group(1))
            if fn.endswith(".json") and "_GAPS_" in fn:
                ngaps += 1
        latest = max(days) if days else ""
        n_daily = len(days)
        print(f"{{ex}}/{{mkt}}|{{latest}}|{{n_daily}}|{{ngaps}}|"
              f"{{','.join(sorted(days))}}|{{','.join(sorted(bad))}}")
PYEOF
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
