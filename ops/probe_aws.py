"""probe_aws — 每 fleet 一发批量 SSM 只读探针。

只调只读：`aws ec2 describe-instances` / `ssm describe-instance-information` +
**一条** `ssm send-command`（host commit / docker ps / 容器内 freshness+wal 扫描 / 各
recorder /health）。**绝不**发会动 AWS/容器的命令。

防引号地狱：远端脚本 base64 编码后 `echo <b64> | base64 -d | sudo -u ec2-user bash`，
脚本体内引号/$ 随便写。aws-cli 须已配置（用 deploy/reco/.env 的 AWS_PROFILE）。
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time

from ops import config, fleet

_AWS_PROFILE = config._env("AWS_PROFILE")


class ProbeError(RuntimeError):
    pass


def _aws(args: list[str], region: str, timeout: int = 30) -> str:
    if not shutil.which("aws"):
        raise ProbeError("aws-cli 未安装")
    cmd = ["aws", *args, "--region", region, "--output", "json"]
    if _AWS_PROFILE:
        cmd += ["--profile", _AWS_PROFILE]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ProbeError(f"aws {args[0]} {args[1]} 超时")
    if r.returncode != 0:
        raise ProbeError(f"aws {args[0]} {args[1]} rc={r.returncode}: {r.stderr.strip()[:160]}")
    return r.stdout


# 自包含扫描器：recorder 镜像 .dockerignore 剥掉了 analytics/，容器内 import 不到
# recorder_health，故把扫描逻辑内嵌（heredoc 喂容器 python）。
# **逻辑镜像 analytics/gui/recorder_health.py 的 scan_freshness/scan_wal_backlog**，
# 行为一致性由 tests/ops/test_remote_scanner.py 对 temp 目录验证。RB 可用环境变量覆盖
# （本地测用），默认 /app/replay_buffer。
_REMOTE_SCANNER = r'''
import json, os, re
RB = os.environ.get("RB", "/app/replay_buffer")
DAY = os.environ.get("DAY", "")       # YYYYMMDD;空跳过 coverage
N_BUCKETS = 144                        # 10-min/桶 × 144 = 24h
RAW = re.compile(r"^(.+)_RAW_(\d{8})_(\d{6})\.parquet(\.corrupted)?$")
def vm(parts):
    if "l2" in parts:
        i = parts.index("l2")
        return (parts[i-2] if i >= 2 else "?", parts[i-1] if i >= 1 else "?")
    return ("?", parts[-3] if len(parts) >= 3 else "?")
def bucket(hhmmss):
    s = int(hhmmss[:2])*3600 + int(hhmmss[2:4])*60 + int(hhmmss[4:])
    return min(N_BUCKETS-1, s // (86400 // N_BUCKETS))
def scan(root):
    latest, cov = {}, {}
    if os.path.isdir(root):
        for base, _, files in os.walk(root):
            for fn in files:
                m = RAW.match(fn)
                if not m or "DAILY" in fn:
                    continue
                sym, day, hhmmss = m.group(1).upper(), m.group(2), m.group(3)
                corrupted = bool(m.group(4))
                rel = os.path.relpath(os.path.join(base, fn), root).split(os.sep)
                ex, mkt = vm(rel); k = (ex, mkt, sym)
                if not corrupted:
                    mt = os.path.getmtime(os.path.join(base, fn)); c = latest.get(k)
                    if c is None:
                        latest[k] = {"exchange": ex, "market": mkt, "symbol": sym,
                                     "last_shard_ts": mt, "n_shards": 1}
                    else:
                        c["n_shards"] += 1
                        if mt > c["last_shard_ts"]:
                            c["last_shard_ts"] = mt
                if DAY and day == DAY:
                    e = cov.get(k)
                    if e is None:
                        e = {"exchange": ex, "market": mkt, "symbol": sym,
                             "_bits": ["0"] * N_BUCKETS}
                        cov[k] = e
                    b = bucket(hhmmss)
                    e["_bits"][b] = "x" if corrupted or e["_bits"][b] == "x" else "1"
    cov_out = []
    for v in cov.values():
        bits = v.pop("_bits")
        v["bitmap"] = "".join(bits)
        cov_out.append(v)
    return list(latest.values()), cov_out
def wal(root):
    counts = {}
    if os.path.isdir(root):
        for base, _, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".segwal"):
                    continue
                rel = os.path.relpath(os.path.join(base, fn), root).split(os.sep)
                ex, mkt = vm(rel); sym = fn.rsplit("_", 1)[0].upper()
                counts[(ex, mkt, sym)] = counts.get((ex, mkt, sym), 0) + 1
    return [{"exchange": e, "market": m, "symbol": s, "pending_segments": n}
            for (e, m, s), n in counts.items()]
fresh, cov = scan(RB + "/realtime")
print(json.dumps({"freshness": fresh, "wal": wal(RB + "/wal"),
                  "coverage": cov, "day": DAY, "n_buckets": N_BUCKETS}))
'''


def _remote_script(cfg: config.FleetConfig, day_utc: str = "") -> str:
    """远端只读脚本（以 ec2-user 跑：git 属主 + docker group）。

    day_utc(YYYYMMDD) 传给容器内 scanner 算当日 144 桶 coverage bitmap;空则跳过。
    """
    ports = " ".join(str(p) for p in cfg.health_ports) or ""
    return f"""set +e
cd {cfg.deploy_path} || exit 3
echo '###COMMIT###'
git rev-parse --short HEAD 2>/dev/null
git log -1 --format=%s 2>/dev/null
echo '###PS###'
COMPOSE_PROFILES={cfg.profiles} docker compose ps -a --format '{{{{.Name}}}}|{{{{.Status}}}}|{{{{.Image}}}}' 2>/dev/null
echo '###SCAN###'
C=$(docker ps --filter name=narci-recorder --format '{{{{.Names}}}}' | head -1)
[ -n "$C" ] && docker exec -i -e DAY={day_utc} "$C" python3 - <<'PYEOF' 2>/dev/null
{_REMOTE_SCANNER}
PYEOF
echo '###CLOUDSYNC###'
docker logs --tail 120 --timestamps narci-cloud-sync 2>&1 | grep -E 'cloud-sync\\] (syncing|done)' | tail -30
echo '###HEALTH###'
for p in {ports}; do printf 'port=%s code=%s\\n' "$p" "$(curl -s -o /dev/null -m 3 -w '%{{http_code}}' http://localhost:$p/health 2>/dev/null)"; done
"""


def _ssm_exec(cfg: config.FleetConfig, script: str, comment: str, poll_sec: int = 60) -> str:
    """base64 传脚本 → 一条 SSM send-command(ec2-user)→ 轮询取 stdout。"""
    b64 = base64.b64encode(script.encode()).decode()
    element = f"echo {b64} | base64 --decode | sudo -u ec2-user bash"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"commands": [element]}, f)
        params = f.name
    out = _aws(["ssm", "send-command", "--instance-ids", cfg.instance_id,
                "--document-name", "AWS-RunShellScript", "--comment", comment,
                "--parameters", f"file://{params}",
                "--query", "Command.CommandId"], cfg.region)
    cmd_id = json.loads(out)
    deadline = time.time() + poll_sec
    status = "Pending"
    while time.time() < deadline:
        time.sleep(3)
        inv = json.loads(_aws(["ssm", "get-command-invocation",
                               "--command-id", cmd_id, "--instance-id", cfg.instance_id,
                               "--query", "{S:Status,O:StandardOutputContent}"], cfg.region))
        status = inv.get("S", "Pending")
        if status in ("Success", "Failed", "TimedOut", "Cancelled"):
            if status != "Success":
                raise ProbeError(f"SSM status={status}")
            return inv.get("O", "") or ""
    raise ProbeError(f"SSM 轮询超时（{poll_sec}s，status={status}）")


def _ssm_batch(cfg: config.FleetConfig, day_utc: str = "") -> str:
    return _ssm_exec(cfg, _remote_script(cfg, day_utc), "narci ops probe (read-only)")


# 注:gdrive 全量列举(列每个 shard 文件名/时间)实测 >180s 超时——cloud-sync `rclone copy`
# 只增不删,gdrive 累积海量 shard(还混着旧扁平路径),列举又慢又烧 quota,不可行。
# 推送腿健康改由 cloud-sync 容器日志(主探针 ###CLOUDSYNC### 段)判,零 gdrive quota。
# 真要看 gdrive 实际内容用手动:rclone lsf gdrive:/narci_raw/realtime --max-depth 3。


def probe_fleet(name: str, day_utc: str = "") -> dict:
    """探一个 fleet，返回结构化结果（含 error 字段而非抛，方便 GUI 局部降级）。

    day_utc(YYYYMMDD) 用于算当日 coverage bitmap;空则跳过。
    """
    try:
        cfg = config.fleet_config(name)
    except KeyError as e:
        return {"fleet": name, "error": str(e)}

    res: dict = {"fleet": name, "instance_id": cfg.instance_id, "region": cfg.region,
                 "ts": time.time(), "day": day_utc, "error": None}
    try:
        ec2 = json.loads(_aws(["ec2", "describe-instances", "--instance-ids", cfg.instance_id,
                               "--query", "Reservations[].Instances[].State.Name"], cfg.region))
        res["ec2_state"] = ec2[0] if ec2 else "unknown"
        ssm = json.loads(_aws(["ssm", "describe-instance-information",
                               "--filters", f"Key=InstanceIds,Values={cfg.instance_id}",
                               "--query", "InstanceInformationList[].PingStatus"], cfg.region))
        res["ssm_ping"] = ssm[0] if ssm else "offline"

        sections = fleet.split_sections(_ssm_batch(cfg, day_utc))
        res["commit"] = fleet.parse_commit(sections.get("COMMIT", ""))
        res["containers"] = fleet.parse_ps(sections.get("PS", ""))
        res["health"] = fleet.parse_health(sections.get("HEALTH", ""))
        scan = fleet.parse_scan(sections.get("SCAN", ""))
        res["freshness_raw"] = scan["freshness"]
        res["wal"] = scan["wal"]
        res["coverage"] = scan.get("coverage", [])
        res["n_buckets"] = scan.get("n_buckets", 144)
        res["cloudsync"] = fleet.parse_cloudsync(sections.get("CLOUDSYNC", ""))
    except ProbeError as e:
        res["error"] = str(e)
    return res
