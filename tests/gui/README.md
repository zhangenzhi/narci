# tests/gui — GUI 数据层入口

测 `analytics/gui/` 里**纯数据层**(不测 Streamlit 渲染):
| 测试 | 保证什么 |
|---|---|
| `test_recorder_health.py` | `recorder_health` 的三信号扫描:freshness(最新 RAW 年龄/stale 判定/shard 计数,排除 DAILY)、gap_reports(读 cold-tier {SYMBOL}_GAPS_*.json)、wal_backlog(.segwal 残留计数)、health_summary(GREEN/RED) |

面板渲染(`panel_*.py`)是薄 Streamlit 层,逻辑都下沉到可测数据层。
