#!/bin/bash
# ============================================================
# narci recorder 内存/CPU 占用测算 — 决定能不能开 tokyo-extra profile
#
# 在 narci-tokyo 上跑:
#   bash deploy/measure_recorder_footprint.sh
#
# 输出每个 recorder 容器 RSS + CPU,系统总内存使用,verdict 绿/黄/红
# ============================================================

set -eu

# 系统总内存 (KB → MB)
total_mb=$(awk '/MemTotal/ {printf "%.0f", $2/1024}' /proc/meminfo)
avail_mb=$(awk '/MemAvailable/ {printf "%.0f", $2/1024}' /proc/meminfo)
used_mb=$((total_mb - avail_mb))
used_pct=$((used_mb * 100 / total_mb))

echo "===== 系统内存 ====="
echo "Total:     ${total_mb} MB"
echo "Used:      ${used_mb} MB (${used_pct}%)"
echo "Available: ${avail_mb} MB"
echo ""

# 容器级 stats (mem usage / cpu %)
echo "===== narci-* 容器 ====="
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}" \
  | grep -E "^narci-|^NAME" || echo "  (no narci containers running)"
echo ""

# 估算:每个新 recorder ~150 MB (跟 recorder-bitbank 类比)
# 4 个新 recorder = ~600 MB 需求
needed_extra_mb=600

echo "===== Verdict ====="
echo "若要开 tokyo-extra (bitflyer×2 + gmo×2),预估需要额外 ~${needed_extra_mb} MB"

if [ "${avail_mb}" -ge "${needed_extra_mb}" ]; then
    margin=$((avail_mb - needed_extra_mb))
    echo "🟢 GREEN — 当前 available=${avail_mb} MB >= 需求,启用后预留 ~${margin} MB margin"
    echo ""
    echo "建议:可以试 \`COMPOSE_PROFILES=tokyo,tokyo-extra docker compose up -d\`"
    echo "      5-10 min 后 re-run 本脚本看实际占用是否在预算内"
elif [ "${avail_mb}" -ge "$((needed_extra_mb / 2))" ]; then
    echo "🟡 YELLOW — 当前 available=${avail_mb} MB 不足全开,可分批启用"
    echo ""
    echo "建议先开 2 个 (例如 bitflyer-fx + gmo-leverage,echo 团队主战场):"
    echo "  docker compose --profile tokyo-extra up -d recorder-bitflyer-fx recorder-gmo-leverage"
    echo "观察 24h 稳定再决定是否上剩 2 个,或考虑升级实例"
else
    echo "🔴 RED  — 当前 available=${avail_mb} MB 太少,**不要开 tokyo-extra**"
    echo ""
    echo "建议先升级实例:"
    echo "  - t4g.small  (2GB) → t4g.medium (4GB)  +\$15/月"
    echo "  - t4g.small  (2GB) → t4g.large  (8GB)  +\$45/月"
    echo "升级后再 re-run 本脚本"
fi

echo ""
echo "===== 5 个 recorder 落盘速率参考 ====="
# 每个 recorder 落盘目录下最近 1h 的 parquet 大小总和
if [ -d /var/lib/docker/volumes/narci_narci-data/_data/realtime ]; then
    base=/var/lib/docker/volumes/narci_narci-data/_data/realtime
elif [ -d ./replay_buffer/realtime ]; then
    base=./replay_buffer/realtime
else
    base=""
fi

if [ -n "${base}" ]; then
    for venue in coincheck binance_jp bitbank bitflyer gmo; do
        if [ -d "${base}/${venue}" ]; then
            for mkt in $(ls "${base}/${venue}" 2>/dev/null); do
                dir="${base}/${venue}/${mkt}/l2"
                [ -d "${dir}" ] || continue
                count=$(find "${dir}" -name "*.parquet" -mmin -60 2>/dev/null | wc -l)
                size_kb=$(find "${dir}" -name "*.parquet" -mmin -60 -exec du -k {} + 2>/dev/null | awk '{s+=$1} END {print s+0}')
                printf "  %-25s %3d files / %6d KB in last 1h\n" "${venue}/${mkt}" "${count}" "${size_kb}"
            done
        fi
    done
else
    echo "  (data volume 未找到 — 跑过容器后会出现)"
fi
