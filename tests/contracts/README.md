# tests/contracts — 对外契约层(echo/nyx 边界)入口

测 `contracts/`(echo/nyx 跨仓库消费的契约):
- `test_schema.py` — 事件 DTO(Decision/Fill/Cancel/SessionMeta)+ RAW_L2_SCHEMA 往返/校验。
- `test_contract_snapshots.py` — **逐字冻结** FEATURE_NAMES/FEATURES_VERSION、SAMPLING_MODES/
  TARGET_KINDS/MODEL_OUTPUT_UNITS、SCHEMA_VERSION + 各事件 arrow 字段。漂移即红 → 逼版本 bump + 通知 echo/nyx。
- `test_writers.py` — `EchoLogWriter`(产出契约事件 parquet;与 schema 同放因共用样例事件 helper)。
