# Narci 重构设计文档

> 状态:草案 v1 · 2026-05-26 · 作者:zhangenzhi + Claude
> 目标读者:narci 维护者。本文先给结论与路线图,再逐项给设计。**本阶段只交付设计,不动代码。**

---

## 0. TL;DR

四个痛点,根因一句话:

| # | 痛点 | 根因 | 优先级建议 |
|---|------|------|-----------|
| 3 | 录制经常丢数据 | 重连清空 buffer、Coincheck 无 gap 检测、落盘非原子、熔断前不 flush | **P0(最先)** |
| 1 | 时间网格采样(下一阶段主方向) | 采样逻辑硬编码在 `L2Reconstructor.process_dataframe` 的 `sample_interval_ms=100`,无采样器抽象,且存在两套并行采样实现 | P1 |
| 2 | 回测撮合不一致 | 存在两个 broker:GUI 走 naive 撮合(只看 best bid/ask、无队列、无延迟),生产走 `MakerSimBroker`;两者结果系统性背离 | P2 |
| 4 | 文件冗余、可维护性差 | **三套并行回测/模拟栈 + 两个 `FeatureBuilder`**;parquet/config/hash 逻辑各处复制;7 个 >500 行混职责文件 | 贯穿所有阶段 |

**最重要的一个判断(回答"哪套栈是主线"):**

> 主线 = `data/`(录制+重建)+ `features/realtime.py` + `simulation/` + `calibration/`。
> `backtest/` 整体降级为遗留路径(仅 `orderbook.py` 的 numba 内核与 `symbol_spec.py` 值得保留);`data/feature_builder.py` 合并进主线后删除。

依据见 §2。

---

## 1. 架构现实(与 CLAUDE.md 的偏差)

CLAUDE.md 只描述了 `backtest/` 一套回测栈。**实际仓库里有三套并行栈,且 CLAUDE.md 描述的那套恰恰是最不活跃的一套。**

### 1.1 这是一个多仓库生态,不是单体

```
nyx (另一个仓库)          narci (本仓库)                     echo (另一个仓库)
─────────────            ─────────────────────             ──────────────
训练 alpha 模型     →     calibration/alpha_models          实盘 maker 交易
(OLS/LGB/GRU)            (load_alpha_model, manifest 校验)        │
   │ manifest.json            │                                  │ 实盘 decisions/fills/cancels
   │ + 权重                   ↓                                  ↓
   └──────────────→   simulation/MakerSimBroker  ←──  calibration/writers.EchoLogWriter
                      (参考模拟器,镜像 echo 实盘 broker)      (parquet 日志)
                             │                                  │
                             ↓                                  ↓
                      calibration/replay.calibrate_session(把模拟器输出 vs echo 实盘成交对账,
                                                            产出 CalibrationReport + 参数建议回喂 nyx)
```

`narci` 在这个闭环里的职责:**录制实盘数据 → 重建 L2 → 生成特征 → 用参考模拟器跑策略 → 与 echo 实盘对账校准**。

### 1.2 三套栈 + 两个 FeatureBuilder 的真实导入图

| 栈 / 模块 | 谁引用它 | 活跃度(近 60 commits 文件触动) | 定位 |
|----------|---------|------------------------------|------|
| `calibration/` | simulation, research, 自带 21 个测试 | **19** | **生产主线**(nyx↔narci↔echo 桥) |
| `data/`(recorder/reconstruct) | main.py, 全栈 | **15** | 主线(数据底座) |
| `simulation/`(MakerSimBroker, backtest_alpha) | calibration, research | 3(但承载实盘 incident 修复) | **主线**(参考撮合器) |
| `features/realtime.py`(FeatureBuilder, `FEATURES_VERSION=v6`) | calibration, simulation, research | 0(已稳定,有版本契约) | **主线**(特征金标准) |
| `backtest/`(BacktestEngine/Jit/Event, broker.py) | 仅 `gui/panel_backtest.py` | 2 | **遗留**(GUI 回测) |
| `data/feature_builder.py`(另一个 FeatureBuilder) | 仅 main.py build-cache、backtest/backtest.py | — | **遗留**(旧离线特征) |

关键事实:
- `main.py` 的 CLI **完全不引用** backtest/simulation/calibration —— 它只是数据管线(record/compact/download/build-cache)。
- GUI 唯一的回测入口 `gui/panel_backtest.py:11` 走 `JitBacktestEngine` → `BacktestEngine` → `backtest/broker.py` 的 **naive 撮合**。
- **两个 `FeatureBuilder`**:`data/feature_builder.py`(固定 100ms 批处理网格)与 `features/realtime.py`(事件驱动 + `metric_sample_interval_ms`,带 `FEATURES_VERSION` 契约)。前者是遗留,后者是金标准。这是痛点 1 和痛点 4 共同的核心混乱源。

---

## 2. 主线栈裁决(回答"哪套保留")

**裁决:以 `calibration/ + simulation/ + features/realtime.py + data/` 为主线;`backtest/` 与 `data/feature_builder.py` 降级。**

理由:
1. **活跃度**:calibration 近 60 commits 有 19 次文件触动,backtest 仅 2 次。
2. **保真度**:`MakerSimBroker`(simulation/maker_broker.py)有 FIFO 队列估计、价格穿透成交(P3.1 修复 2026-05-13)、快照型 venue 支持、撤单延迟模型;而 `backtest/broker.py:61-78` 的 `_match_limit_orders` 只看 best bid/ask、无队列、无延迟、无成交流确认。
3. **契约绑定**:calibration 是 nyx↔narci↔echo 协议的实现方(schema/manifest/writers),实盘 incident 的修复都落在 simulation+calibration。
4. **特征版本**:`features/realtime.py` 有 `FEATURES_VERSION=v6` + `FEATURE_NAMES`,被 manifest 校验 pin 住;`data/feature_builder.py` 无版本概念。

**但 backtest/ 不是全删:**
- 保留 `backtest/orderbook.py`(numba `@njit` 事件驱动 L2 内核,增量型 venue 高性能)——可作为主线撮合的底层加速。
- 保留 `backtest/symbol_spec.py`、`backtest/venue_registry.py`(纯数据类/注册表,被 simulation/calibration 复用)。
- 移除/隔离 `BacktestEngine`/`JitBacktestEngine`/`EventBacktestEngine` 三个引擎与 `broker.py` 的 naive 撮合;GUI 回测改走主线撮合内核(见痛点 2)。

---

## 3. 不可破坏的契约(重构护栏)

重构必须把这些当成冻结接口,任何阶段不得无声破坏:

1. **RAW 录制格式(冻结)**:`replay_buffer/realtime/{exchange}/{market}/l2/*_RAW_*.parquet` 的 4 列 `[timestamp, side, price, quantity]`,side 编码 0-4。这是**不可变的真相源,必须永远向后兼容**——任何丢数据修复、新 schema 都不得改动 raw 既有列语义。
   - **cold-tier 不在此约束内**:`replay_buffer/cold/...` 的 DAILY 层是 daily_compactor + Vision official 合并的**派生物,可从 raw 完整重生成** → schema 自由演进(加 `venue`/`seq`/gap 标记列、重新分区都可),无需迁移脚本,改完重跑 compaction 即可。这是重要的降风险点:痛点 3 的 gap 标记、痛点 1 的采样产物都可落在 cold-tier 而不污染 raw。
   - feature cache 同理(派生物,可变)。
2. **echo↔narci 事件 schema**:`calibration/schema.py` 的 `DecisionEvent / FillEvent / CancelEvent` + pyarrow schema。
3. **nyx↔narci manifest 契约**:`calibration/alpha_models.py`
   - `SAMPLING_MODES` 冻结集合(§4.1 采样设计必须对齐)
   - `TARGET_KINDS`、`model_output_unit`
   - `manifest.narci_features_version_required` vs 运行时 `FEATURES_VERSION` 的 pin 校验
4. **接口文档**:`docs/INTERFACE_NARCI_NYX.md`、`docs/INTERFACE_NARCI_ECHO.md`、`docs/CALIBRATION_PROTOCOL.md`、`docs/ECHO_RAW_L2_SIDECAR_SPEC.md` —— 改动需同步更新。
5. **特征语义**:`features/realtime.py` 的 `FEATURE_NAMES` 顺序与 `FEATURES_VERSION`;改特征必须 bump 版本(下游模型按版本 pin)。

> 校准回归门禁:`calibration/replay.calibrate_session()` 对真实 echo 会话产出的 `CalibrationReport`(verdict: HEALTHY/ACCEPT_WITH_TUNING/UNHEALTHY)应作为撮合相关改动的验收基线 —— 改动前后 verdict 与关键指标(match_rate、timing_offset、adverse delta)不得退化。

---

## 4. 痛点逐项设计

### 痛点 3:录制稳健性(P0)

**现状丢数据路径**(均有 file:line 实证):

| # | 场景 | 位置 | 后果 |
|---|------|------|------|
| 1 | depth WS 重连清空 buffer | `data/l2_recorder.py:270-275` | 重连窗口内事件无声丢失 |
| 2 | Coincheck 无 gap 检测 | `data/exchange/coincheck.py:139-140`(`needs_alignment=False`) | channel 静默挂死仍写陈旧数据(2026-05-08 曾挂 15h);`_seen_snapshot` 是死代码 |
| 3 | 落盘非原子、无 fsync | `data/l2_recorder.py:461-464` | 崩溃于 `to_parquet` 中途 → 文件损坏/当前 cycle 丢失 |
| 4 | 熔断硬退出绕过 flush | `data/l2_recorder.py:220`(`os._exit(2)`) | 未落盘 buffer 全丢 |
| 5 | 对齐重试清 buffer | `data/l2_recorder.py:390` | Binance 每次重对齐丢 `pre_align_buffer`,10 次后退出 |
| 6 | pre_align_buffer 上限 2000 | `data/l2_recorder.py:353-354` | 溢出 FIFO 丢最旧事件 |

**关键事实(直接读码修正)**:重连**不**清空 `self.buffers`(只清 `pre_align_buffer`/`orderbooks`),已录事件不因重连丢。真正的丢数据是:
- 所有 recorder `save_interval_sec=600` → **硬崩溃(OOM/SIGKILL/掉电)丢每 symbol 最多 10 分钟**。
- 熔断 `os._exit(2)` 绕过 flush;`to_parquet` 非原子(崩溃留损坏 shard,daily_compactor 已有 corrupted-shard 处理 = 真发生过)。
- `save_loop:436` 在 `not stream_aligned` 时整段跳过落盘,而 trade 无条件入 buffer → 未对齐 symbol 的 trade **无限堆积且永不落盘**(内存泄漏 + 全丢)。
- 对齐循环找到首个对上事件即 `return` 清空 `combined` → **丢弃其后有效事件**。

**设计方向(已拍板:WAL 流式落盘)**:把"每 600s 攒内存→一次性写 parquet"改为 **WAL(write-ahead log)+ 定时合并**,使崩溃窗口从 10 分钟降到 ~秒级:

- **段式 WAL(推荐)**:每 `wal_flush_interval_sec`(默认 ~2s)或每 K 事件,把内存 micro-buffer **原子写**成一个完整小 parquet 段 `wal/{SYMBOL}_SEG_{seq}.parquet`(temp→`os.replace`,可选 fsync)。每个段本身都是合法可读 parquet。
- **定时合并**:`save_loop` 每 `save_interval` 读取该 symbol 全部段→按序 concat→原子写出规范的 `{SYMBOL}_RAW_*.parquet`→删段。**RAW 输出格式与文件数完全不变**,WAL 只是 `wal/` 子目录里的实现细节(GUI/compactor 只扫 `*_RAW_*.parquet`,看不到它)。
- **崩溃恢复**:`start()` 启动时扫 `wal/` 残留段→合并成 RAW(或留给 compactor)。残留段都是合法 parquet,恢复零成本。这同时缓解痛点 4 的"小文件过多"(段在 interval 内合并)。
- **熔断前强制 flush**:`_maybe_trip_alignment_breaker` 在 `os._exit` 前先同步 flush micro-buffer 到段。
- **修未对齐 symbol 落盘**:buffer 永远 flush(trade-only 段合法);snapshot 注入(side 3/4)只在已对齐且 book 非空时做。
- **不丢对齐后续事件**:对齐成功后处理 `combined` 余下全部,而非首个即 return。
- **Coincheck gap 标记**:无 U/u 序列,gap 检测放 **cold-tier 离线**(cold-tier 允许改 schema),不在录制热路径阻塞;录制侧靠现有 watchdog(`depth_stale_threshold_sec=180`)+ `snapshot_refresh_on_save`(coincheck 已开)覆盖。删 `coincheck.py` 死字段 `_seen_snapshot`。

**验证**:补充回归测试 —— 段原子写崩溃不留半文件、启动恢复残留段、熔断前已 flush、未对齐 symbol 的 trade 能落盘、对齐后续事件不丢。沿用 `calibration/tests/` 现有风格。

---

### 痛点 1:时间网格采样(P1,下一阶段主方向)

**现状:采样逻辑有两套,且都耦合在重建里:**
- 批处理:`data/l2_reconstruct.py:141-179` 的 `process_dataframe(sample_interval_ms=100)`,全局对齐网格 `(ts // interval + 1) * interval`。调用点:`backtest/backtest.py:177`、`data/feature_builder.py:104`、`gui/panel_l2_insight.py:158`。
- 流式:`features/realtime.py:407` 的 `metric_sample_interval_ms`(默认 500ms)。

两套各写一遍网格逻辑,且无法表达 nyx 已经在用的事件型采样。

**约束:采样抽象必须 1:1 对齐 `calibration/alpha_models.py:118` 的 `SAMPLING_MODES` 冻结集合:**
```
1s_grid · event_at_cc_trade · event_at_book_update · event_at_bj_trade · event_at_simulated_maker_fill
```
用户要的"固定时间网格"本质是把 `1s_grid` 泛化为任意 `{N}ms_grid`。

**设计方向(本阶段保底:做好固定网格抽象):**

引入显式 `Sampler` 抽象,从重建中剥离采样决策:

```python
class Sampler(Protocol):
    """决定『何时』从重建器快照一行特征。重建器只负责维护 book 状态。"""
    def should_emit(self, event_ts_ms: int, event_side: int) -> bool: ...
    @property
    def mode_tag(self) -> str: ...   # 必须 ∈ SAMPLING_MODES,写入 manifest/缓存元数据

class FixedGridSampler(Sampler):
    """保底实现:任意间隔的全局对齐时间网格。interval_ms=1000 即 SAMPLING_MODES 的 '1s_grid'。"""
    def __init__(self, interval_ms: int): ...
    # 复用现有 (ts // interval + 1) * interval 对齐逻辑,唯一权威实现
```

- 重建器(`L2Reconstructor` / `features/realtime`)接收一个 `Sampler`,把"是否在此刻 emit"的判断外包出去;事件型采样(`event_at_*`)后续作为 `EventSampler` 子类接入,无需再改重建核心。
- `mode_tag` 写入特征缓存文件名/元数据,使缓存与 nyx manifest 的 `sampling_mode` 可校验对齐(防止用错网格的缓存喂给模型)。
- 本阶段只交付 `FixedGridSampler` + 抽象骨架;事件型留接口。

**收益**:消灭两处重复网格逻辑;为 nyx 的多采样模式提供干净挂载点;缓存与 manifest 对齐可验证。

---

### 痛点 2:回测撮合一致性(P2)

**现状:两个 broker,结果系统性背离。**
- GUI/遗留:`backtest/broker.py:61-78` `_match_limit_orders` —— 只看 best bid/ask,价格穿越即全量成交,**无队列、无时间优先、无延迟、不看成交流**。回测会系统性高估成交率与成交速度。
- 生产/主线:`simulation/maker_broker.py` —— FIFO 队列估计、价格穿透成交、快照型 venue 支持、撤单延迟、库存预留(实盘 incident 0510/P3.1 修复都在此)。

"模拟盘不一致" = 用户经由 GUI 看到的回测走了 naive 路径,而真正与实盘对账的是 `MakerSimBroker`,两者不是同一套撮合。

**设计方向:单一撮合内核。**

- **撮合内核唯一化**:以 `MakerSimBroker` 为唯一 maker 撮合实现;底层增量 book 用 `backtest/orderbook.py` 的 numba 内核加速(增量型 venue),快照型 venue 走 `L2Reconstructor`。
- **删除 naive 路径(已拍板)**:`gui/panel_backtest.py` + `BacktestEngine`/`JitBacktestEngine`/`EventBacktestEngine` + `backtest/broker.py` 直接删除,不做改道。回测统一经由 `simulation/backtest_alpha` / `calibration/replay` 的主线撮合。删除前全仓 grep 确认无残留引用。
- **明确 venue→撮合矩阵**(写进文档,代码按 venue 选择):

  | venue 类型 | 代表 | book 重建 | 撮合 |
  |-----------|------|----------|------|
  | 增量型(U/u) | Binance UM/Spot/JP | orderbook.py numba 增量 | MakerSimBroker + FIFO queue |
  | 全快照型 | Coincheck/bitbank/GMO/bitFlyer | L2Reconstructor 原子替换 | MakerSimBroker + prune-dust |

- **删除/隔离** `backtest/broker.py` 的 `_match_limit_orders` naive 路径与 `BacktestEngine`/`JitBacktestEngine`/`EventBacktestEngine`(若 GUI 迁移完成)。
- **一致性门禁**:把 `calibration/replay` 的对账作为撮合改动的 CI 回归(§3)。

> 注意:这不是"撮合机制缺失",生产撮合其实相当完善;问题是**存在第二个劣质撮合且暴露给了 GUI**。重构本质是"收敛到唯一内核 + 让所有入口都用它"。

---

### 痛点 4:文件冗余 / 可维护性(贯穿)

**A. 三套栈 → 一套**(见 §2 裁决):删 `backtest/` 三引擎 + naive broker;合并两个 `FeatureBuilder` 到 `features/realtime.py`,删 `data/feature_builder.py`(调用方 main.py build-cache、backtest 改道)。

**B. 抽取公共件**(消除复制粘贴):
- `data/_io.py`:`save_parquet/load_parquet`(统一 pyarrow+snappy+index=False,现散在 10+ 处)。
- `data/_config.py`:`load_config_section(path, section)`(现散在 5 处,错误处理不一致)。
- `data/_cache.py`:`compute_cache_hash(files, prefix)`(现 main.py/backtest/feature_builder 三处逐行重复)。
- 统一 L2 重建入口(`backtest/backtest.py:166-177` 与 `feature_builder.py:96-104` 几乎逐行相同)。

**C. 拆分 >500 行混职责文件**(7 个):优先 `data/l2_recorder.py`(611 行,5 职责:WS 连接/对齐状态机/快照注入/buffer/IO)——痛点 3 重构时顺带拆为 `connection / alignment / buffer / sink`。

**D. 删死代码/别名**:
- `BinanceL2Recorder`(l2_recorder.py:608)、`BinanceDownloader`(download.py:121)、`BinanceDataValidator`(validator.py:118)别名 → 直接用中性名。
- `Orderbook.apply_diff`、`L2Reconstructor.apply_diff`(已注释为遗留)。
- `coincheck.py` 的 `_seen_snapshot` 死字段。
- main.py 新旧目录结构双路径回退(若历史数据已统一)。

**E. 配置收敛**:5 个 recorder yaml 共享同一 `recorder:` schema,`exchange+market_type` 已隐含在文件名 → 评估收敛为单 `recorder.yaml` + profile 选择(低优,可选)。

---

## 5. 分阶段路线图

每阶段独立可交付、可回滚。顺序按"风险隔离 + 用户主方向"排:

| 阶段 | 内容 | 痛点 | 依赖 | 验收 |
|------|------|------|------|------|
| **P0 护栏** | 固定测试基线;冻结 §3 契约为显式测试;录一份 echo 会话作校准金标准;`FEATURE_NAMES`/`SAMPLING_MODES` 快照测试 | — | 无 | 全测试绿;校准 verdict 基线记录 |
| **P1 录制稳健** | 段式 WAL + 定时合并(崩溃窗口→秒级);原子写;熔断前 flush;修未对齐落盘;不丢对齐后续事件;启动恢复残留段 | 3(+4C) | P0 | 回归测试(见痛点3);线上灰度单 symbol 后全量 | 
| **P2 冗余底座** | 抽 `_io/_config/_cache`;统一 L2 重建入口;**合并两个 FeatureBuilder**;删别名/死代码 | 4(A/B/D) | P1 | 行数下降;`data/feature_builder.py` 删除;无行为变化(diff 测试) |
| **P3 采样抽象** | `Sampler` 抽象 + `FixedGridSampler`;两处网格逻辑收敛;mode_tag 写缓存元数据;对齐 `SAMPLING_MODES` | 1 | P2 | 缓存与 manifest sampling_mode 可校验;事件型采样接口预留 |
| **P4 撮合收敛** | 撮合内核唯一化;**删** GUI 回测面板 + naive broker + 三遗留引擎;venue→撮合矩阵落地 | 2(+4A) | P3 | 校准对账 verdict 不退化;backtest/ 仅剩 orderbook/symbol_spec/venue_registry |

> 排序理由:P1 最危险且最独立(数据是一切的根),先做止血;P2 清底座让后续改动安全;P3 是你点名的主方向;P4 依赖 P2/P3 的干净底座与采样抽象。如需提前主方向,P3 可与 P1 并行(两者不冲突),但 P2 必须在 P4 前。

---

## 6. 风险登记

| 风险 | 影响 | 缓解 |
|------|------|------|
| 改录制热路径引入新丢数据 | 数据是全系统根 | P0 先建回归;P1 灰度单 symbol 跑;raw append-only 改动本身降低丢数据面 |
| 破坏 nyx↔narci manifest 契约 | 模型加载失败/采样错配 | §3 契约测试;`SAMPLING_MODES`/`FEATURES_VERSION` 快照 |
| 合并 FeatureBuilder 改变特征值 | 下游模型偏移 | 合并需 bit-level diff 测试;必要时 bump `FEATURES_VERSION` |
| 删 backtest/ 三引擎 + GUI 面板误删仍被引用项 | import 崩 | 删前全仓 grep;orderbook/symbol_spec/venue_registry 保留 |
| 误改 RAW 既有列语义 | 旧录制读不了、真相源损坏 | RAW 冻结(§3.1);所有新 schema 只落 cold-tier(派生可重生成) |

---

## 7. 开放问题

**已拍板:**
- ✅ **数据兼容性**:RAW 冻结、永远向后兼容;cold-tier 是可重生成派生物,schema 自由演进(见 §3.1)。
- ✅ **GUI 回测**:直接砸掉。`backtest/` 的 `BacktestEngine`/`JitBacktestEngine`/`EventBacktestEngine` + `broker.py` naive 撮合 + `gui/panel_backtest.py` 直接删除;仅保留 `orderbook.py`/`symbol_spec.py`/`venue_registry.py`。P4 因此大幅简化为"删除 + 收口",不再需要 GUI 改道。

**待拍板:**
1. **配置收敛(§4E)**:5 个 recorder yaml 是否收敛为单文件 + profile?(低优,可推迟。)
2. **事件型采样何时做**:本文 P3 只交付 `FixedGridSampler` 保底;`event_at_*` 系列是否本轮一并落地,还是仅留接口待 nyx 需要时再填?

---

## 附:关键文件索引

| 关注点 | 文件 |
|--------|------|
| 录制 + 丢数据路径 | `data/l2_recorder.py`、`data/exchange/{base,binance,coincheck}.py` |
| L2 重建 + 采样 | `data/l2_reconstruct.py`、`features/realtime.py` |
| 参考撮合器(主线) | `simulation/maker_broker.py`、`simulation/backtest_alpha.py` |
| 撮合 numba 内核(保留) | `backtest/orderbook.py`、`backtest/symbol_spec.py` |
| naive 撮合(待删) | `backtest/broker.py`、`backtest/backtest.py` |
| 契约 | `calibration/{schema,alpha_models,priors,writers}.py` |
| 校准对账门禁 | `calibration/replay.py` |
| 遗留特征(待合并) | `data/feature_builder.py` |
