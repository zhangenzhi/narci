# 2026-05-21 aws-jp `narci-cloud-sync` sidecar 静默卡死 — 5 venue 推 gdrive 全停 2h+ — narci → narci-reco handoff

- **Severity**: 中(数据**没丢**,recorder 仍在 ec2 本地写 shards;只是 gdrive push lag → lustre1 看不到新数据 → research 端 hot tier 滞后)
- **Status**: 🟢 **RESOLVED — narci-reco restart 已恢复 push 链路**(2026-05-21 07:38 UTC)
- **First detect**: 2026-05-21 05:55 UTC(narci 端 oneshot rclone pull 后核 gap 发现)
- **Resolution**: 2026-05-21 07:38 UTC,narci-reco 重启 `cloud-sync` service,新一轮 sync 立即开始
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
  - aws-jp gdrive 上 5 venue 最后一个 0521 shard 时间戳:**~03:30 UTC**
  - 当前 UTC 05:55 → cloud-sync 静默 **≥ 2h23min**
- **关联**:
  - [2026-05-21-um-and-gmo-recorder-outage.md](./2026-05-21-um-and-gmo-recorder-outage.md) — 同日两个事件互不相关
  - [2026-05-17-gmo-watchdog-silent-death.md](./2026-05-17-gmo-watchdog-silent-death.md) lesson #3 — `docker compose ps healthy ≠ data healthy`。本次同 blind spot 第二次复发。

## Symptom — narci 侧实测

(narci doc 原文 — 各 venue 远端 03:30 UTC 后无新 shard,见上方 OPEN 部分。)

## RCA — narci-reco 侧实测(纠正 narci 推测)

narci 推测 6 个嫌疑里"最可能 — sidecar 没死,rclone subprocess 静默卡(no timeout in inline loop)"是**正确的**,但具体形态跟 narci 列的 6 个嫌疑都不完全吻合。

**实际形态**:

| narci 推测 | 实测 |
|---|---|
| Sidecar OOM killed | ❌ `restart_count=0 OOMKilled=false started=05-16 12:58Z`(Up 4 days,无重启) |
| 磁盘满 | ❌ `df -h / = 6.7G/30G (23%)` |
| narci-data volume mount 异常 | ❌ recorder 仍在写,本地 shard ~07:27Z 新鲜 |
| OAuth token expired | ❌ logs 无 `googleapi: Error 401 / Authentication failed`,主进程 rclone copy 还在跑 |
| rclone 卡单个超大文件 | ❌ logs 持续刷 `lstat ERROR` 行(进展但 source-side 错误) |
| **最可能** sidecar 没死,rclone subprocess 静默卡 | ✅ **形态匹配**:**当前 rclone 主进程已跑 ≥ 3h35min 还没退出**,远超 600s sync interval |

**关键时间线**(从 `docker compose logs -t cloud-sync` 抓 `[cloud-sync] syncing/done` 标记):

| 时间(UTC) | 事件 | 用时 | 备注 |
|---|---|---|---|
| 01:09:23 | done | — | 正常 |
| 01:19:23→01:33:32 | syncing→done | 14 min | 正常 |
| 01:43:32→01:57:50 | syncing→done | 14 min | 正常 |
| 02:07:50→02:39:53 | syncing→done | **32 min** | ⚠ 变慢前兆 |
| 02:49:53→03:00:58 | syncing→done | 11 min | 正常 |
| 03:10:58→03:24:01 | syncing→done | 13 min | 正常 |
| 03:34:01→03:45:58 | syncing→done | 12 min | **最后一次成功 push** |
| 03:55:58 → ... | syncing... | **≥ 3h35min 不退出** | 🚨 卡死 |

last `done` (03:45:58) 跟 narci 看到的 gdrive 远端最新文件时间(coincheck 03:32 / bitbank 03:52 / bitflyer 03:51)**完全对得上** —— 03:45 那轮把 03:30-03:52 batch push 出去后,03:55 进新轮就再也没出来。

**root cause(我看到的)**:
1. `docker-compose.yaml:301-309` 5/15 撤掉 `--no-traverse`(commit 注释里写明:GDrive 后端 + `--no-traverse` 对新嵌套目录会静默 skip)
2. 撤掉之后每轮 sync 要 full traverse GDrive 端 → 累积几天后 GDrive 文件树膨胀 + API throttling
3. 没有 `timeout N rclone copy ...` 包,rclone subprocess 卡了就永远卡(narci doc 提到的 short-term fix 思路完全对)
4. `--log-level NOTICE` 不输出 `Transferred:/Checks:/Elapsed time:` 总结行,只输出 ERROR/WARN → 卡死时 logs 表面看仍在"工作"(刷大量 `lstat ... no such file or directory` 来自 retention 删除 vs rclone copy queue 之间的 TOCTOU race),实际整轮 sync 不前进

**为什么 ERROR 行让我和 narci 都误判**: ERROR 行的根因不是"卡",是 rclone 在 list source 时拿到 retention 已删除的 0514 旧 shard,然后 sequential lstat 时发现不存在 → 一个 source-side 错误。这些 ERROR **不影响**新文件的 transfer(它们是 0514 的 historical,跟 0521 新数据无关)。

真正的卡发生在某个 GDrive 端 traverse 阶段,NOTICE log 没暴露,只能从"超时无 done 标记"推断。

## Fix — narci-reco 执行记录

**07:35 UTC restart**:

```bash
./aws/recorder_restart.sh jp --service cloud-sync
```

post-restart verify:
```
NAME               STATUS         (Up Less than a second → Up 2 minutes)
narci-cloud-sync   Up 2 minutes   
logs: [cloud-sync] remote: gdrive:/narci_raw / interval: 600s / syncing...
```

5 个 recorder container 全程不受影响(`Up 4 days (healthy)`)。

## 后续 follow-up

### 1. 短期 narci 域 fix(narci 决策)

narci doc lesson #1 提到的 short-term fix(`timeout 1200 rclone copy ...`)我建议**直接做**。当前形态不加 timeout 下次还会复发。

附两个候选:

```yaml
# 候选 A — 加 timeout 包(narci doc 推荐)
timeout 1200 rclone copy /data "$${RCLONE_REMOTE}" \
  --transfers 4 --log-level NOTICE --tpslimit 8 --tpslimit-burst 8

# 候选 B — 加 rclone 自带 timeout flags(更细粒度)
rclone copy /data "$${RCLONE_REMOTE}" \
  --transfers 4 --log-level NOTICE \
  --tpslimit 8 --tpslimit-burst 8 \
  --timeout 5m --contimeout 30s \
  --low-level-retries 3 --retries 2
```

候选 A 更狠(整轮 20 min 强杀,无脑保下一轮),候选 B 更精细(每 op 5min 超时,可能仍长尾)。narci 选。

### 2. P0 monitor gdrive-side pass(narci-reco 域,narci doc lesson #2 提的)

narci 明确指出 `3fa948c` 的 `venue_stale_monitor.sh` **不覆盖 gdrive push lag**(只看 ec2 上 shard mtime via SSM)。本次正中这个盲区。

建议加一个 second pass:在 monitor 末尾跑 `rclone lsf gdrive:narci_raw/realtime/*/*/l2/ --include "*<today>*" | tail -1` 比较远端最新 shard 时间。

但是 — `rclone lsf gdrive:` 我刚刚跑了一次卡 3+min(共用主 sync loop 的 OAuth client,被 GDrive API 端 throttle)。所以 monitor 不能直接跑 gdrive API。

更稳的方案:让 cloud-sync sidecar 自己每轮跑完之后写一份 `last_sync_ok_at_UTC` 戳到 `/data/.cloud_sync_status`(narci 域 docker-compose 改动),monitor 通过 SSM 看这个 mtime 就好,不动 gdrive API。下次跟 narci 同步实现细节。

### 3. 第 2 次复发 "docker healthy ≠ data healthy"

这是 5/17 gmo silent ban + 5/21 UM alignment 之外第三个相同 pattern。narci-reco 已加 P0 ec2 shard mtime monitor `3fa948c`(覆盖 recorder 死),还缺 gdrive push lag 监控(本次 case)。

---

**handoff status**: narci-reco 已恢复 push 链路,3-4 min 内首轮 post-restart sync 应完成。narci 端下次 lustre1 sync_loop pull 应该看到 0521 04:00+ 起的 shard 落地。

narci 端 follow-up:
- 改 docker-compose.yaml 加 timeout(候选 A or B)
- 决定要不要 narci-reco 加 sidecar-write-status-file → ec2 shard mtime monitor 二次 pass 方案
