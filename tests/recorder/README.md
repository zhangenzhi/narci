# tests/recorder — 录制器模块的行为与稳健性入口

这个目录是**读懂 recorder 模块的入口**:不必先读 611 行的 `data/l2_recorder.py`,
先看这里的测试就能知道录制器**保证什么、在什么故障下不丢数据**。

录制器的职责:WebSocket 多交易所 L2 + 成交捕获 → 段式 WAL 崩溃安全落盘 →
定时合并成规范 `{SYMBOL}_RAW_*.parquet`。对应被测代码:
`recorder/l2_recorder.py`、`recorder/wal.py`、`recorder/exchange/*`、
`recorder/{validator,daily_compactor,sanity_gate,live_publisher,backfill_*}.py`、
`deploy/healthcheck.py`。

## 每个测试锁定的保证

**功能正确性(数据契约)**

| 测试文件 | 保证什么 |
|---------|---------|
| `test_exchange_adapters.py` | adapter 的 **side 编码契约**:Binance/Coincheck 的 depth→0/1、aggTrade→2(卖方主动 = 负 qty)、snapshot→3/4;Binance U/u 对齐窗口边界;Coincheck list-of-lists 成交解析(秒→ms、sell 负量、坏行跳过)、orderbook/trade 消息路由;符号转换。**一个解析 bug = 静默脏数据** |
| `test_validator.py` | `DataValidator` 质量规则:空 df / 价格≤0 判废;时间戳单位推断(s/ms);日期不匹配、单位错误(年份>3000)检出;stats 填充 |

**录制 / 落盘稳健性**

| 测试文件 | 保证什么 |
|---------|---------|
| `test_segment_wal.py` | 段式 WAL(`recorder/wal.py`):append/flush/harvest 往返一致;flush **原子**(写中途崩溃不留半个有效段);`recover_orphans` 把残留段合并成 RAW;`.segwal` 段对 `*.parquet` 扫描隐形 |
| `test_l2_recorder_wal.py` | 录制器接入 WAL 的三个止血修复:**未对齐 symbol 的 trade 仍落盘**;对齐成功后**不丢后续事件**;**熔断 `os._exit` 前先 flush** micro-buffer |
| `test_l2_recorder_refresh.py` | save_loop 的 REST 重拉盘口:成功原子换 book;失败回落旧 book;重拉期间并发事件进 pre_align_buffer 不污染待换 book;watchdog 静默重连阈值 |
| `test_alignment_circuit_breaker.py` | U/u 对齐循环熔断:重试/耗时阈值触发,避免 28h 静默僵死 |
| `test_healthcheck_per_symbol.py` | per-(venue,symbol) staleness 健康检查:静默 channel 死亡能被探测 |
| `test_daily_compactor_*` / `test_sanity_gate` / `test_live_publisher` / `test_backfill_vision_bookticker` | 日级聚合(corrupted shard 隔离 / 归档守卫)、sanity 黑名单、实时发布、Vision 回填 |

## 怎么跑

```bash
pytest tests/recorder              # 只跑录制器
pytest                              # 全套(testpaths = tests)
```

> 测试按模块组织在顶层 `tests/<module>/`(contracts/calibration/features/research/
> analytics/simulation/recorder),各带 README 作模块入口。见
> `docs/design/REFACTOR_DESIGN.md §9`。
