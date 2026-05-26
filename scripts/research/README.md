# scripts/research/ — 研究/缓存构建脚本(非包,非发布代码)

`scripts/` 脚本区的 research 子目录:一次性调查 + nyx 训练缓存构建工具。**不是
narci 包的组成部分**,不参与分层 lint,不被 pytest 收集。脚本用绝对 import
(`analytics.*` / `recorder.*` / `contracts.*`),从仓库根运行。

当前内容(均被 `docs/INTERFACE_NARCI_NYX.md` / `DATA_INTEGRITY_v10_*` 按 commit 引用为
provenance/工具):
- `build_v10_{cc,bj}_btc_cache.py` — 为 nyx P3/v10 构建 fill-PnL 训练样本缓存(经 `analytics/segmented_replay.py` 并行重放)。
- `data_integrity_v10_window.py` — v10 窗口的数据完整度扫描(产出 DATA_INTEGRITY 报告)。
- `diag_segreplay_vs_raw.py` / `diag_v10_trade_rate_bucket.py` — 支撑 nyx 决策的诊断脚本。

已结案、结论已沉淀到代码注释/文档的一次性脚本(p0_nan_bisect/profile/lead_lag/ols_e2e)
已删除(git 历史可恢复)。
