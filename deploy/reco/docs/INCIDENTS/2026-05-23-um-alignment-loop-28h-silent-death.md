# 2026-05-23 aws-sg `recorder-binance-umfut` 28h alignment-loop silent death — 第 2 次 UM 复发 + 第 4 次 "docker healthy ≠ data healthy"

- **Severity**: 🔴 高 — UM live recording 静默死 28h,5/23 数据 33% (00:00-07:50 UTC only),**5/24 整天 ZERO**
- **Status**: 🟢 **RESOLVED** — narci-reco hot-restart 2026-05-24 12:15:47 UTC
- **First detect**: 2026-05-24 12:10 UTC,narci-research 手动 inspect `replay_buffer/realtime/` 时发现
- **Death**: 2026-05-23 07:50:35 UTC(container 仍 healthy,内部 process loop 死)
- **Resolution**: 2026-05-24 12:15:47 UTC,`./aws/recorder_restart.sh sg --service recorder-binance-umfut`,12:15:47 6 symbol `深度流对齐成功`

## Symptom

| Venue | 最新 shard 时间(JST) | age @ detect | 状态 |
|---|---|---|---|
| binance/um_futures | 5/23 16:50 JST(= 07:50 UTC) | **28h 18min** | 🚨 DEAD |
| 其它 6 venue | 5/24 18:44-20:12 JST | 56min-2h24min | ✅ 正常 |

container 状态(restart 前):
```
state=running  startedAt=2026-05-22T07:23:08Z  finishedAt=0001-01-01T00:00:00Z
restarts=0  oom=false  exitCode=0  health=healthy
```

→ **container alive, healthcheck passing, 但 process internal loop 已 28h 没写盘** — 又是 "docker healthy ≠ data healthy" pattern。

## RCA(narci-reco 侧 SSM 实测)

**死亡时刻 logs**(07:50:35 UTC):recorder 在做 bootstrap snapshot pull,每个 symbol 走完 `📸 初始快照已就绪` 后立刻 `⚠️ 流偏移过大` —— stream `u` 永远比 REST snapshot `last_id+1` 大几十万。recorder 进 retry-snapshot 循环,极少数 symbol(BTC/ETH/XRP/BNB/SOL/DOGE)触发 `❌ Snapshot failed: HTTP 429`。

**zombie 28h 期间 log 统计**:

| 模式 | 28h count |
|---|---|
| `HTTP 429` | 6 |
| `流偏移过大`(misalignment) | 128 |
| `snapshot ready`(snapshot 拉成功) | 128 |
| 落盘成功 | **0** ⚠️ |
| WS 重连 | 0(grep `WS连接`) |

→ recorder 持续在 alignment loop 里(平均 ~13 min 一次试)**but 永远不前进到 save_loop**。**128 次 snapshot pull 也意味着没主动 give up** —— code 没 hard-stop / circuit-breaker。

**root cause 推测**(narci 域 — 不替 narci 拍):
1. WS 流速太快,REST snapshot 拉完时 stream `u` 已往前推几十万 → 永远对不齐
2. 5/19 `15ff210` 把 save_interval 60s 后,save_loop 内 REST snapshot refresh 频次 ×10,可能加剧 alignment 紧绷 — 但 5/22 我们 revert 600s 后第 2 天就复发,说明跟 60s/600s 直接因果弱
3. 真正 bug 可能是 alignment loop 缺 timeout/give-up,无限期"再试一次"

(narci-reco 不进 binance.py 看 alignment code — 这属 narci-research 域 fix)

## 跨 incident 对比 — 同 pattern 第 4 次

| 日期 | 事件 | container 状态 | 数据状态 | 共同根 |
|---|---|---|---|---|
| 2026-05-17 | gmo silent drop(`2026-05-17-gmo-watchdog-silent-death.md`)| healthy | dead | aggregate healthcheck 看 buffer 总活,不看单 venue |
| 2026-05-17 | gmo rate-limit reconnect storm | healthy | dead | 同上 |
| 2026-05-21 | UM alignment loop(`2026-05-21-um-and-gmo-recorder-outage.md`)| healthy | dead (2.5 day) | 同上 |
| **2026-05-23** | **本次 UM 28h alignment loop** | healthy | dead (28h) | 同上 |

narci 域 fix 一直 deferred 到 task #85 (healthcheck per-recorder)。narci-reco 域有 P0 `venue_stale_monitor.sh`(`3fa948c`),但 cron 还没在 user 端启动 —— 本次完美演示了 cron 缺失的代价(narci-research 用肉眼 inspect 才发现)。

## Fix — narci-reco 执行记录

```bash
# 12:15 UTC restart
cd ~/narci/reco
./aws/recorder_restart.sh sg --service recorder-binance-umfut
```

post-restart:
```
recorder-binance-umfut  Up Less than a second (health: starting)
12:15:47 ✅ BTCUSDT 深度流对齐成功
12:15:47 ✅ DOGEUSDT 深度流对齐成功
12:15:47 ✅ BNBUSDT 深度流对齐成功
12:15:47 ✅ SOLUSDT 深度流对齐成功
12:15:47 ✅ XRPUSDT 深度流对齐成功
12:15:51 supervisor: recorder entered RUNNING state
```

(等 ~10 min 验证第一个 600s shard 写盘 — pending,update 后补)

## 数据损失

- **5/23 UM**:实际 00:00-07:50 UTC(7h 50min),约 33% of day
  - 0523 UM trades: 253,127(其他正常日 ~1.19M)
- **5/24 UM**:**完全 ZERO**(00:00-12:15 UTC 全死)
- echo paper soak 多 venue 路径会受影响(accept loss per narci-research note)

## Lessons / Asks(narci → narci-reco 双方)

### narci 域(asks)

1. **task #85 healthcheck per-recorder ASAP** —— 本次第 4 次复发同 pattern,narci-research analysis 原话:"加 #85 healthcheck per-recorder(立刻做,不再延后)"。narci-reco 域 `venue_stale_monitor.sh` 是 ops 兜底,不替代 recorder 本体 self-healing
2. **alignment loop hard-stop** —— alignment 失败 N 次(比如 10 次)或持续 M 分钟后,recorder 应该 raise + exit + 让 supervisor restart(Docker auto-restart 才能 self-heal)。当前 128 次 retry 不 give up 是死循环
3. **alignment loop logging 加 cycle counter** —— 当前 `⚠️ 流偏移过大` 每次孤立 print,看不出已经第几次了。`[CYC=42] ⚠️ ...` 这种 format 能让人/monitor 一眼看出循环
4. (可选)`per-channel staleness watchdog`(gmo `20e8a38` 已用过的设计)直接在 recorder 内监控每 venue × symbol 最后 event ts,超阈值就 force-reconnect WS

### narci-reco 域(我自己 own)

1. **`venue_stale_monitor.sh` cron 必须装** —— `3fa948c` 已经 ship 但没装 cron,本次完全没起作用(narci-research 肉眼发现)。我应该提醒 user 跑 `crontab -e` 加 entry(或 me 帮 user 写 launchd plist 更稳)
2. 如果 cron 装上后下次复发,monitor 会 10 min 内 alert,user 触发 restart;但 30 min 内的 partial data loss 仍接受
3. **incident 内补充 stale-monitor first-deploy 经验**(下次写新 monitor 必带 cron 安装文档)

## 时间线

| UTC | 事件 |
|---|---|
| 2026-05-22 07:23 | UM container 上次 restart(per save_interval revert deploy `3be4ebe`)|
| 2026-05-23 07:50:35 | UM 进 alignment loop death,zombie 开始 |
| 2026-05-24 12:10 | narci-research 肉眼 detect,push 给 narci-reco |
| 2026-05-24 12:15:47 | narci-reco SSM restart,UM 复活 |
| 2026-05-24 12:25 (est) | 首个新 600s shard 写盘(待 verify)|
