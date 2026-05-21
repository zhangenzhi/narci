# 2026-05-21 aws-jp `narci-cloud-sync` sidecar 静默卡死 — 5 venue 推 gdrive 全停 2h+ — narci → narci-reco handoff

- **Severity**: 中(数据**没丢**,recorder 仍在 ec2 本地写 shards;只是 gdrive push lag → lustre1 看不到新数据 → research 端 hot tier 滞后)
- **Status**: 🟡 **OPEN / 待 narci-reco 接管**
- **First detect**: 2026-05-21 05:55 UTC(narci 端 oneshot rclone pull 后核 gap 发现)
- **Affected venues**(aws-jp 上的 cloud-sync sidecar 推不出去):
  - `coincheck/spot` — 7 symbols
  - `binance_jp/spot` — 24 symbols
  - `bitbank/spot` — 5 symbols
  - `bitflyer/spot` — 3 symbols(BTC/ETH/XRP_JPY)
  - `bitflyer/fx` — 1 symbol(FX_BTC_JPY)
- **Not affected**:
  - `aws-sg` `narci-cloud-sync` 健康(`binance/spot` + `binance/um_futures` 5/12 min gap,正常)
  - `aws-jp` 上的 7 个 recorder container **本身没死**(继续在 ec2 ephemeral 盘写 shard;只是 sidecar 不推 gdrive,research 端看不到)
- **Time window**:
  - aws-jp gdrive 上 5 venue 最后一个 0521 shard 时间戳:**~03:30 UTC(coincheck XRP_JPY 03:32:23、bitbank 03:52:29、bitflyer 03:51:01)**
  - 当前 UTC 05:55 → cloud-sync 静默 **≥ 2h23min**
- **关联**:
  - [2026-05-21-um-and-gmo-recorder-outage.md](./2026-05-21-um-and-gmo-recorder-outage.md) — 同日两个事件互不相关:那条是 SG UM recorder 死(narci-reco 已 resolve)+ JP gmo silent ban(narci 决策 PARK)。**本条是同 JP host 上的 cloud-sync sidecar 静默卡死**,跟 gmo PARK 没因果(gmo container stopped 不应该影响 sidecar)。
  - [2026-05-17-gmo-watchdog-silent-death.md](./2026-05-17-gmo-watchdog-silent-death.md) lesson #3 — `docker compose ps healthy ≠ data healthy`。本次完美复发同一 blind spot:cloud-sync sidecar 在 docker 视角应该是 healthy(假设它没 crash),但实际 rclone push 卡了。

## Symptom — narci 侧实测

narci 端 `replay_buffer/realtime/` (symlink → `/lustre1/work/c30636/dataset/narci_raw/realtime/`) 各 JP venue 最新 0521 shard 时间:

| Venue | local latest | gdrive remote latest(rclone lsf 直查) | local vs remote | gap from now (05:55 UTC) |
|---|---|---|---|---|
| coincheck/spot XRP_JPY | 03:32:23 UTC | **03:32:23 UTC** | 同步 | **2h23min** 🚨 |
| binance_jp/spot XRPJPY | 03:26:56 UTC | (同上同步) | 同步 | **2h29min** 🚨 |
| bitbank/spot XRP_JPY | 03:52:29 UTC | (同上) | 同步 | **2h3min** 🚨 |
| bitflyer/spot XRP_JPY | 03:51:01 UTC | (同上) | 同步 | **2h4min** 🚨 |
| bitflyer/fx FX_BTC_JPY | 03:49:45 UTC | (同上) | 同步 | **2h6min** 🚨 |
| binance/spot XRPJPY *(aws-sg)* | 05:14:47 UTC | 05:14:47+ UTC | OK | 41min ✅ |
| binance/um_futures XRPUSDT *(aws-sg)* | 05:21:38 UTC | **05:43:56 UTC** | lustre1 落后(等下次 pull) | 12min ✅ |

诊断关键:**`rclone lsf gdrive:narci_raw/realtime/coincheck/spot/l2/` 远端最新跟 local pull 后一致** → 不是 lustre1 → gdrive pull 慢,**是 aws-jp → gdrive push 停了**。

## 推测 — 待 narci-reco 验证

narci 端不能 ssh ec2(per memory `feedback_no_crypto_network`)。形态推测:

| 嫌疑 | 表现 | 验证(narci-reco 侧) |
|---|---|---|
| rclone OAuth token expired | logs 有 `googleapi: Error 401` / `Authentication failed` | grep logs |
| 磁盘满(rclone cache 写不下) | logs 有 `no space left on device` | `df -h /` on host via SSM |
| rclone 卡单个超大文件 | logs 卡在 single-file progress 但 stats 0 movement | check stats lines mtime |
| sidecar OOM killed | `docker compose ps` 显示 Exited 137 / 高 restart count | `docker compose ps` + `docker ps -a` |
| narci-data volume mount 异常 | logs 报 read error | docker exec |
| **最可能** — sidecar 本身没死,rclone subprocess 静默卡(no timeout in inline loop) | logs 最后一行 `[cloud-sync] syncing...` 或 stats 凝固于 03:42 UTC 附近 | grep `cloud-sync` 关键字 |

cloud-sync sidecar 实现(`docker-compose.yaml:283-325`)是个 dumb `while true; do rclone copy ... ; sleep ${SYNC_INTERVAL}; done` bash 循环。**无 timeout / 无 watchdog / 无 retry / 无 exit-on-stuck**。rclone 进程卡了不会自愈,sidecar 整个就停在那里。

## Fix — narci-reco 端 ops recipe

### 1. ps + logs 确认状态

```bash
cd ~/narci/reco

# 看 cloud-sync container 状态
aws ssm send-command --instance-ids "$NARCI_JP_INSTANCE_ID" \
  --region "$AWS_REGION_JP" \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["cd /home/ec2-user/narci && docker compose ps narci-cloud-sync && echo --- && docker compose logs --since 4h --tail 200 narci-cloud-sync 2>&1 | tail -100"]' \
  --output text --query 'Command.CommandId'
# 5s 后 aws ssm get-command-invocation 取结果
```

判断:
- 若 `docker compose ps` 显示 `Up X hours` 但 logs 凝固于 03:42 UTC 附近 — **rclone subprocess hang**,goto step 2
- 若显示 `Exited` / restart_count 高 — **OOM / crash**,看 logs 找 exit 原因再 restart
- 若 logs 有 `googleapi: Error 401` — **OAuth token expired**,重发 RCLONE_GDRIVE_TOKEN 后 restart

### 2. 单 service 重启(最快路径)

```bash
./aws/recorder_restart.sh jp --service narci-cloud-sync
```

不影响 7 个 recorder container。预期 t+5min 内 cloud-sync 开始推 gdrive。

### 3. 验证(narci 端跑 / Mac Studio 远程查)

```bash
# narci-reco 在 Mac Studio 跑(或 lustre1 也行)
RCLONE_CONFIG=/lustre1/work/c30636/narci/.rclone/rclone.conf rclone lsf \
  gdrive:narci_raw/realtime/coincheck/spot/l2/ --include "*20260521*" 2>/dev/null | sort | tail -3
```

期望:出现 04:XX UTC 之后的 shard 文件。

之后 lustre1 端等 `.rclone/sync_loop.sh` 下次 pull(最长 1h),或手动跑:

```bash
RCLONE_CONFIG=/lustre1/work/c30636/narci/.rclone/rclone.conf rclone copy \
  gdrive:narci_raw /lustre1/work/c30636/dataset/narci_raw \
  --transfers 16 --checkers 32 --tpslimit 20 --no-traverse --max-age 6h \
  --stats 30s --stats-one-line --log-level NOTICE
```

## Lessons / TODO

1. **cloud-sync sidecar 需要 stuck detection**(narci 域):
   - 当前 `while true; do rclone copy ... ; sleep $INTERVAL; done` 没有 `timeout` 包 rclone,也没有 stats-based stuck-detect。
   - 短期 fix:`timeout 1200 rclone copy ...`(20 min 强杀),让 sidecar 自己进下一轮 sleep 循环,避免无限期卡。
   - 长期 fix:跟 [[task-#85 healthcheck per-recorder]] 合并,加一份 cloud-sync 自身的 push-lag healthcheck(对比 host local mtime vs rclone last successful push log line)。

2. **gdrive push-lag 不在 venue_stale_monitor.sh 覆盖范围内**(narci-reco 域):
   - `3fa948c` 加的 `venue_stale_monitor.sh` 监控的是 ec2 上 recorder shard mtime via SSM。aws-jp recorder 仍在写,SSM 看 shard mtime 是 fresh → monitor 不报警。
   - 但 gdrive 上没新东西 = research 端用不上 = real impact。
   - 需要加一份 gdrive-side 监控:`rclone lsf gdrive:narci_raw/realtime/*/*/l2/ --include "*latest_day*"` 找每个 venue 最新 shard,过阈值(比如 30 min)报警。可放进 venue_stale_monitor.sh 作为 second pass。

3. **本次第 2 次复发 "docker healthy ≠ data healthy"**(参 `2026-05-17-gmo-watchdog-silent-death.md` lesson #3)。每次都是同一个 blind spot:docker 视角的 health 不代表业务 health。这次问题转移到 sidecar 上,模式一致。

---

**handoff status**:narci 端 oneshot rclone pull 已确认 lustre1 → gdrive pull 链路健康(SG 一侧拉到 5/12 min gap)。aws-jp → gdrive push 链路停在 03:42 UTC。**narci-reco 角色接手** — 跑 step 1-3,看 logs 定位根因 + restart narci-cloud-sync,close 本 incident。

narci 端无主动 fix 动作(无 ec2 ssh 凭证)。
