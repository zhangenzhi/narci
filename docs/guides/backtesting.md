# 回测 / 撮合

> 2026-05-26 重写:旧的 `backtest/`(`BacktestEngine`/`JitBacktestEngine`/
> `SimulatedBroker`/naive 撮合)在 P4 已删除。回测现在统一走主线撮合引擎
> `MakerSimBroker`。本文描述当前状态。

## 唯一撮合引擎:`analytics/simulation/maker_broker.py` → `MakerSimBroker`

narci 只有**一个** maker 撮合实现,它同时是 echo 实盘 broker 的参考实现
(`calibration/replay` 用它的输出与 echo 真实成交对账)。撮合保证(逐项有测试,
见 `tests/simulation/`):

- **FIFO 队列**:限价单被同价成交流按 `queue_ahead_qty` 消耗后才成交;队列前量
  只在 book 减少时前进。
- **价格穿透成交**:taker 打穿我方价位 → 以我方价成交(P3.1)。
- **post_only / 现货库存**:拒穿价单;现货只多不空,并发卖单预留库存防超卖。
- **撤单延迟**:p50 延迟模型,ack 前不生效;撤单窗口内被成交则成交优先。
- **venue→撮合**:增量型(Binance)与全快照型(CC/BJ/GMO/bitFlyer)同一内核
  覆盖 —— 内部用 `analytics/l2_reconstruct.L2Reconstructor`(`prune_snapshot_dust`)。

## 跑一次回测:`analytics/simulation/backtest_alpha.py`

`backtest_alpha_model(model_path, days, symbol, ...)` —— 把一个 `AlphaModel`
(经 `calibration.alpha_models.load_alpha_model` 加载)在 cold-tier 历史上重放,
经 `MakerSimBroker` 下单/成交,产出 daily PnL / sharpe / fills。报价策略
`join_back` vs `improve_1_tick`(CC echo 生产绑定后者)。

```python
from analytics.simulation.backtest_alpha import backtest_alpha_model
res = backtest_alpha_model(model_path="…/manifest.json", days=["20260517"],
                           symbol="BTC_JPY")
```

## 与实盘对账:`analytics/calibration/replay.py`

`calibrate_session(echo_session_dir)` 把 `MakerSimBroker` 重放 echo 的真实
decisions/cancels,与 echo 实盘 fills 对账,产出 `CalibrationReport`
(verdict: HEALTHY / ACCEPT_WITH_TUNING / UNHEALTHY)+ 参数建议回喂 nyx。这是
撮合改动的回归门禁。

## 数据来源

cold-tier(`replay_buffer/cold/{exchange}/{market}/*_DAILY.parquet`)经
`analytics/l2_reconstruct` 重建 + `analytics/sampling` 采样;特征由
`analytics/features/realtime.FeatureBuilder` 计算(`FEATURES_VERSION` 契约)。
nyx 训练缓存由 `scripts/research/build_v10_*` 经 `analytics/segmented_replay` 并行构建。

> GUI 没有回测面板(P4 删除);回测经 CLI/notebook 调上述函数。
