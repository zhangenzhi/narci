"""narci.contracts —— 对外契约层(P5).

narci 暴露给 echo(实盘)/ nyx(训练)的跨仓库契约结构与常量,纯定义、无重依赖:
  - schema:   echo 事件 DTO(Decision/Fill/Cancel)+ RAW_L2_SCHEMA + SCHEMA_VERSION
  - manifest: nyx 模型 manifest 契约(SAMPLING_MODES/TARGET_KINDS/Manifest/...)
  - features: 特征契约(FEATURE_NAMES 有序列表 + FEATURES_VERSION)

改动这里 = 改动跨仓接口,需同步 echo/nyx 与 docs/INTERFACE_NARCI_*.md。
"""
