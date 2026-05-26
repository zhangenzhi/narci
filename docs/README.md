# docs/ 索引

按性质分四类。**活契约**留在 docs/ 根(被 echo/nyx 等外部仓库按路径引用,位置稳定);
其余按用途进子目录。

## 活契约(根)—— 改动 = 改跨仓接口,需同步 echo/nyx
| 文件 | 是什么 |
|---|---|
| `INTERFACE_NARCI_ECHO.md` | narci↔echo(实盘)接口契约:echo 消费哪些 `narci.*` 符号 |
| `INTERFACE_NARCI_NYX.md` | narci↔nyx(训练)接口契约 |
| `CALIBRATION_PROTOCOL.md` | echo 会话日志 → narci 模拟器校准协议 |
| `ECHO_RAW_L2_SIDECAR_SPEC.md` | echo 进程内 raw L2 sidecar 落盘规格 |

> P5 物理分层后,这些文档里的 `narci.*` import 路径已更新(见
> `docs/design/MIGRATION_P5_IMPORTS.md`);echo/nyx 仓需按该清单同步。

## guides/ —— how-to(随代码保持最新)
| 文件 | 是什么 |
|---|---|
| `data_recording.md` | 录制器(`recorder/`):WS 采集 + 段式 WAL 落盘 + 对齐 |
| `backtesting.md` | 回测/撮合:`analytics/simulation.MakerSimBroker` + `calibration` |
| `visualization.md` | GUI 仪表盘(`analytics/gui/`,4 tab) |

## design/ —— 重构/过程文档
| 文件 | 是什么 |
|---|---|
| `REFACTOR_DESIGN.md` | data 模块重构主设计(P0–P5 路线图 + 决策 + 落地记录) |
| `MIGRATION_P5_IMPORTS.md` | P5 物理分层后 echo/nyx 的 `narci.*` import 迁移清单 |

## archive/ —— 时点报告 / 陈旧流水账(不维护)
| 文件 | 是什么 |
|---|---|
| `DATA_INTEGRITY_v10_BJ_WINDOW_2026-05-24.md` | v10 窗口数据完整度的 point-in-time 快照 |
| `daily_progress.md` | 早期开发流水账(止于 2026-04-16) |

> 另:`deploy/reco/docs/`(TOPOLOGY / INCIDENTS)是 reco 运维子项目自己的文档树。
