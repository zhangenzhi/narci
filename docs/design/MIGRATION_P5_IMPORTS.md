# P5 物理分层 —— echo / nyx import 迁移清单

> 2026-05-26 · narci 把扁平模块重组为四层物理包 `core/ contracts/ recorder/
> analytics/`。这是**跨仓库 breaking change**:echo 与 nyx 里写死的 `narci.*`
> import 路径需要按下表机械替换。narci 侧已全绿(240 tests),但 narci 无法
> 改/验证 echo 与 nyx —— 请在那两个仓库各做一次替换并跑各自测试。

## 一句话
- **对外契约**(DTO / 枚举 / 特征名)→ `narci.contracts.*`
- **分析/模拟/校准/特征实现** → `narci.analytics.*`
- **录制/采集/历史/策展** → `narci.recorder.*`
- **共享底座**(SymbolSpec / parquet IO / config)→ `narci.core.*`

## old → new(逐条替换)

| 旧路径(echo/nyx 当前) | 新路径 |
|---|---|
| `narci.calibration.schema` | `narci.contracts.schema` |
| `narci.calibration.alpha_models` 的 `SAMPLING_MODES` / `TARGET_KINDS` / `MODEL_OUTPUT_UNITS` / `MANIFEST_SCHEMA_VERSION` / `Manifest` | `narci.contracts.manifest` |
| `narci.calibration.alpha_models` 的 `load_alpha_model` / `AlphaModel` / `OLS/LGB/GRUAlphaModel` / `register` | `narci.analytics.calibration.alpha_models` |
| `narci.calibration.writers`(`EchoLogWriter`) | `narci.analytics.calibration.writers` |
| `narci.calibration.replay`(`calibrate_session` / `compute_reference_y`) | `narci.analytics.calibration.replay` |
| `narci.calibration`(包顶层 re-export) | `narci.analytics.calibration` |
| `narci.features` 的 `FeatureBuilder` | `narci.analytics.features` |
| `narci.features` 的 `FEATURE_NAMES` / `FEATURES_VERSION` | `narci.contracts.features`(权威;`narci.analytics.features` 亦 re-export) |
| `narci.simulation`(`MakerSimBroker` / `backtest_alpha` / `backtest_alpha_model`) | `narci.analytics.simulation` |
| `narci.data.l2_reconstruct`(`L2Reconstructor`) | `narci.analytics.l2_reconstruct` |
| `narci.data.sampling` | `narci.analytics.sampling` |
| `narci.data.l2_recorder` / `wal` / `exchange` / `historical` / `download` / `daily_compactor` / `validator` / `sanity_gate` / `live_publisher` / `cloud_sync` / ... | `narci.recorder.<同名>` |

## 机械替换建议(在 echo / nyx 仓库内)
按"长前缀优先"顺序 sed,避免误伤(如 `narci.calibration.schema` 必须在
`narci.calibration` 之前替换):

```
narci.calibration.schema            → narci.contracts.schema
narci.calibration.writers           → narci.analytics.calibration.writers
narci.calibration.replay            → narci.analytics.calibration.replay
narci.calibration.alpha_models      → (见下:符号二分)
narci.calibration                   → narci.analytics.calibration
narci.features                      → narci.analytics.features   (FEATURE_NAMES/版本可改指 narci.contracts.features)
narci.simulation                    → narci.analytics.simulation
narci.data.l2_reconstruct           → narci.analytics.l2_reconstruct
narci.data.sampling                 → narci.analytics.sampling
narci.data                          → narci.recorder
```

`narci.calibration.alpha_models` 需按符号二分:契约枚举/`Manifest` 去
`narci.contracts.manifest`,加载器(`load_alpha_model` 等)去
`narci.analytics.calibration.alpha_models`。

## 校验
替换后在 echo / nyx 各跑:`python -c "import narci.analytics.calibration, narci.contracts.schema, narci.contracts.manifest, narci.contracts.features"`(PYTHONPATH 指向 narci 父目录),应无 ImportError。
