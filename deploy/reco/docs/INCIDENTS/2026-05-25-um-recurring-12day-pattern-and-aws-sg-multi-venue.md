# 2026-05-25 aws-sg `recorder-binance-umfut` 12-day recurring outage pattern + 5/24 multi-venue 同步 outage — narci 域已交付 fix,reco 域请查 host

- **Severity**: 🔴 高 — UM 在 5/13 → 5/24 区间 **12 天里大部分时间是 dead/degraded**。已严重影响 v10 cache OOS 扩展 + 任何 production multi-venue strategy
- **Status**: 🟡 narci 域 fix 已 ship(#85 healthcheck per-symbol + #105 alignment circuit-breaker),**reco 域请投入 host-level 调查**
- **First detect (本次综合 pattern)**: 2026-05-25 user 在 review v10 cache 历史时 catch
- **Resolution status**:
  - 5/23 28h alignment death:已 resolved(narci-reco hot-restart 5/24 12:15 UTC,见 2026-05-23 incident doc)
  - 5/24 multi-venue 2.5h outage:**未单独 RCA**(本 doc 首次记录)
  - 5/14 multi-venue ~10h outage:**未单独 RCA**(详见 narci `docs/archive/DATA_INTEGRITY_v10_BJ_WINDOW_2026-05-24.md` §2)

## 1. 实测 12-day UM 时间线(`narci/replay_buffer/cold/binance/um_futures/`)

narci-research 端用 `pq.read_table` 直接核 cold tier 每日 DAILY 文件:

| Day | size | n_l2 | n_trade | window (UTC) | coverage | 备注 |
|---|---|---|---|---|---|---|
| 5/17 | 298 MB | 73.8M | 700k | 00:00-23:43 | 98.8% | ✅ 正常 |
| **5/18** | 9 MB | **0** | 1.6M | 00:00-23:59 | 100% | ⚠️ **trade-only**(narci-research 端 Vision backfill,见 §3) |
| **5/19** | 5.7 MB | **0** | 992k | 00:00-23:59 | 100% | ⚠️ 同上 trade-only Vision backfill |
| **5/20** | 10 MB | 2.6M | 27k | **14:50-15:15** | **1.8%** | 🚨 全天只 25 min L2 |
| **5/21** | 141 MB | 36M | 339k | 00:00-10:26 | 43.5% | 🚨 13.6h tail-miss |
| **5/22** | 333 MB | 86M | 807k | 07:23-23:58 | 69.1% | 🚨 7.4h head-miss |
| **5/23** | 115 MB | 30M | 253k | 00:00-07:50 | 32.7% | 🚨 **28h alignment loop 起点**(已 incident 化) |
| **5/24** | 179 MB | 48M | 455k | 12:15-23:59 | 48.8% | 🚨 12.3h head-miss(5/23 zombie 延续)+ **2.5h internal gap** |

**总 12 天里:7 天有严重数据问题,只 2 天(5/17, 5/24 下半天)接近完整**。

## 2. ★ 关键发现:5/24 multi-venue 同步 outage(UM + BS 同 2.5h)

narci 实测 5/24 cold tier:

```
UM /BTCUSDT 5/24 internal gap:  15:06:36 UTC → 17:37:18 UTC   (150.7 min)
BS /BTCUSDT 5/24 internal gap:  14:59:28 UTC → 17:40:15 UTC   (160.8 min)

重叠窗口:                       14:59 → 17:37 UTC   (~2h 38min)
```

UM 跟 BS 几乎完全同步(开始差 7 秒,结束差 3 秒)。

**这是第 3 次 AWS-SG multi-venue 同步 outage**:
- 5/14 ~10h: UM/BS/CC/BJ 全挂(narci `docs/archive/DATA_INTEGRITY_v10_BJ_WINDOW_2026-05-24.md` §2)
- 5/23: UM 单独 alignment death(已 incident)
- **5/24 ~2.5h: UM+BS 同步**(本 doc)

**Root pattern**:UM + BS 共享 AWS-SG host(per `project_recording_topology` memory),共享 host 的 infrastructure event 会同步打挂这两个 venue。BJ + CC 在 AWS-JP,没受影响。

## 3. 关于 5/18-5/19 的"trade-only"

⚠️ 这不是 recorder 当时活着,是**事后 narci-research 端用 Vision backfill 补的**:

- 5/13 → 5/19 期间 UM live recording 实际 dead(Binance UM `/market` endpoint split 0423 incident 的延续后遗症,见 memory `project_binance_um_market_endpoint`)
- 2026-05-21 narci-research 端用 `data/backfill_vision_trades.py --create-if-missing` 从 Binance Vision archive 拉 aggTrades,把 5/18 + 5/19 补成 trade-only DAILY(只有 side=2 trades,没有 L2 depth — Vision archive 无 L2)
- 5/20 没补(only 25-min L2 是真实 recorder 活那 25 分钟,backfill 只补 trade-only 现在已经没意义)
- task 跟踪:narci 端 Task #96 `Backfill UM 0518/0519 trade-only DAILY from Vision aggTrades` ✅

**所以 cold tier file size 5/17 298MB → 5/18 9MB 的 "暴跌" 不是 recorder 在 5/18 凌晨突然死,而是 5/13 已经死了,5/18 的 9MB 文件是事后只补 trades 的结果**。

## 4. narci 域已交付的 fix(本周 ship)

### 4.1 `#85` healthcheck per-(venue,symbol)(commit `35fe08d`,2026-05-24)

- 修改 `deploy/healthcheck.py` 加 `check_per_symbol_staleness()`,扫 `replay_buffer/realtime/` 每 (venue_relpath, symbol) 最新 shard mtime,超 `PER_SYMBOL_STALE_THRESHOLD_SEC` (default 900s = 15min) → /health 503
- 修改 `docker-compose.yaml` 9 个 recorder container 各设 `NARCI_HEALTH_DATA_DIR=/app/replay_buffer/realtime/{venue}` —— 每 container 只看自己 venue 子树,**绕开 shared volume 上其它 venue fresh shards 掩盖自己 dead 的 5/23 pattern**
- 8 个 test 全 pass(含 5/23 UM 28h incident shape 精确复现)

**reco 域 deploy 后**:UM dead 后 15 min 内 docker compose `unhealthy` → 取决于 reco 端 monitor / autoheal 策略

### 4.2 `#105` alignment loop circuit-breaker(commit `c72b245`,2026-05-24)

- 修改 `data/l2_recorder.py` _handle_depth,per-sym 追踪 retry count + elapsed since last alignment OK
- 阈值跨越 → `os._exit(2)` → supervisord/docker 重启(visible restart_count > 0)
- 默认 10 次 retry OR 10 分钟 未 align 即触发
- env 可调:`NARCI_ALIGNMENT_MAX_RETRIES` / `NARCI_ALIGNMENT_MAX_DURATION_SEC` / `NARCI_ALIGNMENT_BREAKER_ACTION` (exit|log)
- 10 个 test pass(含 28h elapsed + 128 retries 实际 incident 复现)

**reco 域 deploy 后**:5/23-style alignment loop 进入后 **10 分钟内** 容器自动重启,**bounded blast radius 从 28h → 10min**

## 5. 给 narci-reco 的 ask

### Ask A — 立刻 deploy 上面 2 个 fix

```bash
# pull narci main 到 image build context
git pull
docker compose build recorder-binance-umfut
docker compose up -d recorder-binance-umfut

# verify
docker exec narci-recorder-umfut env | grep NARCI_HEALTH_DATA_DIR
# 应该看到:NARCI_HEALTH_DATA_DIR=/app/replay_buffer/realtime/binance/um_futures
```

需要 deploy 的 9 个 service(任何 recorder 都受益):
- recorder-coincheck / recorder-binance-jp / recorder-bitbank
- recorder-bitflyer-spot / recorder-bitflyer-fx
- recorder-gmo-spot / recorder-gmo-leverage(PARKED 也可 deploy)
- recorder-binance-spot / **recorder-binance-umfut**(★ 高优先)

### Ask B — AWS-SG host-level root cause(P1)

UM+BS 共享 AWS-SG host,**3 次重叠 outage**(5/14, 5/24)+ 持续 12 天间歇 down,**几乎肯定不是 single-recorder bug**,是 host-level:

可能原因(reco 端有 SSM access 可查):
1. **t4g.medium 仍不够**(memory `project_aws_sg_oom` t4g.small → t4g.medium 升级是 5/07,但 5/14 + 5/24 还在挂)→ 是否再升 t4g.large / m6g.medium?
2. **EBS gp3 throughput throttle**(L2 写盘高频,gp3 baseline 250 MB/s,要看 spike)
3. **Network 抖动** —— AWS-SG to Binance UM endpoint(Asia routing)是否不稳?
4. **Kernel / docker daemon 周期性 hang**(查 `journalctl -k -p err` + `docker events`)
5. **Container 间互相 OOM-kill**(2 个 recorder + cloud-sync sidecar 共 host)

建议 reco 端拉 5/14 + 5/24 的 AWS CloudWatch metrics(CPU / mem / disk / network)+ `dmesg` + `docker events`,看那两次 outage window 内 host 状态。

### Ask C — UM 5/20-5/22 recovery 期 root cause

5/13-5/19 dead 的是 endpoint split bug(已有 fix `4582d02`),但**fix 后 5/20 → 5/22 仍间歇 down**,说明 fix 不完整 或 还有第二个 bug。

reco 端能否查 5/20-5/22 期间 container log(`docker logs --since='2026-05-20' --until='2026-05-22'`)看每次 down 的 root cause?可能是同 alignment loop 模式只是 elapsed 还没到 28h 就被外部因素打断重生。

### Ask D — restart_policy / health-based auto-restart

#85 healthcheck 现在会 503,但 docker compose 默认 `restart: unless-stopped` **不看 health**。reco 端是否考虑:
1. 加 docker compose `healthcheck:` block 让 docker daemon 看 /health
2. 加外部 monitor(eg. systemd timer cron 每 N 分钟 curl /health,503 就 `docker restart`)
3. 直接用 docker swarm / k8s 的 liveness probe 替代

## 6. 跨 incident pattern summary

12 天里所有相关 incident 串起来看:

| 日期 | event | recorder log 状态 | 数据后果 | 已修? |
|---|---|---|---|---|
| 5/13-5/19 | UM endpoint split fix 后仍间歇 dead | mixed | 5/18-19 trade-only backfill;5/13-17 部分丢 | ⚠️ 部分(`4582d02`),仍有残余 bug |
| 5/14 | AWS-SG multi-venue ~10h outage | 共 4 venue 同步死 | 多 venue 各 ~50% 缺数 | ❌ 未 RCA |
| 5/20-5/22 | UM 间歇 down | 各日缺 7-23h | cold tier 各日不全 | ❌ 未 RCA |
| 5/23-5/24 | UM 28h alignment loop | container healthy 但 0 落盘 | 5/23 33% + 5/24 12h 缺 | ✅ #105 (fix 后 10min 自重启) |
| 5/24 14:59-17:37 | UM+BS 同步 2.5h | 共 2 venue 同步死 | UM/BS 各缺 2.5h | ❌ 未 RCA(本 doc 首记录) |

**Pattern**:
- ★ UM 是最不稳的 recorder(5 次 incident,4 个根因)
- ★ AWS-SG host 至少 2 次 multi-venue 同步事件(host-level 嫌疑)
- ★ "docker healthy ≠ data healthy" pattern 重复 5+ 次,#85 + #105 ship 后理论上根除

## 7. narci 域无更多代码 fix 需求

narci 这边 #85 + #105 已完成上限工作:
- per-symbol stale detection(✅)
- alignment loop circuit-breaker(✅)
- corrupted shard quarantine(✅,2026-05-22 ship `ee4ac2b`)
- compact UTC day boundary guard(✅,`feedback_compact_utc_day_boundary` memory)

剩下问题**全是 narci-reco 域 + host infrastructure 域**:
- deploy fix 到 production(reco)
- host root cause(reco SSM / AWS Console)
- restart policy(reco docker compose)

## 8. 参考文档

- 2026-05-23 incident: `2026-05-23-um-alignment-loop-28h-silent-death.md`
- 2026-05-21 incident: `2026-05-21-um-and-gmo-recorder-outage.md`
- 2026-05-21 cloud-sync: `2026-05-21-aws-jp-cloud-sync-stuck.md`
- narci 综合数据完整度报告: `narci/docs/archive/DATA_INTEGRITY_v10_BJ_WINDOW_2026-05-24.md`
- narci 域 fix commits:
  - `35fe08d` healthcheck per-symbol
  - `c72b245` alignment circuit-breaker
- 相关 task / memory:
  - narci Task #103(coord with reco — BJ 1802s + 5/14)
  - memory `project_aws_sg_oom`(5/07 t4g.small OOM)
  - memory `project_binance_um_market_endpoint`(0423 endpoint split)
  - memory `feedback_silent_drop_pattern`(`STALE-VENUE-ALERT total_shards=0` + /health green > 30min = silent drop 必查)
