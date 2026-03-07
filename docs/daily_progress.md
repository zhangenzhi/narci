# Daily Progress

Narci 项目每日开发进度记录。

---

## 2026-03-07

**云同步解耦与部署优化**

- 将 rclone 云同步从录制器中完全剥离，新建 `data/cloud_sync.py` 独立守护进程
- `docker-compose.yaml` 改为双容器架构：recorder (纯写盘) + cloud-sync (rclone 官方镜像 sidecar)，通过 named volume 共享数据
- Dockerfile 移除 rclone 安装，recorder 镜像更精简
- `deploy/entrypoint.sh` 移除 rclone 配置生成逻辑
- `main.py` 新增 `cloud-sync` 子命令，支持非 Docker 环境独立运行
- 更新 `deploy/server-aliases.sh`：新增 `nsynclog`/`nsyncrestart` 等云同步管理命令，移除过时的 `ncompact`/`nsync`
- 修复 `npull` 别名：加入 `--no-cache` 强制重建 + 自动 `source` 重载别名
- 修复 Google Drive API 限流问题：rclone 加入 `--tpslimit 8` 和 `--transfers 2` 限速参数

## 2026-03-06

**云端录制架构重构**

- 重构为云端录制专用架构：10 分钟落盘周期 + rclone 即时推送至 Google Drive
- 新增 `retain_days` 自动清理机制，删除超过 N 天的本地 parquet 文件
- 容器启动时自动创建 Google Drive 远端目录

## 2026-03-05

**Docker 部署与录制器稳定性**

- 新增 Docker/ECS 部署方案：Dockerfile、docker-compose、supervisord、healthcheck
- 新增 `deploy/server-aliases.sh` 服务器运维快捷命令集
- 修复录制器快照失败时影响整个 WebSocket 连接的问题，改为按交易对独立重试
- 切换至现货市场 USDT + JPY 交易对
- 测试 Binance Japan API 端点后回退至标准端点（日本 IP 无限制）
- 使用 rclone 官方安装脚本替代手动安装
- 新增 `data/third_party.py` CryptoChassis 第三方数据管道

## 2026-03-02

- 新增现货 JPY 交易对录制器配置

## 2026-02-27

- 核心功能基本完成：录制、重构、回测、GUI 全链路打通

## 2026-02-26

- L2 逐笔成交重构特征工程 (`feature_builder.py`)
- 回测引擎重构，增加 JIT 盘口重构与特征缓存

## 2026-02-25

- 重构回测引擎 (`BacktestEngine` / `JitBacktestEngine`) 与撮合引擎 (`SimulatedBroker`)
- 重写现货/合约录制器，统一 WebSocket 多流架构
- 合并 L2 盘口重构模块

## 2026-02-24

- GUI 冷数据仓库面板 (`panel_cold_db.py`)
- GUI L1/L2 重构可视化面板调试

## 2026-02-23

- GUI 盘口重构面板开发 (`panel_l2_insight.py`)

## 2026-02-22

- 添加 Binance 账户配置

## 2026-02-21

- L1/L2 全链路测试完成
- 回测策略框架 (`BaseStrategy`) 与示例策略
- 更新 README

## 2026-02-18

- 录制器测试
- GUI 框架搭建 (`dashboard.py`)
- 数据下载器配置 (`downloader.yaml`)

## 2026-02-13

- 项目模块化拆分
