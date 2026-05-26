# tests/simulation — 撮合引擎(MakerSimBroker)的行为入口

这是**读懂主线撮合引擎的入口**。narci 的**唯一**回测/模拟撮合是
`simulation/maker_broker.py` 的 `MakerSimBroker`(P4 收敛后,遗留的
`backtest/broker.py` naive 撮合已删)。它是 echo 实盘 broker 的参考实现 ——
`calibration/replay.py` 用它的输出与 echo 真实成交对账校准。

被测代码:`simulation/maker_broker.py`、`simulation/backtest_alpha.py`(报价策略)。

## 撮合引擎保证什么(逐项锁定)

队列与成交(`test_maker_broker.py`):

| 保证 | 测试 |
|------|------|
| book 双边就绪后才可成交 | `test_book_seed_makes_state_ready` |
| 限价单被同价成交流按 **FIFO 队列**消耗后成交 | `test_place_then_filled_by_trade` |
| 队列前量**只在 book 减少时前进**,增加不前进 | `test_book_qty_{decrease_advances,increase_does_not_advance}_queue` |
| **价格穿透成交**:taker 打穿我方价位 → 以我方价成交(P3.1) | `test_buy_filled_by_penetrating_trade_below_quote`、`test_sell_filled_by_penetrating_trade_above_quote` |
| 成交流走在我方不利方向 → **不成交** | `test_no_fill_when_trade_in_our_favor` |
| 穿透**不与剩余队列量重复成交** | `test_penetration_does_not_double_fill_with_remaining_quantity`、`test_same_price_queue_path_unchanged` |

下单约束 / 库存(现货语义):

| 保证 | 测试 |
|------|------|
| `post_only` 拒绝穿价单 | `test_post_only_rejects_cross` |
| 现货只多不空:库存不足拒卖 | `test_spot_only_inventory_blocks_short_sell`、`test_sell_after_buy_works` |
| 并发卖单**预留库存**,防超卖(echo 0510 incident) | `test_concurrent_sells_reserve_inventory` |
| tick / min-notional 校验 | `test_validate_rejects_bad_tick`、`test_min_notional_enforced` |

撤单(延迟与竞态):

| 保证 | 测试 |
|------|------|
| 撤单有 **p50 延迟**,ack 前不生效 | `test_cancel_with_latency` |
| 撤单窗口内被成交 → 成交优先(保守 PnL) | `test_filled_during_cancel` |
| 撤未知 / 重复撤 返回 False | `test_cancel_unknown_oid_returns_false`、`test_double_cancel_returns_false` |

报价策略(`test_backtest_quote_strategy.py`):`join_back` vs `improve_1_tick`
的定价与回退(CC echo 生产绑定 `improve_1_tick`)。

## venue → 撮合(P4 矩阵)

`MakerSimBroker` 内部用 `L2Reconstructor`(`prune_snapshot_dust=True`),
对**全快照型** venue(CC/BJ/GMO/bitFlyer)在每个快照批关闭时清跨档脏量,
对**增量型** venue(Binance)按增量更新 —— 同一撮合内核覆盖两类 venue,
不分叉。

## 怎么跑

```bash
pytest tests/simulation        # 撮合引擎
pytest                          # 全套
```

> 注:`calibration/tests/test_maker_smoke.py` 是基于真实 cold-tier 的**手动**
> 全天 smoke 脚本(`__main__` 运行,非单测),暂留原处,后续随分析层测试迁移再归位。
> 见 `docs/REFACTOR_DESIGN.md §9`(P4.5 测试提升)。
