# Narci ↔ Echo Calibration Protocol

> 由 narci session 在 2026-05-06 起草。
> Narci 本身是 simulator；echo 是真实环境。两者必须**互相校准**，否则 narci 的 backtest 数字 = 噪声，echo 的策略迭代 = 盲目。这份文档是双向反馈环的契约。

---

## 0. Why this exists

Narci 的 maker simulator 包含一堆**经验参数**：
- 队列位置估计模型（无 order ID 时的近似）
- Adverse selection 比例（baseline + 受信号影响后）
- Cancel/replace 时延（网络 + 撮合）
- Quote stale 期间的成交概率（你 reprice 还在路上时被 fill）

这些参数**只能从真实 fill 数据校准**。Echo Phase 0 上线的核心产出就是给 narci 提供这些数据。**没有这份契约，echo 跑了也白跑——数据散在 log 里 narci 用不了。**

---

## 1. 核心反馈环

```
┌─ narci ─┐                            ┌─ echo ─┐
│         │                            │        │
│ v1 sim  │ ───── backtest PnL ─────►  │ deploy │
│         │                            │ Phase 0│
│         │                            │        │
│   v2 ◄──┼─── replay+compare ─────── │ logs   │
│   sim   │                            │        │
└─────────┘                            └────────┘
                                              ▲
                                              │
                            calibrated PnL ───┤
                                              │
                                            decide
                                          Phase 1 上不上
```

**关键约束**：echo 任何阶段不可以"跳级"——必须先用 narci v_N simulator 跑通 + 真实数据校准 narci v_{N+1} + 才能进 Phase N+1。

---

## 2. Echo 的日志契约

Raw L2 stream **不是** echo 的责任——narci recorder 已经在跑（`data/l2_recorder.py` 里的 `L2Recorder`）。

Echo 只写 3 类**决策/执行**事件，narci recorder 提供原料。

### 2.1 部署：跨主机 + Echo 自录 L2（推荐）

实际部署：narci 跟 echo 各占一台 EC2，都在 ap-northeast-1。Echo 在自己进程内同时跑 L2 recorder（库形式，复用 `narci.data.l2_recorder.L2Recorder`）。

```
narci instance (ap-northeast-1, host A):
  ├── narci-recorder-coincheck    (docker, canonical 30 symbol stream)
  ├── narci-recorder-binance-jp   (docker)
  ├── narci-recorder-binance-um   (docker)
  └── rclone sidecar → gdrive

echo instance (ap-northeast-1, host B):
  └── echo (process):
      ├── L2Recorder (库)             ← 录 echo 关心的 symbol (~4 个)
      ├── Strategy / LiveTradingEngine ← 决策
      └── 共享 WsBus（同 WS 帧 0 jitter 给两个 consumer）
```

#### 双 L2 源的角色分工

| L2 源 | 用途 | 时钟对齐 echo 决策 |
|---|---|---|
| **Echo 自录 L2**（同进程同 WS 帧）| 复盘 echo 决策 — calibration 主用 | **<1 ms** |
| Narci recorder L2 (host A) | 跨 symbol 训练 / 历史 backtest / canonical | 5-15ms (与 echo) |

**关键**：calibration replay 时 narci 优先用 echo 自录 L2，因为 echo 决策时看到的就是这份。narci recorder 仅用于其他研究用途。

#### 在这种部署下能做 / 不能做

| 能做（calibration 可信）|
|---|
| sub-ms 决策序对账（用 echo L2）|
| 队列动力学复盘（同 WS 帧）|
| Adverse selection 任意 window（同 WS 帧时戳）|
| Cancel latency 分布 |
| Fill rate 精确量级 |

| 仍然受限 |
|---|
| Echo 看到的 vs 别的市场参与者看到的 race condition（任何外部观察者都做不了）|
| Narci recorder L2 vs echo L2 的横向对账精度（5-15ms，但这不是关键路径）|

⇒ Phase 0 / 1 / 2 都够。

#### 实例规格 & 成本

不考虑成本——但记录下：echo 录 4 symbol 大约 +500MB/天 disk + 5-10% CPU 一核。c7g.large（2 vCPU 4GB） 还有 80%+ 余量。

#### 必要的 ops 配置

为了把 5-15ms jitter 控制在量级内：

| 配置 | 推荐 |
|---|---|
| **NTP sync** | 两台都用 AWS Time Sync Service (`169.254.169.123`)；clocks 漂移 <1 ms |
| **AZ 选择** | 两台尽量在同一 AZ（ap-northeast-1a），cross-AZ 增加 ~2ms |
| **EC2 实例类型** | c7g.large 或更高；avoid burst credits 类（t 系列）防 schedule jitter |
| **WS 订阅独立** | narci 各 recorder 一条 WS, echo 一条 WS，互不复用——可靠性优于性能 |
| **时间戳记录方式** | 双方都用 `time.monotonic_ns()` 记内部 ts；同时存 `time.time_ns()` 做跨主机对账 |

#### 例外：UM 信号 features 的特殊处理

Echo 实时下单依赖 UM 信号 features，必须从 echo 自己的 UM WS 算（不能依赖 narci recorder——延迟太高）。

但 calibration replay 时 narci 用**自己的 recorder UM stream** 重新算 features → 跟 echo 决策时用的 features 不完全一致（host jitter 5-15ms）。

解决：echo 必须 **log 决策时刻的 features 快照**（hash + 关键值），不仅是 alpha_pred_bps。详见 §2.2 DecisionEvent 的 `alpha_features_hash` 字段。Calibration replay 时：
- 比 echo 记的 features hash 与 narci 复算的 features hash
- 不一致 → host jitter 影响信号；记录 jitter 量级，不重算 alpha
- 一致 → 用 narci 复算来 backtest 同样决策



### 2.2 Decision log（每个 place / cancel / reprice）

新文件，narci 这边的 schema 定义在 `narci/calibration/schema.py`：

```python
@dataclass
class DecisionEvent:
    ts_ns: int                    # 高精度本地时钟（决策时刻）
    event_type: str               # "PLACE" | "CANCEL" | "REPRICE_TRIGGER"
    client_oid: str
    
    # 决策时的市场状态快照
    mid_price: float              # 当时本地 reconstruct 的 mid
    best_bid: float
    best_ask: float
    top1_bid_qty: float
    top1_ask_qty: float
    spread_bps: float
    
    # 决策的 alpha + 来源
    alpha_pred_bps: float | None  # OLS 预测的 1s mid log-return * 10000
    alpha_features_hash: str      # feature snapshot 的 hash（用于 narci 复算 alpha 是否一致）
    alpha_source: str             # "naive" | "ols_v1" | "ols_v1_fallback"
    
    # 行为
    side: str                     # "BUY" | "SELL"
    price: float                  # quote price
    qty: float                    # quote size
    estimated_queue_position: int # narci 队列模型给的估计值
    estimated_queue_ahead_qty: float  # 估计前面累计 size

    # for CANCEL only
    place_oid: str | None         # 被 cancel 的对应 PLACE 的 client_oid
    
    # for REPRICE_TRIGGER only
    reason: str | None            # "MID_DRIFT" | "ALPHA_FLIP" | "POST_FILL" | "STALE_TIMEOUT"
```

文件路径：`echo/logs/decisions/{symbol}/{YYYYMMDD}/{HHMMSS}.parquet`

### 2.3 Fill log（每个 execution_report 触发）

注：`mid_at_*` 字段由 echo 在自己内存里维护的 orderbook 给出（基于 echo 自己看到的 WS 流）。这跟 narci recorder 的 mid 应当一致（同主机时差 <1ms）；calibration replay 时若发现 echo 记录的 mid 与 narci recorder 重建的 mid 显著偏离，就是同主机假设被打破的信号。


```python
@dataclass
class FillEvent:
    ts_ns: int                    # echo 收到 execution_report 时刻
    exchange_ts_ms: int           # 交易所标的成交时刻
    client_oid: str
    exchange_oid: str
    side: str                     # BUY/SELL
    fill_price: float
    fill_qty: float
    is_maker: bool                # CC 必为 True；其他场地可能 False
    
    # 关键校准字段
    place_ts_ns: int              # 这个 oid 当初的 PLACE ts_ns
    quote_age_ms: float           # fill_ts - place_ts
    mid_at_place: float
    mid_at_fill: float
    spread_at_fill_bps: float
    
    # adverse selection 测量（fill 后未来 mid 移动）
    mid_t_plus_500ms: float | None
    mid_t_plus_1s: float | None
    mid_t_plus_5s: float | None
    mid_t_plus_30s: float | None
    
    # 队列实际位置反推
    estimated_queue_at_fill: int  # 成交瞬间 narci 模型估计还排第几（理论上应该是 0/1）
    estimated_queue_at_place: int # 当初挂单时的估计
    
    # 库存状态
    inventory_before: float       # base asset
    inventory_after: float
    
    # alpha 复盘
    alpha_pred_bps_at_place: float | None
    alpha_realized_bps_post: float | None  # log(mid_t_plus_1s / mid_at_fill) * 10000
```

文件路径：`echo/logs/fills/{symbol}/{YYYYMMDD}/{HHMMSS}.parquet`

### 2.4 Cancel log（每个 cancel ack 触发）

```python
@dataclass
class CancelEvent:
    ts_ns_request: int            # 发出 cancel REST call
    ts_ns_ack: int                # 收到交易所 ack
    cancel_latency_ms: float
    client_oid: str
    exchange_oid: str
    place_ts_ns: int              # 原 PLACE 的 ts
    quote_age_at_cancel_ms: float
    cancel_reason: str            # "STRATEGY_REPRICE" | "RISK_LIMIT" | "KILL_SWITCH" | "INVENTORY"
    final_state: str              # "CANCELLED" | "FILLED_DURING_CANCEL" | "REJECTED"
    qty_filled_before_cancel: float  # 部分成交后才取消的情况
    mid_at_cancel: float
```

文件路径：`echo/logs/cancels/{symbol}/{YYYYMMDD}/{HHMMSS}.parquet`

### 2.5 时钟与 ID 一致性

跨主机部署关键：

| 要求 | 说明 |
|---|---|
| **`ts_ns` 用 monotonic_ns()** | 内部事件顺序 — 不会被 NTP 跳变扰动 |
| **`ts_wall_ns` 用 time_ns()** | wall clock — 跨主机对账唯一手段，要求 NTP 同步 (AWS Time Sync) |
| **`exchange_ts_ms`** | 交易所返回的 timestamp — 同一笔成交两台机器可以对齐 |
| **`client_oid` 全局唯一** | echo 生成 `echo-{uuid_hex16}`；不能跨 session 重复 |
| **每个事件都标 session_id** | parquet 文件名里带 session_id |
| **session meta 含 host info** | hostname, AZ, NTP source — 一旦 jitter 异常知道往哪查 |

---

## 3. Narci 的 replay & validation 协议

输入：echo 一个 session 的 4 套 parquet（raw + decisions + fills + cancels）。

### 3.1 三阶段流程

```python
# narci/calibration/replay.py

def calibrate_session(echo_session_dir: Path,
                      narci_recorder_dir: Path | None = None) -> CalibrationReport:
    """
    echo_session_dir: echo 的 decisions/fills/cancels + raw_l2 logs
    narci_recorder_dir: 可选，narci 跨主机 canonical L2 (用于 sanity 对账)

    Replay 默认用 echo 自录 L2 (sub-ms 精度), narci recorder 仅用于：
      1. echo 漏录 fallback
      2. 跨主机 jitter 测量 (echo L2 时戳 vs narci L2 时戳的偏差分布)
    """
    decisions, fills, cancels = load_echo_logs(echo_session_dir)
    raw = load_echo_l2(echo_session_dir / "raw_l2")
    if narci_recorder_dir is not None:
        narci_l2 = load_recorder_window(narci_recorder_dir, fills.window)
        jitter_stats = measure_cross_host_jitter(raw, narci_l2)
    else:
        jitter_stats = None
    
    # === Stage 1: deterministic replay ===
    sim_broker = MakerSimBroker(...)
    strategy = load_strategy_from_decisions(decisions)  # rebuild strategy state from logs
    
    for event in raw.iter_events():
        sim_broker.on_event(event)
        # 模拟 strategy 决策点
        sim_broker.maybe_emit_decisions(at=event.timestamp)
    
    sim_decisions = sim_broker.get_decisions()
    sim_fills = sim_broker.get_fills()
    sim_cancels = sim_broker.get_cancels()
    
    # === Stage 2: pairwise comparison ===
    decision_match = compare_decisions(decisions, sim_decisions)
    fill_match = compare_fills(fills, sim_fills)
    cancel_match = compare_cancels(cancels, sim_cancels)
    
    # === Stage 3: parameter calibration ===
    new_params = fit_calibration(
        observed_adverse=measure_adverse(fills),
        observed_queue_jump_rate=measure_queue_jumps(fills, decisions),
        observed_cancel_latency=measure_cancel_latency(cancels),
    )
    
    return CalibrationReport(
        match_metrics=...,
        suggested_params=new_params,
        confidence=...,
    )
```

### 3.2 比对指标

每个跑 session 后输出：

```
SESSION: 2026-05-15-coincheck-btcjpy
  Duration: 4h 23m
  Real fills: 247 | Sim fills: 261 | match within 2s: 198 (80.2%) ✓
  Real cancels: 1438 | Sim cancels: 1402 | match: 1361 (94.7%) ✓
  
  Adverse selection (post-fill mid move 1s):
    Real mean:  +0.42 bps
    Sim mean:   +0.51 bps
    Δ:          -0.09 bps  (sim 高估 21%)  ⚠️ 微调
  
  Adverse selection 5s:
    Real mean:  +0.38 bps
    Sim mean:   +0.49 bps
    Δ:          -0.11 bps  (sim 高估 29%)  ⚠️ 微调
    
  Queue position bias:
    Real fill 时实际位置 (echo 推算):  median 1.2, p95 4.8
    Sim 估计位置 at fill:              median 0.8, p95 3.5
    → sim 低估排队人数; 加 1.4× scaling factor
    
  Cancel latency:
    Real p50: 187ms  p95: 412ms  p99: 1.8s
    Sim assumes constant 100ms → unrealistic, use real distribution
    
  Suggested narci v1.1 params:
    adverse_baseline_bps: 0.40 (was 0.55)
    queue_scaling: 1.4 (was 1.0)
    cancel_latency_dist: empirical (was constant 100ms)
```

### 3.3 接受 vs 拒绝

| 指标 | 健康范围 | 不健康 → 动作 |
|---|---|---|
| Fill count match (sim/real) | 0.7-1.4× | 修队列模型 / fill 概率模型 |
| Fill timing match (within 2s) | ≥ 70% | 修撮合时序模型 |
| Adverse 1s mean Δ | <±0.2 bps | 修 adverse 分布 |
| Cancel match rate | ≥ 90% | 修 cancel state machine |
| Cancel latency Δ p95 | <±50% | 用 echo 实测分布替代 |
| Queue position bias | scaling 0.5-2.0× | 整体 scaling 即可；超出说明模型机制错 |

如果 ≥ 3 个指标 unhealthy，**simulator 不可信**——narci 这边停止做新 backtest，先修。

如果全部健康但 < 70%/90% 的硬阈值不达到，给出 ⚠️ 警告但允许继续。

---

## 4. 反馈节奏

| 频率 | 谁做 | 内容 |
|---|---|---|
| **实时（echo session 跑期间）** | echo | 写 4 套 log parquet + 滚动 1h flush |
| **每日 09:00 JST** | narci 自动跑 | 上一天的 calibration report，发 Slack |
| **每周一 review** | 双方人 | 一周累计 calibration trend, 决定 simulator 升级 |
| **simulator 升级时** | narci | 标记 v1 → v1.1，所有 backtest 必须重跑 |
| **echo 阶段切换前** | 双方 | 必须有最近 7 天 healthy report |

---

## 5. 阶段验收门槛

不同阶段需要不同的校准成熟度：

| 阶段 | 必要条件 | 简单说就是 |
|---|---|---|
| **Echo 0a → 0b** | raw log 录制无丢失 | 数据齐 |
| **Echo 0b → 0c** | narci 能 replay decisions, decision_match ≥ 90% | sim 能跟上策略逻辑 |
| **Echo 0c → 1a** | 7 天 calibration report 全 healthy | sim 在量纲上正确 |
| **Echo 1a → 1b** | adverse 模型校准误差 ≤ 0.1 bps | alpha PnL 估计可信 |
| **Echo 1b → 2** | 30 天累计 simulator predicted PnL 与实际偏差 < ±20% | 端到端可信 |

---

## 6. Bootstrap：没数据时怎么办

Echo 还没跑前，narci v1 simulator 必须给一组**先验默认值** + 显式 uncalibrated 标志：

```python
# narci/calibration/priors.py
COINCHECK_BTC_JPY_PRIORS = CalibrationParams(
    adverse_baseline_1s_bps=0.55,        # 文献 + 直觉; 校准前不可信
    adverse_baseline_5s_bps=0.45,
    queue_scaling=1.0,                   # 1:1 默认
    cancel_latency_ms_p50=200,
    cancel_latency_ms_p95=500,
    fill_probability_per_sec=0.05,       # echo 还没跑时的粗估
    UNCALIBRATED=True,                   # 任何 backtest 输出强制带 [UNCAL] 前缀
)
```

任何 backtest 在 UNCALIBRATED 状态下输出的 PnL 都加显式警告：

```
=== Backtest result ===
[⚠ UNCALIBRATED — narci sim params are priors only, not validated against echo data]

Daily PnL: +$45 ± $30 (wide error bar)
...
```

校准后这个标志自动消失。

---

## 7. 文件 / 命名规范

```
# Echo 写决策/执行 logs + 自己的 L2 录制
echo/logs/{session_id}/
├── meta.json                  # session metadata
├── raw_l2/                    # echo 自录, 用于精确 calibration replay
│   └── {symbol}_RAW_{date}_{time}.parquet
├── decisions/
│   └── {symbol}_DEC_{date}_{time}.parquet
├── fills/
│   └── {symbol}_FILL_{date}_{time}.parquet
└── cancels/
    └── {symbol}_CXL_{date}_{time}.parquet

# Narci recorder（已有，canonical 30 symbol，跨用途）
narci/replay_buffer/realtime/{exchange}/{market}/l2/
└── {symbol}_RAW_{YYYYMMDD}_{HHMMSS}.parquet

# Calibration 输出（新）
narci/calibration/reports/{date}/
├── {session_id}_summary.json
├── {session_id}_metrics.parquet     # per-fill diff metrics
└── {session_id}_suggested_params.yaml
```

`session_id` 格式：`YYYYMMDD-HHMMSS-{exchange}-{symbol}-{strategy_v}`

**meta.json 必须记录 `recorder_session_id` 或时间窗口**——这是 calibration replay 找对应 narci recorder 文件的依据。

---

## 8. Echo 必须 commit 的 dummy data 一次

为让 narci 单测能跑，echo 必须在 repo 里 commit 一份小型 dummy session（1 hour, ~100 fills），路径 `echo/tests/fixtures/calibration_session_v0/`. narci 的 calibration 单测 import 这个跑断言。

这保证 schema 变化 = test fail，不会 silent drift。

---

## 9. 接口冻结清单

下面这些 narci 这边要先实现，作为 echo 的依赖：

| narci 模块 | 给 echo 用 |
|---|---|
| `narci.calibration.schema` | DecisionEvent, FillEvent, CancelEvent dataclass + parquet schema |
| `narci.calibration.writers` | 提供 `EchoLogWriter` helper class，echo 直接 import |
| `narci.calibration.priors` | 默认 v1 params；echo 启动时 load |
| `narci.calibration.replay` | `calibrate_session(session_dir)` |
| `narci.simulation.MakerSimBroker` | 接受 calibration params; 输出与 echo 同 schema 的 sim_decisions/fills/cancels |

接口一旦冻结，**不能 breaking change** 直到 v2 simulator 正式 release。

---

## 10. 一个 worked example

假设 echo 04-23 跑了一个 4 小时 session，narci 跑 calibration：

**Step 1: load**
```python
session = load_calibration_session("/echo/logs/20260423-100000-coincheck-btcjpy-naive_v1/")
# → 14,400 raw events, 247 fills, 1438 cancel events, 421 decisions
```

**Step 2: replay**
```python
sim = MakerSimBroker(params=PRIORS_V1)
for ev in session.raw_events: sim.on_event(ev)
sim_fills = sim.get_fills()
# → 261 sim fills
```

**Step 3: pair**
```python
matches = pair_fills(real=session.fills, sim=sim_fills, tolerance_ms=2000)
# matches: 198 (80% 包内)
```

**Step 4: report**
```
narci/calibration/reports/20260423/
└── coincheck-btcjpy-naive_v1_summary.json
```

里面包含：
```json
{
  "fill_match_rate": 0.802,
  "adverse_1s_real_mean_bps": 0.42,
  "adverse_1s_sim_mean_bps": 0.51,
  "queue_scaling_suggestion": 1.42,
  "cancel_latency_p50_real_ms": 187,
  "cancel_latency_p50_sim_ms": 100,
  "verdict": "ACCEPT_WITH_TUNING",
  "next_step": "apply suggested_params and rerun"
}
```

**Step 5: 应用建议**
```python
new_params = PRIORS_V1.update(suggested)
PRIORS_V1_1 = new_params  # version bump
```

下次任何 backtest 用 V1.1 即可——所有以 V1 跑过的结果 mark stale。

---

## 11. 反例：什么会让 calibration 失败

记下这些坑：

1. **Narci recorder 跨 region 部署** → 时序差几十 ms，1s 窗口都不够；必须同 region
2. **Narci 或 echo 有一台没启用 NTP / 用了不同 NTP server** → wall clock 漂移积累；强制 AWS Time Sync (169.254.169.123)
3. **Narci recorder 在 echo session 期间停了 / 漏录** → 时间段对不上，那段无法 calibrate
4. **Echo log 用 wall clock 当事件序** → NTP 跳变把时序搞乱；必须用 monotonic_ns() 做内部序
5. **Strategy 状态依赖隐藏 random** → replay 不可重复，决策 match 假
6. **Schema 没 commit 测试 fixture** → 升级 schema 后 echo 静默失败 → narci 拿到坏数据
7. **报告每天 100 个但没人看** → 漂移累积；必须有 anomaly alert
8. **Calibration 跑在 echo run 同时** → I/O 冲突；narci calibration 必须延迟一天跑
9. **跨主机 WS 各订一份**（必然，跨主机情况下唯一选择）→ 收到的微秒级顺序不同；fill_match 用 ±2s 容忍窗口；alpha_features_hash 不一致是预期，不是 bug
10. **EC2 实例类型选了 t 系列**（burst credit）→ 高负载时 schedule jitter 飙升；用 c/m 系列稳定 baseline

---

## 12. 谁负责什么（角色清单）

| 工作 | narci session | echo session |
|---|---|---|
| Raw L2 录制 | ✅ recorder docker（已有，部署到 echo 同主机）| — |
| Schema 定义 + 维护 | ✅ owner | implements writer |
| Writer helper (EchoLogWriter) | ✅ implements | imports |
| Reader / replay | ✅ implements | — |
| Simulator (MakerSimBroker) | ✅ implements | — |
| Decision/fill/cancel logging in trading loop | — | ✅ implements |
| Daily calibration cron job | ✅ implements | — |
| Weekly review report | shared | shared |
| Schema 升级 (v1 → v2) | ✅ propose + execute | adapts writer |
| 部署运维：narci recorder 跟 echo 部一台 | shared | shared |

---

## 13. 一句话

**没有 calibration protocol，echo 是盲飞，narci 是空想。**
**有了它：echo 上线一周 = narci 升级一次 = 下次 echo 跑得更准 = 越跑越靠谱。**

这是整个 narci-echo 系统的核心反馈回路。
