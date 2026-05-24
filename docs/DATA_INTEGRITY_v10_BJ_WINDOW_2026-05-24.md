# 数据完整度报告 — v10 BJ BTC 24 天窗口(2026-04-17 → 2026-05-17)

**生成时间**:2026-05-24
**Scope**:nyx 7c28591 P3 first-asset(BJ BTC v10)使用的 24 天 cold tier 数据完整度
**Builder**:`research/data_integrity_v10_window.py` (commit pending)
**触发**:user 要求"就数据完整度专门出一个报告",起源于 v10 BJ BTC cache `n=640,760` 看起来偏低的疑问

---

## 0. TL;DR

- **narci pipeline 没问题**:emit/raw trade 99.8% (TRAIN) / 99.96% (OOS),几乎完美
- **真正的问题在 BJ 录得不全**:24 天 4 个 venue 综合 coverage 96.46%,但有 **3 类系统性问题**:
  1. **2026-05-14 multi-venue 10 小时 coordinated outage**(BJ/CC/UM/BS 同时大幅缺数据)
  2. **BJ 1802s "magic gap"**(4 个不同日子复现 ~30 min 断连,系统性 reconnect bug)
  3. **CC 0508 single-day 9 小时 outage**(原因未知,需查 narci-reco log)
- v10 cache 仍然 valid;**OOS R² 解读时需要 weight 0514 较低**

---

## 1. 4-venue 24-day summary

| Venue | Role | Total trades | Avg coverage | gaps≥60s | 1802s magic-gap |
|---|---|---|---|---|---|
| **BJ** binance_jp/spot/BTCJPY | **emit** (sampling 触发) | 855,862 | **96.46%** | 11 | **4** |
| **CC** coincheck/spot/BTC_JPY | feature | 578,053 | **96.20%** | **72** ⚠️ | 0 |
| **UM** binance/um_futures/BTCUSDT | feature | 28,508,781 | **96.79%** | 2 | 0 |
| **BS** binance/spot/BTCUSDT | feature(`DROP_NAMES` 移除) | 14,584,379 | **96.36%** | 6 | 0 |

**Caveat**:BS 的"96.36%" 不包含 missing 的 3 天(0510/0511/0512,文件根本不存在),仅在有文件的 21 天里平均。

---

## 2. ★ 严重问题 #1:0514 multi-venue coordinated outage

**4 个 venue 在 0514 同时挂掉 ~10 小时**:

| Venue | gap 长度 | 开始时间 (UTC) | coverage that day |
|---|---|---|---|
| BJ | **10h 51m** (39053s) | 07:15:50 | **54.60%** |
| CC | 7h 00m (25214s) | 11:04:51 | 70.49% |
| UM | 9h 53m (35614s) | 08:09:54 | 58.45% |
| BS | 9h 53m (35590s) | 08:10:04 | 58.46% |

**关键观察**:
- BJ 最早挂(07:15),CC 最晚挂(11:04),时间不完全对齐
- BJ 持续最长,CC 最短
- UM 跟 BS 几乎同步(08:09 / 08:10,差 10 秒)—— UM 跟 BS 共享 AWS-SG 同台 recorder,合理

**最可能根因**:
- **不是单一 WS 断连**(各 venue WS 是独立连接)
- **多半是 AWS-SG / narci-reco 端的 infrastructure 事件**(host reboot / docker daemon restart / 网络故障)
- 但 BJ 也是 AWS-SG 上跑的吗?需要 cross-ref `project_recording_topology` memory

**对 v10 OOS 影响**:
- 0514 单日 OOS R² 会非常不稳定(只有 ~58% 的市场数据可用)
- 当天 sample 数:BJ 20,337 trades(其他 OOS 日 20-33k 之间,看起来正常)—— 因为剩余 11 小时仍有市场活动
- **建议 nyx**:per-day OOS R² breakdown 时 explicitly weight by `coverage_pct`,或者 drop 0514 单独看

---

## 3. ★ 严重问题 #2:BJ "1802s magic gap" — 4 天复现的 ~30 min reconnect bug

24 天中有 **4 天**在不同时间出现几乎一模一样的 ~30 min 断连:

| Day | Gap 长度 | 开始时间 (UTC) | 推测当地时间 (JST) |
|---|---|---|---|
| 20260505 | **1801s** | 06:11:09 | 15:11 JST |
| 20260509 | **1802s** | 04:27:21 | 13:27 JST |
| 20260515 | **1802s** | 21:28:46 | 06:28 JST(+1d) |
| 20260516 | **1802s** | 01:39:03 | 10:39 JST |

**Pattern**:
- 时间分布**没有规律**(不是同一钟点),说明不是固定 cron 重启
- 长度高度集中在 **1801-1802 秒**(差异 ≤ 1 秒,几乎 byte-for-byte 一致)
- **只在 BJ 出现**(CC / UM / BS 24 天里没一个)

**最可能根因**:
- Binance 全球 WS 服务端**有 24h forced disconnect 是已知行为**,但是 24h ≠ 30min
- **30 min 是 BJ 端 idle-disconnect**(更可能):BJ 比 spot/UM 流量低 30 倍(0.41 trade/s vs UM 13/s),如果 BJ recorder 的 keep-alive ping 间隔大于 30 min,Binance 端可能把空闲连接断掉
- 断开后 recorder 重连大约花 30 min(可能是某种 exponential backoff 卡在大值)

**Action item**:`reco/docs/INCIDENTS/2026-05-24-bj-1802s-reconnect-gap.md` 建议建立,让 narci-reco 检查:
1. BJ adapter 的 keep-alive ping 间隔(应 ≤ 60s)
2. WS reconnect logic 的 exponential backoff cap(应 ≤ 60s)
3. 是否 OOS 没有 stale-venue alert(memory `feedback_silent_drop_pattern`)

---

## 4. ★ 严重问题 #3:CC 2026-05-08 单日 ~9 小时 outage

CC venue **0508 唯一**异常严重:
- top gap 长度未在表里直接捕获(因为单一 gap 不到 60s,但 outage_min 总和 533.8m = 8h 54m)
- coverage 只 **62.93%** —— CC 24 天里最差的一天
- 同日 trades 仅 16,890(其他正常 CC 日 30-50k)

由于 outage 不是一个 single big gap,而是分散的小 gaps,**最可能根因**:
- CC adapter 的 trade 解码 bug?(CC 的 trade 是 list-of-lists,memory `CLAUDE.md` 有提)
- CC venue 那天本身限流 / rate-limit
- 网络抖动密集

**对 v10 训练 implication**:0508 是 **TRAIN** day,数据缺一半会让 0508 那块的 feature distribution 偏移。但 16 天里只这一天,影响有限。

---

## 5. CC 全 24 天**普遍**有小 gap

CC 总共 **72 个 gaps ≥ 60s**(其他 venue 都 ≤ 11)。CC 默认 baseline 比其他 venue 更碎:

| Venue | gaps ≥ 60s 总数 / 24 天 | 平均/天 |
|---|---|---|
| CC | **72** | 3.0 |
| BJ | 11 | 0.46 |
| BS | 6 | 0.25 |
| UM | 2 | 0.08 |

**最可能根因**:
- Coincheck WS 服务端比 Binance 全球更不稳定(Coincheck 是日本 OTC,基础设施投入低)
- memory `CLAUDE.md` 描述 CC 没有 update-id alignment,完全依赖 REST snapshot + WS diff,**容错更差**

CC 是 v10 emit-venue 的**后续 target**(Path 6),如果 CC 端 silent gap 多,Path 6 production 的 backtest 准确度会受影响。**建议**:启动 CC L2 production-grade quality 监控(per-day gap audit 加入 cron)。

---

## 6. 0417 多 venue head-missing

| Venue | head_missing | 解释 |
|---|---|---|
| BJ | 88 min | BJ recorder 0417 启动 88 min 后才有第一个事件 |
| CC | 88 min | CC 也是 0417 启动晚 |
| UM | 138 min | UM 更晚 |
| BS | 0 min | BS 那天 100% coverage |

**最可能根因**:0417 是**所有 venue 同时启动的"首日"**(narci-reco 部署或迁移日),只有 BS 当时已经在跑(memory:BS recorder live since 2026-05-13;但 0417 也有 BS 数据,可能来自 Vision backfill)。

**对 TRAIN 影响**:0417 当天有效数据约 22 小时,16 天 train 里有这一天 ~6% 缺数,**忽略不影响**。

---

## 7. UM 0417/0419/0420 也有大 outage

| Day | UM gap | 备注 |
|---|---|---|
| 0417 | 198.7m (= 3h 18m) | top gap 3101s @ 13:05 |
| 0419 | 122.7m (= 2h 02m) | 但 0 internal gaps,全在 tail (122m tail-missing) |
| 0420 | 142.0m | 138m head + 3.9m tail |

UM 也是 AWS-SG 上的 recorder。0417/0420 的 head-missing 跟 BJ/CC 一致(同日启动晚)。0419 的 122 min tail-missing 说明那天 UM **当天晚些时候挂了** —— 跟 0414 multi-venue outage 不同(0414 是 mid-day),0419 是 end-of-day 挂掉。

但 UM 总体最稳健(96.79% avg,2 个 60s+ gaps 24 天),仍然是 narci 最可靠的 venue。

---

## 8. 对 v10 cache 的 implication 汇总

| Phase | Days | 高质量(cov ≥ 99%) | 中等(95-99%) | 低(< 95%) |
|---|---|---|---|---|
| TRAIN(16d) | 0417-0509 | 9 days | 6 days | **1 day**(0509 93.3%) |
| OOS(8d) | 0510-0517 | 5 days | 2 days | **1 day**(0514 54.6%) |

**Train 集 outage 加权**:16 天 × 96.46% = 等价 **15.4 全天**有效数据
**OOS 集 outage 加权**:8 天 × 95.5% = 等价 **7.6 全天**有效数据(0514 拉低)

v10 cache `n=640,760` 是真实的 = 0.988 × raw_trade_count;**与 v9_bj_midy 同窗口比较 apples-to-apples**(同样的 outage,同样的样本损失)。

---

## 9. Action items

### 立刻
- [x] 本报告 ship + 同步给 nyx(让 P3 训练时 weight 考虑 0514 outage)
- [x] 通知 narci-reco 端 BJ 1802s 问题(建议开 `reco/docs/INCIDENTS/2026-05-24-bj-1802s-reconnect-gap.md`)

### 短期(narci-side)
- [ ] **#85 healthcheck per-recorder**:5 次 silent outage 仍无报警 — 本次发现 0514 multi-venue + CC 0508 都没触发任何 alert,**升级 P0**
- [ ] CC venue 单独的 gap-audit cron(72 gaps 远超其他 venue,可能 CC adapter 有 silent-drop)

### 长期(narci-reco-side,通过 INCIDENTS 文档协调)
- [ ] **BJ 1802s 复现 + 修复**:查 BJ adapter keep-alive + reconnect backoff cap
- [ ] **0514 multi-venue post-mortem**:查 AWS-SG host log 找 infrastructure root cause(可能跟 memory `project_aws_sg_oom` 类似 t4g instance OOM)
- [ ] **CC adapter 稳定性**:Coincheck WS 重连策略,trade list-of-lists 解码 robustness

---

## 10. 附录:per-day per-venue 完整表

参见 `research/data_integrity_v10_window.py` 输出(写入 `/tmp/data_integrity_v10_window.txt`)。

每行字段:`day, phase, trades, head_missing, tail_missing, n_gaps≥60s, total_outage, coverage%, has_1802s_pattern, top_gap`

---

## Maintainer notes

- 本报告是 **point-in-time snapshot**,2026-05-24 生成。如 cold tier 后续有 backfill,数字会变化。
- 1802s "magic gap" 模式如未来其他 venue 也复现,update §3,可能升级为 cross-venue infrastructure 问题。
- nyx P3 训练完成后,如果 0514 单日 OOS R² 明显异常(eg. ≥3σ 偏离),回头加 §11 "v9/v10 OOS per-day 对比下 outage 实际影响"。
