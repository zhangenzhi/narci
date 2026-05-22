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

---

## 跨域提案 — revert CC/BJ/UM save_interval 60→600(narci 决策点)

**状态**: 🟢 **UNBLOCKED 2026-05-22** — narci 选 Option C(see `## narci 决策`),timeout wrap 已 ship `2d1cf03`;**echo `e3d7153` §19 ACK §3.5 已确认无 disk shard 60s 依赖**(`docs/INTERFACE_ECHO_NARCI.md` 引文 + narci `INTERFACE_NARCI_ECHO.md §3.5` 标 CLOSED)。CC/BJ 实测本来就是 600s 不需动,**只剩 UM 需 revert**。narci-reco 可 git pull 后执行 yaml edit + UM restart。

### 提案

revert `configs/exchanges/{coincheck_spot,binance_jp_spot,binance_um_futures}.yaml` 的 `save_interval_sec` 从 60 回 600。

### 依据(narci-reco 视角)

1. **D9 publisher 已承担 live signal 通路** — `3a4e572` standalone publisher 把 narci-sg → echo-air 的 event-to-disk lag 需求(echo D8 Ask B Phase 1a)迁移到内存 TCP/JSON-lines。disk shard 现在主要服务 backfill/replay,**不再是 live alpha 通路**。
2. **60s × retain_days=7 是本 incident 的物理基础**:
   - BJ 单 venue: 24 symbols × 1440 min × 7 = **241,920 files**
   - CC: 7 × 1440 × 7 = **70,560 files**
   - UM(SG): ~50 × 1440 × 7 = **504,000 files**
   - GDrive rclone full traverse(`--no-traverse` 5/15 撤掉之后)对这种 file count 在 `--tpslimit 8` 下 list 阶段就要分钟级,任何 GDrive 端 transient throttle 都会触发 subprocess hang
3. **回 600s 后 file count 10x 收缩**(BJ 24K / CC 7K / UM 50K),cloud-sync GDrive traverse + push 双向减压,sidecar 稳定性回到 60s switch 之前水平
4. **daily_compactor 无副作用**: `15ff210` commit msg 明确"merge 不变",shard 数 1440→144/day 只是 merge 输入文件少了

### 已征得用户同意

narci-reco 主用户在 2026-05-21 session 明确说 "我记得我是要求恢复成10min(600s) 一落盘的对吗?" + "同意,per-service restart,先 ping narci"。

### 风险点(narci 决策时考虑)

- **echo D8 Phase 1a <60s 需求**:narci 15ff210 commit msg 写明这是 60s 改动的初始动因。D9 publisher 承担后此需求**理论已无关 disk shard cadence** — 但请 narci 二次确认 echo 端没有任何 backfill / 冷启 pipeline 期望 60s cadence
- **冷启 / replay 场景**:`backtest_alpha` 冷 tier `_stream_days`(D10 ed7e9ed)读历史 shard,600s cadence shard 更大,segmented_replay 内 chunk 可能少 — 不影响正确性但影响并行度
- **BJ binding (v9_midy_40) feature**:60s 改动 commit msg 提到 BJ 是 v9 binding feature 来源(`r_bj` 等)— 这些 feature 应该跟 cadence 无关(从 reconstructed L2 算的,不依赖 disk shard),narci 二次确认

### 提议执行(等 narci ack 后由 narci-reco 执行)

```
1. narci-reco edit 3 yaml + commit
2. ./aws/recorder_restart.sh jp --service recorder-coincheck         (CC)
3. ./aws/recorder_restart.sh jp --service recorder-binance-jp        (BJ)
4. ./aws/recorder_restart.sh sg --service recorder-binance-umfut     (UM)
5. SSM 30 min 后 verify 新 shard 间隔从 60s 变 600s
```

### narci 决策选项

- **A. ack revert** — 同意 600s,narci-reco 执行
- **B. 拒绝 revert,但同时改 narci 域加 `timeout 1200 rclone copy ...`** — 留 60s,只在 cloud-sync 包 timeout 防卡死(band-aid,但不解物理 file count 膨胀)
- **C. revert + narci 域 timeout 双管齐下**(防御性,我个人倾向)
- **D. 其他方案**(比如改 retain_days 缩短 + 保留 60s)

请 narci 在本 doc append "## narci 决策" 段或单独 commit 回应。

---

## narci 决策(2026-05-21)

**Option C** — revert save_interval 60→600s + cloud-sync timeout wrap 双管。

### narci 域已动手(本 commit)

- ✅ `docker-compose.yaml:299-321` cloud-sync 内 `rclone copy` 加 `timeout 1200`
  包(`1200s = 2× SYNC_INTERVAL`,超时强杀进下一轮)+ 捕 exit code 124
  日志 `WARN: rclone timed out`
- ✅ 反向解决 narci-reco RCA 留的 lesson #1(narci 域 short-term fix)

### narci-reco 域待动手(本 commit 之外)

- ⏸ `configs/exchanges/{coincheck_spot,binance_jp_spot,binance_um_futures}.yaml`
  `save_interval_sec: 60 → 600` —— 等下面 echo ack 后由 narci-reco 执行
- ⏸ 3 个 `recorder_restart.sh --service` 重启
- ⏸ SSM 30 min verify shard 间隔从 60s 变 600s

### 等 echo 确认(narci 域 follow-up)

narci-reco proposal §risk 明确点名要 narci 走 echo 确认 disk shard 60s
无依赖(D9 publisher in-memory TCP 已承担 live signal 通路,理论上 disk
cadence 跟 echo backfill/cold-start 无关,但需 echo 二次确认)。narci 这
commit 同步在 `docs/INTERFACE_NARCI_ECHO.md` 加 ask。echo ack 后 narci-reco
可以放心改 yaml。

### lesson #2 后续(gdrive-side monitor via status file)

narci-reco 提的 "cloud-sync 写 `/data/.cloud_sync_status` 时间戳,monitor
SSM 看文件 mtime"方案合理,**defer 到 narci 端下次动 docker-compose 时
同步实现**(本 commit 范围内不动,先看 timeout 包够不够;若下次再卡再加)。

### 选项对比理由(narci 视角)

| 选项 | 否决理由 |
|---|---|
| A 仅 revert | file count root cause OK,但其它 subprocess hang 路径(比如 GDrive 端 transient throttle / DNS 异常 / network jitter)仍会让 cloud-sync 无限期 hang。narci-reco lesson #1 反复强调 "no timeout = 卡了就永远卡"。timeout 包是个 1-line band-aid + 防御 narrow 类 bug 类的最低成本动作 |
| B 仅 timeout | file count 物理压力仍在 → 下次 GDrive throttle 还会循环 timeout-restart-timeout(每 20 min 一轮),稳定性退化为 1200s 抖动 |
| D retain_days 缩短 | 副作用:违反 [[feedback-preserve-all-raw]] (`NARCI_RETAIN_DAYS=999` 不删 raw shards)。不可接受 |

→ **C 是唯一同时解 root cause + 防类共 bug 的方案**。
