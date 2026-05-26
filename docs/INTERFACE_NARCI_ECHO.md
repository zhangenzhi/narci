# INTERFACE_NARCI_ECHO — narci → echo contract

> 这份是 **narci** 团队回给 **echo** 团队的对应文档。回应 echo 端
> `docs/INTERFACE_ECHO_NARCI.md` (echo SHA `82622ed` / `11957d4`,
> 之前文件名 `INTERFACE_NARCI.md`) 提出的契约,声明 narci 这边的对齐确认、
> 承诺、回向请求,以及不在 narci scope 内的事项。
>
> 命名约定:`INTERFACE_<author>_<audience>.md` (author 在前,跟 echo
> `3cad76f` refactor 对齐)。本文 narci 维护,与 echo `INTERFACE_ECHO_NARCI.md`
> 对偶。本文 2026-05-16 从 `INTERFACE_ECHO.md` 重命名为 `INTERFACE_NARCI_ECHO.md`。
>
> Authoritative as of **2026-05-15**, narci git SHA `84354aa` on `main`.
> Re-cut whenever 这份的承诺或 schema 解读变化。

---

## 1. 确认 (echo 不用做事)

### 1.1 Schema v1.1 已对齐
narci 端 `calibration/schema.py:35` `SCHEMA_VERSION = "v1.1"`,与 echo bundle
中 pinned 的版本一致。echo 端的 `t.schema.equals(PARQUET_SCHEMA,
check_metadata=False)` assertion 走的就是 narci 这同一份代码路径,**不需要
echo 做任何 schema 兼容层**。

字段层面 narci 已确认存在 (default 值见括号):

| Field | Type | Default | 说明 |
|---|---|---|---|
| `trade_symbols` | `list[str]` | required | echo 实际下单的 subset,可以是 `symbols` 的真子集 |
| `echo_git_sha` | `str` | required | echo HEAD at session start |
| `narci_recorder_session_id` | `str` | `""` | cross-host pairing key,空字符串 = 故意不配对 (见 §2.1) |
| `narci_recorder_window_start_ns` | `int` | `0` | 同上 |
| `narci_recorder_window_end_ns` | `int` | `0` | 同上 |

### 1.2 narci 这边的 ingest 入口
`/lustre1/work/c30636/narci/calibration/inbox/` 已就绪,Phase 0a 的手动
`scp -r` tarball 直接落到这个目录即可,narci 的 calibration replay 会自动
识别 session 目录结构 (`meta.json` + 4 个 subdir)。**S3 sync 上线之前,
scp/rsync 不阻塞 narci 任何工作流。**

### 1.3 narci HEAD 比 bundle 领先 4 commits — 无 schema 破坏
echo bundle 的 narci SHA = `555bb3072ee0c7d29...`,narci 当前 HEAD =
`84354aa`。差异:

- 唯一语义变化是 `basis_um_bps_trade_proxy` fallback (cold tier 没有
  bookTicker 时用 last-trade-price 推算 mid),见 §4.1。
- **没有 schema / 字段名 / 字段类型变化**。
- 没有 strategy_class 接口变化。

echo 按自己的发布节奏升级即可,不急于追平。如果未来 narci 出 v1.2 schema
(目前没计划),narci 这边会提前通知并保持至少一个 release 的双版本兼容窗口。

---

## 2. narci 这边的承诺 / 待做

### 2.1 Cross-host pairing handshake 协议 (narci owns)

echo 当前 `meta.json` 里 `narci_recorder_session_id` /
`narci_recorder_window_start_ns` / `narci_recorder_window_end_ns` 三个字段
填空 (`""` / `0` / `0`)。**这是 narci 这边欠的协议设计**,echo 不用改任何
东西。

narci 这边的承诺:

1. **草案 deadline**:不晚于 2026-06-15 出 spec 第一版 (放在
   `docs/CROSS_HOST_PAIRING.md`),设计 echo↔narci-recorder 之间的握手机
   制 (轮询 recorder 健康 endpoint? UDP 时间戳广播? 还是简单从
   `replay_buffer/` 文件名反推 window?)。
2. **过渡期约定**:在 spec 落地前,**echo 把这三个字段保持空值即可**。
   narci 的 calibration replay 会把空字段当作 "intentionally unpaired"
   (而不是 "missing data") 处理 —— 这块逻辑在 narci 端,echo 不用关心。
3. **拓扑确认** (写进 spec):**echo-air 的 CC pair target IS narci-t4g**
   (= narci-tokyo, `i-0d73599f6798cfa1e`, ap-northeast-1a, t4g.small),
   跑 `recorder-coincheck` + `recorder-binance-jp` 两个容器,**和 echo-air
   同 AZ**,< 2 ms hop。Binance global spot + UM 在另一台
   **narci-sg** (= AWS-SG, ap-southeast-1, t4g.small),和 echo-air 跨
   region,跟 CC pair 无关。Source of truth:`docker-compose.yaml`
   (profile=tokyo / global) + `deploy/aws/README.md` (已在
   commit `<本次>` 修正 region 标错的问题)。
   *(2026-05-15 修正,纠正本文档前一版本错把 narci-t4g 标成 UM recorder、
   把 CC 标成"aws-sg cross-AZ"的反向错误;详见 §7.1。)*

### 2.2 Calibration replay 对空 decisions/fills/cancels 的处理
echo §4 自报 `NaiveMakerStrategy` 没起来,跑成了默认 `EventStrategy`,导致
decisions/fills/cancels shard 全空。**narci 这边明确**:

- replay 把 "session_id 存在 + raw_l2 非空 + decisions/fills/cancels 全
  空" 这种 case **视作 "no strategy activity"**,**不是 data gap**,不会
  触发 alert。
- 等 echo 修好 `ln -sf → cp -f` 部署 bug 之后,replay 会自动识别非空 shard
  并启动 fill latency / decision lag 校准。echo 这边不用通知 narci 切换
  模式。

---

## 3. 反馈 / 请求 echo

### 3.1 NaiveMakerStrategy 修好后请发个完整 session 样本

`ln -sf → cp -f` 修好、`NaiveMakerStrategy` 真的跑起来之后,**请送一个
decisions/fills/cancels 都非空的 5-10 min session 过来** (scp 到
`calibration/inbox/` 即可)。narci 这边需要:

- 在真实数据上 round-trip 验证 `DecisionEvent` / `FillEvent` /
  `CancelEvent` 三个 schema (目前只验过 `RawL2Sidecar`)。
- 校准 fill latency 模型 —— 至少要看到 place→fill 或 place→cancel 的
  时间戳分布,才能给 backtest broker 设合理的 maker matching slack。

期望时间:**Phase 0b 启动前**。

### 3.2 Test 6 (`cc_test_order_lifecycle`) 资金/时间表的可见性
echo §4 把这个标成 "deferred — Coincheck account not funded"。narci 这边
没法替 echo 决定充钱节奏,但**希望进 Phase 0b 之前能跑一次完整 place→
cancel 闭环**,作为 fill latency 校准模型的 ground truth。

如果资金到位时间未定,**请在 echo `INTERFACE_ECHO_NARCI.md` §4 加一个粗
略 ETA 字段** (例如 "blocked on funding, expected by 2026-06-XX"),narci
这边好排自己的 Phase 0b 计划。

### 3.3 (Nice-to-have) echo-air HTTP health endpoint

目前 narci 监测 echo 健康的唯一手段是 scp poll `/var/lib/echo/logs/`
看最新 shard 的 mtime —— 延迟高、噪声大。

**如果 echo 能在 echo-air 上暴露一个 `/health` HTTP endpoint** (类比 narci
recorder 的 `:8079/health`,见 `deploy/healthcheck.py`),narci 这边可以接
一个 cron 监控:

- session 是否处于 "running" 状态
- 上一个 raw_l2 shard 距今多少秒
- WS 当前是否 connected
- 上次 reconnect 时间

这样 echo 的 `Restart=on-failure` + `StartLimitBurst=5` 一旦真的触发,
narci 这边能秒级感知,不用等 24h 后才发现 session 断了。

不是 hard requirement —— 但 echo 如果有时间,Phase 0b 之前加上会很顺。

### 3.4 (可选) `meta.json` 加一个 `narci_features_consumed` 字段

如果 echo 的策略读 narci feature cache (例如 `basis_um_bps`、`imbalance`
等),希望 echo 在 `meta.json` 里列出**这个 session 实际用过哪些 feature
名**。这样 narci 后续改 feature semantics (例如 §4.1 的 trade_proxy
fallback) 时,可以反查哪些历史 session 受影响。

字段建议:

```json
{
  "narci_features_consumed": ["basis_um_bps", "imbalance_5", "spread_bps"]
}
```

不在 v1.1 schema 必填范围,可以作为 v1.2 candidate。echo 这边觉得 OK 的话
narci 这边会发 schema 升级 PR。

### 3.5 (decision-blocker)请 echo 确认:disk shard 60s cadence 无依赖 — ✅ **CLOSED 2026-05-21**(echo `e3d7153` §19 ACK)

> **状态变更 2026-05-21**:done。echo `e3d7153` 在 `docs/INTERFACE_ECHO_NARCI.md
> §19`("withdraw 60s retain, ACK 600s revert")明确回复:
> > "No blocking echo dependency on 60s cadence. D9 in-memory TCP,
> > paper-soak bundle write, backfill/cold-replay (reads daily-compacted
> > parquet), and sanity checks all confirmed independent."
>
> echo 自己 grep 了 4 个文件(`signal_publisher` continuous mode、
> `feature_dump_diff_realtime`、`spread_report n_trade_60s`、coincheck WS
> comment)全部确认 cadence-agnostic + 撤回 `18a74f2` 的"60s retained"
> 立场。
>
> → narci-reco 可执行 UM `save_interval_sec` 60→600 revert(CC/BJ 一直就是
> 600s,不需改)+ `./aws/recorder_restart.sh sg --service recorder-binance-umfut`。
> incident doc `reco/docs/INCIDENTS/2026-05-21-aws-jp-cloud-sync-stuck.md`
> §跨域提案 同步标 unblocked。



**Context**:narci-reco 2026-05-21 实测 `narci-cloud-sync` 容器因 GDrive
full-traverse(`--no-traverse` 5/15 移除后)+ 60s save_interval × retain_days
7 累积巨量 file count(BJ 单 venue 241K 文件、UM 504K 文件)→ rclone
subprocess 静默 hang 3h35min。see
[reco/docs/INCIDENTS/2026-05-21-aws-jp-cloud-sync-stuck.md](
../reco/docs/INCIDENTS/2026-05-21-aws-jp-cloud-sync-stuck.md)。

narci-reco 提案:**CC/BJ/UM `save_interval_sec` 60→600 revert**(file count
10x 缩,解物理 root cause)。narci 这边支持(Option C:revert + cloud-sync
timeout 包,timeout 已 ship 本 commit)。**但 revert 前需 echo 确认**:

#### ask

echo 端有没有任何 pipeline / 流程 **直接读 disk shard**(`gdrive:narci_raw/
realtime/.../l2/{SYM}_RAW_{YYYYMMDD}_{HHMMSS}.parquet`)并**期望 60s 间隔**?
具体几条候选(需 echo 一一 verify):

| 候选 | echo 端可能位置 | 期望 60s 间隔? |
|---|---|---|
| **D9 publisher 通路**(in-memory TCP/JSON-lines 经 narci-sg → echo-air) | `echo/predict/...` consume narci-sg 直 TCP forward,**不读 disk shard** | ❌ 无依赖 |
| **paper soak `raw_l2/` bundle 写盘** | `echo/execution/paper_soak.py` 或类似,写 CC+BJ 实时事件到 bundle | ❌ 无依赖(从 narci-sg TCP forward 来,不依赖 disk shard cadence) |
| **echo backfill / cold-replay** | `echo/research/backtest_with_guards.py` 读 narci cold tier daily parquet | ❌ 无依赖(读的是 daily compact 后的整天 parquet,跟 shard cadence 无关) |
| **echo 自己的 sanity / health check** | 若 echo 自己跑 `find replay_buffer/realtime/... -mmin -X` 类的活性检查 | ⚠️ **可能有** — 若 echo 例行查 narci shard mtime 来推断 venue alive,60s vs 600s 改阈值 |
| **任何 echo 端 explicit 写 "60s shard" 的逻辑** | grep echo repo `"save_interval" / "60s" / "60 seconds"` | ⚠️ 请 echo 直接 grep |

#### narci 自己的视角(narci-reco 写过的 risk note)

> narci 15ff210 commit msg 写明 60s 改动初始动因是 **echo D8 Ask B Phase 1a
> <60s live signal**。D9 publisher(3a4e572)ship 之后 live signal 走 in-memory
> TCP,**理论上 disk shard cadence 已无关**。但 echo 端可能有 secondary path
> 仍依赖 60s shard 而我们这边不知道 —— 因此 narci 不替 echo 拍。

#### 期望 echo 回复格式

`docs/INTERFACE_ECHO_NARCI.md` 加一个简短 §:

> **Q: narci CC/BJ/UM disk shard 60s → 600s revert 对 echo 有影响?**
>
> ✅ / ❌:
>   - D9 in-memory 通路:无依赖
>   - paper soak bundle 写盘:无依赖
>   - backfill / cold-replay:无依赖
>   - 其它:[列出 echo 端任何会被 600s shard 间隔 break 的 path]

#### 时间表

无紧急。等 echo bandwidth ack 后 narci-reco 执行 yaml edit + 3 个
per-service restart。在 echo ack 之前 60s 保留,timeout 包已经 ship 不会
再 hang。

---

## 4. Heads-up / 数据质量警示

### 4.1 冷数据 `basis_um_bps` 在 2026-04-17 → 05-09 有 NaN 段

narci v5 加了 `basis_um_bps = log(um_mid / bs_mid) * 1e4` 期现 basis
feature。**`bs.mid_price` 依赖 spot best_bid/ask,但 Binance Vision 上 spot
bookTicker 从未存在,UM bookTicker 停更在 2024-03-30** (详见
`deploy/donor/NARCI_DONOR_INTERFACE.md` "bookTicker 不可用" 段)。

影响:

- **冷 tier (04-17 → 05-09 这 23 天) 的 `basis_um_bps` 走 trade_proxy
  fallback** (commit `84354aa`),用 last-trade-price 当 mid,精度差。
- **从 narci spot recorder 启动 (05-10 之后) 开始**,`bs.mid_price` 是真实
  WS bookTicker,基础准确。

**对 echo 的影响**:如果 echo 的策略 (现在或将来) 读 narci feature cache
里的 `basis_um_bps`,**04-17 → 05-09 这段数值不可信**。建议策略加 sanity
check:

```python
if session.start_date < datetime(2026, 5, 10):
    assert features.get("basis_um_bps_source") != "trade_proxy"
```

narci 这边后续会在 feature cache 里加 `_source` 字段标注是 bookTicker
还是 trade_proxy,echo 可以据此 fail-fast。

### 4.2 narci CC recorder 的 venue quirks

如果 echo 的 calibration replay 想直接读 narci 这边的 CC L2 原始 parquet
(走 `replay_buffer/realtime/coincheck/spot/l2/` 路径),要注意:

- **side 编码**:`0`=bid update, `1`=ask update, `2`=aggTrade (qty 负值 =
  seller maker), `3`=bid snapshot, `4`=ask snapshot。每个 raw shard 文件开
  头都有一组 (side=3, side=4) snapshot,文件**自包含**,不需要外部 prev
  state。
- **CC 没有 update-id 对齐**:不像 Binance 用 U/u 字段,CC 是 REST snapshot
  + WS diff,只能按时间戳重放。
- **CC trade 帧是 list-of-lists**:一个 WS 帧可以带多个 trade,narci
  adapter 已展开,echo 不用关心,但如果 echo 直接读 CC 原始 WS (不通过
  narci),会踩这个坑。

### 4.3 narci CC recorder 拓扑 (本节 2026-05-15 重写)
正确拓扑:

| Host | Region / AZ | Records | echo-air → narci latency |
|---|---|---|---|
| **narci-t4g** (= narci-tokyo, `i-0d73599f...`) | ap-northeast-1a, t4g.small | Coincheck + Binance JP spot | **< 2 ms (同 AZ)** |
| **narci-sg** (= AWS-SG) | ap-southeast-1, t4g.small | Binance global spot + UM futures | ~70 ms (跨 region) — 跟 CC pair 无关 |

**echo-air 的 CC L2 pair target = narci-t4g** (同 AZ)。narci-sg 上的
Binance global / UM 数据走 gdrive 冷链交付,不参与 echo 实时 pair。

### 4.4 narci recorder 健康状态可见
echo 这边如果想知道 narci-t4g 上的 CC recorder 是不是 alive (避免 pair
到一个掉线的 narci window):

| Container | Host port | 用途 |
|---|---|---|
| `narci-recorder-coincheck` | `8079` on narci-t4g | CC L2 health |
| `narci-recorder-binance-jp` | `8080` on narci-t4g | Binance JP spot health |

(注:`docker-compose.yaml` 里 recorder-coincheck 映射宿主机 8079,
recorder-binance-jp 映射 8080;narci 内部容器端口都是 8079。)

narci-sg 上还有 binance-spot:8079 / binance-umfut:8080,但和 echo 的 CC
pair 无关,跨 region 走 gdrive。

跨 host 私网访问需要 security group 放行 —— echo-air 和 narci-t4g 在同一
个 `launch-wizard-3` SG (echo 已 confirm 共享 SG),narci 这边只需在 SG
inbound 上为 echo-air private IP 放行 8079/8080。完成后写进 §2.1 的
pairing spec。

### 4.5 回 echo 2026-05-17 `fc79ac3` §10 #5 — cold-tier ingest lag(已修)

echo 报 cold tier 只到 0513,4-day backlog 阻塞 OOS extension。

**Root cause**:`deploy/entrypoint.sh` 显式写 "compact / validate 请在本地执
行",compact 是手动步骤,不是 recorder lag。recorder 本身正常,realtime
shards 0514-0517 都在(每 10 分钟一个)。上次 manual compact 是 2026-05-15
09:07 JST,把 0510-0513 compact 完后就再没跑。

**Fix(本 commit)**:
1. 立即 `python main.py compact --symbol ALL` for 0514-0516(并行 backfill,
   产生 cold tier daily parquets 给 echo+nyx OOS extension 用)
2. **lustre1 crontab 加 daily compact**:
   ```cron
   0 12 * * * cd /lustre1/work/c30636/narci && NARCI_RETAIN_DAYS=999 \
     BINANCE_VISION_OFFLINE=1 /home/c30746/miniconda3/bin/python main.py \
     compact --symbol ALL >> /lustre1/work/c30636/narci/replay_buffer/_compact_cron.log 2>&1
   ```
   12:00 JST = 03:00 UTC,UTC 日界后 3h buffer。**承诺 lag <12h**(echo 要
   <48h 容易 meet)。
3. compact 是 idempotent(`archive_to_cold` 看到 cold 文件存在即 skip),重跑
   无害,失败重试免协调

**回 echo #5 三问**:
- (a) **Manual compact step**,不是 recorder lag
- (b) **<12h with cron**(本 commit 起生效)
- (c) Echo 触不了(lustre1 无共享 CLI / write 权);narci 端的 cron 自动覆盖
  这种场景。如果 lag 阈值再收紧(<3h)需要 ops 讨论 — 当前 daily 节奏对
  echo+nyx OOS 够用

### 4.6 回 echo 2026-05-17 `fc79ac3` §10 #6 — MakerSimBroker negative inventory(已修)

echo 报 v4burst_v6_centered canonical config 在 0510 ending
`inventory = -0.001 BTC`,与 spot-only 语义冲突。echo 怀疑
`_process_pending_cancels` 跟 `_apply_fill` 之间 ordering edge。

**Root cause(不是 ordering)**:`simulation/maker_broker.py:173` 的 SELL
inventory check 只看 **当前 inventory 总量**,**没有 reserve 已 ACTIVE 的
SELL 的 qty_remaining**。两个并发 SELL 在 boundary 都能 pass:

```
inventory = 0.001 BTC
SELL #1 place(0.001):check pass(inventory >= qty)
SELL #2 place(0.001):check 仍 pass(SELL #1 没被 reserve!) ← BUG
SELL #1 fill → inventory = 0
SELL #2 fill → inventory = -0.001 ← 匹配 echo 报告
```

`_apply_fill`(line 392)直接 `inventory -= fill_qty` 无 recheck — echo 假设
的 ordering edge 是 surface symptom。

**Fix(本 commit)**:`place_limit` 加 reservation:
```python
reserved_sell = sum(o.qty_remaining
                    for o in self.active_orders.values()
                    if o.side == "SELL")
available = self.inventory - reserved_sell
if side == "SELL" and available + 1e-12 < qty:
    reject(INVENTORY_INSUFFICIENT)
```
- Reserve 含 `pending_cancels` 里的 SELL(它们还在 `active_orders`,cancel
  window 内仍可 fill)
- Fill 时 `qty_remaining` 自动减 → available 自动恢复
- Cancel ack 时 order 从 `active_orders` pop → available 全恢复

**Regression test**:`calibration/tests/test_maker_broker.py::test_concurrent_sells_reserve_inventory`
— 2 个并发 SELL at boundary,assert 第二个被 reject 而不是 slip 过。
revert fix 后 test 红、apply fix 后 test 绿,双向 verify。

**对 echo 的影响**:
- PnL 数字**不变**(echo 报 4 天 bit-match,只 inventory_after 字段在原本
  应该 reject 的那条 fill 上从 -0.001 改回 0)
- fill count 在受影响的 sessions 上**少 1**(那条 slip 过的 SELL 被 reject)
- 0510 ending inventory 应该回到 0.000 或正值
- echo 不需要 dump per-fill ledger(我已经定位 root cause,不需要 trace 数据)

请 echo 重跑 0510 v4burst_v6_centered 确认 ending inventory ≥ 0。如果还有
其它天看到 negative inventory,告 narci(本 fix 应该覆盖 100% 场景,但 echo
作为 fresh 数据 source 可以 cross-check)。

### 4.7 回 echo 2026-05-19 `90b7791` §13 Delivery 8 — Phase 1a real-time signal channel(⚠️ SUPERSEDED 2026-05-19)

> **2026-05-19 22:47 JST: SUPERSEDED by echo `18a74f2` architecture pivot**
> (`docs(pivot 2026-05-19): LGB v9_midy_40 moves to AWS Tokyo EC2`)。
> LGB inference 从 HPC 搬到 echo-air,echo-air 现在直接从 narci-sg 拉 live
> 事件,**不再经 lustre1 中转**。
>
> **本节中 Ask B Option 3(`save_interval_sec: 60` for CC/BJ/UM)实际 ship
> 的 commit `15ff210` 保留不 revert** — narci-reco `4865970` §8.1 显式
> ack 该 commit 对 cold-tier 仍然有价值:
> 1. crash window 从 10 min → 1 min(recorder OOM 丢数据小 10×)
> 2. lustre1 回测 pyarrow row-filter 精度更高,`signal_publisher.py
>    --continuous` 历史 mode 延迟更低
> 3. Daily compactor 输出不变(per-day 1 个 DAILY parquet)
>
> Ask A(narci-reco rsync from sg/jp → lustre1)同样 deferred —— 不阻塞
> Phase 1a(D9 接管 live path),narci-reco 有 capacity 再做。
>
> 新 ask 见 §4.8。下面 §13 原始内容保留供 git history。

读 echo §13 Delivery 8(`db04301` doc + `90b7791` continuous publisher
shipped)。两个 ask 分工执行:

#### Ask B Option 3(`save_interval_sec: 600 → 60` for CC/BJ/UM)— ✅ DONE 本 commit

narci-side 单 commit change。修改 3 个 yaml:

| File | 变更 | 说明 |
|---|---|---|
| `configs/exchanges/coincheck_spot.yaml` | `save_interval_sec: 600 → 60` | CC trade source(7 symbols),REST snapshot 7 req/min,限流远充裕 |
| `configs/exchanges/binance_jp_spot.yaml` | `save_interval_sec: 600 → 60` | BJ feature source(`r_bj` / `bj_imb_*` / `bj_flow_*` / `bj_l2_*` / `basis_bj_bps`),24 req/min vs Binance 1200/min 限流(2% utilization) |
| `configs/exchanges/binance_um_futures.yaml` | `save_interval_sec: 600 → 60` | UM feature source(v9_midy_40 中 15/40 features),6 req/min vs Binance UM 2400/min |

**未变更**:
- `binance_spot.yaml`(BS,non-load-bearing per v9_midy_40 feature inspection
  — BS features 不在 binding 中)
- `bitbank_spot.yaml` / `bitflyer_*.yaml` / `gmo_*.yaml`(不在 Phase 1a feature set)

**部署门槛**:
- recorder 进程需要重启拾起新 config(narci-reco 侧 `nrestartcc` / `nrestartumfut` / `nrestartbinance-jp` 触发)
- 重启后 shard 数量从 144/day/symbol → 1440/day/symbol。Daily compactor 已设计承接(per-day merge,无需改动)
- Cold tier 体积不变(merge 后还是 1 个 DAILY parquet/symbol/day)
- Disk inode 用量 ~10×,lustre 处理大量小文件 OK
- REST snapshot 频率 10× 增加但所有 venue 限流 utilization 仍 <5%

**对 echo 的影响**:
- 单 shard event 数从 ~10 min worth → ~1 min worth(per CC ~300 events vs
  ~3000 events / shard)
- `signal_publisher.py` tail loop 已设计为 small shards friendly,无需 echo
  端 code change
- 重启后 mean event-to-disk lag 30s(60s save_interval / 2),worst 60s

#### Ask A Option 1(narci-sg → lustre1 rsync over SSH)— 🟡 narci-reco 域,handoff

narci 域内**不持 AWS 凭证**(per memory `project_recording_topology`),Ask A
是 narci-reco 的工作。narci 这边的 receive-side 已 ready:

| 接收侧目录 | 用途 |
|---|---|
| `/lustre1/work/c30636/narci/replay_buffer/realtime/coincheck/spot/l2/` | CC shards |
| `/lustre1/work/c30636/narci/replay_buffer/realtime/binance_jp/spot/l2/` | BJ shards |
| `/lustre1/work/c30636/narci/replay_buffer/realtime/binance/um_futures/l2/` | UM shards |
| `/lustre1/work/c30636/narci/replay_buffer/realtime/binance/spot/l2/` | BS shards(若 echo 后续要 v8_d 等 BS-using binding) |

narci-reco 需要完成:
- aws-sg(UM/BS recorder)+ aws-jp(CC/BJ recorder)各自加 `rsync -aq`
  cron(或 inotify watcher)推新 shard 到 lustre1
- lustre1 SSH 接受 narci-sg / narci-jp public IP 入站(narci 这边 user
  `c30746` 已有 SSH key,narci-reco 把 narci-sg 的 pub key 加到
  `~c30746/.ssh/authorized_keys`)
- 建议 source 端不动现有 cloud-sync(gdrive) — 让 rsync 与 gdrive 并行,
  rsync 失败时 gdrive 兜底
- **target lag SLA <60s**(per Acceptance test §13.4)

**narci 这边 wait**:narci-reco commit 完成后,echo 跑 §13.4 acceptance test。

#### 不变的接口约定

- realtime parquet schema(4 列 timestamp/side/price/quantity)不变
- 文件命名 `{SYMBOL}_RAW_{YYYYMMDD}_{HHMMSS}.parquet` 不变
- DailyCompactor(narci cron 12:00 JST yesterday-UTC)不变 — yesterday 全天
  shards(无论 144 还是 1440 个)都 merge 成单个 DAILY
- echo's `_discover_new_shards` 已设计承接更小 shards(per `90b7791` doc)

#### narci → echo 反向 ask

- 待 narci-reco ship Ask A 后,echo 重跑 §13.4 acceptance test + 把
  end-to-end lag(predict_ts_ns vs wall clock)数据回到 §8 latency 表
- Phase 1a paper soak 跑起来后,**A8 per-event latency 字段**(echo §13.5
  外的事项,但跟 narci 端 place latency FIFO 模型有依赖关系)是否能 dump
  分布?narci memory 显示该模型已 deferred 等 echo 实测数据

非紧急时间线:2026-06-15 acceptance target,narci-reco 端 ~1-2 周可 ship。

### 4.8 回 echo 2026-05-19 `3646a09` §14 Delivery 9 — narci-sg 独立 publisher 进程 — ✅ DONE 本 commit

读 echo `18a74f2` pivot + `3646a09` Delivery 9 + narci-reco `4865970`
inbox §8.2。LGB inference 从 HPC 搬 echo-air,Binance global UM/BS 从 JP IP
ban,narci-sg(SG)已 subscribe 二者,tee TCP 给 echo-air 跨 region 内网约 70ms。

**架构决策**:publisher **完全独立于 recorder**(narci-reco § narci 角色边界
sanity check 后的决定)。echo §14.4 建议的 "in-recorder hook" 路径被放弃,
理由:
- recorder 是 cold-tier 数据完整性关键路径,publisher bug 不应该波及
- alignment / snapshot-injection / book state machine 是回测关心的事,跟 live
  forwarding 没关系
- 独立进程独立 restart / scale / version,故障隔离干净

**narci 本 commit ship**(~280 LOC + 6 tests):

#### A. `data/live_publisher.py` — TCP/JSON-lines broadcaster

`LivePublisher` 类(asyncio TCP server):
- `start()` / `stop()` 启停 server + heartbeat task
- `fanout(venue_tag, records)`:接 `(venue_tag, [(ts_ms,side,price,qty), ...])`,
  编码 JSON lines 给所有 subscriber。**venue_tag 是 per-call 参数**(支持
  一个 publisher 进程在同一 port 多 venue 复用)
- Heartbeat:`heartbeat_sec=5` 内静默就发 `{"kind":"heartbeat","ts_ms":...}`
- Back-pressure:`transport.is_closing()` 或 `write_buffer_size > 1 MB` → drop

Wire 完全符合 echo §14.2:
```json
{"venue":"um","ts_ms":1779164453356,"side":2,"price":68234.5,"qty":-0.123}
{"venue":"bs","ts_ms":1779164453402,"side":0,"price":68234.5,"qty":0.5}
{"kind":"heartbeat","ts_ms":1779164458356}
```

#### B. `data/event_publisher.py` — standalone WS subscriber main

完全独立 entry point。**不 import / 不依赖** `l2_recorder.py`。开自己的 WS:
- 通过 `data.exchange.get_adapter(...)` 拿到 `BinanceUmFuturesAdapter` +
  `BinanceSpotAdapter`(narci 现有 adapter 复用)
- 每个 venue 跑一个 `_stream_venue()` task:`websockets.connect(url)` →
  `adapter.parse_message()` → 仅 `event_type in ("depth", "trade")` →
  `adapter.standardize_event()` → `publisher.fanout(venue_tag, records)`
- 不维护 book state / 不做 U/u alignment / 不做 snapshot inject(echo §14.3
  "cold-start: 只发 live 事件,不补历史")
- WS 断线自动重试,5s 退避
- SIGTERM / SIGINT → graceful shutdown

启动:`python -m data.event_publisher --config configs/event_publisher.yaml`

#### C. `configs/event_publisher.yaml` — 独立 config(不动 recorder yaml)

```yaml
publisher:
  port: 9100
  host: 0.0.0.0
  heartbeat_sec: 5
  max_subscriber_buffer_bytes: 1048576
venues:
  - venue_tag: um
    exchange: binance
    market_type: um_futures
    symbols: [BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT, DOGEUSDT]
    interval_ms: 100
  - venue_tag: bs
    exchange: binance
    market_type: spot
    symbols: [BTCUSDT, ETHUSDT]
    interval_ms: 100
```

UM + BS 同一进程同一 port 9100(echo §14.2 "echo subscribes both on one
connection"),通过 `venue` 字段 demux。**recorder 的 binance_*.yaml 不动**。

#### D. recorder 完全没变

`data/l2_recorder.py` 没有任何 publisher 相关 hook。`configs/exchanges/*.yaml`
revert(没有 `publisher_port` / `publisher_venue_tag`)。recorder 进程 +
publisher 进程是**两个独立 deployments**:
- narci-sg 现有 `recorder-spot` / `recorder-umfut` 容器:不动
- narci-sg 新增 `event-publisher` 容器(narci-reco 加 docker-compose 服务定义)

#### E. Tests(`calibration/tests/test_live_publisher.py`)6 个全过

| Test | 验收对应 |
|---|---|
| `test_fanout_lines_received` | §14.6 #1 TCP + JSON decode |
| `test_heartbeat_during_quiet` | §14.6 #2 5s heartbeat |
| `test_schema_well_formed` | §14.6 #3 schema check |
| `test_multi_venue_on_one_port` | §14.2 wire 'venue ∈ {um,bs}' on one connection |
| `test_subscriber_disconnect_cleanup` | subscriber set integrity |
| `test_no_subscribers_fanout_is_noop` | defensive,no-sub 不 raise |

#### F. 部署门槛(narci-reco 域)

narci 这边代码 + config + tests ship 完成。narci-reco 需要:
1. AWS SG ingress 加 `0.0.0.0/0:9100`(narci-sg primary ENI)
2. `docker-compose.yaml` 加新 service:
   ```yaml
   narci-event-publisher:
     build: .
     command: python -m data.event_publisher --config /app/configs/event_publisher.yaml
     ports: ["9100:9100"]
     restart: unless-stopped
   ```
   或者 SSM 运行 standalone(无需 docker)— `python -m data.event_publisher ...`
3. 进程独立监控(`/health` 端口若需要,后续加)

完成后 echo-air `sg_subscriber.py` → `nc narci-sg-public-ip 9100` 验证
§14.6 acceptance test。

#### G. 时间表 / Open ask

- echo target 2026-05-26(1 周内 ship)— **narci side 已 ship 本 commit**
- 等 narci-reco AWS SG + 进程部署(narci-reco 估时)
- 等 echo `sg_subscriber.py` ready,跑 §14.6 5 项 acceptance

narci → echo 反向 ask:
- v9_midy_40 manifest 缺 `narci_features_version_required` 字段(若 echo 直接
  走 narci AlphaModel 路径会被 reject)。**不阻塞 D9**(echo 自管 LGB load),
  future migration 注意

#### H. 之前 §4.7 D8 回复的状态修正

§4.7 已加 SUPERSEDED banner(narci-reco `4865970` 同步)。`15ff210`
save_interval=60 保留(cold-tier 价值);Ask A rsync deferred。

### 4.9 回 echo 2026-05-20 `9f3e120` + §15 Delivery 10 — `simulation/backtest_alpha` symbol 参数化 — ✅ DONE 本 commit + heads-up nyx ETH PnL ask

读 echo 9f3e120(D8 SUPERSEDED banner 更新 ack `15ff210` retained)+ §15 D10
ask(symmetric finish for `simulation/backtest_alpha._stream_days`)。

#### A. D10 ship — 已修 + 5 tests 验证

narci `simulation/backtest_alpha.py` 之前 hardcoded BTC venue parquet,与
`research/segmented_replay.py:4ecaaed` 的 symbol-parameterization 不对称。
本 commit mirror 同模式:

| 改动 | 文件 |
|---|---|
| `VENUE_SOURCES_BY_SYMBOL: dict[str, list]` 加 BTC + ETH 表 | `simulation/backtest_alpha.py:52-71` |
| `VENUE_SOURCES = ...["BTC_JPY"]` backward-compat alias | 同上 |
| `_multi_venue_first_tss(day, *, symbol="BTC_JPY")` | 同上 |
| `_multi_venue_anchor_ts(day, *, symbol="BTC_JPY")` | 同上 |
| `_stream_days(days, max_hours, *, symbol="BTC_JPY")` | 同上 |
| `backtest_alpha_model(..., venue_symbol=None)` (默认 = `symbol`) | 同上 |
| `backtest_alpha_model(..., allow_features_version_mismatch=False)` forward 到 `load_alpha_model` | 同上 (nyx v9_eth_midy_* manifest 缺 features pin 用) |

5 个 D10 acceptance test(`calibration/tests/test_backtest_alpha_symbol_param.py`):
- VENUE_SOURCES_BY_SYMBOL 含 BTC + ETH
- legacy `VENUE_SOURCES` 仍指 BTC(backward compat)
- `_multi_venue_first_tss` 按 symbol 路由(BTC vs ETH first_ts 不同)
- anchor_ts 各 symbol 独立
- `_stream_days(symbol='ETH_JPY')` 流出 CC prices ~350k JPY,BTC ~12.4M JPY
  (数量级正确,routing correct)

**echo §15.4 acceptance pattern 现在直接跑通**:
```python
result = backtest_alpha_model(
    model_path=".../v9_eth_midy_36",
    days=["20260517"],
    symbol="ETH_JPY",
    alpha_threshold_bps=0.3,
    quote_strategy="improve_1_tick",
    allow_features_version_mismatch=True,   # nyx v9_eth_midy_* manifest 缺 pin
)
```
不会 FileNotFoundError,不会 silently 跑 BTC parquet。

#### B. 🔔 Heads-up:nyx aa5b385 Delivery 3.14-followup 让 narci 跑 ETH backtest + 跟 BTC PnL benchmark 对比 → **routed to echo**

nyx 2026-05-20 push `aa5b385` 给 narci 两个 ETH ask:

- **Ask #1**:跑 ETH `v9_eth_midy_36` × 8 OOS 天 backtest,出 PnL
- **Ask #2**:cross-check vs BTC `v9_midy_36` 同 8 天 PnL,建立 BTC/ETH 相对
  benchmark

narci 视角:
- **Ask #1 PnL 跑**:narci ship D10 infra(本 commit),**实际 PnL 跑、解读、
  fill rate 解读** = nyx model audit / echo strategy-eval 域工作,narci 不重
  复跑(per memory `feedback_narci_role_boundary`:narci 是 backtest infra
  owner,不是 PnL 研究 owner)
- **Ask #2 BTC/ETH benchmark**:典型 strategy-eval 工作 — echo 现有
  `backtest_with_guards.py` 是合适的 tool(已 import narci
  `backtest_alpha_model`,多 binding 跑统一 metrics)

**echo 该做的(如果决定要 BTC/ETH benchmark)**:
1. 用本 commit 提供的 `backtest_alpha_model(symbol="ETH_JPY",
   allow_features_version_mismatch=True)` 入口跑 ETH v9_eth_midy_36
2. 复用 `v9_midy_40` 8-day BTC OOS results(`PBS 537841`,already on hand)
3. cross-binding metric table:per-day fills / PnL JPY / edge bps / Sharpe

narci 不主动接 这个 ask;echo 决定优先级。如果 narci 跑 PnL 那等于绕过 echo
的 strategy-eval 流程,产生重复结论。

#### C. 不变的接口约定

- `backtest_alpha_model` 签名向后兼容(新增 kwargs 默认值不破坏旧 caller)
- `VENUE_SOURCES` 仍 export 为 BTC 表(echo `backtest_with_guards.py:65,68,192`
  import 不影响)
- 4-tuple `(exchange, market, symbol, venue_tag)` schema 不变
- `MakerSimBroker` + `SymbolSpec` 不动(`_make_default_symbol_spec` 已有
  ETH_JPY entry)

#### D. narci → echo 反向 ask(non-blocking)

- echo 决定要不要接 nyx Ask #2 → narci 看 echo doc update;不接就告诉 nyx
- echo `backtest_with_guards.py` 跑 ETH 时若发现 D10 routing 还有 corner
  case 没覆盖,raise narci(本 commit 5 个 test 是 minimal coverage,真实
  load 跑可能暴露 edge case)

---

### 4.10 回 echo 2026-05-20 `4419c91` §16 D11 — Orderbook CC bug + `raw_l2` scope — ✅ (b)+(c) DONE 本 commit

读 echo §16 D11(2026-05-20 14:25):两件事 — narci `Orderbook` 对 CC
全 snapshot 协议不兼容 heads-up + §3.1 first-non-empty bundle 状态。

#### A. (b)+(c) 双管齐下,合 echo lean

| 选项 | 文件 | 状态 |
|---|---|---|
| (b) `Orderbook` docstring 加 venue-compat warning | `backtest/orderbook.py:1-21` (header) | ✅ |
| (b) `L2Reconstructor` 加 class docstring + cross-ref `Orderbook` | `data/l2_reconstruct.py:9-22` (class top) | ✅ |
| (b) `EventEngine` `Orderbook` 实例化点加 inline note | `backtest/event_engine.py:52-56` | ✅ |
| (c) `MakerSimBroker.book` 默认 `prune_snapshot_dust=True` | `simulation/maker_broker.py:94-100` | ✅ |

(a)(port snapshot-reset 进 Orderbook)**deferred**:撮合引擎状态机
touch 风险大,(b)+(c) 已经 cover echo air Option A 的边界。如果未来真要
把 `Orderbook` 喂 CC-style live WS,再独立做 (a)。

#### B. 影响范围

- echo air Option A(`AlphaAwareMakerStrategy` 读 `broker.book` 而非
  `engine.book`)继续 work — narci 不动 echo 端
- `MakerSimBroker.book` 现在默认 `prune_snapshot_dust=True`,所有 echo
  端 + narci 端 backtest 自动获 belt-and-suspenders。**反向影响**:某些
  cross-spread short-run dust 会被 prune 掉,与之前对比可能 fill 率有
  微调(向 ground truth 靠拢,不应 hurt)
- `Orderbook` 行为不变,只是 doc 上明示 venue 限制
- `EventEngine` 行为不变,只是 inline note 提醒未来 contributors

回归:`test_maker_broker.py` + `test_maker_smoke.py` + 邻近 broker tests
全 pass(29/29)。

#### C. 回 echo §16.3 §3.1 open Q:`raw_l2/` 是否包含 UM/BS

**narci 立场:✅ echo 默认假设正确 — `raw_l2/` 只放 CC + BJ**。

理由:
- narci-sg 收 UM / BS WS → 写 cold-tier parquet + 推 narci-air (D9 path,
  `data/event_publisher.py`)。**narci-sg 的 cold-tier 是 authoritative
  source of truth**(gdrive-pushed,有 daily compact validation)
- echo air predict_loop in-memory consume narci-sg TCP forward 是为了
  inference fresh,**不应再 echo-side disk-fork** — 那会产生第二份不
  authoritative 的 raw_l2
- cross-host UM/BS event pairing audit:narci 这边走 `narci-sg cold-tier
  parquet` ↔ `narci-air cold-tier parquet` ↔ `echo bundle CC+BJ`,三方
  按 ts_ms join,不需要 echo bundle 自己也带 UM/BS

如果 narci 未来真发现 cross-host pairing 有 ms-级 latency anomaly 怀疑
narci-sg TCP forward 链路,会单开 ask 让 echo fork 一份 SGSubscriber 到
disk 用于 audit。**现在不需要,echo 不要预先实现 fork-to-disk。**

#### D. §3.1 bundle ETA 接受

echo §16.3 给出 ETA "minutes after Option A merges + soak restarts"。
narci 端等 bundle 到 `calibration/inbox/`,届时:
- round-trip 验证 narci 三 schema(decisions / fills / cancels)
- 用 fills / quote_ts 估 fill latency 分布(filling out
  [[project_place_latency_deferred]] 的 measurement loop)

#### E. narci-internal future-proof TODO(非紧急,non-blocking)

`narci.backtest.event_engine.EventEngine:52` 用 `Orderbook`。当前 narci
backtest 通过的是 daily compact 后的 parquet(side=3/4 是 narci recorder
自己写的,**recorder 端已经把 venue 协议归一化** — Binance 是 incremental
+ qty=0 delete,CC 是 full snap 但 recorder 用 `L2Recorder` 状态机 dedupe
后再写盘),所以 backtest 喂 `Orderbook` 不会撞 echo air 的 bug。但
**任何把 `Orderbook` 接到 raw live WS 的 code 都会 hit** — 内部 future
contributor 看到 docstring 警告就好。

---

### 4.11 回 echo 2026-05-21 `a7d3b28` §18 D13 — D9 wire protocol multi-symbol contamination — ✅ F1 + F2 DONE + acceptance VALIDATED 2026-05-21

> **acceptance update 2026-05-21**:echo `7775f69` §0.5.30 跑完 1h soak,
> C-clean session 20260521-092003,n=1,155 paired predictions:
>
> | 指标 | Pre-fix | Post-fix | 比较 |
> |---|---|---|---|
> | **Pearson** | 0.318 | **0.6464** | ✅ **超过 backtest ens5 0.628** |
> | Spearman | 0.272 | 0.6333 | ✅ |
> | R² | 0.101 | 0.418 | ✅ |
> | yh/y compression | 137% | 58.6% | ✅ training 57.5% bit-match |
>
> D13 narci fix 完整 validated。**`basis_um_bps` 不再 OOD,LGB trees fire
> in-distribution leaves,corr-gap 全恢复**。同步:
> - echo F2 subscriber 侧 `87e60d9` (`relay/sg_subscriber.py`)ship — 加
>   `(venue, symbol)` dispatch table 默认 `{(um,BTCUSDT),(bs,BTCUSDT)}` +
>   消费 hello/heartbeat 内部不 yield + 缺字段 defensively reject
> - **narci 端 yaml 现在可放开多 symbol(echo subscriber 端会 drop 不在
>   dispatch table 的事件)**,但当前没下游消费者要其他 symbols,保持
>   BTCUSDT-only。未来若 nyx ship 新 binding 需要 ETH/SOL 等 → narci
>   yaml 加回去,echo dispatch table 扩 → 两侧独立 deploy 不串。



读 echo §18 D13(2026-05-21):D9 wire protocol(`{venue, ts_ms, side, price, qty}`
无 `symbol` 字段)+ `configs/event_publisher.yaml` 多 symbol fan-out → echo-air
subscriber 端所有 symbols 塞同一个 `L2Reconstructor` → bid=BTC 77943 vs
ask=XRP 25 crossed → `_resolve_top_non_crossing` 走 altcoin pair → um_mid
≈ $25 → **`basis_um_bps ≈ -99,289`(training ±30)→ 15 个 UM-derived features
全 OOD → live R² 37.7%→12.1%、Pearson 0.628→0.347**。

narci 端责任:**echo §18 §3 明确两段都该 narci 拥有**(F1 yaml + F2 publisher 代码),
本 commit 一并 ship。

#### F1 ✅ — yaml 缩 BTCUSDT-only(contract-preserving config fix)

`configs/event_publisher.yaml`:
- UM venue_tag symbols 从 `[BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT, DOGEUSDT]` 缩到 `[BTCUSDT]`
- BS venue_tag symbols 从 `[BTCUSDT, ETHUSDT]` 缩到 `[BTCUSDT]`
- 加 inline comment 说明 root cause 跟 §18 reference

只动 config,**code 不变即 narci-sg push 出去的 wire 仍然是 v1 格式**(可以
独立 deploy F1 而 echo 不动)。

narci-reco 端 deploy:
```bash
cd ~/narci/reco
./aws/recorder_restart.sh sg --service event-publisher
```

#### F2 ✅ — wire protocol v2:加 `symbol` field + `hello` frame

| 文件 | 改动 |
|---|---|
| `data/live_publisher.py:35-78`(docstring + class header) | v2 wire spec + `PROTOCOL_VERSION = 2` 常量 |
| `data/live_publisher.py:_handle_subscriber` | 新 subscriber 连进来立刻发 `{"kind":"hello","protocol_version":2}` 一次性 frame |
| `data/live_publisher.py:fanout` | 签名 `fanout(venue_tag, symbol, records)` —— 加 `symbol` 必传参;JSON 每行加 `"symbol"` 字段 |
| `data/event_publisher.py:_one_url` | 把 `sym, _, _ = adapter.parse_message(msg)` 拿到的 `sym` 透传给 `publisher.fanout(venue_tag, sym, records)` |
| `calibration/tests/test_live_publisher.py` | 8 个 tests(原 6 个 + hello frame test + multi-symbol fan-out regression test);全 pass |

**v2 wire format**:
```json
{"kind":"hello","protocol_version":2}
{"venue":"um","symbol":"BTCUSDT","ts_ms":1779164453356,"side":0,"price":68234.5,"qty":0.5}
{"venue":"um","symbol":"BTCUSDT","ts_ms":1779164453402,"side":2,"price":68234.55,"qty":-0.123}
{"kind":"heartbeat","ts_ms":1779164458356}
```

**Backward-compat**(echo §18.2 Part B.5 提到):
- 旧 v1 subscriber 看到 `hello` frame 的 unknown `"kind":"hello"` 应当 ignore
  (per JSON forward-compat),`symbol` 字段也 ignore — narci 这边没 fallback
  机制(每条 data line 都带 symbol),实现简洁。如果 echo 侧有不能容忍 unknown
  kind 的 v1 subscriber,需要 echo 端先升级再 deploy
- narci 这边**不维护 v1-emitting mode**(只一种 wire,v2 永久)

**Multi-symbol 重新放开**:F2 ship 之后,narci-sg 端**可以**把 yaml symbols
list 重新放回多个(BTC/ETH/SOL 等)。echo subscriber 端按 echo §18.2 Part
B.4 的 `(venue, symbol) → FB venue tag` dispatch table 路由就好。但本
commit 暂时**保留 BTCUSDT-only**,等 echo 端 ship dispatch table 之后 narci
端 yaml 改回多 symbol 再独立 deploy(避免乱序导致 echo air 端 transient
contamination)。

#### Acceptance(预期 echo 端验)

| 指标 | F1 单独 ship 前 | F1+F2 ship 后 |
|---|---|---|
| `basis_um_bps` 均 \|Δ\| | 99,283 bps | < 5 bps |
| `um_n_5s` 均 \|Δ\| | 84 events | < 5 events |
| `r_um` 均 \|Δ\| | ~5 bps | < 0.5 bps |
| live Pearson 1-2h soak | 0.318 | ≥ 0.55 |
| hello frame on connect | (无) | ✅ 每个新 subscriber 收到 |
| 多 symbol fan-out 时 `(venue,symbol)` 区分 | (collide) | ✅ subscriber 自己 dispatch table 路由 |

#### echo 端 follow-up(echo 域)

按 echo §18.2 Part B.4:`runtime/sg_subscriber.py` 加 `(venue, symbol) → FB
venue tag` dispatch table + drop 不在 table 的事件。narci 端 ship 完毕,等
echo ship dispatch 后我们再把 yaml 放开。

#### 反向 ack(narci 自责)

echo §18.1 写了 lab 自己也有 share of blame(§14.2 spec 没明 `symbol`)。但
narci 这边在 `3a4e572` ship D9 时**没意识到 spec 的隐含 single-symbol
assumption**,直接按 yaml 多 symbol 跑,这一段是 narci 实施层的疏忽。下次 echo
spec 出多个 examples 时 narci 端要主动 challenge "spec 是不是对全部 examples
class 都覆盖"。

---

## 5. 不在 narci scope (留给 echo 自己)

为避免越界,以下事项 narci 不会主动跟进,echo 自己 own:

- `ECHO_AZ` / `ECHO_INSTANCE_TYPE` 环境变量没接 systemd unit —— echo deploy
  follow-up。
- `ln -sf → cp -f` 那个部署 bug (导致 `NaiveMakerStrategy` 没起来) —— echo
  deploy bug fix。
- Coincheck 账户充值与合规 —— echo / 运营。
- AWS Secrets Manager `echo/coincheck` 的 key rotation cadence —— echo
  ops。
- S3 sync (`s3://echo-logs-383941187690/`) 的 bucket 创建、IAM、生命周期
  —— Phase 0b echo ops。
- echo-paper 容器内的 strategy code (`narci.backtest.strategy.*`) ——
  虽然 import 自 narci 模块,但运行时挂载 / 加载 / 异常重启都是 echo
  ops。narci 只保证模块语义稳定。

---

## 6. Retrieval / 联系

### narci 这边的 ingest 路径

```bash
# echo-air → narci /lustre1 (Phase 0a)
scp -i ~/.ssh/aws-narci.pem -r \
    ec2-user@<echo-air-ip>:/var/lib/echo/logs/<session_id>/ \
    /lustre1/work/c30636/narci/calibration/inbox/

# 落到 inbox 之后 narci replay 会自动 pick up
```

### 文档维护
- 这份文档由 narci 在 `narci_git_sha` 变化引发对 echo 契约的影响时重新
  cut (例如 schema bump、recorder topology 变动、pairing spec 出台)。
- echo 端对应的 `docs/INTERFACE_ECHO_NARCI.md` 在 echo 这边维护(2026-05-16
  从 `INTERFACE_NARCI.md` 重命名)。两份文档**互
  相不引用对方源码路径,只引用对方 git SHA + 文档段落**,避免 cross-repo
  symlink rot。
- 协议级争议:narci 端 owner = `zhangenzhi@narci`,通过 git PR comment 或
  直接邮件 `zhangsuiyu657@gmail.com` 联系。

### Changelog
- **2026-05-15** (`84354aa`) — initial cut,响应 echo `INTERFACE_NARCI.md`
  authoritative 2026-05-14 (`03bb63b`)。(echo 端这份后来在 `3cad76f`
  重命名为 `INTERFACE_ECHO_NARCI.md`)
- **2026-05-15** (`289d61c`) — §2.1.3 / §4.3 / §4.4 拓扑重写;
  追加 §7 appendix 回应 echo `INTERFACE_NARCI.md §12` (echo SHA `82622ed`)。
  同时修 `deploy/aws/README.md` 的 region 错标 (narci-us → narci-sg)。
- **2026-05-15** (`6bb7663`) — §7.4 标 A17 "scaffold ready (`18e68b3`),
  deployment gated by ops capacity";§7.5 状态表更新 A15/A16/A17/cloud-sync 修复进度。
- **2026-05-16** (本次 commit) — 文件从 `INTERFACE_ECHO.md` 重命名为
  `INTERFACE_NARCI_ECHO.md`,统一 narci/echo/nyx 三边的
  `INTERFACE_<author>_<audience>.md` 命名约定 (跟 echo `3cad76f` 对齐)。
  内容无变化。

---

## 7. Appendix — 回应 echo `INTERFACE_NARCI.md §12` (2026-05-15, echo SHA `82622ed`)

### 7.1 Topology — narci §2.1.3 / §4.3 / §4.4 已重写,谁对谁错

echo §12.1 指出本文档前一版本 (narci SHA `429c3eb`) 拓扑错认,**主要责任
在 narci**。具体:

| 错认点 | 我之前的版本 | 实际正确 | echo 提出的版本 |
|---|---|---|---|
| narci-t4g 录什么 | "binance-global UM recorder" ❌ | **CC + Binance JP** ✓ | "CC + Binance JP" ✓ |
| echo-air CC pair target | "aws-sg, cross-AZ ~1ms" ❌ | **narci-t4g, same AZ < 2ms** ✓ | "narci-t4g, same AZ < 2ms" ✓ |
| binance global / UM 在哪 | (我隐含说在 narci-t4g) ❌ | **AWS-SG (Singapore, ap-southeast-1)** | "narci-us in us-west-2" ❌ |

**echo 在 CC 拓扑上 100% 对**(narci-t4g = narci-tokyo,同 AZ,< 2ms);
narci 之前的 §2.1.3 把 CC 和 UM 整个调换了,这是 narci 的错。**已在本次
commit 修正**。

**但 echo 在 UM 拓扑上 source 取错了** — `deploy/aws/README.md` 写
"narci-us / us-west-2 / t4g.micro" 是早期 plan,**没跟实际部署同步**。
narci 仓库代码里 4 处独立引用 `AWS-SG` 才是 source of truth:

- `features/realtime.py:334` — "BS recorder lives on AWS-SG, recording since..."
- `tools/probe_um_endpoints.py:3,10` — "Run from AWS-SG host" / "AWS-SG egress blocked"
- `calibration/tests/test_l2_recorder_refresh.py:202` — "out of recording 5h+ on AWS-SG 2026-05-09"
- (narci-side memory `project_aws_sg_oom`,基于 0507 journalctl 实证)

**已在本次 commit 同步修正 `deploy/aws/README.md`**,把 narci-us /
us-west-2 / t4g.micro 改成 **narci-sg / ap-southeast-1 / t4g.small**,
并加 explanatory warning 顶部。echo 后续如果再以 narci 仓库为 source of
truth,会拿到正确的拓扑。

**对 echo 实操的影响**:零。UM 在 SG 还是 us-west-2 跟 echo CC pair 都
无关 (跨 region 都没用),CC pair target 已经在 narci-t4g 上,echo §12.1
"echo-air's CC pair target IS narci-t4g, same AZ" 这条结论本身正确,可以
按这条往 §2.1 的 pairing spec 推。

### 7.2 A15 — `recorder-bitbank` on narci-tokyo (blocking 0b) ✓ accept

narci 接 A15。选 **(a) 独立 recorder**,理由跟 echo 一致 — cross-check
echo `RawL2Sidecar` 才有意义。

实施计划:

- 新 adapter `data/exchange/bitbank.py` 实现 `ExchangeAdapter` ABC,
  WS 协议 socket.io v2 (EIO=3),参照 echo `echo/exchange/bitbank.py` (SHA
  `6569402`) 的 parser。`python-socketio[asyncio_client]` 加进
  `requirements.txt`。
- side 编码沿用 narci 标准:0=bid update, 1=ask update, 2=aggTrade
  (qty 负值=seller maker), 3=bid snapshot, 4=ask snapshot。
- 新 config `configs/exchanges/bitbank_spot.yaml` + `configs/bitbank_recorder.yaml`
  symlink。
- `docker-compose.yaml` 加 service `recorder-bitbank`,profile=tokyo,
  宿主机端口 **8082**(8079/8080 已被 CC/BJP 占)。
- gdrive 路径:`gdrive:narci_raw/realtime/bitbank/spot/l2/`。
- `daily_compactor.py` 沿用既有 exchange-neutral 流程,bitbank 跟 CC 一样
  没有官方 Vision 归档,skip validation gracefully。

**时间表**:narci 这边按 echo 的 Phase 0b cutover 排期。echo §12.3 标
"before Phase 0b cutover" — 请 echo 给一个绝对日期 (例如 "Phase 0b
cutover ≥ 2026-06-XX"),narci 这边好倒推 ETA。在没拿到日期之前,narci
按 **2026-06-30 ready (含联调测试)** 自规划。

### 7.3 A16 — Calibration replay venue dispatch (blocking 0b) ✓ accept

narci 接 A16。`MakerSimBroker` + calibration replay 加 venue dispatch
层,按 `meta.json.exchange` 切换:

- `SymbolSpec` 表 (per-venue tick / lot / minNotional / maker-rebate
  eligibility)
- fee schedule (CC `0/0 bps`,bitbank `-2/+12 bps`,future venues TBD)
- trade-side decoding (CC list-of-lists; bitbank explicit `side`;
  bitFlyer `executions[]`)
- order-type matrix (`post_only` 是否支持、IFD/OCO、stop-limit 等)

实现路径:在 `backtest/broker.py` 加 `VenueRegistry`,根据 session 的
`exchange` 字段在 replay 启动时 inject 对应规则;现有
`SpotBroker`/`SimulatedBroker` 改成读 registry 而非硬编码 CC 默认。

**时间表**:跟 A15 绑定,同一个 Phase 0b 窗口完成 (2026-06-30 ready)。

### 7.4 A17 — `recorder-gmo` + `recorder-bitflyer` — **scaffold ready (2026-05-15),deployment gated by ops capacity**

narci 没等 A15 稳定就把 A17 全套 scaffold 提前做了 (commit `18e68b3`),
避免后续回头看时再做。**代码层 ready,部署 gated on narci-tokyo 资源**:

#### Scaffold 已完成 (narci 端)

- `data/exchange/bitflyer.py` — JSON-RPC over plain WS,`market_type='spot'|'fx'`
  双模 (FX 用 `FX_BTC_JPY` symbol)。走标准 `ws_url + parse_message` 路径。
- `data/exchange/gmo.py` — `market_type='spot'|'leverage'`。**关键差异**:GMO
  `orderbooks` channel 每条都是 full snapshot 而非 incremental,所以走
  `custom_stream` hook 自管 book reset (跟 bitbank 同 pattern)。
- VenueRegistry 4 个 entry:
  - `bitflyer_spot`: 15/15 bps
  - `bitflyer_fx`: 0/0 bps + 日内 SWAP funding
  - `gmo_spot`: -1/+5 bps (rebate-eligible)
  - `gmo_leverage`: -1/+5 bps (echo 偏好的主战场)
- 4 yaml configs + 4 symlinks + 4 docker-compose services on **新 profile
  `tokyo-extra`** (不放默认 tokyo,避免无脑 pull 后 OOM)。
- 每个新 service `mem_limit: 256m` 防御性硬上限。
- `deploy/measure_recorder_footprint.sh` — 测当前 narci-tokyo 内存余量,
  输出 🟢/🟡/🔴 verdict + 升级建议。

#### Deployment 阻塞点 (ops capacity)

narci-tokyo 当前是 **t4g.small (2GB)**,已经跑 3 个 recorder
(CC + Binance JP + bitbank) + cloud-sync。加 4 个新 recorder 预估额外
~600MB 需求,**很可能需要先升级到 t4g.medium (4GB, +$15/月)**。

决策树 (narci-tokyo 操作员):

```bash
ssh narci-tokyo
cd narci && git pull
bash deploy/measure_recorder_footprint.sh
# 🟢 GREEN  → COMPOSE_PROFILES=tokyo,tokyo-extra docker compose up -d
# 🟡 YELLOW → 分批先开 fx + leverage 2 个主战场
# 🔴 RED    → 升级 t4g.small → t4g.medium 再开
```

#### 仍需 echo 配合的项 (Phase 0c+ 前)

- **echo 明确 venue × market 组合**:bitFlyer 走 spot 还是 FX?GMO 走
  spot 还是 leverage?如果两边都做需要明说 — 影响 narci 是否要 4 个 recorder
  全开 (vs 选择性开 2 个)。
- **bitFlyer FX JST business-hour audit** — echo §12.2 自己 flag 的
  "max 110ms 尾巴可疑",重测后给个 verdict (录 vs 不录)。
- **bitbank pattern 稳定 30 天** — 这条 narci 自己跟,以 bitbank 录制 24h+
  无中断 + watchdog 不触发为基线。

### 7.5 narci 端反向追踪 — echo §12.3 表的更新建议

echo 表里第二项 "Topology re-cut of `INTERFACE_ECHO.md` §2.1.3 / §4.3 /
§4.4 — Due: next narci re-cut" — **本次 commit 已 done**。请 echo 下次
re-cut INTERFACE_NARCI.md 时勾掉 / 删除这行,免得长期挂着。

其它 commitments 状态:

| Item | Status | 备注 |
|---|---|---|
| Cross-host pairing spec | 进行中 | due 2026-06-15,在 `docs/CROSS_HOST_PAIRING.md` |
| Topology re-cut | ✓ done | 本次 commit |
| A15 `recorder-bitbank` | ✓ scaffold + Phase 2 + Phase 3 watchdog done (`e55dd75`/`ac88d28`/`1c8349f`) | 已部署 narci-tokyo,2 个 save cycle 数据已上 gdrive,5 个 symbol 全健康 |
| A16 venue dispatch | scaffold done (`e55dd75`) | VenueRegistry + apply_to_broker helper ready;broker 完整重构 (默认读 registry) 待 Phase 3 |
| A17 GMO + bitFlyer | **scaffold ready** (`18e68b3`),deployment gated | bitflyer-spot + bitflyer-fx + gmo-spot + gmo-leverage 4 个 recorder on `tokyo-extra` profile;需要 ops 决定 (capacity verdict) |
| cloud-sync `--no-traverse` 修复 | ✓ done (`acaf96c`) | 新嵌套目录 (bitbank 首次踩雷) 自动 catch up,免去未来 GMO/bitFlyer 同样的坑 |
| empty-shard semantics | ✓ done in §2.2 | confirmed |

### 7.6 narci 端反向 ask (再次重申)

承接 §3 的请求,**给 echo 的优先级排序**:

1. **(高)** §3.1 — NaiveMakerStrategy 修好后送一个非空 session 样本
   (Phase 0b 启动前)。
2. **(高)** §3.2 — Test 6 funding ETA 写进 echo INTERFACE_NARCI §4 (这次
   re-cut 时顺便加上)。
3. **(中)** §3.3 — echo-air `/health` HTTP endpoint (Phase 0b 之前 nice
   to have)。
4. **(低)** §3.4 — `narci_features_consumed` 字段,v1.2 schema candidate,
   bitbank/multi-venue 上线后再讨论。

---

## 8. Inbox — 来自 echo 的 ask (narci 待回复)

> 这一节登记 echo 主动 push 过来的 Delivery / Req,narci 这边按优先级回。
> 每条注明:**echo SHA · echo doc 段落 · 状态 (open / in-progress / done)**。
> 回复方式:在 §7 appendix 增加 `7.X — reply to echo Delivery N`,然后把
> 状态从 open → done。

### 8.1 Delivery 8 — Phase 1a real-time signal channel (⚠️ SUPERSEDED 2026-05-19; narci Ask B partial ship retained)

> **状态:SUPERSEDED by §8.2** (echo SHA `18a74f2`,architecture pivot
> commit `docs(pivot 2026-05-19): LGB v9_midy_40 moves to AWS Tokyo EC2`)。
>
> echo 把 LGB inference 移到 echo-air (Tokyo EC2),不再通过 HPC 当 live
> signal source。本节里的两个 ask (UM/BS 实时落 lustre1 + save_interval
> 600→60) **从 echo 关键路径上 parked** —— 它们是 recorder 内部优化。
>
> **特别 ack narci `15ff210`** (`feat(recorder): cut save_interval to 60s
> for CC/BJ/UM`): 该 commit 是 D8 Ask B Option 3 的完整 ship,**已经
> done**。即便 D8 整体被 D9 supersede,**60s save_interval 本身对 cold-tier
> 仍然有持续价值**:
>
> 1. crash window 从 10 min → 1 min,recorder OOM/重启丢的数据量小 10×
> 2. echo 在 lustre1 跑回测时 shard 越小 = pyarrow row-filter 越精确,
>    `signal_publisher.py --continuous` (backtest 模式) 跑历史数据延迟更低
> 3. Daily compactor 输出不变(per-day 1 个 DAILY parquet),lustre 上 inode
>    用量 ~10× 不构成问题
>
> 所以 `15ff210` **保留**(不要 revert),只是它从"live alpha blocker"降级为
> "cold-tier 质量提升"。
>
> Ask A (narci-reco 域的 rsync from sg/jp → lustre1) 也保留 deferred 状态:
> 等 narci-reco 有 capacity 再做,**不阻塞 Phase 1a**(因为 echo-air 现在
> 直接从 narci-sg 拉 live 事件,见 §8.2 D9)。
>
> 新 ask 见 [§8.2](#82-delivery-9-narci-sg-内存事件转发-open)。
>
> 下面的旧内容保留供 git history。

- **来源**:echo SHA `90b7791` · `docs/INTERFACE_ECHO_NARCI.md §13`
  (committed 2026-05-19 22:xx JST)
- **背景**:echo 已 ship Phase 0c continuous-mode `signal_publisher`
  (commit `90b7791` echo/lab/signal_publisher.py::run_continuous),
  能 tail narci realtime shards across CC/BJ/UM/BS,time-merge events,
  emit SignalEvent per CC trade。Air-side plumbing (AlphaAware +
  S3SignalSource + StaleningSignalSource 30s gate + workstation_bridge)
  全部 ship 完。**echo 不再要任何自己端代码改动 —— 后续只看 narci。**
- **End-to-end smoke (2026-05-19 21:33 JST)**:
  - 221,739 events / 914 CC-trade signals 30s 内 round-trip
  - 全部 914 都 emit HOLD with `feature_stale=true`(UM 37h stale,
    BS 7h stale)
  - air-side StaleningSignalSource(30s) → None → fallback to NaiveMaker
- **两个 narci-side blocking ask**:

#### Ask A (高) — UM / BS 实时通路 lustre1,≤ 60s

参 echo §13.2。**当前实测 lag**:CC/BJ 同 lustre1 ~30 min ✓,
BS gdrive → lustre1 ~7 h ❌,UM gdrive → lustre1 ~37 h ❌❌。

**why blocking**:echo 的 v9_midy_40 binding (8-day OOS +26,398 JPY
on thr=0.2 / ii=0.075) 40 个 feature 里有 **15 个 UM feature**。
UM 37h stale → feature_stale gate 每个 CC trade 都 trip → 全 HOLD。
**Alpha 通路实际等于关闭。**

echo 给的三个 transport 选项:
1. narci-sg → lustre1 rsync push(narci-sg 写完 shard 立刻 rsync)
2. AWS S3 中转(narci-sg → S3 → lustre1 `aws s3 sync`)
3. WS push channel(narci-sg pub-sub,绕过磁盘)—— 长期答案

echo 偏好:**1 或 2**(Phase 1a 短期),3(长期)。

#### Ask B (高) — recorder save_interval_sec cut OR push channel

参 echo §13.3。**当前**:所有 `configs/exchanges/*.yaml` 都
`save_interval_sec: 600`(10 分钟一次落盘)。**Mean event-to-disk lag
≈ 5 分钟,worst-case 10 分钟。** 即使 Ask A 关掉,echo-air 的 30s
staleness gate 也会 reject 每个信号。

echo 给的三个选项:
1. `save_interval_sec: 5–10` for CC/BJ/UM/BS(shard 数 ×120,但
   filename pruning + daily_compactor 已经能扛)
2. Per-venue push channel(WS / Unix socket,parquet flush 保留 600s
   for cold tier 持久化)
3. **Compromise**: `save_interval_sec: 60` for CC/BJ/UM only
   (shard 数仅 ×12,mean lag 30s → 刚好过 air 30s gate)

echo 偏好:**3**(Phase 1a 最小改动)。**2**(长期 — 永久去掉
disk-flush ceiling)。

#### 验收 (echo 端会跑)

narci ship A + B 后,echo 重跑同 smoke:
1. UM lustre1 lag < 60s
2. CC event-to-disk lag < 60s
3. End-to-end:continuous publisher 跑 10min,air 端 ≥ 1 signal
   age < 30s

#### 时间表 (echo 期望)

- 目标 acceptance:**2026-06-15**
- Phase 1a paper soak ready by **end of 2026-06**

#### narci 这边的初步反应空间

narci 这边收到后:
- (a) 评估 Ask A 三个选项里哪个 narci-sg ops capacity 能接 —— 是否需要
  升级 narci-sg t4g.small(目前已经 8h+ recorder uptime,加 rsync /
  S3 sync 任务后内存余量?见 narci §4.5 cron 已加的 daily compact 是
  否能扩成 daily push)
- (b) 评估 Ask B 三个选项 —— 改 `save_interval_sec: 60` 是否会拖累
  既有 cold-tier validator / daily_compactor
- (c) 如果两 ask 短期都接不下来,告诉 echo 哪个先做,echo 端的
  continuous-mode publisher 框架已经 ship,所以等 narci 任意一边
  好了就能 partial 提速

**这一条 done 后** 把状态从 open 改成 done,并在 §7 增加
`7.7 — reply to echo Delivery 8`。

### 8.2 Delivery 9 — narci-sg 内存事件转发 (✅ DONE · narci `3a4e572` · reply in §4.8)

- **来源**:echo SHA `18a74f2` · `docs/INTERFACE_ECHO_NARCI.md §14` +
  `docs/INTERFACE_ECHO_AIR.md §0.5 + §3.1` (committed 2026-05-19 22:47 JST)
- **状态**:✅ DONE · narci `3a4e572` (2026-05-19 后续重构) · reply in §4.8
  · 替换 §8.1 (D8)
- **narci-side ship 内容**:
  - `data/live_publisher.py`(TCP/JSON-lines broadcaster,multi-venue 一端口)
  - `data/event_publisher.py`(独立 main,WS subscribe + fanout,不依赖 recorder)
  - `configs/event_publisher.yaml`(独立 config,UM + BS venues)
  - `calibration/tests/test_live_publisher.py`(6 tests 全过)
- **下一步**(narci-reco 域 + echo 域):
  1. narci-reco:AWS-SG SG ingress `0.0.0.0/0:9100` + `docker-compose.yaml` 加
     `narci-event-publisher` service(或 SSM standalone `python -m
     data.event_publisher --config configs/event_publisher.yaml`)
  2. echo:`sg_subscriber.py` ready 后跑 §14.6 acceptance test 5 项

#### 背景

echo 在 air-side 做了架构 pivot:LGB v9_midy_40 inference 从 echo-lab (HPC)
**搬到 echo-air (AWS Tokyo EC2)**。原因:Binance global (UM + BS) 从
JP IP-blocked,echo-air 自己 subscribe 不到;但 **narci-sg (Singapore)
已经在 subscribe UM + BS**,只要 narci-sg 把这些事件再喷一份 TCP 出去,
echo-air 就能从 cross-region 内网拉到 (~70 ms)。

**结果**:
- 旧 D8 两个 ask (UM/BS 实时到 lustre1 + `save_interval_sec` 600→60)
  **全部不需要做** —— 它们都是 recorder 内部优化,echo 不再依赖。
- 新 ask 比 D8 范围小一个量级 (~50 LOC,纯 additive)。

#### Ask (高,echo 期望 narci own)

`narci/data/l2_recorder.py` 里,每个事件被 append 到 `self.buffers[sym]`
**之前/同时**,tee 一份给一个 `asyncio.start_server` broadcaster。订阅者
(echo-air) 通过 TCP 拉 JSON-lines 实时流。**不改 parquet 节奏、schema、
gdrive、save_interval 任何东西** —— 现在的冷数据路径完全保留。

实现位点(narci 自己决定细节,echo 给的建议):

- hook 进 `_handle_depth` / `_handle_trade` callback,append 到 buffer
  之后立刻 `self.live_publisher.fanout(sym, ts_ms, side, price, qty)`
- `live_publisher` 是一个小 broadcaster (asyncio):
  - `set[StreamWriter]` 管订阅者
  - 写 buffer 满或 transport closing → drop 该 subscriber
  - 5s heartbeat 维持心跳
- config:`publisher_port: 9100` 加进 `configs/exchanges/binance_um_futures.yaml`
  和 `binance_spot.yaml`

总量 ~50 LOC + 一个 config knob。

#### Wire 协议 (echo 已 spec,可直接 implement)

完整 spec 在 `INTERFACE_ECHO_NARCI.md §14.2 / §14.3`。要点:

- TCP / JSON-lines,port 9100,`0.0.0.0/0:9100` SG ingress (Phase 1a 不
  加 auth/TLS,公开市场数据)
- **wire shape 等于 buffer 行 verbatim**:
  ```json
  {"venue":"um","ts_ms":1779164453356,"side":2,"price":68234.50,"qty":0.123}
  ```
  零翻译。`side` 沿用 §4.2 的 0/1/2/3/4 编码。
- Heartbeat:`{"kind":"heartbeat","ts_ms":...}`,5s 静默期发一次。
- Back-pressure:慢订阅者 drop + 强制重连。
- Cold-start:**只发 live 事件,不补历史**。FB warmup 用 echo-air
  自己 30s 滚出来,长期训练用 lustre1 冷数据(narci 现有路径,不动)。

#### narci-sg ops 影响 (echo 端的预判)

- narci-sg 已经从 t4g.small 升级到 **t4g.medium**(根据 air 端
  `[[project-narci-sg-ec2]]` memory),内存余量足够加一个 broadcaster。
- CPU:fanout 是 O(N_subscribers × write),Phase 1a 只有 1 个订阅者
  (echo-air),quiet 期 ≤ 10 KB/s,busy 期 < 100 KB/s。可忽略。
- Network:cross-region (sg → tokyo) 出口流量 narci 这边会有一些 AWS
  内网费用(几 GB/月 数量级,$0.01 级)。

#### 验收 (echo 端会跑)

narci ship 后,echo-air subscriber (`echo/relay/sg_subscriber.py`,
air session 在写)对接 narci-sg:`9100`。验证 5 条:

1. TCP reachability + JSON-lines 解码 OK
2. 30s quiet 期 ≥ 5 个 heartbeat 帧
3. 每个非 heartbeat 帧 schema 正确(`{venue,ts_ms,side,price,qty}`)
4. 模拟 slow subscriber → 1s 内被 drop
5. End-to-end:echo-air `FeatureBuilder` + v9_midy_40 `predict()` 30s
   内吐出有限 alpha

#### 时间表

- narci 接 Ask + 实现 + deploy:**target 2026-05-26**(比旧 D8 的 06-15
  快 3 周,因为 scope 小一个量级)
- Phase 1a paper soak ready:narci ship 后立即(air 这边
  `sg_subscriber.py` 在并行写)

#### narci 这边的初步反应空间

- (a) 评估 narci-sg t4g.medium 当前内存余量 + asyncio 在 recorder 主
  loop 里加 broadcaster 是否影响 WS 接收稳定性
- (b) 决定 publisher hook 放进 `l2_recorder.py` 还是 `data/exchange/binance.py`
  (echo 推荐前者,但 narci 更懂 recorder 内部)
- (c) 如果 narci 想优先把 broadcaster 做成 sidecar 而不是 in-recorder hook,
  也可以 —— 只要协议(§14.2/§14.3)端不变,echo 不在意进程边界。

**这一条 done 后** 把状态从 open 改成 done,并在 §7 增加
`7.8 — reply to echo Delivery 9`。

### 8.3 Delivery 10 — `simulation/backtest_alpha._stream_days` symbol 参数化 (open · low priority)

- **来源**:echo `docs/INTERFACE_ECHO_NARCI.md §15` (committed
  2026-05-20)
- **状态**:open · low priority(不在 Phase 1a critical path 上)

#### 背景

narci `4ecaaed` 已把 `research/segmented_replay.py` 的
`VENUE_SOURCES_BY_SYMBOL` 参数化(nyx ETH/JPY 训练用)。但
`simulation/backtest_alpha.py` 这边的 runtime backtest 路径
(`backtest_alpha_model` + `_stream_days` + `_multi_venue_first_tss`)
**还在用模块级硬编码的 BTC-only `VENUE_SOURCES`**。

#### 影响

echo 现在想跑 nyx Delivery 3.14 ask G(`v9_eth_midy_36` 在 0517 跑
1-day dry-run on narci 端 canonical `backtest_alpha_model`),但:

```python
backtest_alpha_model(
    model_path=".../v9_eth_midy_36",
    days=["20260517"],
    symbol="ETH_JPY",   # broker/priors 接受了
)
```

→ `_stream_days(days)` 走 `VENUE_SOURCES` BTC-only → 打开
`BTC_JPY_RAW_20260517_DAILY.parquet` 而不是
`ETH_JPY_RAW_20260517_DAILY.parquet`,FB 喂 BTC 数据给 ETH binding,
predictions garbage。echo 的 `backtest_with_guards.py`(line 65,68,192
import narci `_stream_days`)同样阻塞。

#### Ask

mirror `research/segmented_replay.py:44-58` 在
`simulation/backtest_alpha.py`,然后 thread `symbol` 进:

- `_stream_days(days, max_hours=None, *, symbol="BTC_JPY")`
- `_multi_venue_first_tss(day, *, symbol="BTC_JPY")`
- `_multi_venue_anchor_ts(day, *, symbol="BTC_JPY")`
- `backtest_alpha_model(...)` 已经接 `symbol`,只需 pass through

**estimated**:~15 LOC + 1 backward-compat alias
(`VENUE_SOURCES = VENUE_SOURCES_BY_SYMBOL["BTC_JPY"]`)。

完整 diff 建议在 echo §15.2。

#### 不在 ask 范围

- ❌ 不动 `VENUE_SOURCES_BY_SYMBOL` schema
- ❌ 不动 `MakerSimBroker` / `SymbolSpec` defaults(已经支持 ETH_JPY
  via `_make_default_symbol_spec` line 186-197)
- ❌ 不加新 venue / 新 schema

#### 时间表

**低优先级**。D9 (narci-sg publisher) 是 Phase 1a critical path,这条
只是解开 research-tier 1-day dry-run。**无 deadline**,narci 有 slack
hour 时做即可。

#### 验收

narci ship 后,echo 跑:

```python
from narci.analytics.simulation.backtest_alpha import backtest_alpha_model
r = backtest_alpha_model(
    model_path="<v9_eth_midy_36 path>",
    days=["20260517"],
    symbol="ETH_JPY",
    alpha_threshold_bps=0.3,
    quote_strategy="improve_1_tick",
)
# expect: r['daily_pnl']['20260517'] finite, r['n_fills'] > 0,
#         no FileNotFoundError
```

**这一条 done 后** 把状态从 open 改成 done,并在 §7 增加
`7.9 — reply to echo Delivery 10`。

### 8.4 Delivery 11 — heads-up + §3.1 status (✅ (b)+(c) DONE 2026-05-21 · §3.1 still waiting echo bundle · 见 §4.10)

> **状态变更 2026-05-21**:§A heads-up 走 (b)+(c) 合 echo lean,本 commit
> ship。(a) deferred。§C `raw_l2/` open Q 回:**默认 CC+BJ 不含 UM/BS**
> 正确,narci-sg cold-tier 是 authoritative。§B §3.1 first non-empty bundle
> 等 echo Option A 落地后到 inbox。详见 §4.10。



- **来源**:echo SHA `c9a17c3` · `docs/INTERFACE_ECHO_NARCI.md §16`
  (committed 2026-05-20)
- **状态**:open · 两件事 —— 一个 narci-side heads-up (低优),
  一个 §3.1 status update

#### A. Heads-up:`Orderbook` 对 CC 全 snapshot 协议不兼容

echo-air paper soak 发现 `narci.backtest.orderbook.Orderbook.apply_event`
(line 180-182) 把 `SIDE_BID_SNAP (3)` / `SIDE_ASK_SNAP (4)` 当成普通
incremental upsert 处理(`_book_apply(...)` 只 upsert,没 clear / 没
prune)。

- ✓ **Binance UM / spot** OK(它们发 incremental delta + explicit
  `qty=0` deletes)
- ✗ **CC / bitbank / GMO / bitFlyer** 不 OK(它们发 full top-N
  snapshot,disappeared level 没显式 delete)

`narci.analytics.l2_reconstruct.L2Reconstructor.apply_event` (line 257-258)
做了 *"atomically replace bid book"* on new snapshot ts —— 正确。

echo `engine.book` (`echo/execution/live_engine.py:67`) 用 Orderbook,
跑 CC 20 分钟后实际 stale **26K JPY**,quote 落在 deep queue,0 fills。

**narci-internal 影响**:同 class 还被
`narci.backtest.event_engine.EventEngine:52` 使用。任何把 Orderbook
放在 CC-style 全 snapshot venue 前面的 code 都会撞这个 bug。

#### B. 给 narci 的 options(无强偏好)

| 选项 | 改动 | 推荐度 |
|---|---|---|
| (a) `Orderbook.apply_event` 加 snapshot-reset 语义 | port `L2Reconstructor.apply_event:257-258` 进 `Orderbook` | 范围大,touch matching engine,谨慎 |
| (b) 加 docstring header 标明 "incremental-L2 only;CC/bitbank/GMO 用 L2Reconstructor" | 几行注释 | echo lean **这个先做**,低风险 |
| (c) `MakerSimBroker.book` 改成 `L2Reconstructor(depth_limit=..., prune_snapshot_dust=True)` | `maker_broker.py:94` 一行 | echo lean (c),free 收益,跟 lab signal_publisher 对齐 |

**不是 ask narci 必须做。** echo 这边 air Option A
(`AlphaAwareMakerStrategy` 直接 read `broker.book` 而不是 `engine.book`)
已经 work around。这个 heads-up 是 narci 内部 future-proof 用。

#### C. §3.1 status update — first non-empty session bundle ETA

narci §3.1 ask "decisions/fills/cancels 非空 5-10 min session bundle"
since 2026-05-15。echo 端 status:

| Layer | State |
|---|---|
| Predict pipeline (live WS + V8LiveAdapter + LGB predict) | ✅ shipped, 1h dry-run validated (1033 predictions, 0 errors) |
| PaperSimBroker (narci MakerSimBroker 嵌入) | ✅ shipped (echo air `e77a67a`+follow-ons) |
| Live book consistency on CC (engine.book bug) | 🐛 修复 in-flight (Option A, ~20 LOC) |
| **First non-empty bundle to inbox** | 🟡 ETA **minutes after Option A merges + soak restarts** |

预期 bundle 内容:

- `meta.json` with `strategy_class=AlphaAwareMakerStrategy`,
  `narci_git_sha` pinned,binding sha (`v9_midy_40`) populated
- `raw_l2/` — CC + BJ WS events
- `decisions/` — 非空,带 `model_artifact_sha` /
  `binding_target_kind=cc_l2_mid_log_return_1s`
- `fills/` — 非空(option A 后)
- `cancels/` — 非空(5s quote lifetime 驱动高 cancel rate)

#### D. Open Q for narci(C 相关)

`raw_l2/` 要不要把 UM/BS WS 事件也写进去?currently echo predict_loop
通过 narci-sg TCP forward (D9 path) consume UM/BS in-memory,
**没写盘**。narci-sg 自己有 gdrive cold-tier 拷贝是 authoritative。

echo 默认假设:**raw_l2/ 只放 CC + BJ**;UM/BS pairing narci 端通过
narci-sg → cold-tier 自己接。

如果 narci 想要 UM/BS 也进 echo bundle(cross-host pairing audit
用),echo 需要在 SGSubscriber 那加 fork-to-disk(net new code)。
**flag 如果默认假设错。**

#### E. 不在 ask 范围

- ❌ 不 ask narci 立即修 Orderbook
- ❌ 不 ask narci 改 schema
- ❌ 不 ask narci 跑任何 backtest
- ❌ 不 ask narci 加任何 deadline

**这一条 done 后**(narci 决定 option a/b/c 之后)把 §A 移到
done;§C 状态会在 echo Option A 落地 + 第一 bundle 到 inbox 之后
自动 close。

### 8.5 Delivery 12 — 扩展 `TARGET_KINDS` + `SAMPLING_MODES` 接受 BJ-target bindings (✅ DONE 2026-05-21 · 通过 nyx 主线合入 · 见 §4.10 邻近 + INTERFACE_NARCI_NYX 2026-05-21 entry)

> **状态变更 2026-05-21**:done。narci 通过 nyx 7f7f10f 主线 ask #1 合入
> `bj_l2_mid_log_return_1s` + `bj_l2_microprice_log_return_1s` (future-proof)
> +`event_at_bj_trade`,加 `test_bj_target_kind_and_sampling_mode_accepted`
> 回归。详见 `INTERFACE_NARCI_NYX.md` 2026-05-21 entry §A。echo PBS 539458
> 的 monkeypatch 可拆。



- **来源**:echo SHA pending · `docs/INTERFACE_ECHO_NARCI.md §17`
  (committed 2026-05-21)
- **状态**:open · ~6 LOC fix · echo backtest 已 monkeypatch 绕过,
  production 需要 narci 端正式合入

#### 背景

nyx 2026-05-20/21 两个 commit 引入 BJ-target binding 家族:

- `7f7f10f` v9_bj_midy_36 (BTC/JPY BJ mid-y)
- `26bafa2` v9_bj_eth_midy_36 (ETH/JPY BJ mid-y)

两个 binding 引入了 narci canonical set 没有的两个枚举值:
- `target_kind = "bj_l2_mid_log_return_1s"` ← 不在 `TARGET_KINDS`
- `sampling_mode = "event_at_bj_trade"` ← 不在 `SAMPLING_MODES`

`narci/calibration/alpha_models.py:166-175` 硬 reject 任何不在 frozenset
里的 target_kind / sampling_mode,**没 bypass flag**。

echo PBS 539458 backtest 用 in-process monkeypatch 绕过测试,但 production
echo-air `load_alpha_model` 不能 ship runtime monkey patches。

#### Ask

`narci/calibration/alpha_models.py:54-65`:

```python
TARGET_KINDS = frozenset({
    # 原有...
    'cc_l2_microprice_log_return_1s',
    'cc_l2_mid_log_return_1s',
    'cc_maker_conditional_fill_pnl_buy_τ1000ms',
    'cc_maker_conditional_fill_pnl_sell_τ1000ms',
    'cc_mid_event_log_return',
    'cc_trade_event_log_return',
    'mid_10s_log_return',
    'mid_1s_log_return',
    'mid_30s_log_return',
    'mid_5s_log_return',
    'trade_10s_log_return',
    'trade_1s_log_return',
    'trade_30s_log_return',
    'trade_5s_log_return',
    # NEW (nyx 7f7f10f / 26bafa2):
    'bj_l2_mid_log_return_1s',
    'bj_l2_microprice_log_return_1s',  # future-proof
})
```

`narci/calibration/alpha_models.py:100-108`:

```python
SAMPLING_MODES = frozenset({
    '1s_grid',
    'event_at_book_update',
    'event_at_cc_trade',
    # NEW:
    'event_at_bj_trade',
})
```

**~6 LOC, 无语义改动** — 只是 enum 扩展,跟 `4ecaaed` symbol 化套路一样。

#### 为啥从 echo 走

这其实是 **nyx 的责任** — ship 新 binding 家族时应该跟 narci 协调
canonical 值的扩展。但 echo 是发现 breakage 的下游:

1. echo backtest 第一时间撞到这个 gate
2. echo 已有 D11 / D10 inbox 渠道到 narci,pipeline 一下省事
3. fix 不依赖 echo PnL 数据,narci 可以独立 ship

**Process suggestion**:narci 这次 merge 之后,narci ↔ nyx 约定:**未来
nyx 在 ship 新 binding 家族(v10 family、新 venue、新 target...)时,canonical
扩展跟 binding PR 一起进**,而不是 echo 撞墙后才反推回来。

#### 验收 (echo 端)

narci ship 后,echo 跑:

```python
from narci.analytics.calibration.alpha_models import load_alpha_model
m = load_alpha_model(".../v9_bj_midy_36",
                     allow_features_version_mismatch=True)
# 期望: no ValueError on target_kind or sampling_mode
```

然后 echo 删 PBS 539458 里的 monkeypatch,clean re-ship。

#### 不在 ask 范围

- ❌ narci 不需要 validate BJ target 语义正确性 (nyx 域)
- ❌ narci 不需要加 BJ-side broker / matching (echo 仍 trade CC)
- ❌ narci 不需要改 feature pipeline

#### 时间表

无 deadline,但 cheap (单文件 6 行)。任何 narci slack 时间都可以做。

**这一条 done 后** 把状态从 open 改成 done,并在 §7 增加
`7.10 — reply to echo Delivery 12`。
