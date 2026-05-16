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

---

## Changelog

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
