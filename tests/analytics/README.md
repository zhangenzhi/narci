# tests/analytics — 分析层顶层模块入口

测 `analytics/` 顶层模块(非子包):
- `test_l2_streaming.py` — `L2Reconstructor` 流式 apply_event 与批处理 process_dataframe 等价。
- `test_sampling.py` — `Sampler`/`FixedGridSampler` 网格 + process_dataframe 行为保持。
