# narci ↔ narci-reco 交接

> narci = 代码 + 录制器镜像(本仓库)。narci-reco = AWS 运维域(在 aws-jp / aws-sg
> 上跑录制器容器、管 EC2 生命周期 / SSM、donor、cloud-sync;见 `deploy/reco/`)。
> 边界:narci 交付**正确、崩溃安全、可测**的录制管线 + 镜像;reco 负责**部署、
> 长跑、AWS 侧告警/IAM**。reco 自己的拓扑/事故文档在 `deploy/reco/docs/`。

---

## 2026-05-27 交接:录制端重构落地,以下需 reco 行动

narci 这轮把 data 模块重构 + 录制端补全(P0–P5,见 `docs/design/REFACTOR_DESIGN.md`)。
narci 域已全绿(332 passed),但有几项**只能在真实 AWS 主机上落地/验证** —— 交给 reco。

### 1. 录制器落盘改为段式 WAL(P1)—— 需重新部署镜像 + 确认 cloud-sync 范围
- 录制器现把事件先写内存 micro-buffer,每 `wal_flush_interval_sec`(默认 2s)原子落
  到 `replay_buffer/wal/{exchange}/{market}/l2/*.segwal`,`save_interval_sec` 再合并成
  规范 `{SYMBOL}_RAW_*.parquet`。**硬崩溃丢数据窗口 10min → ~秒级**;启动 `recover_orphans()`
  兜底恢复残留段。
- **reco 行动**:
  - [ ] 用新镜像重新部署 aws-jp / aws-sg 录制器(`main.py record` 入口、配置键不变;
        新增可选 `wal_flush_interval_sec` / `wal_fsync`,不填用默认)。
  - [ ] 确认 **cloud-sync 不上传 `replay_buffer/wal/` 树**(它是 `realtime/` 的平级兄弟,
        `.segwal` 扩展名;只同步 `realtime/` 与 `cold/` 的 sidecar 就不会扫到它。若 reco
        是整盘 `replay_buffer/` 同步,请加 `--exclude 'wal/**'`)。

### 2. 包重命名(P5)—— 容器入口不变,但路径引用要核
- 顶层重组为 `core/ contracts/ recorder/ analytics/` 四层。`main.py record` / docker
  `COPY . .` / supervisord 都不受影响(镜像里整仓拷贝)。
- **reco 行动**:[ ] 若 reco 的脚本/配置里有按**模块路径**引用 narci(如 `python -m data.x`、
  `narci.data.*`),按 `docs/design/MIGRATION_P5_IMPORTS.md` 改(`data.*`→`recorder.*`/
  `analytics.*`)。`main.py <subcommand>` 这类不受影响。

### 3. donor 迁入 `deploy/reco/donor/` + check_health 改 linux/CloudWatch —— 需 aws-sg 部署
- donor(下 Binance Vision → rclone 推 `gdrive:narci_official`)已并入 reco 子项目,预期
  跑在 **aws-sg**(它能上 Binance + 自带 rclone→gdrive)。`check_health.sh` 已改 linux 版:
  可移植 `stat`、tmux 判活在无 tmux 时跳过、告警发 CloudWatch 指标 `Narci/Donor/DonorHealthy`。
- **reco 行动**:
  - [ ] 在 aws-sg 上以 systemd-timer / cron 跑 `deploy/reco/donor/donor_loop.sh` + 定期
        `check_health.sh`(`donor_loop.sh` 的 tmux 可换 systemd)。
  - [ ] aws-sg 实例 IAM 角色加 **`cloudwatch:PutMetricData`**(现有 narci-reco 控制面策略
        只有 CW 读权限,见 `deploy/reco/aws/iam/narci-reco-policy.json`)。
  - [ ] 建 **CloudWatch Alarm**(`Narci/Donor/DonorHealthy < 1` 或 missing-data breaching)→ SNS。

### 4. gap 检测 sidecar —— 需纳入监控
- compact 现对**无官方归档 venue**(Coincheck/bitbank/bitFlyer/GMO)写 cold-tier sidecar
  `{SYMBOL}_GAPS_{date}.json`(per-stream gap 区间 + 覆盖率;见 `recorder/gap_detect.py`)。
  这是这些 venue **唯一的丢数据信号**(无 U/u 序列、无官方对账)。
- **reco 行动**:[ ] 把 sidecar 纳入每日完整度核查 —— `coverage_pct` 偏低 / `n_gaps` 多 /
  `max_gap_sec` 大 = 录制有洞,优先级类比 docs/archive/DATA_INTEGRITY 报告。

### 5. 测试 —— narci 验了单测/逻辑;生产长跑/真 AWS 只能 reco 验
- narci 域:332 passed(单测全 mock,不连网)。
- **reco 行动(在真实 aws-sg/aws-jp 主机上)**:
  - [ ] **WAL 生产长跑验证**:连 live WS 跑数日,确认无静默丢数据;模拟一次 SIGKILL
        后重启,确认 `recover_orphans()` 把残留 `.segwal` 段合并成 RAW(narci 沙箱封 WS,
        这条 narci 验不了)。
  - [ ] opt-in 集成测试可在真主机跑:`NARCI_NET_TESTS=1 pytest tests/recorder/test_integration.py`
        (真连 Coincheck WS / Vision)、`NARCI_AWS_TESTS=1 pytest tests/deploy/test_aws_smoke.py`
        (只读 `aws sts`)。

---

## 已验证 / 待 reco 验证 边界

| 项 | narci 验了 | reco 待验 |
|---|---|---|
| WAL 逻辑(原子写/recover/止血修复) | ✅ 单测 + 本地端到端 smoke | 生产长跑 + 真 SIGKILL 恢复 |
| adapter 解析契约 | ✅ 单测(构造样例) | 真 WS 消息(`NARCI_NET_TESTS=1`) |
| Vision verify(SHA-256 修复) | ✅ 单测 + LIVE checksum | 真下载→verify 全链路 |
| gap 检测 | ✅ 单测 + 集成 | 真实数据上的 gap 阈值调参 |
| donor check_health(linux/CW) | ✅ 本机模拟 PASS/FAIL/无 tmux | aws-sg 部署 + IAM + Alarm |
| deploy 配置/IAM | ✅ 静态 + 只读 aws 冒烟 | 真实 ECS/IAM apply |

> 索引:`docs/design/REFACTOR_DESIGN.md`(总设计)、`docs/design/MIGRATION_P5_IMPORTS.md`
> (import 迁移)、`docs/guides/data_recording.md`(WAL 落盘)、`deploy/reco/donor/NARCI_DONOR_INTERFACE.md`。
