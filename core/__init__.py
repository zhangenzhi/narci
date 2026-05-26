"""narci 核心层(P5):跨层共享的格式/契约/工具,无业务依赖。

 - symbol_spec: SymbolSpec(tick/lot/notional 规格)
 - io:         parquet 读写(含崩溃安全原子写)
 - config:     YAML 配置段加载

core 不得 import recorder/analytics(见 tests/test_layering.py)。
"""
