# tests/recorder — 录制器模块的行为与稳健性入口

这个目录是**读懂 recorder 模块的入口**:不必先读 611 行的 `data/l2_recorder.py`,
先看这里的测试就能知道录制器**保证什么、在什么故障下不丢数据**。

录制器的职责:WebSocket 多交易所 L2 + 成交捕获 → 段式 WAL 崩溃安全落盘 →
定时合并成规范 `{SYMBOL}_RAW_*.parquet`。对应被测代码:
`data/l2_recorder.py`、`data/wal.py`、`data/exchange/*`、`deploy/healthcheck.py`。

## 每个测试锁定的保证

| 测试文件 | 保证什么 |
|---------|---------|
| `test_segment_wal.py` | 段式 WAL(`data/wal.py`):append/flush/harvest 往返一致;flush **原子**(写中途崩溃不留半个有效段);`recover_orphans` 把残留段合并成 RAW;`.segwal` 段对 `*.parquet` 扫描隐形 |
| `test_l2_recorder_wal.py` | 录制器接入 WAL 的三个止血修复:**未对齐 symbol 的 trade 仍落盘**(不再内存堆积+全丢);对齐成功后**不丢后续事件**;**熔断 `os._exit` 前先 flush** micro-buffer |
| `test_l2_recorder_refresh.py` | save_loop 的 REST 重拉盘口:成功原子换 book;失败回落旧 book 继续录制;重拉期间并发事件进 pre_align_buffer 不污染待换 book;watchdog 静默重连阈值 |
| `test_alignment_circuit_breaker.py` | U/u 对齐循环熔断:重试/耗时阈值触发,避免 28h 静默僵死 |
| `test_healthcheck_per_symbol.py` | per-(venue,symbol) staleness 健康检查:静默 channel 死亡能被探测 |

## 怎么跑

```bash
pytest tests/recorder              # 只跑录制器
pytest                              # 全套(testpaths = tests + calibration/tests)
```

> P4.5 进行中:测试正从历史的 `calibration/tests/` 按模块提升到顶层 `tests/`。
> recorder 因 P1 已稳定先行;analytics/core 测试随 P4/P5 迁移。新写的录制器测试
> 放这里。见 `docs/REFACTOR_DESIGN.md §9`。
