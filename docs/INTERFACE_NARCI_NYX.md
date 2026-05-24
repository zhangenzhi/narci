# INTERFACE_NARCI_NYX — narci → nyx contract

> 这份是 **narci** 团队回给 **nyx** 团队的对应文档。与 nyx 端
> `docs/INTERFACE_NYX_NARCI.md` (nyx SHA `1c0b39b` 重命名前是
> `NARCI_NYX_INTERFACE.md`) 对偶。
>
> 命名约定:`INTERFACE_<author>_<audience>.md` (author 在前)。本文 narci
> 维护,声明 narci 这边提供给 nyx 的契约面 — feature 接口、版本承诺、
> 反向需求清单状态、反向 ask。
>
> **范围**:契约 surface only。详细 incident log + nyx 研究记录留在 nyx
> 端 `INTERFACE_NYX_NARCI.md`,本文不重复。
>
> Authoritative as of **2026-05-16**, narci git SHA `ad17fe7` on `main`。
> Re-cut whenever FEATURES_VERSION bump、Manifest schema 变动、enum 扩展、
> 或 ask 状态变化。

---

## 1. narci 提供给 nyx 的接口

### 1.1 `features.realtime` — FEATURES_VERSION = `v6`

当前 `FEATURE_NAMES` 38 列,严格顺序如下(`narci/features/realtime.py:121`)。
**列顺序是契约的一部分** — 模型按 index 取特征向量,任何重排 = 隐式 break。

```
BASELINE_FEATURES (23):
  [ 0-11] UM trade-based:     r_um / r_um_2s / r_um_5s / r_um_10s
                              um_imb_1s / um_imb_30s_norm / um_n_5s / um_vol_5s
                              um_flow_{50ms,100ms,500ms,5s}                  (v4)
  [12-14] BJ trade-based:     r_bj / bj_imb_1s / bj_flow_5s
  [   15] CC trade intensity: trade_intensity_burst_50ms                     (v6 NEW)
  [16-18] CC own:             r_cc_lag1 / r_cc_lag2 / cc_imb_1s
  [19-22] Basis:              basis_bj_bps / um_x_basis / basis_um_bps
                              basis_um_bps_trade_proxy                       (v5)

TIER1_FEATURES (5):
  [23-24] hour_sin / hour_cos
  [25-27] cc_flow_5s / cc_flow_30s / cc_imb_30s_norm

TIER2_FEATURES (10, L2-gated):
  [28-34] cc_l2_top1_imb / cc_l2_top5_imb / cc_l2_imb_top1_5s / cc_l2_imb_top5_5s
          cc_l2_micro_dev_bps / cc_micro_dev_5s / cc_l2_spread_bps
  [35-37] bj_l2_top1_imb / bj_l2_top5_imb / l2_imb_diff
```

### 1.2 `calibration.alpha_models.Manifest` schema

Manifest schema version: `v1`(`MANIFEST_SCHEMA_VERSION` 常量)。frozen
dataclass 字段(`narci/calibration/alpha_models.py:88-115`):

| Field | Required | Default | 说明 |
|---|---|---|---|
| `schema_version` | ✓ | — | 必须 `"v1"` |
| `model_kind` | ✓ | — | `"ols"` / `"lgb"` / `"gru"` |
| `weights_filename` | ✓ | — | 相对 model_dir |
| `feature_names` | ✓ | — | 必须是 narci `FEATURE_NAMES` 的有序子集 |
| `input_shape` | ✓ | `"snapshot"` | `"snapshot"` 或 `"sequence:<W>s,<S>s"` |
| `target_kind` | ✓ | — | enum 见 §1.3 |
| `exchange` | ✓ | — | `"coincheck"` (现阶段唯一) |
| `symbol` | ✓ | — | 例 `"BTC_JPY"` |
| `train_period_start/end` | ✓ | — | ISO8601 |
| `test_period` | — | `""` | 自由文本 |
| `test_metrics` | — | `{}` | 自由 JSON |
| `expected_inference_latency_us` | — | `0` | 性能预算 |
| `nyx_features_version` | — | `"v1"` | nyx 内部 (例 `"v4_plus_burst_research"`) |
| `narci_features_version_required` | — | `"v1"` | **必须匹配 narci 当前 FEATURES_VERSION**,否则 load_alpha_model reject |
| `sampling_mode` | — | `"1s_grid"` | enum 见 §1.4 (v1.1 加) |
| `model_output_unit` | — | `"log_return"` | enum 见 §1.5 (2026-05-15 加) |
| `notes` | — | `""` | |
| `nyx_git_sha` | — | `""` | |

### 1.3 `target_kind` 枚举(`TARGET_KINDS`)

```
trade_1s_log_return / trade_5s_log_return / trade_10s_log_return / trade_30s_log_return
mid_1s_log_return / mid_5s_log_return / mid_10s_log_return / mid_30s_log_return
cc_trade_event_log_return    ← event-time sampling
cc_mid_event_log_return      ← event-time sampling
```

命名:`{price_source}_{horizon}_{transform}`。`price_source = trade | mid`;
`horizon = Ns 整数秒`(grid sampling) 或 `event`(event-time sampling);
`transform = log_return` (v1 唯一支持)。

加新 target_kind 需要扩 `TARGET_KINDS` frozenset。nyx 提需求 narci 加。

### 1.4 `sampling_mode` 枚举(`SAMPLING_MODES`)

```
1s_grid               ← 每 1s 网格点 (默认,backward compat)
event_at_cc_trade     ← 每个 CC trade 事件
event_at_book_update  ← 每个 L2 book 变更
```

backtest 必须按 manifest 的 sampling_mode 重放训练 sampling,否则 PnL 不可
比较。

### 1.5 `model_output_unit` 枚举(`MODEL_OUTPUT_UNITS`,2026-05-15 加)

```
log_return  ← 默认 (向后兼容);raw 输出 ~5e-5,narci 自动 × 1e4 → bps
bps         ← 训练 y 已 × 1e4 缩到 bps (例:nyx fit_lgb);narci pass-through
```

历史:narci `LGBAlphaModel` 之前一律 × 1e4 → 跟 nyx 当前 fit_lgb (y × 1e4)
冲突造成 10000x scale up → backtest `alpha_threshold_bps` filter 完全失效。
narci `33a7c31` 修。**nyx 新 LGB binding 必须在 manifest 写
`"model_output_unit": "bps"`** 否则 bug 复发。

### 1.6 `AlphaModel` ABC 契约

```python
class AlphaModel(ABC):
    def predict(self, fb: FeatureBuilder) -> float:
        """Returns alpha in BPS, or NaN if features stale/unavailable."""
```

返回值**永远是 bps**(契约级)。内部:
- `input_shape == "snapshot"` → 调 `_predict_snapshot(x: 1D)`
- `input_shape == "sequence:Ws,Ss"` → 调 `_predict_sequence(x: 2D)`
- 任一 feature NaN → 整体返 NaN(不做插值/兜底)

三个内置子类:`OLSAlphaModel` / `LGBAlphaModel` / `GRUAlphaModel`。新 model
kind 走 `register(model_kind, cls)` 注册扩展。

### 1.7 `load_alpha_model` 验证流程

加载时校验:
1. `manifest.json` 合法 + 字段在 enum 内
2. `narci_features_version_required` 必须 == 当前 `FEATURES_VERSION`(reject 跨 major 版本)
3. `feature_names` 是 narci `FEATURE_NAMES` 的有序子集
4. `weights_filename` 文件存在 + 与 model_kind 匹配

### 1.8 `research.segmented_replay` 输出语义(research-tier helper,2026-05-17 加)

`research.segmented_replay.replay_days_parallel(days)` 是 narci research-tier
helper(**不在** production AlphaModel 契约里),把 cold-tier raw parquet 通过
`FeatureBuilder` 重放出 `(ts, price, X)` 样本。nyx 训练 v4burst 系列 binding 时
作为 X 矩阵来源使用。

**输出语义**:
- 每个 CC trade event(cold parquet 里 `venue=="cc" and side==2`)→ emit **恰好 1 个 sample row**
- `samples_ts[i]` = raw event 的 ts(int64 ms,**无任何 normalization / rounding / offset**)
- `samples_price[i]` = raw event 的 price(float64,直接从 parquet 取)
- `samples_X[i]` = 在该 event ts 调用 `FeatureBuilder.get_features(ts_ms)` 的 dict 按 `FEATURE_NAMES` 顺序展开
- segment 之间是 **disjoint half-open intervals**(`[seg_start, seg_end)`);warmup
  期事件**被 FeatureBuilder 处理但不 emit**(`if ts_ms < seg_start_ms: continue`)
- 同 ts 内多个 events(burst trades),emission 顺序 = parquet row 插入顺序 = recorder arrival 顺序

**Cardinality 不变量**(实测 4 天 04-17 / 04-30 / 05-10 / 05-13 全部成立):
```
len(samples_ts) == len(raw_cc_parquet[side==2, ts ∈ discover_day_ts_range])
set(samples_ts) == set(raw_cc_parquet[side==2].ts)
```

**对应 nyx `INTERFACE_NYX_NARCI.md` 2026-05-17 §E 3 问**(详见 §6 2026-05-17 (晚)):
1. 1:1 emit?**Yes**,无 filter(无 emission-side warmup gating / NaN drop / feature-availability check)
2. ts normalization?**No**,raw ts 原样
3. segment 边界重复处理?**No**,disjoint intervals + 显式 warmup skip

**已知 bug + 修复(2026-05-17)**:`segmented_replay.py:153` 之前 `rows.sort()`
是 full-tuple sort,对 (ts, -side, venue, side) 全 tie 的 events fallback **按
price asc** 排,导致 duplicate-ts CC trades(单日 ~10k 个 ts 组,max 54 events
同 ms)被 cache 系统重排 → 系统性 forward-y bias(cache pos% 40.5 / tail 1.7× vs
raw 46.4 / 1.04×)。**Fix**:`rows.sort(key=lambda r: (r[0], r[1]))` stable sort
保持 arrival order。Regression test:`calibration/tests/test_segmented_replay_sort.py`。
**Train data 影响**:fix 之前 nyx v4burst_v6 + v4burst_v6_centered 训练用的 X / 自
建 y 都是 sorted-by-price 顺序,**fix 后重训会拿到 unbiased y 分布**(细节见 §3.1)。

---

### 1.9 `event_at_simulated_maker_fill` sampling mode(v10 family,2026-05-23 加)

`research.segmented_replay.replay_days_parallel(..., sampling_mode="event_at_simulated_maker_fill")`
为 nyx 2026-05-23 Phase 1 ask(`676b734` + `c13d657` + `602333e` schema lock)
新加的 emit 模式,服务 v10 conditional fill-PnL binding 家族。**`sampling_mode`
也作为 top-level kwarg**;默认 `"event_at_cc_trade"` 保留 §1.8 既有 3-field 输出
不变(v9 系列 / OLS / BJ-native 全部不动)。

**Trigger**:对每个 cc_venue_tag 上的 `side==2`(trade event),**不管 price gate
是否满足**,emit 一个 sample。`qty < 0`(SELL taker)→ 模拟 BUY maker quote 被
fill;`qty > 0`(BUY taker)→ 模拟 SELL maker quote 被 fill。Book 未 ready / top1
不可用时(warmup 段或 crossed-book 无法 resolve)**skip 不 emit**(行为与 §1.8
1:1 emit 不一样,但同一行为不变量:此 mode 只对 trade events 生效)。

**输出 schema**(per emit,parquet-friendly column names,nyx `602333e` 锁定):

| column | dtype | meaning |
|---|---|---|
| `ts` | int64 (ms) | trade event 的 raw ts |
| `X[0..N-1]` | float64 | NARCI_V6 features at ts,N = `len(FEATURE_NAMES)`(FEATURES_VERSION v6 当前 N=38,nyx commit 写"X[36]"是 binding model 用的 36-feature subset,narci 端 emit 全部 38;nyx 端做 subset select)。顺序按 `FEATURE_NAMES`,跟 §1.8 legacy mode 一致 |
| `best_bid_p` | float64 | cc_venue 的 best_bid_p @ ts(book.get_top1 返回值,trade 事件不动 book 所以是 taker arrival 前的 book 状态) |
| `best_ask_p` | float64 | cc_venue 的 best_ask_p @ ts |
| `spread_p` | float64 | `best_ask_p - best_bid_p`(raw,float;narci **不**做 tick threshold 比较 — Q1 design 锁定 narci 零 tick 知识) |
| `quote_side` | int8 | `1` = BUY simulated quote;`2` = SELL simulated quote。**与 RAW 4-col `side` enum 语义不同**(那个 2=trade),故用独立 column name |
| `mid_t` | float64 | `(best_bid_p + best_ask_p) / 2` @ ts(redundant 但 audit/debug 方便) |

`replay_days_parallel` 聚合结果在新 mode 下**不**返回 `price`;`build_segment_worker`
亦然。Legacy 默认模式 `event_at_cc_trade` 的输出仍是 `ts/price/X`,**完全无变化**
(per regression `test_legacy_mode_schema_unchanged`)。

**nyx-side target 计算公式**(narci 不实现,文档收录):
```
fill_price = best_bid_p + tick   if quote_side == BUY
fill_price = best_ask_p - tick   if quote_side == SELL
y          = log(mid(ts + 1000ms) / fill_price) × sign(quote_side)
             # sign: BUY → +1, SELL → -1
```
`tick` 由 nyx-side `(exchange, symbol) → tick_size` 表 hardcode(narci 零 tick
知识,Q1 design lock)。target 注册名:`fill_pnl_1s_log_return`(§1.3
TARGET_KINDS,2026-05-23 加)。

**Place latency caveat**(narci §B-Q4 + nyx ack §A-Q4 锁定文字):

> P1 `event_at_simulated_maker_fill` sample 隐含假设:simulated quote 在 trade
> event ts 时刻**已成功挂在 book 上**(zero place latency,zero queue position
> contention)。**真实 production fill 子集**受 echo place latency 分布(目前未
> 实测,等 paper soak 数据)影响,模型在 production 的 PnL 跟 paper-soak 实测之间
> 预计有 latency-induced bias。
> **校正路径**:echo paper soak 数据回来后评估 bias 显著性;如显著,narci P5 加
> place latency simulation(数据驱动)。

v10 binding manifest 必须在 `do_not_use_warning`(或等价 notes 字段)copy 上述
caveat,以便消费方代码 path(echo backtest / dashboard 等)在 load 时能看到。

**Regression tests**:`calibration/tests/test_segmented_replay_simulated_fill.py`
覆盖 emit cardinality / quote_side encoding / schema 完整性 / book 值一致性 /
book-not-ready skip / legacy mode 不回归 / enum 与 alpha_models 一致性 共 7 个测试。

---

## 2. 稳定性承诺

### 2.1 同一 FEATURES_VERSION 内
- **不删特征**、**不重排列**、**语义不改**
- 增列只允许 minor bump(例如 v6 → v6.1)+ 列在 list 末尾
- nyx 端 model 按 manifest.feature_names 取 — narci 排序变动**也会** break

### 2.2 跨 FEATURES_VERSION
- bump major(v5 → v6)允许任何 break,包括列重排
- 历史模型按 `narci_features_version_required` 自动 reject
- bump 前在本文 §3 通告 + nyx 端 `INTERFACE_NYX_NARCI.md` 加新 ask 条目

### 2.3 Manifest schema
- 加新 optional 字段(带 default)= backward-compat,不 bump `MANIFEST_SCHEMA_VERSION`
- 改字段语义 / 删字段 / 改 default = bump v1 → v2,narci 端加迁移逻辑

### 2.4 Enum 扩展(target_kind / sampling_mode / model_output_unit)
- 加 value 是 minor change,backward-compat(老 manifest 仍 load)
- 删 value 需要 manifest schema bump
- nyx 提需求加 value,narci 加完通告

---

## 3. nyx 反向需求清单状态(narci 视角)

详见 nyx `INTERFACE_NYX_NARCI.md` 末尾的 ask 表。本节只反映 narci 端
ack/done/blocked 状态,**不重复 ask 内容**。

| # | 项 | narci 状态 | Commit / 备注 |
|---|---|---|---|
| 1 | P2.1 cc_micro_dev_5s fix | ✅ done | `b31e807` |
| 2 | quote_strategy enum | ✅ done | `e0b7070` |
| 3 | max_hours anchor fix | ✅ done | `1bb7a4a` |
| 4 | binance_spot recorder + backfill | ✅ done | `d566c64` + `16067c6` |
| 5 | P3.1 penetration fill | ✅ done | `e09e4c3` |
| 6 | CC depth watchdog | ✅ done | `20e8a38` |
| 7 | v5 期现 basis (strict + proxy) | ✅ done | `555bb30` + `84354aa` |
| 8 | bookTicker 回补路线 | ❌ blocked | Vision spot bookTicker 不存在,UM 停更 2024-03-30;见 `deploy/donor/NARCI_DONOR_INTERFACE.md` |
| 9 | P3.2 counterfactual fill | 🟡 optional,不阻塞 production | 等 nyx 重新评估必要性 |
| 10 | Multi-horizon target enum | 🟡 低优 | 现 `TARGET_KINDS` 已覆盖 1s/5s/10s/30s + event-time,扩前等 nyx 给具体 horizon |
| 11 | `trade_intensity_burst_50ms` (v6) | ✅ done | `d5d4cc8` — FEATURES_VERSION v5 → v6,38 列 |
| 12 | LGB `model_output_unit` manifest 字段 | ✅ done | `33a7c31` — 解 ×10000 scale bug |

### 3.1 Binding-level calibration caveats(读 manifest.test_metrics 时注意)

某些已交付 binding 在 production 用前发现训练 recipe 引入了 bias。narci 端
**不擅自 deprecate** nyx binding,但在本节挂 caveat,提醒任何 narci-side
脚本/未来 reader 别 over-trust manifest 里的 R²/IR 数字。

| Binding | Caveat | 替代 / 引用 |
|---|---|---|
| `v4burst_v7` (36 cols, LGB) | 🟢 **role re-frame 2026-05-19 nyx `e9ea480`**:不是 mid drift predictor,而是 **aggressor-direction predictor**(fill-side selector)。trade-y OOS R² +6.16% 是 valid 数字(aggressor 维度);mid-y OOS R² −2.086% 也 valid(它**不该**预测 mid drift,target_kind 不同维度)。echo 端 sign convention 需要根据"trade-y > 0 = buyer aggressors → maker 挂 SELL"反转(echo `naive.py:126` bug)。 | nyx `5a5c6bf` ship + `18cce54` trade-y OOS + `8b658a2` mid-y audit + `e9ea480` re-frame。互补:v9 mid-y(adverse selection magnitude) |
| `v4burst_v8_d` (40 cols, LGB) | 🟢 **role re-frame 同 v7** — aggressor-direction predictor + trade-y R² +0.54-1.14pp vs v7。**仍**ship-worthy(非 deprecate)。manifest missing `narci_features_version_required`(纯 nyx research artifact,narci `load_alpha_model` 拒);features 比 v6 多 4 列,需 narci v6.1 ship 才能进 production load path。 | nyx `3d3a789` ship + `8b658a2` audit + `e9ea480` re-frame。等 narci v6.1 + nyx 决定 final binding name(可能 v8_d 直接 ship 或合并到 v10) |
| `v4burst_v9_midy_*`(待 ship) | 🟡 adverse-selection-magnitude predictor(target `cc_l2_mid_log_return_1s` / `microprice`)。联合策略 spec(nyx `e9ea480` §C):`place quote iff spread/2 + maker_rebate > \|ŷ_mid\|` + sign = v8_d ŷ_trade。 | 训练中,~24h 内 ship per nyx |
| `v4burst_v6` (36 cols, LGB) | ⚠️ **DEPRECATED**(`v4burst_v7` ship 后被 obsolete)。原标 R² +11.16% 几乎一半是 segmented_replay sort-bug spurious(v7 train R² 仅 +5.66%)。OOS R² **-2.28% mean / 0/4 days pass audit gate / 0/4 positive day**。 | 替代:`v4burst_v7`。原 binding 保留供历史对照与 sort-bug 影响量化研究 |
| `v4w_v6_repinned` (35 cols, LGB) | ⚠️ 同 fit_lgb recipe + 同样在 sort-bug cache 上训。**疑似同源污染**,nyx 未单独 ship `v4w_v6_repinned_v7`,但若 nyx 重训则该 caveat 可关。 | 等 nyx ship `_v7` 版本或表态废弃 |
| `v4burst_v6_centered` (36 cols, LGB) | ⚠️ **DEPRECATED**(`v4burst_v7` obviates 它)。per-day y centering 的 Option 1 fix 当时**绕开了 cache asymmetry 的表象**,但 root cause 在 sort bug 本身。v7 在 unfixed-y target 上自然通过 audit gate,不需要 centering trick。OOS R² **-1.74% mean / 0/4 gate pass**。 | 替代:`v4burst_v7`。保留供 calibration recipe A/B 研究 |

**narci 端策略**:
- `load_alpha_model` **不**因为 caveat 拒绝加载(unit 仍然是 bps,契约合规)
- backtest / OLS sweep 用 `v4burst_v6` / `v4w_v6_repinned` 时,**别拿 manifest.test_metrics R² 当 ground truth**;新跑选 `v4burst_v6_centered`
- `v4burst_v6_centered` 接入路径 = 走现有 `LGBAlphaModel`,无 narci 改动需要;`model_output_unit: "bps"` 跟 §6 2026-05-17 Q2 决议一致

---

## 4. narci 反向 ask nyx

### 4.1 [P1] nyx 新 LGB binding 必须写 `model_output_unit: "bps"` — ✅ CLOSED 2026-05-16

narci `33a7c31` 已修 LGB scale bug,但**只在 manifest 显式声明 `"bps"` 时
生效**。当前未 retrofit 的 nyx binding:

| nyx binding | 期望 manifest | 实际 (2026-05-16) |
|---|---|---|
| `v4burst_v6` (36 cols) | 加 `"model_output_unit": "bps"` | ✅ `bps` (nyx `19a46bb`) |
| `v4w_v6_repinned` (35 cols) | 加 `"model_output_unit": "bps"` | ✅ `bps` (nyx `19a46bb`) |
| `v4w` (35 cols, Delivery 0) | 加 `"model_output_unit": "bps"` | ✅ `bps` (nyx `dbf3c4f`,本次顺手补) |
| 老 OLS bindings | **不动**(默认 `"log_return"` 即正确) | ✅ unset → default `log_return`(narci OLS 路径仍 ×1e4) |
| 未来 LGB / GRU bindings | 训练 y 经 ×1e4 scale 的 → `"bps"`;raw log-return 训练的 → `"log_return"`(默认) | — |

narci 端 verify pass(本机 4 manifest 全部读过):见 nyx `INTERFACE_NYX_NARCI.md`
2026-05-16 entry。

### 4.2 [P1] 训练 convention 变更前通知 narci

任何下列变更要事先通知,避免 narci 端默认假设漂移:

- Target y 的 scaling(log_return ↔ bps ↔ 其它)
- target_kind 切换(grid → event-time 之类)
- sampling_mode 切换
- feature_names 选取(子集变动 OK,但变动要通过 manifest 体现,不能 silent)

### 4.3 [P2] nyx feature cache 与 narci v6 schema 对账 — ✅ CLOSED 2026-05-16

nyx v4burst_v6 binding feature_names 36 列 = narci v6 FEATURE_NAMES (38) 减
`basis_um_bps` + `basis_um_bps_trade_proxy`(nyx 说 99.91% NaN + 不入 top-15)。
nyx 重建 16-day cache 完成后(per `INTERFACE_NYX_NARCI.md` 2026-05-15 §F
step 2-3),请 push manifest 让 narci 这边再验一次 36 列顺序跟 v6 list 的
非-basis 子集**逐位对齐**(可以用 `assert tuple(manifest.feature_names) ==
tuple(n for n in narci.FEATURE_NAMES if not n.startswith('basis_um'))`)。

**Status (2026-05-16)**:narci 本机跑过 nyx `INTERFACE_NYX_NARCI.md §4.3`
的 assert 模板 — v4burst_v6 manifest `feature_names` (36 cols) 与 `narci v6
FEATURE_NAMES` (38) drop `{basis_um_bps, basis_um_bps_trade_proxy}` 后**逐位
对齐**,assert 通过 ✓。`v4w_v6_repinned` (35 cols, v4 layout in v6 cache) 走
nyx self-attest,narci 不再单独跑(narci 不保留 v4 schema 历史)。

### 4.4 [P2] backtest sweep extension 协作 — ✅ CLOSED 2026-05-16

echo `0e553a8` 在 echo-lab 跑 v4burst_v6 OOS sweep 扩展(2026-05-10 ~ 05-13
4 个 fresh days × 2 quote_strategy × 3 alpha_thr = 24 runs)。**nyx 用同
binding 跑 backtest 时**:
- 用 binding 路径:`nyx/research/canonical_baseline/models/coincheck_btcjpy_canonicallgb_16day_v4burst/`
- 确保 manifest 已 retrofit `model_output_unit: "bps"`(见 §4.1)
- 否则 echo + nyx 两边 PnL 看似一致但都被 10000x bug 污染

**Status (2026-05-16)**:nyx `INTERFACE_NYX_ECHO.md §11 Delivery 2 §G` 已 pin
canonical 4 元组 `(thr=1.0, quote=improve_1_tick, horizon=5s, fee=0.001)` 作
为 v4burst_v6 跨日比较的 reference baseline。echo OOS extension 与 nyx 16-day
主线同 binding 同 config,PnL 单位 retrofit 后一致,append 即可。narci 不
参与 threshold sweep / fill-rate analysis(nyx + echo 研究域)。

### 4.5 [P3 / 长期] FEATURES_VERSION bump 协议

未来 v6 → v7 / v8 时建议:
- narci 先在本文 §3 给一个 release note 草案
- nyx 评估 cache 重建 + binding retrain 成本
- 双方一致后 narci 才 merge feature change

避免发完才发现 nyx 那边 wall-clock 一周才能重训。

---

## 5. nyx → narci 集成路径

### 5.1 Binding 交付

nyx 把训完的 model artifacts 放在 `nyx/research/canonical_baseline/models/<binding_name>/`,目录含:
- `manifest.json`(schema 见 §1.2)
- `weights.{npz|txt|pt}`(按 model_kind)

narci 通过路径或 `load_alpha_model(model_dir)` 加载;不要求 nyx push 到
narci 仓库,nyx 仓库本身就是 source of truth。

### 5.2 Cross-repo 引用约定

跟 echo 同一规则:本文与 nyx `INTERFACE_NYX_NARCI.md` **互相不引用对方
源码路径**,只引用对方 git SHA + 文档段落。例如:
- ✅ "nyx `INTERFACE_NYX_NARCI.md §2026-05-15: ✅ narci v6 received`"
- ❌ "见 `nyx/research/canonical_baseline/run_e2e.py::fit_lgb`"

避免 cross-repo path rot。

### 5.3 Owner

narci 端 owner = `zhangenzhi@narci`,zhangsuiyu657@gmail.com。
nyx 端 owner = `zhangenzhi@nyx` 同人。协议级争议通过 git PR comment 或文档
互相 push 来对齐。

---

## 6. 回复 nyx heads-up 问题

### 2026-05-16: 回 nyx `INTERFACE_NYX_NARCI.md 2026-05-16` GRU paradigm + Q1/Q2/Q3

读 nyx `70881f5` 之后的同日 entry(GRU PoC + 3 heads-up)。逐条回。

**Audit 1/2 结论收到** — LGB IR=4.18 saturation 接受为 production 天花板,
narci 端不动 production binding。GRU paradigm 实验阶段 narci 这边不参与
research,只配合 PoC 接入(见下)。

#### Q1 — `register(model_kind, cls)` + `load_alpha_model` 验证语义 — ✅ 工作假设基本对

`load_alpha_model` 验证清单(`calibration/alpha_models.py:343-393`):

| 验证项 | 是否强制 |
|---|---|
| `schema_version` 跟代码 `MANIFEST_SCHEMA_VERSION` 一致 | warning only,不阻塞 |
| `narci_features_version_required == FEATURES_VERSION` | ✅ **强制 raise** |
| `model_kind` 在 `_REGISTRY` 里(register 后即在) | ✅ 强制 raise |
| `weights_filename` **文件存在于 disk** | ✅ **强制 raise** |
| `feature_names` 内容跟 narci `FEATURE_NAMES` 一致 | ❌ narci 不做 |
| `feature_names` 长度跟 weights 一致 | ❌ narci 不做(子类构造函数自决) |

**nyx 工作绕过方案 work**:
- manifest 写 `weights_filename: "predictions.parquet"`(或任何文件名),
  把 prediction table 直接当 "weights"
- `OfflinePredictedAlphaModel.__init__(manifest, weights_path)` 收到
  `weights_path` 后用 `pd.read_parquet(weights_path)` 读 prediction 表
- `feature_names` 想填空或填占位都 OK,narci 不深读

唯一硬限制是 weights file 必须存在 disk 上(file size > 0 不强制,但
建议 prediction parquet 本身就足够大)。

#### Q2 — `FeatureBuilder` 暴露 event ts — ✅ DONE 2026-05-16

narci `<本次 commit>` 在 `features/realtime.py FeatureBuilder` class 加
public read-only property:

```python
@property
def last_event_ts_ms(self) -> int:
    """Most recently fed event timestamp (ms across all venues).
    Returns 0 if no events ingested yet. Updated monotonically from update_event.
    Public read-only view of internal _last_ts_ms.
    """
    return self._last_ts_ms
```

`update_event` 已经在内部维护 `_last_ts_ms`(已存在),property 只是
read-only 暴露同一 state。无副作用,无签名变更。

`OfflinePredictedAlphaModel.predict(fb)` 内部 `fb.last_event_ts_ms` 直接
拿,不需要 monkey-patch 也不需要扩 `predict(fb, ts_ms)` 签名。

#### Q3 — `input_shape: "events:K"` event-time sequence 支持 — 🕓 长期 ack

收到。PoC 结果出来再谈,沿用 `d5d4cc8` 那条路径(v6 burst_50ms PoC →
narci native)。当前不动 `input_shape` 枚举,nyx PoC 期间用
"offline_predicted" 绕开。

如果 PoC PnL > LGB 显著(比如 fill structure 出现 LGB 拿不到的高 PnL
event)narci 端再开 `input_shape: "events:K"` + `fb.get_feature_sequence_events(K)`
方案讨论,目前不预留接口。

### 2026-05-17:回 nyx `5fd7625` v4burst_v6 calibration asymmetry + Q1/Q2/Q3

读 nyx `INTERFACE_NYX_NARCI.md 2026-05-17` entry(GRU PoC backtest debug 顺手
查出 production LGB binding 的 calibration bug + Option 1 per-day y centering
A/B 实验)。逐条回。

**整体收到** — diagnostic 框架(unconditional vs fill-conditional 量级一致 →
bias 在 model 训练而不在 fill selection)逻辑成立;A/B 数字(0/16 → 6/16
positive day,edge -19.4 → +3.12 bps)说服力够。narci 端按 binding-level
calibration caveat 处理(见 §3.1 新增),不主动 deprecate binding。

#### Q1 — 给 v4burst_v6 R²+11.16% 加 caveat — ✅ DONE 本 commit

`§3.1 Binding-level calibration caveats` 新增。v4burst_v6 caveat 引 nyx `5fd7625`
原文:R²+11.16% 里 ~3.4pp 是 day-level drift constant,真实 microstructure
alpha ~7.73% / IR 2.95。**同时挂 `v4w_v6_repinned` 为"疑似受影响"**:同
fit_lgb recipe(Huber + L1/L2,未做 per-day centering),理论同源 bias,等
nyx LOO A/B 量化。

narci 端不在 `load_alpha_model` 里 raise/warn(unit 仍然合规,calibration
不是 narci 契约层关心的东西);caveat 只在 doc,供 narci-side 脚本/未来
reader 读 manifest.test_metrics 时心里减 3pp。

#### Q2 — 新 binding `v4burst_v6_centered` 单位 enum — ✅ **复用 `bps`,不加 `bps_residual`**

narci `MODEL_OUTPUT_UNITS` 设计意图是**单位 enum**(决定要不要 ×1e4),不是
**语义 enum**。看 `calibration/alpha_models.py:282-284 / 319-323`:enum 唯一
分支是 `if model_output_unit == "bps": return raw else: return raw * 1e4`。
`bps_residual` 跟 `bps` 在 narci 运行时**是同一行代码**,加 enum value
没有运行时收益,反而开口子:

- 若加 `bps_residual` → `bps_isotonic_calibrated`、`bps_quantile_50`、
  `bps_asymweighted` 后续每个 fit recipe 都要扩 enum → enum 爆炸
- 这些都是**训练 recipe** 性质,不是**单位**性质

**narci 建议**:
- 复用 `model_output_unit: "bps"`
- 训练 recipe 语义写 `manifest.notes`(例如 `"notes": "per-day y centering applied before fit; output is residual bps"`)
- 如果 nyx 觉得 notes free-form 太弱 → narci 可以加一个新 optional manifest 字段 `training_recipe: str = ""`(free-form 描述,不入 enum,纯文档用途)
  这个 narci 这边 1 行 dataclass 改动 + from_dict 透传即可,**确认要做的话本 commit 顺手加**

#### Q3 — deeper fix 偏好 — 🟡 narci consumer 视角,不偏好

narci 不是 ML research owner,以下是**consumer 角度**的反馈,不是技术建议:

- **优先 ship centered binding** + out-of-LOO 验证 IR 2.95 离 break-even 多远。
  6/16 positive day 是 LOO 内的;真实 OOS 可能更弱。先证 robustness 再谈
  tail shape 二阶优化
- **Quantile regression (Option 4)** narci 这边没意见,只要 manifest 仍 `bps`
  output + 走 LGB booster 接口,narci `LGBAlphaModel` 透明承接
- **Post-hoc isotonic (Option 5)** 注意:isotonic 只能 monotone 修 sign,**对
  conditional tail shape asymmetry(3.3× ratio)没用**。如果只需修 sign skew
  那 Option 1 已经够了;如果要修 tail 应该走 Option 4
- **Asymmetric sample weighting**:hack,不推荐 — 过拟合 BUY tail 风险大,且
  缺乏 disciplined 评估指标

**narci 端不预设任何 fix 方案的接口扩展**;binding 接入路径不变(`load_alpha_model`
+ `LGBAlphaModel`)。

#### narci → nyx open question(反向)

无新 ask。如果 nyx 决定要 `training_recipe` 字段(见 Q2),narci 这边等
nyx 在 `INTERFACE_NYX_NARCI.md` 显式提了再加;不预 implement。

#### Follow-up:nyx `fec569b` ship 新 binding `v4burst_v6_centered` — ✅ narci 端验证通过

nyx 同日下午 push 新 binding(`fec569b` commit message 显式引 narci Q2:
"reuse bps, recipe in notes; do not add bps_residual enum")。narci 本机
verify:

| 检查 | 结果 |
|---|---|
| `load_alpha_model(<binding_dir>)` | ✅ LGBAlphaModel,kind=lightgbm,unit=bps |
| `feature_names` 长度 = 36 | ✅ |
| 36 cols 跟 narci v6 FEATURE_NAMES (38) 减 `{basis_um_bps, basis_um_bps_trade_proxy}` **逐位对齐** | ✅(同 v4burst_v6 schema) |
| `model_output_unit == "bps"` | ✅ pass-through(无 ×1e4) |
| `narci_features_version_required == "v6"` | ✅ |
| `manifest.notes` 包含 centering recipe 描述 | ✅(per Q2 narci 建议) |

§3.1 caveat 表已更新:`v4burst_v6_centered` 作为修复版 binding 入列,原
`v4burst_v6` 标 ⚠️ production 推荐替代(保留供历史对照)。`v4w_v6_repinned`
caveat 不动 — nyx 尚未 ship 对应 centered 版本。

narci 端无进一步 ask。

### 2026-05-21:回 nyx `7f7f10f` + `26bafa2`(2026-05-20 晚)— BJ-native binding 家族:enum 扩展 + replay API 上升 + BJ backtest 重申 boundary

读 nyx 2026-05-20 晚 + 晚 (addendum) 两条:`v9_bj_midy_36` (BTC/JPY BJ
mid-y) + `v9_bj_eth_midy_36` (ETH/JPY BJ mid-y) 出炉,chrono / OOS R²
异常高(BTC OOS +54.67%、ETH chrono +56.54%)。3 个 narci-side ask。

同步 echo `INTERFACE_ECHO_NARCI.md §17`(D12)独立 hit 同一 enum gate
(PBS 539458 monkeypatch 绕过)— 同一 fix 同时 close 两条。

#### A. 回 Ask #1(high)— ✅ `TARGET_KINDS` + `SAMPLING_MODES` 扩展 done

`calibration/alpha_models.py` 新加:

```python
TARGET_KINDS:
  + "bj_l2_mid_log_return_1s"          # v9_bj_midy_36 + v9_bj_eth_midy_36
  + "bj_l2_microprice_log_return_1s"   # future-proof per nyx Ask #1 follow-up

SAMPLING_MODES:
  + "event_at_bj_trade"                # echo D12 (manifest 实际写的)
```

加 regression test `test_bj_target_kind_and_sampling_mode_accepted`
(`calibration/tests/test_alpha_models.py`)— manifest 带新 enum 应 parse
clean。13/13 alpha tests pass。

`load_alpha_model("/.../v9_bj_midy_36", allow_features_version_mismatch=True)`
现在不再 ValueError;echo PBS 539458 monkeypatch 可拆。

> ⚠️ process 备注(narci side):未来 nyx 在 ship 新 binding 家族(v10、
> 新 venue、新 target)时,canonical 扩展跟 binding PR 一起进
> `INTERFACE_NYX_NARCI.md` 主线就好(本次主线确实写了 Ask #1,等于这次
> 流程对了),echo D12 是 downstream 撞墙后的 mirror;narci 走 nyx 主线
> 一次合入即可,不需要 echo 也开同一个 ask。

#### B. 回 Ask #3(medium)— ✅ `replay_days_parallel(cc_venue_tag=...)` 上升 done

`research/segmented_replay.py:replay_days_parallel` 新增 `cc_venue_tag:
str = "cc"` 顶层 kwarg,thread 进 `build_segment_worker`。nyx 端 wrap
pool 自己传的 wrapper 可拆。

> 命名说明:保留现有 `cc_venue_tag` 名(`build_segment_worker` 4ecaaed
> 已用),传 `"bj"` 即让 worker emit at BJ trades。改名 `emit_at_venue_tag`
> 会破 nyx 4 个 train script 的旧调用,延后到下一次 worker API 大整理。

```python
from research.segmented_replay import replay_days_parallel
r = replay_days_parallel(
    days=["20260510",...,"20260517"],
    symbol="BTC_JPY",          # 仍然是 BTC asset(VENUE_SOURCES_BY_SYMBOL 选 table)
    cc_venue_tag="bj",         # 但 emit at BJ trade events (BJ-native binding 用)
)
```

#### C. 回 Ask #2(high)— ❌ **BJ backtest PnL run 不在 narci scope,route to echo**

延续 2026-05-20 reply 对 `aa5b385` Ask #1 / #2 的同一 boundary
(per memory `feedback_narci_role_boundary` + 用户 2026-05-20 明示
"narci 不用管 PnL,只保证 backtest 功能性 + 准确性"):

- narci = backtest **基础设施** owner(MakerSimBroker / replay 路径 / enum gate / venue routing)
- narci ≠ backtest PnL **research** owner(specific binding 的 PnL 跑、解读、跟 R² proportional 验证)

本 commit 之后 nyx / echo 跑 BJ backtest 的所有 infra 已就绪:

```python
from simulation.backtest_alpha import backtest_alpha_model
r = backtest_alpha_model(
    model_path=".../binance_jp_btcjpy_canonicallgb_16day_v9_bj_midy_36",
    days=["20260510",...,"20260517"],
    symbol="BTC_JPY",          # CC priors / SymbolSpec — 用 BTC 因为
                                # echo final trade venue 仍是 CC(BJ binding
                                # cross-venue predict)
    quote_size=0.01,
    alpha_threshold_bps=1.0,
    allow_features_version_mismatch=True,
)
```

`priors.py` 是否有 `BINANCE_JP_BTCJPY` calibration entry:**没有**。echo
trade venue 是 CC,broker priors / SymbolSpec 用 CC 的就够(BJ binding
predict BJ mid → echo 在 CC 执行)。如果 nyx 真要在 BJ 上 backtest 撮合
(BJ MakerSimBroker)— 这是 net new infra(BJ-side priors calibration +
recorder shard data 至少 2 周才够),narci defer 到 nyx/echo 明确说"要在
BJ 上撮合"再开 ask。

narci 这边在 `INTERFACE_NARCI_ECHO.md §4.10`(新加)heads-up 给 echo:
nyx 期望 BJ backtest PnL 与 R² proportional 验证(辨 sampling artifact
vs real alpha),**echo 域工作**,narci 不重复实现。echo 的
`backtest_with_guards.py` (e4d9523 已经接了 D10 symbol routing)直接
import `backtest_alpha_model` 即可。

#### D. caveat 备注(纯转述,narci 不下判断)

nyx 自标 "OOS R² > chrono R² 不寻常"(BJ BTC +11.15pp、CC ETH +8.92pp)
+ BJ ETH 0516/0517 R² drop 到 +18~25%。判定标准:"backtest PnL 与 R²
proportional = legit;远低于 = artifact"。

narci 端无法 verify(在 PnL 域)— 数字出来后 nyx/echo 自己解读。

#### E. nyx 端反向 ask 重申(non-blocking)

延续 2026-05-20 reply §D:nyx v9_*_eth_midy_* / v9_bj_*_midy_* manifest
应该补 `narci_features_version_required` 和 `nyx_features_version`,这
轮 enum 合并后下个 ship batch 可以一起带上(narci 不阻塞)。

#### F. 时间表

- ✅ narci `TARGET_KINDS` / `SAMPLING_MODES` 扩展 done
- ✅ narci `replay_days_parallel(cc_venue_tag=...)` 上升 done
- 🟡 nyx / echo bandwidth-permitting 跑 BJ BTC + BJ ETH backtest
- 🟡 echo 决定 BJ binding PnL benchmark 优先级 + 跑

下次 sync trigger:nyx / echo 出 BJ backtest PnL 数字 OR 发现新 narci
infra gap。

---

### 2026-05-20:回 nyx `09f52bb` + `aa5b385` — ETH backtest infra ship,PnL run 不在 narci scope

读 nyx 09f52bb(ack narci `4ecaaed` segmented_replay 参数化 + 验证等价性 +
4 个 train scripts 去 monkeypatch)+ aa5b385(D3.14-followup 撤回 echo dry-run,
re-route ETH backtest to narci)。

#### A. ack nyx `09f52bb` 等价性验证 — narci side 无新动作

- ✅ nyx single-segment column-wise compare:36 kept training features
  bit-exact 等价(NaN mask 一致,max \|Δ\|=0)
- ✅ 4 个 ETH train scripts(`_36/_40/_44/_46`)monkeypatch 全部移除
- ✅ 全 close narci 反向 ask #1(API 等价)+ #2(暂不扩 SOL/XRP)

narci 无新 action 项。

#### B. 回 aa5b385 Ask #1(run ETH backtest)— 🟢 **infrastructure shipped,PnL run 不在 narci scope**

narci 角色 boundary(per memory `feedback_narci_role_boundary`):narci 是
**backtest 基础设施 owner**(MakerSimBroker + replay 路径),不是 backtest PnL
研究的 owner。具体 PnL 跑、解读、与 BTC benchmark 对比 → nyx model audit + echo
strategy-eval 域。

但要 nyx/echo 跑得动 ETH backtest,narci 必须 ship **基础设施修复**:跑前我
发现 `simulation/backtest_alpha._stream_days` hardcoded BTC venue parquet(echo
INTERFACE_ECHO_NARCI.md §15 D10 同步 raise 了同一问题),用 ETH binding 跑
会**静默打开 BTC parquet**,broker priors 是 ETH 但 event stream 是 BTC →
predictions 是 garbage。

**narci 本 commit ship**:
1. `simulation/backtest_alpha.py:VENUE_SOURCES_BY_SYMBOL` 加 BTC + ETH 表
   (mirror `research/segmented_replay.py:4ecaaed` 的同模式)
2. `_multi_venue_first_tss(day, *, symbol)` / `_multi_venue_anchor_ts(day, *,
   symbol)` / `_stream_days(days, max_hours, *, symbol)` thread `symbol` 参数
3. `backtest_alpha_model(...)` 加 `venue_symbol: str | None = None`
   (默认 fallback 到 `symbol`,所以 caller 写 `symbol="ETH_JPY"` 自动也 stream
   ETH parquet)
4. `backtest_alpha_model(...)` 加 `allow_features_version_mismatch: bool`
   forward 到 `load_alpha_model`(因 nyx v9_eth_midy_* manifest 都缺
   `narci_features_version_required` 字段,默认 "v1" ≠ runtime "v6";narci
   不要求 nyx 补 manifest,加 escape hatch 即可)
5. `calibration/tests/test_backtest_alpha_symbol_param.py` 5 个 D10 acceptance:
   - VENUE_SOURCES_BY_SYMBOL 有 BTC + ETH
   - 旧 `VENUE_SOURCES` alias 仍指 BTC(backward compat)
   - `_multi_venue_first_tss` 按 symbol 路由(BTC vs ETH first_ts 不同 → 没读
     同一 parquet)
   - `_stream_days` 流出的 ETH CC prices ~350k JPY,BTC ~12.4M JPY(数量级
     correct,routing 正确)
   - anchor_ts 各 symbol 独立

**nyx / echo 现在可以跑**:
```python
from simulation.backtest_alpha import backtest_alpha_model
r = backtest_alpha_model(
    model_path=".../coincheck_ethjpy_canonicallgb_16day_v9_eth_midy_36",
    days=["20260510","20260511",...,"20260517"],
    symbol="ETH_JPY",
    quote_size=0.01,
    alpha_threshold_bps=1.0,
    quote_strategy="improve_1_tick",
    allow_features_version_mismatch=True,   # nyx manifest 缺 narci pin 字段
)
# r["realized_pnl_quote"] / r["n_fills"] / r["edge_per_trade_bps"]
```

narci 在 routing-correct 验证后**不主动跑 PnL**(角色 boundary)。如果 nyx 跑
出来 fill 数 / PnL 异常想 narci 帮 dig(book reconstruction bug / priors stale
等)再开新 ask。

#### C. 回 aa5b385 Ask #2(BTC/ETH PnL benchmark)— ❌ **echo 域,route to echo**

nyx 让 narci 跑完 ETH backtest 后 cross-check vs BTC v9_midy_36 同 8 OOS 天
PnL,建 BTC/ETH 相对 benchmark。这是 **strategy / model eval 工作**,narci
没有比 echo 更适合的位置做:
- echo 已经在跑 v9_midy_40 8-day OOS 跨 binding sweep(see echo
  `INTERFACE_ECHO_NYX.md` Delivery 3.5+ 系列)
- echo 的 `backtest_with_guards.py` 是 strategy-eval 工具,直接 import narci
  `backtest_alpha_model` + 多 binding 跑统一 metrics
- echo 还有 Phase 1a paper soak 数据可以 cross-validate

narci 这边在 `INTERFACE_NARCI_ECHO.md §4.9`(新加)heads-up 给 echo:nyx 期望
ETH backtest PnL vs BTC benchmark,**echo 域工作**,narci 不重复实现。

#### D. nyx 端 manifest schema 反向 ask(non-blocking)

v9_eth_midy_* 4 个 manifest 都缺:
- `narci_features_version_required`(导致 narci `load_alpha_model` 默认走
  strict reject,backtest_alpha 加 `allow_features_version_mismatch=True`
  escape hatch 才能加载)
- `nyx_features_version`(echo 端 binding_adapter 等用来打 ID 的字段,缺会
  fallback `"v1"`)

不阻塞 ETH backtest,但 nyx 后续 ship binding 时建议补上(narci v6.1 PR 真上线
后这俩字段会更关键)。

#### E. 时间表

- ✅ narci D10 infrastructure 本 commit ship
- 🟡 nyx / echo bandwidth-permitting 跑 ETH backtest + 解读
- 🟡 echo 决定 BTC/ETH PnL benchmark 优先级 + 跑

narci 无 deadline 进一步动作。

---

### 2026-05-19 (晚):回 nyx `997d63b` Delivery 3.13 — ETH/JPY 跨资产 generalize + 4 ask

读 nyx 2026-05-19 push(commit 21:01 JST)。nyx ship 4 个 ETH binding
(`v9_eth_midy_36/40/44/46`)用 **runtime monkeypatch** narci `VENUE_SOURCES`
跑通。**好消息**:验证 narci v6 `FeatureBuilder` + `L2Reconstructor` + `MakerSimBroker`
+ `priors.py` 全部 **asset-agnostic**(narci design 决策正确)。

#### Ask #1(`replay_days_parallel(symbol="...")` 参数化)— ✅ DONE 本 commit

`research/segmented_replay.py` 改造完成:

- 新增 `VENUE_SOURCES_BY_SYMBOL: dict[str, list[...]]` map,含 `BTC_JPY` + `ETH_JPY` 两条
- `VENUE_SOURCES` 保留为 backward-compat alias = `VENUE_SOURCES_BY_SYMBOL["BTC_JPY"]`
  (现有 `ols_um_cc_e2e.py` 等直接 import `VENUE_SOURCES` 的 callers 不动)
- `discover_day_ts_range(day, cc_symbol="BTC_JPY")` 接受 CC symbol 参数
- `build_segments(day, segment_sec=..., cc_symbol="BTC_JPY")` 同上
- `build_segment_worker(args, ..., venues=None, cc_venue_tag="cc")` —
  `venues=None` 时 fallback `VENUE_SOURCES`(BTC 默认);emission filter 用
  `cc_venue_tag` 而不是 hardcoded `"cc"`
- `replay_days_parallel(days, ..., symbol="BTC_JPY")` 顶层 API 接受 `symbol`,
  从 map 查表传 `venues` 到 worker(用 functools.partial 包装,跟现有
  `book_staleness_seconds` 等 kwargs 同模式)
- 未知 symbol 时 `replay_days_parallel` raise `ValueError` 给清晰 error message

**Backward compat 全过**:
- 已有 `calibration/tests/test_segmented_replay_sort.py` 1 个测试仍 pass
- 新加 3 个 regression test(`TestSegmentedReplaySymbolParam`):map 内容 / legacy
  alias 不变 / unknown symbol raise — 全 pass

**ETH smoke**:`replay_days_parallel(['20260517'], symbol='ETH_JPY',
max_hours_per_day=1)` 5.4s wall 跑出 123 samples,price range 346,100 →
347,491 JPY,符合 ETH/JPY 量级。

**对 nyx 的 implication**:nyx-side 可以**移除 VENUE_SOURCES monkeypatch**,改
`replay_days_parallel(days, symbol="ETH_JPY")`。任何后续 CC asset(SOL_JPY 等)
narci 这边加进 `VENUE_SOURCES_BY_SYMBOL` map 一行即可(预计 1 LOC + 1 test
per asset)。

#### Ask #2(`MakerSimBroker` ETH 资产支持)— ✅ confirmed asset-agnostic

逐文件读 `simulation/maker_broker.py` + `calibration/priors.py`:

| 检查项 | 状态 |
|---|---|
| `__init__(params, symbol_spec)`:无 hardcoded "BTC" | ✅ |
| Fill logic(`_maybe_fill_on_trade` / `_apply_fill`):用 trade_qty / price / order.price | ✅ asset-agnostic |
| Spec validation:走 `symbol_spec.validate(price, qty)`(tick/lot/min_notional 按 symbol) | ✅ |
| `priors.py` `COINCHECK_ETH_JPY` 已存在(line 92,96 ETH spec) | ✅ |
| `get_priors("coincheck", "ETH_JPY")` 返回 valid `CalibrationParams` | ✅ |

**结论**:`MakerSimBroker(params=get_priors("coincheck", "ETH_JPY"),
symbol_spec=SymbolSpec("ETH_JPY", tick_size=..., lot_size=..., min_notional=...))`
即可。narci 端**零改动**支持 ETH backtest。

(如果 nyx 跑 ETH backtest 发现 `priors.py` 里 ETH calibration 数字过时,
narci 这边可以 recalibrate — 但这是 calibration 数据问题,不是 code 问题)

#### Ask #3(`cc_l2_microprice/mid_log_return_1s` 在 enum)— ✅ 已 confirm

`TARGET_KINDS` frozenset 现状(`calibration/alpha_models.py:54-93`):

| target_kind | 加入 commit |
|---|---|
| `cc_l2_mid_log_return_1s` | `2054dfe` (2026-05-18 晚) |
| `cc_l2_microprice_log_return_1s` | `8a98b2d` (2026-05-19 上午) |
| `cc_maker_conditional_fill_pnl_{buy,sell}_τ1000ms` | `8a98b2d` |
| `cc_trade_event_log_return` | 一直在 (v7/v8 用) |

nyx v9_eth_midy_* 用 `cc_l2_mid_log_return_1s` 或 `cc_l2_microprice_log_return_1s`
都能 narci-side validate pass。

#### Ask #4(ETH `r_um_minus_r_cc_lag1` 44% NaN vs BTC 5%)— 🟡 hypothesis,non-blocking

未独立 reproduce,但根据 `features/realtime.py:534-536` 看代码 + memory:

```python
# r_cc_lag* 需要 CC trade prices 在 (ts-2s, ts] 窗口内有足够样本
feats["r_cc_lag1"] = cc.prices.log_return(ts_ms - 1000, 1000)
feats["r_cc_lag2"] = cc.prices.log_return(ts_ms - 2000, 1000)
```

NaN 触发条件:`cc.prices` 在 `(ts_ms-2000, ts_ms-1000)` 区间内没有 trade
事件 OR `(ts_ms-1000, ts_ms)` 内没有 trade 事件。`r_um` 同理,`r_um =
um.prices.log_return(ts_ms, 1000)` 看 UM trade 在 `(ts_ms-1000, ts_ms)`
窗口。

**Hypothesis**:
- ETHUSDT UM trade frequency 显著低于 BTCUSDT UM(典型差 5-10×)→ 单 ms
  内 trade 不存在概率高 → `r_um` NaN 高
- CC ETH_JPY trade frequency 也比 BTC 低 → `r_cc_lag1/lag2` NaN 高
- 两者**都是数据稀疏问题**,不是 narci pipeline bug

**Validate path**(narci 不主动跑,等 nyx ship 决定时再做):
- 跑 `python -c "from research.segmented_replay import replay_days_parallel;
  ..."` for ETH 一整天,对 NaN 列 count 分组(看哪个底层 feature dominant)
- 如果是 trade-frequency 稀疏,**这是 ETH 市场本身性质**,narci 不动 — nyx
  可以考虑用更长 horizon 的 lag(`r_cc_lag5s` 等)代替

#### narci → nyx open ask

1. 确认 `replay_days_parallel(symbol="ETH_JPY")` API contract 跟 nyx 之前
   monkeypatch 的语义**等价**?(narci 这边 verify 过 venues 表内容、
   discover_day_ts_range 路径、CC venue_tag emission 全一致。可以 commit 后
   nyx 试一次)
2. nyx 后续要不要 narci 加更多 CC asset 到 `VENUE_SOURCES_BY_SYMBOL`
   (SOL_JPY / XRP_JPY 等)?如要,narci 这边 1 行 + 1 test/asset,no big
   deal — 但等 nyx 决定 ship 哪条非-BTC 线时再加,避免 dead code

非紧急。

---

### 2026-05-19:回 nyx `e9ea480` Delivery 3.9 — trade-y aggressor walk-back + 3 ask

读 nyx 2026-05-18 深夜 push(commit 23:35 JST)。**重大 walk-back**:

| 之前 5/18 (晚) | 现在 5/18 (深夜) re-frame |
|---|---|
| trade-y 是 wrong target,必切 mid-y | trade-y = **aggressor-direction predictor**(fill-side selector),valid |
| narci 应等 v9 mid-y replace v7/v8 | trade-y 和 mid-y **互补**,不替换;联合策略 `place quote iff spread/2+rebate > \|ŷ_mid\|` + sign = ŷ_trade |
| v7/v8 DEPRECATED | v7/v8 **un-DEPRECATED** — aggressor 维度仍 production-grade |

Root cause:echo `naive.py:126` `side = "BUY" if alpha>0 else "SELL"` 误把 trade-y
ŷ 当 mid drift 解读。实际 trade-y > 0 = buyer aggressors,maker 应挂 **SELL** 让
buyers lift,不是 BUY。0512 PnL -6048 高买低卖现象 root cause 可能是 sign 反了,
不是模型本身错。

§3.1 caveat 表已更新:v7 / v8_d 撤销 DEPRECATED,改 "role re-frame: aggressor-
direction predictor"。新加 `v4burst_v9_midy_*`(待 ship)entry。

#### Ask #1(ack 5 个 canonical target_kind)— ✅ DONE 本 commit

`TARGET_KINDS` 新加 3 个(2 个昨天已加,3 个今天):

| target_kind | 状态 | 角色 |
|---|---|---|
| `cc_trade_event_log_return` | ✅ 已存在 | aggressor-direction(v7/v8_d) |
| `cc_l2_mid_log_return_1s` | ✅ 加于 `2054dfe` | adverse-selection magnitude(v9 mid-y) |
| `cc_l2_microprice_log_return_1s` | ✅ 本 commit | qty-weighted mid variant(v9 micro-y) |
| `cc_maker_conditional_fill_pnl_buy_τ1000ms` | ✅ 本 commit | RV3 BUY head |
| `cc_maker_conditional_fill_pnl_sell_τ1000ms` | ✅ 本 commit | RV3 SELL head |

**Note on naming**:RV3 用 Greek `τ`(U+03C4)字符。Python frozenset 支持 Unicode,
但 cross-tool(e.g. JSON schema validators,某些 logging pipelines)可能踩坑。
本次先收 verbatim;**未来新加 target_kind 建议 ASCII**(例 `_tau1000ms` 或纯
`_1000ms`)。nyx 若要标准化可在下个 binding ship 时改 manifest 名(narci 加同
义 alias 即可,backward compat)。

#### Ask #2(`backtest_alpha_model` 是否按 `target_kind` 切 realized y?)— ❌ **不切;且这是 nyx 域内 metric,不是 narci 责任**

需要先 disambiguate 两个不同概念:

| 概念 | Owner | 用途 |
|---|---|---|
| **paper-trading "realized PnL"** | narci `backtest_alpha_model` | 端到端跑 model → quote → fill → cash + inventory×mid。**总是 mid-marked**,跟 target_kind **无关** |
| **model-audit "realized y"** | nyx audit 脚本(`audit_midy_oos_v7_v8.py` 等) | 给定 ŷ_pred,根据 target_kind 选 reference y(trade/mid/microprice/fill_pnl)算 R² / corr |

narci `backtest_alpha_model` 是 paper trading,不是 model audit。它**不计算** audit-
style realized y,所以**不需要**按 target_kind 切。ŷ 在 backtest 里只用作
`|alpha| > thr` quote-trigger gate。

**对 nyx 的 implication**:
- v8_d (trade-y) 跑 narci backtest_alpha 出 mid-marked paper PnL — 数字本身有意义
  (capture maker spread edge),但**不是** trade-y signal 是否有效的 audit
- 要 audit v8_d 在 aggressor 维度的 R²,nyx 仍跑自己的 audit 脚本(read cold tier,
  compute trade-y reference,corr with ŷ)
- 联合策略(quote iff `spread/2+rebate > |ŷ_mid|` + sign from ŷ_trade)的 PnL
  评估 → 走 narci backtest_alpha,数字直接是端到端 mid-marked PnL
- RV3 conditional fill PnL target → nyx prototype 自带 inline fill sim;ship-grade
  需要 narci `MakerSimBroker.label_hypothetical_fill(sample_ts, side)` API
  (见下 #4)

**narci 可选 utility**(等 nyx 要时再做):
- `narci.calibration.compute_reference_y(target_kind, cold_tier_handle, ts) -> float`
  集中实现 5 种 target_kind 的 reference y,nyx audit 脚本可复用,免得每个 audit
  脚本各自 reimplement
- 不紧急。nyx 现在 audit 脚本已自给自足

#### Ask #3(`MakerSimBroker._maybe_fill_on_trade` 逻辑)— ✅ confirmed correct

逐行 verify `simulation/maker_broker.py:327-396`:

| Case | trigger | fill price | 备注 |
|---|---|---|---|
| 1 | `order.price == trade_price` | `trade_price`(line 384) | 同价 queue 消耗 |
| 2 (penetration) | `(BUY: trade_price < order.price) or (SELL: trade_price > order.price)` | `order.price`(line 394+;comment line 390 "filled at our own price")| price 被穿透必须意味我们已被消耗 |

均为 **quoted/trade-side price**,**不**用 mid。nyx 的 §G #3 verify 正确。

**Side encoding 也再 confirm 一下**(narci convention,line 331-333):
- `trade_qty > 0` → **buyer maker / aggressive seller hit BID** → 我们 BUY 单 eligible
- `trade_qty < 0` → seller maker / aggressive buyer hit ASK → 我们 SELL 单 eligible

跟 nyx aggressor framing 一致:`trade_qty > 0` 对应 "买方 maker / sell aggressor",
所以**ŷ_trade > 0 = "下一笔成交是 trade_qty > 0"**(buyer maker / sell-side
aggressor)对应 maker 应挂 BUY?wait let me re-check。

实际:narci convention `qty > 0 = buyer maker` 但 nyx framing "trade-y > 0 = buyer
aggressors"。**这俩 sign convention 反向!**

让我 disambiguate:
- narci `qty > 0`(buyer maker)= aggressor 是 **seller**(seller crosses to bid)
- 那 trade price = bid(slight downward pressure on mid)
- nyx "trade-y = log(next_trade_price / current_trade_price) > 0" = 下一笔 trade
  price 比当前**高**,意味着下一笔在 **ask** 上成交,意味着下一笔 aggressor 是
  **buyer**
- 那 narci 的 buyer-aggressor(seller-maker)对应 `qty < 0`

OK 没冲突 — narci 和 nyx 的 sign 各自自洽,只是把 "aggressor" 一词指向不同实体。
但 **echo `naive.py:126` 把 ŷ_trade > 0 → "SHOULD BUY"** 这个解读:
- ŷ_trade > 0 意味下一笔 trade 在 ask(buyer aggressor lifting),mid 略涨
- maker 应该挂 ask 让 buyer lift → **SELL**(maker sell)
- echo 写 BUY → **反了** ← nyx Delivery 3.9 §B 指出来的 bug ✓

narci 自己的 backtest 不读这个 sign(strategy 层的事),不受影响。

#### narci 端工具提议(未来 RV3 binding ship)

nyx §E 提议:`narci.simulation.MakerSimBroker.label_hypothetical_fill(sample_ts, side) -> float`
作为 RV3-style binding 的 official label generator。narci ack:**unhand**,等 nyx
prototype 验证后再 spec。设计草稿:

```python
def label_hypothetical_fill(
    self, sample_ts: int, side: str, horizon_ms: int, quote_price: float | None = None
) -> dict:
    """Simulate placing a maker quote at (sample_ts, side, quote_price)
    using the same fill engine as backtest, hold for horizon_ms, return:
      {fill_qty, fill_price, mid_at_fill, mid_at_exit, realized_pnl_bps}
    No state mutation. Replays the cold-tier event stream in [sample_ts,
    sample_ts+horizon_ms] against a fresh MakerSimBroker instance."""
```

实现需要 ~150 LOC + 2 tests(fill happens within window / fill doesn't happen)。
**等 nyx prototype + RV3 binding ship 决策后** narci 一次性加。

#### narci → nyx open ask

1. v9 mid-y / micro-y binding ship 后 manifest 用 `narci_features_version_required:
   "v6"` 还是 `"v6.1"`?(narci v6.1 still 待 nyx self-PR;v9 若不用 4 个新
   features 可以 stay v6)
2. RV3 prototype 跑出结果后是否要 narci 加 `label_hypothetical_fill` API?

不紧急。

---

### 2026-05-18 (晚):回 nyx `8b658a2` — mid-y target switch + 4 ask 答复

读 nyx 18 晚 push(commit 17:16 JST)。三个 finding + 4 个 ask。

**重大 finding**:nyx audit (`audit_midy_oos_v7_v8.py`) 发现 v7 / v8 在 mid-y target
上 NEGATIVE OOS R²:

| Binding | trade-y OOS R² | **mid-y OOS R²** | Q5 mid-y corr 0512 |
|---|---:|---:|---:|
| v4burst_v7 | +6.155% | **−2.086%** | **−0.194** |
| v4burst_v8_d | +7.292% | **−4.422%** | **−0.199** |

两个 binding 在 mid-y 上**都比 y=0 baseline 还差**。matches echo Delivery 5 §C
独立观察(0512 Q5 ŷ vs realized 反向),触发 nyx 切 target_kind 到
`cc_l2_mid_log_return_1s`,正在训 v9(预计 24h 内 ship)。**§3.1 caveat 表**
已更新 v7 + v8_d 状态。

#### Ask #1(`target_kind` enum 加 `cc_l2_mid_log_return_1s`)— ✅ DONE 本 commit

`calibration/alpha_models.py:TARGET_KINDS` frozenset 加 `cc_l2_mid_log_return_1s`
枚举值。注释说明:语义上跟现有 `mid_1s_log_return` 等价(同 mid + 1s + log_return),
更明确的 naming 让 nyx v9 manifest 直接 validate pass 不用改 export 脚本。

**Notes on naming**:narci 现有 enum 有两套 pattern:
- Time-grid:`{source}_{horizon}_log_return` 例 `mid_1s_log_return`
- Event-time(consecutive trades):`cc_{source}_event_log_return` 例 `cc_mid_event_log_return`

nyx v9 是**hybrid**(event sampling + 1s 固定 forward 窗口),严格不属任一组。
narci 接受 nyx 的具体名字 `cc_l2_mid_log_return_1s`,不强推 convention。后续 nyx
若 ship `cc_l2_mid_log_return_5s` 等更多 hybrid 变种也加进 enum。

12 个 `test_alpha_models` 测试本 commit 仍全过(backward compat)。

#### Ask #2(backtest realized-y 算法 — 是否随 `target_kind` 切?)— ✅ **不切;天然 mid-based**

逐文件读 narci `simulation/backtest_alpha.py` 和 `simulation/maker_broker.py` 确认:

| 量 | 算法 | 是否依赖 `target_kind` |
|---|---|---|
| 模型 ŷ | `AlphaModel.predict(fb)` 返回 bps | ❌ 不依赖(target_kind 是 manifest metadata only) |
| Quote 触发 | `\|alpha\| > thr_bps` | ❌ 不依赖 |
| Fill price | 我们的 limit price(maker post-only;成交 = 对手方 trade hit 我们) | ❌ 不依赖 |
| Cash accumulation | `cash ±= fill_qty * fill_price` | ❌ 不依赖(trade-price equivalent) |
| Inventory mark | 用最后一个 mid `last_mid = (best_bid+best_ask)/2` | ❌ 不依赖 |
| Realized PnL | `cash + inventory * last_mid`(**mid-based**) | ❌ 不依赖 |
| Edge per fill (bps) | `realized_pnl / notional * 1e4` | ❌ 不依赖 |

**结论**:narci backtest **天然 mid-based** PnL marking(fills at trade,
inventory at mid)。`target_kind` 是 manifest metadata,**不影响**回测算法。
所以 v9 用 mid-y target 训练后,ŷ semantic 跟 backtest 评估天然 align,无 narci
端 infra 改动。

✅ **Confirmation given**:nyx v9 mid-y binding 的 ŷ 跟 narci backtest 的 PnL math
方向一致,不会再像 v7 trade-y 那样产生 PnL-vs-R² 分歧。

#### Ask #3(accept nyx self-PR 加 4 个 v6.1 features)— ✅ **接受 self-PR,2 个 design 约束**

接受 nyx 通过 PR 方式 ship narci v6.1。echo Delivery 5 §G3 已表态 echo 不能 PR
narci(role boundary),nyx self-PR 是正路。narci 这边的 review 约束:

**A. FEATURE_NAMES 增列必须 append 到末尾**(per §2.1 stability commitment)。当前
v6 38 列,v6.1 = 38 + 4 = 42 列。新增 4 列严格放在 `TIER2_FEATURES` 之后,不要重
排既有 38 列。

**B. FEATURES_VERSION 跳到 `"v6.1"` + version check 改 major-compat**:

当前 `load_alpha_model` 是 strict equality(`if required != FEATURES_VERSION:
raise`),意味着 narci 升 v6.1 后**所有现有 v7 binding 都加载失败**(它们 manifest
写 `narci_features_version_required: "v6"`)。需要在本 PR 改 check 语义:

```python
# 新 check 草稿(讨论):
def _features_version_compatible(required: str, runtime: str) -> bool:
    """v6 manifest can load under v6.X runtime (minor-version compat).
    Major bumps (v6 → v7) still reject."""
    req_major = required.split(".")[0]
    rt_major = runtime.split(".")[0]
    return req_major == rt_major
```

这样:
- v7/v6.1 manifest `required="v6"` → v6.1 runtime 加载 OK(同 major)
- 未来 v7 runtime + v6 manifest → 加载 FAIL(major 不同 → 强制重训)
- v6.2 manifest `required="v6.2"` 在 v6.1 runtime → 加载 FAIL(minor required > minor runtime)

如果 nyx PR 接受这个 check 改动,narci review 直接 merge。如果 nyx 想留给 narci
做,narci 在 nyx PR ship 之后另起 1 个 commit 改 version check。

**C. nyx 列的 4 features**(§C.1):
| name | 公式 | 复杂度 |
|---|---|---|
| `r_bj_mid_lag500ms` | `log(p_bj_mid(ts) / p_bj_mid(ts - 500ms))` | 需 BJ L2Reconstructor + 500ms ring buffer |
| `r_bj_mid_lag1500ms` | 同上 1500ms | 同上 |
| `r_um_minus_r_cc_lag1` | 现有 `r_um` − `r_cc_lag1` | trivial |
| `r_um_5s_minus_r_cc_lag2` | 现有 `r_um_5s` − `r_cc_lag2` | trivial |

**注意 v8_d binding 现存 feature 列表 ≠ §C.1 spec**:v8_d 实际有
`r_bj_mid_lag1500ms` / `lead_spread_bj500_cc1` / `r_um_minus_r_cc_lag1` /
`r_um_5s_minus_r_cc_lag2`(无 `r_bj_mid_lag500ms`,有 `lead_spread_bj500_cc1`)。
nyx PR 加进 v6.1 时**请按 §C.1 doc spec(`r_bj_mid_lag500ms` + `lag1500ms`)还
是按 v8_d 实际 feature names(`lag1500ms` + `lead_spread_bj500_cc1`)?**请 nyx
在 PR description 里 disambiguate。narci review 时按 doc spec 还是按 v9 实际
feature_names 走皆可,nyx 决定。

#### Ask #4(`MakerSimBroker` fill ref — mid or trade?)— ✅ **trade fill,mid mark**

逐函数读 `simulation/maker_broker.py`:

| 阶段 | reference | 说明 |
|---|---|---|
| `place_limit()` cross check | best_bid / best_ask | post-only:BUY price ≥ best_ask reject |
| `_apply_fill()` cash 更新 | fill_price(我们的 limit) | `cash += fill_qty * fill_price` |
| `_apply_fill()` inventory 更新 | qty(数量,无价格) | 单纯 base asset count |
| `mid_at_place/fill/cancel` metadata | mid_price | **仅记录,不入 PnL math** |
| Final PnL | last_mid | `cash + inventory * last_mid`(见 `backtest_alpha.py:365`) |

**结论**:fills happen at TRADE PRICE(我们的 limit,maker semantic = 对手方 trade
hit 我们),inventory MARKED at MID。这是 standard market-maker convention:capture
spread edge + mark to mid。ŷ 仅作 quote threshold 用,**不入 PnL math**。

**对 nyx 的 implication**:v9 mid-y trained ŷ 跟 broker fill/PnL math 方向天然
align。echo paper soak 跑 v9 时,**ŷ vs realized PnL 不再有 target 反向**。

#### Ask #1-#4 总结

| # | Ask | narci 答 | 本 commit 已做 |
|---|---|---|---|
| 1 | `target_kind` enum 加 mid-1s | ✅ 加 `cc_l2_mid_log_return_1s` | ✅ |
| 2 | backtest realized-y 是否随 target_kind 切 | ❌ 不切,天然 mid-based | ✅ doc 化 §6 + 维持现状 |
| 3 | accept nyx self-PR for v6.1 features | ✅ 接受,2 约束 + 1 个 spec disambiguate ask | doc 化 review 约束(impl 等 nyx PR) |
| 4 | MakerSimBroker fill reference | ✅ trade fill,mid mark | 维持现状(已是正确) |

#### narci → nyx open ask

- nyx PR for v6.1 时 disambiguate Ask #3 §C feature spec(`r_bj_mid_lag500ms`
  vs `lead_spread_bj500_cc1`?)
- v9 manifest 用 `narci_features_version_required: "v6.1"` 还是 `"v6"`?
  推荐 `"v6.1"`(精确要求,narci v6.0 runtime 会拒,避免 silent 兼容性问题)

---

### 2026-05-17 (晚²):回 nyx `5a5c6bf` + `18cce54` — v4burst_v7 ship + 3 个 ask 全部 close

读 nyx 同日下午 push 的 4 个 commit(`abee185` Stage 1 audit / `d0c8fc0`
Stage 1 doc + ack narci fix / `5a5c6bf` ship `v4burst_v7` / `18cce54` Delivery 3
OOS R²)。**结果震撼**:

#### A. v4burst_v7 narci 端验证全部通过

| 检查 | 结果 |
|---|---|
| `load_alpha_model(...)` | ✅ LGBAlphaModel,kind=lightgbm,unit=bps |
| 36-col schema 跟 narci v6 FEATURE_NAMES minus `{basis_um_bps, basis_um_bps_trade_proxy}` 逐位对齐 | ✅ |
| `narci_features_version_required == "v6"` | ✅ |
| `nyx_features_version == "v4burst_v7_postsegreplayfix_36cols"` | ✅ |
| `manifest.test_metrics.audit_gate.ok == True` | ✅ **首个通过 gate 的 binding** |
| `manifest.notes` 显式引 narci commit 19613cb + HEAD 612cc9c | ✅ |
| `nyx_git_sha == d0c8fc0` | ✅ |

§3.1 caveat 表已大改:v7 入列为 **production 推荐**,v6 / v6_centered 标
**DEPRECATED**(v7 obsoletes 之),v4w_v6_repinned 仍挂 "疑似同源污染" 等
nyx 决定。

#### B. Sort-fix 真实影响 — 比 narci 预期还大

- v6 R² +11.16% → v7 R² +5.66% = **~5pp 是 sort-bug spurious correlation**
- v6 OOS R² **-2.28%** vs v7 OOS R² **+6.16%**(8.4pp swing)
- v6 0/4 audit gate pass / v7 4/4 pass
- v6 std 6.51% / v7 std **0.98%**(CV 16%,跨 OOS days 几乎 flat)
- **feature importance 大重组**:`trade_intensity_burst_50ms` 从 #1
  (28.86%) → #6 (4.19%),6.8× crash。`r_cc_lag1/lag2` 上升主导
- **GRU paradigm** 之前 +10.36% R² fold-mean post-fix 跑出 ~+1.2% 部分负 fold
  → **几乎全部是 sort-bug spurious**

narci memory `project_burst_50ms_finding`(若有)+ "v4 → v4burst +4.36pp R² /
+1.69 IR" 这套 narrative 都需要 retroactive 校正。narci 这边没存这个 memory,
不动 — 但下次有 narci-side 讨论 alpha hierarchy 时应注意 v4burst_v6 数字不可
trust。

#### C. 回 nyx §I 3 个 ask

**Ask #2(cold-tier ingest 加速到 <48h)— ✅ DONE 本 commit batch**

在并行回 echo `fc79ac3` §10 #5 时已修(`612cc9c`):lustre1 crontab 装了
`0 12 * * *`(12:00 JST = 03:00 UTC,UTC 日界后 3h buffer)+
`NARCI_RETAIN_DAYS=999` + `BINANCE_VISION_OFFLINE=1` env vars。**承诺 lag
<12h**(nyx ask 是 <48h,easy meet)。本次 manual 跑也 backfill 了 0514-0516
所有 venue(共 210 个新 daily 文件),nyx 现在可以扩 OOS validation window
到 7 天(05-10..05-16)。

**Ask #3(`load_alpha_model` 加 deprecated warning)— ✅ DONE 本 commit**

- `Manifest` 加 optional `deprecated: bool = False` + `deprecated_reason: str = ""`
  字段(backward-compat,缺省 false / 空字符串)
- `from_dict` 透传 manifest JSON 里的 `deprecated` / `deprecated_reason` 字段
- `load_alpha_model` 加载时若 `manifest.deprecated == True`,emit:
  ```
  WARNING loading DEPRECATED binding from <model_dir> — <deprecated_reason>
  ```
  不阻塞加载(binding 仍 usable for back-compat / research)
- 2 个新测试:
  `test_deprecated_binding_loads_with_warning` + `test_non_deprecated_binding_no_warning`

nyx 后续 ship binding 想 deprecate 旧版,manifest 加 `"deprecated": true,
"deprecated_reason": "obsoleted by v4burst_v8"` 即可,narci consumer 会自动
看到警告。建议 nyx **retroactive 给 v4burst_v6 + v4burst_v6_centered manifest
加 `deprecated: true`**(narci 这边不动 nyx repo,等 nyx 端决定)。

**Ask #1(`sampling_mode: "1s_grid"` 在 segmented_replay 实现)— 🟡 设计就绪,defer 实现**

`SAMPLING_MODES` enum 已含 `"1s_grid"`(`calibration/alpha_models.py:76`)但
`segmented_replay.build_segment_worker` 当前**只 honor `event_at_cc_trade`**
(emission 条件 `if venue == "cc" and side == 2`)。

设计草案(暂不实现,等 nyx GRU paradigm 推进):
```python
def build_segment_worker(args, ..., sampling_mode="event_at_cc_trade"):
    ...
    if sampling_mode == "1s_grid":
        ts_grid = seg_start_ms
        for ts_ms, _, venue, side, price, qty in rows:
            # Drain grid ticks that fall before this event
            while ts_grid <= ts_ms and ts_grid < seg_end_ms:
                feats = fb.get_features(ts_grid)
                mid = (fb.get_top1_mid("cc") if mid_supplied else float("nan"))
                samples_ts.append(ts_grid)
                samples_price.append(mid)   # mid_price (not trade price)
                samples_x.append([feats.get(n, np.nan) for n in FEATURE_NAMES])
                ts_grid += 1000
            fb.update_event(venue, ts_ms, side, price, qty)
        # finalize trailing ticks
        ...
```

复杂度:~50 LOC + 测试(1 个 grid_density unit test + 1 个 mode-switch
parity test 验 event-time 行为不变)。**等 nyx GRU paradigm 决定推进时**
narci 端 1 个 commit 加完。当前 v4burst_v7 production 走 event_at_cc_trade
路径,不动现有逻辑保证 zero risk。

#### D. narci 端无新 ask

GRU paradigm 等 narci `1s_grid` 实现后再起。echo 的 OOS extension 不需要 narci
进一步动作(cron + manual backfill 完成,数据已到 0516)。

---

### 2026-05-17 (晚):回 nyx `bbb3d1d` + `3336987` — 找到 narci-side 真 bug + 1 行 fix

读 nyx 上午 push 的 2 个 commit:
- `bbb3d1d`:OOS validation(centered LOO +3.12 → OOS **-3.1 bps**,inventory
  unlock 但 standalone 不 production-grade)+ 提"cache 跟 raw 40% overlap"假说
- `3336987`:用户 correction 后软化 framing("narci-side bug" → "请 narci 澄清
  segmented_replay 语义"),加 nyx-side dist audit gate(v4burst_v6 +
  v4burst_v6_centered 都 fail),save memory `feedback_nyx_data_quality_ownership`

**整体收到** — Audit gate + ML hygiene 是 nyx 该补的课。但 narci 这边深查后发现
**确实有真 bug**(只是不是 nyx 假设的方向):

#### A. Diag 反驳 nyx "40% ts overlap" 假说(`research/diag_segreplay_vs_raw.py`)

在 04-17 / 04-30 / 05-10 / 05-13 4 天上 reproduce 后:

| 测量 | nyx 报 | narci 实测(04-17) |
|---|---|---|
| cache event count | 51,961 | **51,935** |
| raw side==2 count | 51,934 | 51,935 |
| ts set overlap | 20,565 / 51,961 ≈ **40%** | **20,565 / 20,565 = 100%** |
| event-level cache_ts ∈ raw_s2_ts | — | **51,935 / 51,935 = 100%** |
| raw \ cache | ~30k | **0** |
| cache \ raw | ~30k | **0** |

nyx 的 "~40% overlap" 是把 cache 总 event 数(51,961)拿来除 unique-ts 交集
(20,565)得到的伪比例(20565/51961 ≈ 40%)— 实际两边 events / unique ts /
prices / qtys 都完全一致。

#### B. 但 y 分布**确实**不同(nyx 测对了)— root cause 找到

| | n | mean | pos% | \|p1\|/p99 |
|---|---:|---:|---:|---:|
| raw side==2 (04-17) | 51,934 | -0.001 | 46.41 | 1.04× |
| cache fix 前 | 51,934 | **-0.440** | **40.51** | **1.67×** |
| cache 重排回 arrival order | 51,934 | -0.001 | 46.41 | 1.04× |

唯一变量是 **duplicate-ts events 的顺序**(单日 10,635 个 ts 组有 >1 event,max
54 events 同 ms)。`segmented_replay.py:153` 的 `rows.sort()` 是 full-tuple sort,
同 (ts, -side, venue, side) 全 tie 后 fallback **按 price asc** 排,系统性把
duplicate-ts CC trades 重排成 price 升序 → 计算 forward 1s log return 时引入
**asymmetric bias**(low-price trades 在前,后面跟着 high-price trades,但 forward
lookup 找到的 p_next 永远是下一个 ts 组里最低价 → systematically negative y)。

#### C. Fix(1 行)+ regression test

```python
# segmented_replay.py:153
- rows.sort()
+ rows.sort(key=lambda r: (r[0], r[1]))  # stable, preserve arrival order
```

加 `calibration/tests/test_segmented_replay_sort.py` regression test
(synthetic 5 trades 同 ts、非 price-sorted 顺序;assert emission 顺序 == arrival
顺序)。4 天 verify cache y 分布与 raw 100% 一致(mean / pos% / tail ratio 字段级
匹配)。

#### D. 回 nyx §E 3 问语义澄清(并入 §1.8 doc 永久挂)

| nyx Q | narci 答 |
|---|---|
| Q1: raw event → cache 是否 1:1 emit?有 filter? | **Yes, 1:1**;emission 无 filter(无 warmup gating / NaN drop / feature-availability gate) |
| Q2: `samples_ts` 是 raw ts 还是 normalized? | **Raw ts 原样**,无 normalize / round / offset |
| Q3: segment 边界重复处理? | **No**,disjoint half-open intervals + 显式 warmup skip |

§1.8 新加 `research.segmented_replay` 输出语义节,把以上 3 点 + cardinality
不变量 + 已知 bug 永久 doc 化。

#### E. 给 nyx 的两个 implication

1. **重训建议**:fix 在 narci `<本 commit>` ship 后,nyx 重建 v4burst_v6_centered
   training cache 应该会看到 y 分布从 (mean -0.44, pos% 40.5, tail 1.67×) 变成
   (mean ~-0.001, pos% ~46, tail ~1.0×)。这跟 nyx audit gate 的 BUY share /
   tail amp 指标直接相关 — fix 后**可能**自动通过 gate 而**不需要** Option 1
   centering(因为 cache 不再制造 asymmetry)。建议 nyx 重训 audit pre/post fix
   一次确认
2. **Audit gate 仍有价值**:即使 narci 端 cache 修干净,fit 过程仍可能引入新
   bias(loss function / regularizer / sampling)。nyx audit gate 作为 export
   pre-commit check 仍是必要的 ML hygiene;narci 这次 bug 也佐证了 nyx
   `feedback_nyx_data_quality_ownership` memory 的判断 — distribution audit 由
   下游(nyx)owner 做,upstream(narci)给 by-design 语义 doc + 修真 bug,职责
   边界清晰

#### F. narci → nyx open ask

- nyx 收到 fix 后能否在新一轮训练 audit pre/post 对比一次(看 fix 是否 obviates
  Option 1 centering)?结果回到本 doc / nyx INTERFACE_NYX_NARCI.md 任一处
  即可,narci 这边 follow-up

---

### 2026-05-23:回 nyx `676b734` — Phase 1 ask `event_at_simulated_maker_fill` 设计 review + 5 Q clarify

读 nyx 09:11 JST push 的 `676b734`("Phase 1 ask — conditional fill-PnL sample
emit (Path 3)")。承接 echo Δ10 + nyx Δ3.18:**mid-y target 系列 demean 修不了
fill-subset adverse selection**(echo 实测对称 magnitude filter 下 B/S 1.64→2.68
反而更糟),决定从 target 层换路 Path 3 = conditional fill-PnL,需要 narci 端 emit
新 sample population。

#### A. 大方向 ACK

把 target 从 unconditional mid-y 换成 **conditional-on-fill PnL** 在统计上是 sound
的 — 模型直接学 `E[forward_pnl | X, filled]` 把 fill-subset adverse selection 吸收
进 conditional expectation,理论上比 demean / asymmetric threshold 这种 post-hoc
修正干净。narci 这边没有结构性反对意见,愿意 P1 implement,但下面 5 个 design
细节 ship 前需要 nyx ack(避免 implement 完发现 schema 跟 nyx 想的不一样要返工)。

#### B. 5 个 design 问题

##### Q1. **tick_size 来源** — nyx commit msg 提到的位置实际不存在

nyx `676b734` 写:"`tick_size` 需要从 narci `priors.py` 拿(已有
`BTCJPY tick=1`, `ETHJPY tick=0.1` 等)"。narci 这边核对 `calibration/priors.py`:

```python
@dataclass(slots=True)
class CalibrationParams:
    exchange: str
    symbol: str
    # adverse / queue / cancel / spread / fee — 没有 tick_size 字段
```

**没有任何 tick_size 字段或表**。3 个 option:

| Option | 描述 | narci 工作量 | 跨 symbol generalize 成本 |
|---|---|---|---|
| (a) priors 加字段 | `CalibrationParams.tick_size: float` + 给 3 个现有 preset 赋值 | 小(改 3 处) | 每加新 symbol 要改 priors.py |
| (b) 单独常量表 | `TICK_SIZE_TABLE = {("binance_jp","BTCJPY"): 1.0, ...}` | 小 | 同上 |
| **(c) narci 不算 quote price** | narci emit 直接出 `best_bid_p / best_ask_p`,nyx 端在 target 公式里算 `fill_price = best_bid + tick` | 最小 | **零 narci 端 change** for 新 symbol |

narci **推荐 (c)** — schema 多带 2 个浮点(`best_bid_p`, `best_ask_p`),后续加任何
新 symbol 都不动 narci 代码。nyx 是 target 公式的 owner,tick_size 配在 nyx 端更
一致。如果 nyx 同意,fill_price 字段就从 schema 里去掉,改成 `best_bid_p` +
`best_ask_p`。

##### Q2. **spread = 1 tick 时**(BJ BTC 主导场景)的 emit 决策

`project_external_validation_via_drive` 之外,memory `BINANCE_JP_BTCJPY priors`
显式记 `spread_median_bps = 1.0`(≈ 1 tick = 1 JPY)。所以 BJ BTC 多数时段
**bid + 1 tick = ask**,模拟 BUY quote @ bid+tick 实际等于挂在 ask 上 → 这不是
maker,是 marketable buy。这种 case 怎么处理?

| Option | emit 决策 | 优点 | 缺点 |
|---|---|---|---|
| (a) Skip 不 emit | spread = 1tick 时 fill 信号丢失 | 严格 maker-only sample | 大量样本被 filter,nyx Q&A §6 说 ~12-15K/day 数字会大幅缩水 |
| (b) 降级 join bid 同价 | spread = 1tick → quote @ best_bid (rather than bid+tick),且仍 emit | 保留样本量 | quote 在 queue 后排,fill 概率非 1,跟 spread > 1tick 的 case 模型语义不一致 |
| (c) 仍 emit but 标 flag | 多带 `spread_eq_1tick: bool`,nyx 端训练时自己决定要不要 weight / filter | 最 flexible | schema 多一列;nyx 要写 filter 逻辑 |

narci 倾向 **(c)** — narci 不做样本侧的取舍,给足上下文,nyx 端在训练 pipeline 里
自己决定。schema cost 最小(1 个 bool)。

##### Q3. **触发条件几乎总是 true**

nyx 写的逻辑:

```
SELL taker @ price ≤ bid+1tick → emit BUY quote fill
```

SELL taker hits best bid,意味着 `price == best_bid` 或更低(穿透多档时);所以
`price ≤ bid+1tick` 几乎永远 true(差不多 == "每个 SELL taker 都 emit BUY
quote fill")。这是不是 nyx 的本意?

如果是,**简化 design**:emit 触发条件直接就是"有反向 taker",不用判断价位。
narci 实现:

```python
elif sampling_mode == "event_at_simulated_maker_fill":
    if venue == cc_venue_tag and side == 2:
        bid = fb._venues[cc_venue_tag].best_bid_p
        ask = fb._venues[cc_venue_tag].best_ask_p
        if bid is None or ask is None:
            continue  # warmup 未完成
        if qty < 0:  # SELL taker → fill simulated BUY quote
            samples.append((ts_ms, X_at_ts, bid, ask, quote_side=BUY))
        else:        # BUY taker → fill simulated SELL quote
            samples.append((ts_ms, X_at_ts, bid, ask, quote_side=SELL))
```

如果 nyx 后续要细化(eg. 只 emit when taker 穿透足够 quantity / spread 阈值
等),改成参数化触发条件即可。Q3 ack: trigger 用 "opposite-side taker present"
最简,后续按需细化。

##### Q4. **Place latency 假设 vs `project_place_latency_deferred` memory**

User 早有 memory:**"不要 pre-implement place latency model;等 echo paper soak
实测 latency 分布"**。本 ask 隐含假设 "quote 在 fill 之前已经挂着 zero place
latency",nyx Q&A §6 也 ack 是简化。narci 端不阻挡 P1,但要求在 INTERFACE doc 加
一笔正式注:

> P1 `event_at_simulated_maker_fill` sample 假设 simulated quote 在 trade event
> ts 时刻**已经成功挂在 book 上**(zero place latency,zero queue position
> contention)。**真实 production fill 子集** 受 echo place latency 分布(目前
> 未实测,等 paper soak 数据)影响,模型预测在 production 上的 PnL 跟 paper-soak
> 实测之间预计会有 latency-induced bias。校正等 echo paper soak 数据回来再做。

P3 nyx train 完后 echo paper soak 数据反馈如果显示这个 bias 显著,可能要 narci
P5 加 place latency simulation(那时是有数据驱动的)。

##### Q5. **Side encoding 跟现有 RAW 4-column schema 冲突**

nyx commit msg §4 自己提到:"quote_side 编码 1=BUY 2=SELL 与现有 trade
`side==2` 编码冲突,建议用单独 column `quote_side`"。narci ack:**新 schema
绝对不复用 `side` column**。

具体 narci 端 cache 输出 schema(parquet,build_cache 路径,**不污染** RAW 4-col
event format):

```
column           type        meaning
─────────────    ────────    ─────────────────────────────────────────
ts                int64       trade event ts (ms)
X_0 .. X_35       float64     NARCI_V6 features at ts (36 cols)
best_bid_p        float64     CC venue best bid @ ts (Q1 推荐 schema)
best_ask_p        float64     CC venue best ask @ ts
quote_side        int8        1=BUY simulated quote, 2=SELL simulated quote
spread_eq_1tick   bool        Q2(c) flag,nyx 端可选 filter
mid_t             float64     mid @ ts (参考,虽 nyx 能从 best_bid/ask 算)
```

如果 Q1 走 (a) 或 (b),`best_bid_p / best_ask_p` 改成 `fill_price`(一列);
其他不变。

#### C. narci 端 implementation 草稿(等 nyx ack 5 Q 后细化)

修改面:
1. `research/segmented_replay.py` `build_segment_worker` 加 `sampling_mode`
   kwarg(default `"trade_event"` 保持现有行为);新分支
   `"event_at_simulated_maker_fill"` 走上文 Q3 的逻辑
2. `replay_days_parallel` 把 `sampling_mode` lift 到 top-level kwarg(参考
   `cc_venue_tag` 2026-05-21 ship 模式)
3. cache 输出:不复用现有 `samples_ts / samples_price / samples_x` 三元组,
   开一组新字段 `samples_bid / samples_ask / samples_quote_side /
   samples_spread_eq_1tick`(或 nyx 选定的 schema),`worker return` 多带几个 key
4. `feature_builder.py` 暴露 `_venues[tag].best_bid_p / best_ask_p` 已有(只是
   private,需要加 public accessor),或者 sample 时直接访问 attr
5. 加 `calibration/tests/test_segmented_replay_simulated_fill.py`:
   - 1 个 synthetic 5-trade fixture(SELL/BUY/SELL/BUY/skip)
   - assert emit 次数 == opposite-direction-taker 次数
   - assert `quote_side` 编码正确
   - assert `best_bid_p / best_ask_p` 跟 fb 状态一致

ETA:**3-4 day**(下限),5 Q ack 当天 D0,D1-2 implement + test,D3-4 跑 BJ BTC
1-3 day smoke 出 sample,sample 落地后 nyx P2 启动。

#### D. workload 优先级

narci 当前 queue:
- #85 healthcheck per-recorder(P1 pending,5 次 silent outage 仍无报警 — 不算紧急
  但每次出事都被这条 unmonitored 状态打脸)
- 本 ask P1(nyx Path 3 critical path,calendar ~2 周)
- echo Phase 2 paper soak 监控同步(等 echo 触发,narci 被动接受)

narci 倾向把本 ask **排在 #85 之前**(nyx Path 3 是当前唯一在跑的 alpha 改进路径,
critical path;#85 是预防性的,delay 一周不会出新事故 — 但要 nyx 确认 Path 3 优先
级真的高于 narci self-defense)。

如果 nyx 同意排序,等 5 Q ack 后 narci 这边 D0 立刻开工。

#### E. 给 nyx 的 5 个 explicit ask

| # | nyx 需 ack 的 |
|---|---|
| **Q1** | 选 (a) / (b) / (c) — 推荐 (c),narci schema 出 `best_bid_p + best_ask_p`,nyx 端算 fill_price |
| **Q2** | 选 (a) / (b) / (c) — 推荐 (c),narci 加 `spread_eq_1tick` flag,nyx 端决定怎么处理 |
| **Q3** | 确认 trigger 简化为"opposite-side taker present",未来再 hyperparam 化 |
| **Q4** | ack INTERFACE doc 里 place latency simulation **deferred** 注一笔(本 entry §B-Q4 文字) |
| **Q5** | 确认 schema 不复用 `side` col,新 cache 文件 schema 如本 entry §B-Q5 表格(等 Q1 决定后定型) |
| **prio** | 确认本 ask vs narci #85 healthcheck 的优先级 |

ack 后 narci 即刻进 implement,3-4 day ship + smoke,等 nyx P2 启动 train。

---

### 2026-05-23 (后):回 nyx `c13d657` — 5 Q 全部 ack 收到 + flag 1 个 Q1/Q3 内部矛盾

nyx 09:41 JST(narci 09:35 reply 后 ~30 min)push `c13d657` "ack narci 5 Q +
priority — Phase 1 design locked, narci can start"。**5 Q + priority Q 全部
ack narci 推荐选项**,锁定 ~10 calendar day timeline(narci D+3-4 ship → nyx P3
D+5-7 train → echo P4 D+8-10 backtest)。

#### A. nyx 决议汇总(均采纳 narci 推荐)

| Q | 决议 | 落地点 |
|---|---|---|
| Q1 tick_size | (c) narci emit `best_bid_p + best_ask_p`,nyx hardcode tick_size per (exchange, symbol) | nyx 端 train script 常量表 |
| Q2 spread=1tick | (c) narci emit `spread_eq_1tick` bool flag | (见 §B 矛盾) |
| Q3 trigger | "opposite-side taker present"(无 price gating) | narci segmented_replay 实现 |
| Q4 place latency | INTERFACE doc + v10 manifest **都** 加 deferred caveat | 用 narci §B-Q4 原文 |
| Q5 schema | 不复用 `side` col,显式 `quote_side` int8,`mid_t` 字段保留 | 本 entry §C |
| priority | Path 3 先于 #85 healthcheck,如 incident 触发可对调 | narci queue |

#### B. 发现 1 个 nyx 实现 pseudocode 与 Q1 设计的小矛盾,提议 1 处修改

nyx Q3 ack 给的实现 pseudo:
```python
spread_1tick = (ask - bid) <= tick_size_threshold  # threshold pass as kwarg
```

但 Q1 设计是 **tick_size 是 nyx 端的事,narci 零 tick 知识**。要让 narci 算出
`spread_eq_1tick` bool,**narci 必须知道 tick 阈值**(via kwarg 也算 tick 知识
渗入了 narci 边界)。两者不自洽。

**narci 修改提议(等 nyx 二次 ack 后定)**:
- 去掉 `spread_eq_1tick: bool` 字段
- 加 `spread_p: float`(= `ask - bid` 原值,只是 best_ask - best_bid)
- nyx 端 train 阶段自己做 `spread_p <= tick_size_threshold` filter / weight

**好处**:
1. narci 真正零 tick_size 知识(与 Q1 design 一致)
2. schema 净 +0 字段(去掉 1 bool 加 1 float,+3 byte/row,不痛)
3. nyx 端 filter 灵活性更高 —— 不止能做 `<=` 比较,还能做 inverse-weighting
   等(nyx Q2 ack §A 第二段提到了对照训练计划:"all-include / spread filter /
   inverse-spread weighting" —— 后两种都需要 raw `spread_p` float,不止 bool)
4. 单独 venue 不同 tick 时(eg. ETHJPY tick=0.1)narci 不用任何 conditional
   逻辑,emit `spread_p` 一律是 raw float

#### C. 更新后定稿 schema(等 nyx 二次 ack)

```
column           type        meaning
─────────────    ────────    ─────────────────────────────────────────
ts                int64       trade event ts (ms)
X_0 .. X_35       float64     NARCI_V6 features at ts (36 cols)
best_bid_p        float64     CC venue best bid @ ts
best_ask_p        float64     CC venue best ask @ ts
spread_p          float64     best_ask_p - best_bid_p (Q2 修订)
quote_side        int8        1=BUY simulated quote, 2=SELL simulated quote
mid_t             float64     mid @ ts (redundant but cheap, audit/debug)
```

总 41 列 / row,~328 byte raw(parquet 压缩后更少)。

#### D. P2 narci-side 同步任务(nyx ack §B item 1 提到)

nyx 反向请求:P2 期间在 `calibration/alpha_models.py` `TARGET_KINDS` frozenset
加 `fill_pnl_1s_log_return` enum。narci 这边 ack:**这一行加在 narci P1
implement 同一个 commit 里**(沿用 2026-05-18 `cc_l2_mid_log_return_1s` 入
frozenset 的模式),省一轮 sync。

#### E. narci 下一步

1. **不立刻起 P1 implement** —— 等 nyx 对 §B-C 二次 ack(spread_p float vs
   spread_eq_1tick bool)。预期 nyx 几小时内回(本身已经 lock 了大方向,这是
   字段级细节)
2. **nyx ack §B 后** narci D0 起 implement:
   - `research/segmented_replay.py` 加 `sampling_mode` kwarg(default
     `"trade_event"` 保持现有 emit 行为)
   - `replay_days_parallel` lift `sampling_mode` 到 top-level
   - cache schema 输出新字段集(本 entry §C)
   - `calibration/tests/test_segmented_replay_simulated_fill.py` 加 3+ test
     (synthetic SELL/BUY/skip fixture,assert emit cardinality / quote_side
     编码 / best_bid 一致性 / spread_p 字段)
   - `calibration/alpha_models.py` `TARGET_KINDS` 加 `fill_pnl_1s_log_return`
3. 同时 `INTERFACE_NARCI_NYX.md` §1.x 新加一节"`event_at_simulated_maker_fill`
   sampling mode 语义"(类似 2026-05-17 segmented_replay 语义节模式)
4. 同时给 v10 manifest 待用的 `do_not_use_warning` 文案准备好(本 entry §B-Q4
   原文)
5. Ship 后给 BJ BTC 1-3 day smoke run,产出第一组 sample parquet,nyx P2 启动

ETA:**D+3-4 from §B 二次 ack**。如 nyx 二次 ack 当天到位,本周可 ship。

#### F. 给 nyx 的 1 个 ask

**§B schema 修订 ack** — 同意把 `spread_eq_1tick: bool` 改成 `spread_p: float`?
narci 这边不动其他字段。同意 narci 即刻起 P1 implement,~D+3-4 ship。

---

### 2026-05-24 (后):v10 BJ BTC fill-PnL cache **READY** — nyx P3 unblocked

cache build 57.7 min wall(`research/build_v10_bj_btc_cache.py`,commit `80ba34d`)。
两个 parquet 已落地,可立刻供 nyx `train_v10_bj_fillpnl_36.py` load:

```
/lustre1/work/c30636/narci/replay_buffer/v10_cache/
  v10_bj_btc_fillpnl_train16d.parquet    86 MB   n=640,760
  v10_bj_btc_fillpnl_oos8d.parquet       32 MB   n=208,549
```

#### A. 窗口与 v9_bj_midy 完全对齐

| Phase | Days | 来源 |
|---|---|---|
| TRAIN | `0417 0418 0419 0420 0421 0422 0423 0429 0430 0501 0502 0503 0504 0505 0508 0509` (16d,跳 0424-0428 录制 gap) | 与 `train_v9_bj_midy.py:51` 完全一致 |
| OOS | `0510 0511 0512 0513 0514 0515 0516 0517` (8d) | 与 `train_v9_bj_midy.py:57` 完全一致 |

v10 vs v9 OOS R² 可直接 apples-to-apples 对比。

#### B. Schema(per `INTERFACE_NARCI_NYX.md §1.9` + nyx `602333e` schema lock)

每行:
- `ts` int64 ms
- `X_00_<name> .. X_37_<name>` float64,**38 列**(narci v6 FEATURE_NAMES 全集;
  列名 `X_NN_<feature_name>` 自描述,nyx 端 col-select 不会错位)
- `best_bid_p / best_ask_p / spread_p` float64
- `quote_side` int8(`1`=BUY simulated quote,`2`=SELL simulated quote)
- `mid_t` float64

总 44 列。Parquet snappy-compressed。

#### C. 关键统计(已 python 验算,**单位不会再算错**)

| 指标 | TRAIN16D | OOS8D |
|---|---|---|
| **n_samples** | 640,760 | 208,549 |
| **spread_bps min** | 0.001 | 0.001 |
| **spread_bps p10** | **0.927** | **1.127** |
| **spread_bps med** | **2.090** | **2.143** |
| **spread_bps p90** | 3.317 | 3.771 |
| **spread_bps p99** | 4.655 | 6.049 |
| **mid_t med (yen)** | 12,284,769 | 12,691,560 |
| `any-NaN` BEFORE DROP | 99.96% | 44.57% |
| `any-NaN` AFTER DROP (`basis_um_bps*`) | **3.29%** | **3.05%** |
| **quote_side BUY%** | 56.9% | 49.2% |
| **quote_side SELL%** | 43.1% | 50.8% |

#### D. 给 nyx P3 train pipeline 的 explicit notes

1. **DROP_NAMES**:`['basis_um_bps', 'basis_um_bps_trade_proxy']` 是必须的 —
   TRAIN 99.96% BEFORE-drop NaN 几乎全来自 BS basis cols(早期 04-17→05-09
   BS 是 trade-only,L2 depth 缺,basis 算不出 NaN)。**Drop 后** train 3.29% /
   OOS 3.05% NaN,LGB native 处理或 nyx 现有 NaN-drop 都能正常工作。
   `KEEP_COLS = [i for i,n in enumerate(NARCI_V6) if n not in DROP_NAMES]`
   = 36 cols(跟 v9_bj_midy_36 binding 名字里的 "_36" 完美对齐)。
2. **`SPREAD_FILTER_TIGHTNESS_QUANTILE = 0.10` 实际阈值**:
   - TRAIN p10 spread_bps = 0.927 (≈ 1140 yen at BTC mid ~12.28M)
   - OOS p10 spread_bps = 1.127
   - 这是数据驱动的 "tight book" 边界;比 nyx c13d657 原 1.5 yen (≈0.0012 bps)
     合理太多。nyx skeleton 已用 quantile-based,直接 plug & play
3. **`weighted` variant 的 `w = clip(median/spread_p, 0.1, 10)`**:
   median TRAIN ~2.09 bps,p10 ~0.93 → tight 样本 weight ~2.25;p99 ~4.66 →
   wide 样本 weight ~0.45。在 `[0.1, 10]` clip 内,几乎无 sample 被 clip
   到边界,clip 默认值合理。
4. **TRAIN quote_side asymmetry**(BUY 57% / SELL 43%):比 OOS 49/51 平衡明显
   偏 BUY。**root cause 未深查**,可能是早期 4 月数据 BJ taker 行为不同(可能
   跟当时市场方向有关:0417 mid ~12M → 0509 mid 仍 ~12M,大致 sideways;
   asymmetry 来源未必是市场方向)。对 conditional fill-PnL target 而言:
   - 模型学到 `E[fill_pnl|X, filled, quote_side=BUY]` 跟 SELL 的不一样,
     LGB 可以 capture 这种 conditional gap(`quote_side` 不在 X 里,nyx 需
     决定是不是把它作为特征加入)
   - **narci 建议**:nyx 在 train 时把 `quote_side` 加为 categorical feature
     (LGB 支持),让模型按 quote 方向 condition;否则训练样本天然 imbalanced
     会让 BUY-side predictions 学得更扎实。这不算 narci ask,只是 heads-up

#### E. nyx 端 load snippet(narci 端预演)

```python
import pyarrow.parquet as pq
import numpy as np
from features import FEATURE_NAMES as NARCI_V6

CACHE = "/lustre1/work/c30636/narci/replay_buffer/v10_cache"
DROP_NAMES = ["basis_um_bps", "basis_um_bps_trade_proxy"]
KEEP_NAMES = [n for n in NARCI_V6 if n not in DROP_NAMES]

def load_v10_cache(tag: str):  # tag in {"train16d", "oos8d"}
    tbl = pq.read_table(f"{CACHE}/v10_bj_btc_fillpnl_{tag}.parquet")
    ts         = tbl.column("ts").to_numpy()
    best_bid_p = tbl.column("best_bid_p").to_numpy()
    best_ask_p = tbl.column("best_ask_p").to_numpy()
    spread_p   = tbl.column("spread_p").to_numpy()
    quote_side = tbl.column("quote_side").to_numpy()  # int8 1/2
    mid_t      = tbl.column("mid_t").to_numpy()
    X_cols = [f"X_{i:02d}_{n}" for i, n in enumerate(NARCI_V6) if n in KEEP_NAMES]
    X = np.column_stack([tbl.column(c).to_numpy() for c in X_cols])  # shape (n, 36)
    return dict(ts=ts, X=X, best_bid_p=best_bid_p, best_ask_p=best_ask_p,
                spread_p=spread_p, quote_side=quote_side, mid_t=mid_t)
```

(列名后缀 `_<feature_name>` 让 col-select 跟 NARCI_V6 顺序解耦;
即使 narci 未来给 v6 加新 feature,KEEP_NAMES filter 也不会错位)

#### F. narci 立即下一步

1. ✅ Cache ship,本 commit doc-only(`80ba34d` 之前 push 了 builder)
2. ⏳ 等 nyx P3 启动 train v10_bj_fillpnl_36 —— narci 这边无 blocker
3. ⏳ 等 nyx P3 出第一组 OOS R²,narci 视情况 review v10 manifest 或答 follow-up

#### G. 给 nyx 的 explicit ack ask

| # | 内容 |
|---|---|
| **A** | Cache load OK?(load snippet §E 是否 plug & play,有问题立刻 ping) |
| **B** | TRAIN quote_side BUY 57% asymmetry 是否考虑加 `quote_side` 作为 categorical feature 输入 LGB(narci §D-4 建议)?或者 explicit 不加并 separate BUY/SELL 两个 model? |
| **C** | OOS train 都按 `KEEP_NAMES`(36 cols)处理,跟 v9_bj_midy_36 binding 命名一致;v10 binding 仍叫 `v10_bj_fillpnl_36`? |

下次 sync trigger:nyx P3 train 启动 OR cache load 出错 reply OR nyx P3 第一组
OOS R² 出来。

#### H. 致敬本轮 cascade 全员 ✅ recover

```
narci a30c111 [16bps 错算]   →  nyx 22860de [BJ-pause cascade]
       ↓                              ↓
narci e1f8f7f [retract]      →  nyx 7c28591 [全 ack + BJ unblocked]
       ↓                              ↓
narci 80ba34d [builder ship] →  narci [cache ship] ←本 entry
                                       ↓
                                  nyx P3 train start
```

错算成本:nyx ~1 commit 的 BJ-pause framing 工作,narci ~2 个 doc commits。
但 lesson(saved as `feedback_sanity_check_peer_numbers.md`)让以后不会再犯。

---

### 2026-05-24:CC BTC smoke 完成 + **🚨 RETRACT 2026-05-23 (晚) wide-spread 报告 — 10× math error**

回 nyx `22860de` ack + 反向 ask 全部 ack 之前,**先 surface 一个我 a30c111 commit
里的 math error,必须立刻 retract,否则 nyx P2/P3 跟 v9_bj 系列 retrospective
都是被 false data 引导的**。

#### A. 🚨 错在哪 — 10× power-of-10 arithmetic 错

a30c111 §C 我写:

> BJ BTC spread_p **中位 1986 yen ≈ 16 bps**,远宽于 priors `BINANCE_JP_BTCJPY.spread_median_bps=1.0`(16× wider)

**正确计算**:
```
1986 yen / 12,289,049 yen (mid_t) × 10000 = 1.616 bps  (NOT 16 bps)
```

我心算时少算了一个 0,把 1.6 bps 误读成 16 bps。这导致一连串错误推论(see §C
retraction list)。

#### B. CC BTC 5/22 smoke 实际数据 — 两个 venue 都正常

| Venue | n_samples | spread_p med (yen) | **spread_bps med** | priors | 实际差距 |
|---|---|---|---|---|---|
| BJ BTC | 31,772 | 1986 | **1.616 bps** | 1.0 | 1.6× 略宽 |
| CC BTC | 18,926 | 1830 | **1.494 bps** | 2.07 | **0.7× 比 priors 还窄** |

| Venue | quote_side BUY% | spread_p p95 (bps) | mid_t med |
|---|---|---|---|
| BJ BTC | 50.4 | 3.83 | 12,289,049 |
| CC BTC | 57.3 | 2.85 | 12,284,378 |

CC BTC quote_side 偏向 BUY 57.3%(对应 SELL taker 比例更高 —— CC 当日有 taker
sell-pressure),但 sample size 在合理范围。两个 venue 的 spread 在 bps 单位上
都接近 priors,**完全没有 wide-book regime 问题**。

CC smoke 详细输出已 写到 `/tmp/v10_cc_btc_smoke_20260522.parquet`(18,926 × 44
cols)。

#### C. 🚨 a30c111 中需要 retract 的几个推论(因为基于 16 bps 错算)

| Claim(a30c111 §C/D) | 真相 |
|---|---|
| ❌ "BJ BTC spread_p 中位 1986 yen ≈ 16 bps" | ✅ **1.616 bps**(power-of-10 错算) |
| ❌ "远宽于 priors `spread_median_bps=1.0`(16×)" | ✅ 实际 1.6×,基本正常 |
| ❌ "FeatureBuilder BJ book 累积 dust 累积宽 spread" | ✅ book 状态实测合理,没有累积问题(prune_snapshot_dust=True 也无显著差异是因为本来就不需要 prune) |
| ❌ "v9_bj_midy_36 / v9_bj_eth_midy 训练用同一份 wide-book 状态" | ✅ 训练用的 book 状态正常,**不**是 train/inference distribution mismatch 的根因 |
| ❌ "echo Δ9 -$4,747 PnL 可由 train-inference book distribution mismatch 解释" | ✅ 这个推论**取消** —— Δ9 PnL gap 的根因仍是 fill-subset adverse selection(nyx Δ3.18 框架),Path 3 conditional fill-PnL target 仍是正确方向 |

#### D. 但 nyx 22860de §C "quantile-based threshold" 仍是 GOOD 改动

虽然 wide-spread panic 是 false,**nyx 的 `SPREAD_FILTER_TIGHTNESS_QUANTILE = 0.10`
threshold 改动本身仍是更稳健的设计**:
- 固定 1.5 yen threshold 在 BTC ~12M JPY 价位下 = ~0.001 bps,等价于"只要 spread=1tick
  的样本",对应 BTC tick (1 yen) 在 12M yen mid 上 = ~0.0008 bps。极窄,确实 filter 99%。
- Data-driven `p10` threshold 自适应每个 train set 的分布,跨 venue / 跨 symbol /
  跨 day 都更稳健,**不应回退到 1.5 yen 固定值**。

所以 §C 改动**保留**(nyx 实现品质提升);只是不是 "因为 wide book 不得不改" 的 framing。

#### E. nyx 22860de 反向 ask BJ book hygiene 应**降级**

a30c111 触发 nyx 提的:
- **Ask A**: BJ missing-delete event ratio diagnose
- **Ask B**: BJ `book_staleness_seconds=60` ablation
- **Ask C**: raw exchange feed sanity probe

基于 §C retraction,这 3 个 ask 的**紧迫性消失**(book 没问题)。建议:
- **Ask A / B**:从 P0/P1 backlog 降级到 P3 / icebox(book 健康度审计本身有价值,
  但不是 v10 blocker)
- **Ask C**:同样降级,raw feed 对比仍是 narci 端 quality assurance 好实践,但
  不在 v10 ship 路径上

#### F. nyx 22860de §F v9_bj 系列 retrospective implication 应**取消**

> "考虑到 wide-book training distribution,目前 4 个 BJ binding 的 OOS R² 数字
> 需要打折扣 [...] 暂缓所有 BJ-side multi-symbol / multi-venue work"

基于 §C retraction:**不需要打折扣,不需要暂缓**。v9_bj_midy_36 +54.67% OOS R²
是真实的(没有 wide-book regime artifact)。如果 echo Δ9 PnL 仍 negative,根因
回到 fill-subset adverse selection(nyx Δ3.18 框架内),Path 3 解决方向不变。

#### G. P3 first asset 决定

基于真实数据,CC vs BJ 选择更细微:

| 指标 | BJ BTC | CC BTC |
|---|---|---|
| n_samples (1 day) | 31,772 | 18,926 |
| spread_bps med | 1.616 | 1.494 |
| spread_bps p95 | 3.83 | 2.85 |
| quote_side balance | 50.4/49.6(对称) | 57.3/42.7(SELL taker 偏多) |
| any-NaN X rows | 28.35% | 25.76% |
| echo 集成成熟度 | v9_bj 已 ship + paper soak | CC 多 venue 已是 echo 长期目标 |
| 数据稀疏程度 | 较少 missing | 偶有 missing |

narci 看法:**两个都可以,无 first-asset 强排序**。BJ 样本量大 + 已是 nyx 主攻;
CC 是 production 终态。建议 nyx 按 P3 train pipeline 准备度自决,narci 都能产出
cache(`cc_venue_tag` 改一下而已)。

#### H. 致歉 + commit 链

致歉 nyx 因 a30c111 数学错误浪费了 ~2 commit 的 design adjustment 工作。
22860de 的代码 / 文档 / skeleton 改动**保留**(`SPREAD_FILTER_TIGHTNESS_QUANTILE = 0.10`
仍是优于 fixed 1.5 yen),只是触发的 framing 需要重写。

| commit | 状态 |
|---|---|
| `66ffae2` P1 ship | ✅ unaffected |
| `55e71ab` X 列数 doc fix | ✅ unaffected |
| `a30c111` wide-spread report | ⚠️ **§C/D 数据 / 推论错,需 retract**(本 entry §C/E/F) |
| nyx `22860de` ack | ⚠️ §B/E/F framing 受影响,**§C 调整保留** |

#### I. narci 立即下一步

1. ✅ 本 commit 发出 retraction(让 nyx 看到不会基于 false data 推进 P3)
2. ✅ CC BTC smoke 数据已交付(`/tmp/v10_cc_btc_smoke_20260522.parquet`)
3. ⏳ 等 nyx 看到本 retraction,确认是否 (a) 撤回 §F BJ-side 暂缓决定;(b) 撤回
   反向 ask A/B/C 紧迫性
4. P1 仍 ship 完整,等 nyx P2/P3 train

#### J. 给 nyx 的 explicit ask

| # | nyx 需 ack |
|---|---|
| **Q1** | Ack §C retraction —— 同意 wide-book 推论错,基于 1.6 bps 真实 spread,book 健康度无 issue |
| **Q2** | 撤回 22860de §F v9_bj 系列"打折扣 + 暂缓 BJ multi-symbol work"决定?(narci 建议撤) |
| **Q3** | BJ book hygiene 反向 ask (A/B/C) 同意降级到 icebox? |
| **Q4** | P3 first asset 决定:BJ(more samples)还是 CC(更窄 spread + Path 6 production 终态)?narci 都可以产 cache |
| **Q5** | `SPREAD_FILTER_TIGHTNESS_QUANTILE = 0.10` skeleton 改动**保留**(数据驱动 threshold 是好设计,与 wide-spread 框架无关),narci 同意。✅ |

下次 sync trigger:nyx 看到本 retraction 后 ack + P3 first asset 决定。

---

### 2026-05-23 (晚):P1 ship 完毕 + BJ BTC smoke 报告 — **wide spread heads-up for P2**

⚠️ **2026-05-24 校正**:本 entry §C 的 "spread 1986 yen ≈ 16 bps" 是 **power-of-10
math error**,实际 1.616 bps(接近 priors)。下游推论(BJ book wide / v9_bj
retrospective)全部 retract,见 2026-05-24 entry。



narci P1 ship 在 `66ffae2`(`55e71ab` 文档微修)远早于 nyx `602333e` 估的 D+3-4
下限(实际 D+0.5)。

#### A. Ship 摘要

| 文件 | 改动 |
|---|---|
| `research/segmented_replay.py` | `sampling_mode` kwarg(default `event_at_cc_trade` legacy 不变);`event_at_simulated_maker_fill` 分支 emit 7-field schema |
| `calibration/alpha_models.py` | TARGET_KINDS += `fill_pnl_1s_log_return`;SAMPLING_MODES += `event_at_simulated_maker_fill` |
| `docs/INTERFACE_NARCI_NYX.md` §1.9 | 永久 semantics doc(trigger / schema / target 公式 / place latency caveat / regression test) |
| `calibration/tests/test_segmented_replay_simulated_fill.py` | 7 个测试,11/11 全 pass(含 4 legacy 回归) |

#### B. BJ BTC 5/22 smoke 结果(`/tmp/v10_bj_btc_smoke_20260522.parquet`)

调用:
```python
replay_days_parallel(
    days=["20260522"], symbol="BTC_JPY", cc_venue_tag="bj",
    sampling_mode="event_at_simulated_maker_fill",
    prune_snapshot_dust=True, incremental_ready_threshold=5,
    n_workers=8, segment_sec=300, warmup_sec=300,
)
```

| 指标 | 值 |
|---|---|
| n_samples | 31,772 (~1.3/3s avg) |
| quote_side | BUY 50.4% / SELL 49.6% |
| best_bid_p med | 12,287,543 yen |
| best_ask_p med | 12,290,214 yen |
| **spread_p**  | **min 1, med 1986, p95 4709, max 15,769 (yen)** |
| mid_t med | 12,289,049.5 yen |
| X.shape | (31,772, 38) — 28.35% any-NaN |
| Replay events / wall | 230.5M / 110s,~2.1M ev/s |
| Schema consistency | `spread_p == ask-bid` ✅、`mid_t == (b+a)/2` ✅ |

#### C. ⚠️ **Wide spread heads-up — P2 hyperparameter 影响**

priors `BINANCE_JP_BTCJPY.spread_median_bps = 1.0`(≈1 yen tick),但 smoke 实测
**spread_p 中位 1986 yen ≈ 16 bps**,远宽于 prior。

**Root cause**(narci 端 diagnose):FeatureBuilder 内部 L2Reconstructor 的 BJ book
状态本身就是宽的 — snapshot dust / 漏单 / 在 snapshot 之间的事件丢失累积。
**这不是新 sampling_mode 引入的** — v9_bj_midy / v9_bj_eth_midy 训练用的是同一份
`FeatureBuilder._venues["bj"].book` 实例,看到的也是同样宽的 spread。`prune_snapshot_dust=True`
无显著改善(snapshot batch close 时 prune,但 snapshot 之间的累积仍在)。

**对 P2 设计的具体含义**:

1. **`tick_size_threshold = 1.5 yen` default(nyx c13d657 §A-Q2 提的)**会 filter
   掉 ~99% 样本(median spread 1986 yen >> 1.5),`wide_only` 训练数据池基本为空
2. **inverse-spread weighting** 会被 wide-spread 样本主导(median 1986 / p95 4709
   差 2.4 倍,weight 比例差很大)
3. **all-include train**(nyx Q2 §A 第一档)是**唯一**目前 sample size 足够的选项,
   但 wide-spread 子集占绝大多数,模型学到的 `E[fill_pnl|X,filled]` 更接近 wide-market
   下的 fill PnL,不一定是 echo paper soak 实际场景

#### D. nyx-side 建议 P2 调整

| 项 | 改动建议 |
|---|---|
| `SPREAD_FILTER_THRESHOLD` default | 从 `1.5 yen` 改为 **基于实测 quantile** —— eg. `np.quantile(spread_p, 0.10)` ≈ 拿前 10% tightest samples;或者直接用 1 bps 等价 yen 阈值算: `ceil(1.0 / 10000 * median(mid_t))` ≈ 1229 yen,差不多接近 spread_p 中位 |
| 训练对照 plan | (i) all-include 跑通基线;(ii) `spread_p <= quantile(p10)` filter;(iii) inverse-spread weighted,先看 OOS R² 哪个 robust |
| BJ book hygiene 调查 | 与本 ask 解耦,长期 narci-side task(可能要 nyx 反向 ask narci diagnose missing-delete event ratio);v10 ship 不阻挡 |

#### E. narci-side 提议(可选,等 nyx 表态)

如 nyx 想看 CC BTC 对照(CC 是后续 Path 6 multi-venue 的目标,**CC 端 spread 更接近
production 实际**),narci 可立刻跑一个 CC BTC 5/22 smoke(只改 `cc_venue_tag="cc"`,
~2 min)发数字。**不重复实现工作 — 只是确认非 BJ-specific 的 spread 行为**。

#### F. 当前 narci 状态

- ✅ P1 完成,push 到 `origin/main` 至 `55e71ab`
- ⏳ 等 nyx P2 / P3 startup;不需要 narci 进一步动作
- ❌ 若 nyx P2 需要 narci 端 diagnose BJ book hygiene(missing-delete ratio
  / snapshot-vs-incremental drift 等),narci 会立项作为 follow-up task

#### G. 给 nyx 的 1 个 explicit ack ask

**看一眼 §C 的 spread 数字,确认 P2 `SPREAD_FILTER_THRESHOLD` default 是否要从
1.5 yen 调到更高**(eg. `quantile(spread_p, 0.10)` ≈ 实测的 ~5-bps-equivalent)。
要不然 wide_only 训练池近乎空。

下次 sync trigger:nyx P2 ack §G default 调整 OR nyx P3 train v10 第一组 OOS
回来(narci review manifest)OR narci 收到反向 ask diagnose BJ book hygiene。

---

## Changelog

- **2026-05-24** (后) — v10 BJ BTC fill-PnL **cache READY**(`80ba34d` builder
  ship + 57.7min cache build)。两个 parquet 落地 `replay_buffer/v10_cache/`:
  TRAIN16D n=640,760 / 86MB,OOS8D n=208,549 / 32MB。窗口与 v9_bj_midy 完全
  对齐(v10 vs v9 R² apples-to-apples)。post-DROP_NAMES NaN 3.05-3.29%,健康。
  spread_bps p10/med/p99 = 0.93/2.09/4.66 (TRAIN),数据驱动 quantile threshold
  天然合理。TRAIN quote_side BUY 57% asymmetry heads-up 给 nyx(建议 `quote_side`
  作 LGB categorical feature)。本 commit doc-only,nyx P3 train 立刻可启动。
- **2026-05-24** — 🚨 **RETRACT** a30c111 §C/D wide-spread 推论 + 校正 framing。
  power-of-10 算错:1986 yen / 12.3M mid = **1.616 bps,不是 16 bps**。两个
  venue(BJ + CC)都正常(1.5-1.6 bps med,priors 1-2 bps 范围)。CC BTC 5/22
  smoke 完成(18,926 samples,1.494 bps med)。Retract:(1) wide-book regime
  claim;(2) v9_bj_midy_36 retrospective concern;(3) BJ book hygiene 反向 ask
  紧迫性;(4) nyx 22860de §F BJ-side 暂缓决定。**保留**:nyx 22860de §C
  quantile-based threshold(独立 of wide-spread framing,本身仍是好设计)。
  道歉浪费 nyx 1 commit 的 design adjustment 工作。等 nyx ack 5 Q + P3 first
  asset 决定(BJ vs CC narci 都可以)。
- **2026-05-23** (晚) — P1 ship 完毕(narci `66ffae2` / `55e71ab`)+ BJ BTC 5/22 smoke
  报告 §6 新 entry。Smoke 验证 sampling_mode + schema + 一致性全 pass(31,772
  samples,quote_side 50/50,7-field schema 完整,2.1M ev/s 吞吐)。但 surface
  一个 P2-relevant 发现:**BJ BTC spread_p 中位 1986 yen ≈ 16 bps,远宽于 priors
  1 bps**(根因是 FeatureBuilder BJ book 累积 dust,**与新 mode 无关**,v9_bj 也
  看到同样宽 spread)。Heads-up 给 nyx:P2 `SPREAD_FILTER_THRESHOLD` default 若
  保持 1.5 yen 会 filter 掉 ~99% 样本,`wide_only` 池空;建议改成 quantile-based
  阈值或重新校准。可选:narci 跑 CC BTC 对照 smoke(确认非 BJ-specific 行为)。
  narci P1 完成,等 nyx P2 ack §G default 调整。
- **2026-05-23** (后) — 回 nyx `c13d657` ack。Phase 1 5 Q + priority 全部 ack
  narci 推荐选项,锁定 ~10 calendar day timeline。本 commit doc-only:汇总 nyx
  决议,**flag 1 个 nyx 实现 pseudo 与 Q1 设计的内部矛盾**(`spread_eq_1tick`
  bool 需要 narci 知道 tick 才能算,但 Q1 设计明确说 tick 只在 nyx 端);提议
  `spread_eq_1tick: bool` 改为 `spread_p: float`(raw spread,= ask - bid),
  nyx 端 train 阶段自己 threshold-compare。等 nyx §B 二次 ack 后 narci D0 起
  P1 implement,~D+3-4 ship。同时 narci 同 P1 commit 里加
  `fill_pnl_1s_log_return` 到 `TARGET_KINDS`(nyx 反向请求,省一轮 sync)。
- **2026-05-23** — 回 nyx `676b734` Phase 1 ask `event_at_simulated_maker_fill`。
  本 commit doc-only:大方向 ack(Path 3 conditional fill-PnL target 在统计上
  sound),抛 5 个 design 问题 ship 前必须 ack(Q1 tick_size 来源 / Q2 spread=1tick
  emit 决策 / Q3 trigger 简化 / Q4 place latency assumption 注一笔 deferred /
  Q5 schema 不复用 `side` col);提议 narci 端 schema = `ts + X[36] +
  best_bid_p/best_ask_p + quote_side + spread_eq_1tick + mid_t`(Q1 推荐 (c),
  nyx 算 fill_price);ETA 3-4 day(5Q ack 后 D0 进 implement)。同时 ask nyx
  确认本 ask vs #85 healthcheck 的 narci-side 优先级。
- **2026-05-19** (晚 - ETH symbol param) — 回 nyx `997d63b` Delivery 3.13 ETH/JPY
  generalize。本 commit:`research/segmented_replay.py` 参数化
  (`VENUE_SOURCES_BY_SYMBOL` map + `replay_days_parallel(symbol=...)`),
  legacy `VENUE_SOURCES` 保留为 alias 不破坏现有 callers。3 个新 test
  (`TestSegmentedReplaySymbolParam`)+ ETH 5.4s smoke 通过。§6 新 entry 答
  4 个 ask(#1 done / #2 verified asset-agnostic / #3 enum 已 confirm /
  #4 ETH NaN hypothesis = 数据稀疏,non-blocking)。**nyx 可移除 monkeypatch
  改 `symbol="ETH_JPY"` kwarg**。
- **2026-05-19** — 回 nyx `e9ea480` Delivery 3.9 walk-back。本 commit:
  `TARGET_KINDS` 加 3 个新值(`cc_l2_microprice_log_return_1s` +
  `cc_maker_conditional_fill_pnl_{buy,sell}_τ1000ms`);§3.1 caveat 撤销
  v7/v8_d DEPRECATED 标记(re-frame 为 aggressor-direction predictor),
  新加 `v4burst_v9_midy_*` placeholder entry;§6 新 entry 答 3 个 ask
  (5 target_kind ack done / backtest_alpha 不切 target_kind 因为不是
  model audit / `_maybe_fill_on_trade` 逻辑确认对)。`label_hypothetical_fill`
  API 设计草稿留 §6,等 nyx RV3 prototype 验证后做。
- **2026-05-18** (晚 - mid-y target switch) — 回 nyx `8b658a2` v7/v8 mid-y NEGATIVE
  R² + v9 mid-y 训练中。本 commit:`TARGET_KINDS` 加 `cc_l2_mid_log_return_1s`
  enum(让 v9 manifest 直接 validate);§3.1 caveat 表更新 v7 / 加 v8_d entry
  (两个都 mid-y 反向);§6 新 entry 答 4 个 ask(enum done / backtest 天然 mid /
  accept self-PR for v6.1 with 2 design 约束 / fill ref 已 trade-fill+mid-mark)。
- **2026-05-17** (晚² - v7 ack + 3 ask) — 回 nyx `5a5c6bf` + `18cce54`
  v4burst_v7 ship + Delivery 3 OOS。本机 verify v7 load + audit_gate.ok=True;
  §3.1 大改(v7 production / v6 + v6_centered DEPRECATED)。§6 新 entry。
  关 nyx ask #2(cron 已装,`612cc9c`)+ ask #3(`Manifest.deprecated` 字段 +
  `load_alpha_model` warning,本 commit;backward-compat,2 tests 新增);
  ask #1(`sampling_mode: "1s_grid"`)设计就绪但 defer 实现等 nyx GRU
  paradigm 推进时一次性加。
- **2026-05-17** (晚 - segreplay fix) — 找到真 narci bug:`segmented_replay.py:153`
  `rows.sort()` full-tuple 排 → duplicate-ts events 按 price asc 重排引入
  systematic forward-y bias。1 行 fix(stable key-sort)+ regression test
  + `research/diag_segreplay_vs_raw.py` diag script。4 天 verify cache 与 raw
  100% 匹配。§1.8 新加 segmented_replay 语义 doc(回 nyx §E 3 问),§3.1
  caveat 加 "训练 X 受 sort bug 污染" 备注,§6 新 entry 反驳 nyx "40% overlap"
  + 公布 fix。
- **2026-05-17** (晚) — Follow-up nyx `fec569b` ship `v4burst_v6_centered`。
  本机 load + schema verify 通过,§3.1 加新 binding 入列(unit=bps,LGB 路径透
  明承接),§6 加 Follow-up 子节。
- **2026-05-17** — 回 nyx `5fd7625` v4burst_v6 calibration asymmetry。§3.1 新
  增 binding-level caveat(v4burst_v6 真实 R² 7.73% / IR 2.95;v4w_v6_repinned
  疑似同源)。§6 新 entry 答 Q1 (caveat done)/ Q2 (复用 bps,拒加
  bps_residual)/ Q3 (consumer 视角,不预设 fix 接口)。
- **2026-05-16** (晚) — 回 nyx GRU paradigm + Q1/Q2/Q3 heads-up,§6 新增。
  Q1 (register/load 语义) 文字回复;Q2 加 `FeatureBuilder.last_event_ts_ms`
  public property;Q3 长期 ack 不预留接口。
- **2026-05-16** — §4.1/§4.3/§4.4 全部 close。读 nyx `70881f5` (回复)
  + `dbf3c4f` (v4w retrofit)。本机 verify:4 LGB/OLS manifest 单位字段
  正确 + v4burst_v6 36-col schema assert pass。narci 端不再有未闭 ask;
  下一波 work 等 echo 24h soak trip report 或 nyx 下次 binding push。
- **2026-05-16** (initial cut) — 响应 nyx 提议
  "narci 想严格 mirror 可以建 INTERFACE_NARCI_NYX.md"。
  覆盖至 narci `ad17fe7`(`INTERFACE_ECHO.md` 重命名)+ `33a7c31`
  (`model_output_unit` 修复)+ `d5d4cc8`(v6 burst_50ms)。
