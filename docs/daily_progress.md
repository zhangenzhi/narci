# Daily Progress

Narci 项目每日开发进度记录。

---

## 2026-03-08

**多交易所适配层重构（全面完成）— Coincheck 主力打通 + 架构清理**

- `BaseBroker` ABC：提取 `SpotBroker` / `SimulatedBroker` 共享的 L1/L2 更新、限价 maker 撮合、市价 sweep、账本查询（record_equity/get_trade_history/get_equity_history）；两子类实现各自 `_execute()` / `get_equity()` 决定账本逻辑
- GUI 面板适配多交易所：`panel_l2_insight` / `panel_backtest` 新增 exchange 下拉，自动发现 `realtime/{exchange}/{market}/l2/` 目录；`panel_cold_db` symbol 解析用 `_RAW_` 切分，兼容 Coincheck 含下划线的 `ETH_JPY` 命名
- 配置重组：`configs/exchanges/{binance_spot,binance_um_futures,coincheck_spot}.yaml` 为规范路径，旧文件名保留 symlink 向后兼容
- CLAUDE.md 重写：新交易所抽象层架构、历史数据源层、Docker 三容器拓扑、数据格式契约全部入文档

- Coincheck 实盘实测：发现 WS trade 推送真实格式是 list-of-lists（`[[ts_s, id, pair, rate, amount, type, ...], ...]`），单消息可批量携带多笔成交，与之前按文档猜测的格式不同
- 修复 `CoincheckAdapter.parse_message`/`standardize_event`：正确识别批量 trades，一条消息可产出多条 `side=2` 记录；用 BTC_JPY 2 分钟窗口实测抓到 64 笔成交
- 修复 `L2Recorder._handle_shutdown`：不再用 `loop.stop()` 硬停 gather，改为事件驱动优雅退出（无 `RuntimeError: Event loop stopped`）
- 重构 `data/daily_compactor.py`：通过 `HistoricalSource` 注入交叉校验源，Coincheck 等无官方归档的交易所自动跳过
- `main.py cmd_compact`：支持新旧目录结构（`realtime/{exchange}/{market}/l2/` 与 `realtime/{market}/l2/`），按路径推断 exchange 决定是否启用 Binance Vision 校验

- Coincheck adapter 骨架完成：WS URL / REST 快照 / 订阅消息 / 消息解析（orderbook + trades 数组格式）/ 符号转换
- `ExchangeAdapter.subscribe_messages()`：新增基类方法，支持 Coincheck 这类「连接后主动订阅」的交易所
- `L2Recorder.record_stream()`：连接成功后自动发送订阅消息；`_process_depth_event()` 跳过 U/u 去重当 adapter 不需要对齐
- 新增 `configs/coincheck_recorder.yaml`：7 对主流 JPY 现货（BTC/ETH/XRP/SOL/DOGE/AVAX/BCH）
- 新增 `recorder-coincheck` Docker 容器（宿主机端口 8081）
- 新增 `SpotBroker`：现货撮合引擎，无杠杆/无做空/maker=taker=0，适配 Coincheck JPY 对零费率
- 服务器别名新增 `nrestartcc/nlogcc/nhealthcc/nshellcc`

- 新增 `data/exchange/`：`base.py`（ExchangeAdapter ABC）+ `binance.py`（现货/合约）+ `coincheck.py`（骨架，含 WS/REST/消息解析/符号转换）
- 重写 `data/l2_recorder.py` 为交易所无关，保留别名 `BinanceL2Recorder`；落盘路径加 exchange 级目录
- 新增 `data/historical/`：`base.py`（HistoricalSource ABC）+ `binance_vision.py`（含 MD5 校验）+ `tardis.py`（整合 L2 depth）
- 重写 `data/download.py` 为 `HistoricalDownloader`：委托给 HistoricalSource，可配置 `source: binance_vision | tardis`
- 精简 `data/validator.py`：移除 Binance 特有 checksum 逻辑（下沉至 source.verify()），保留通用 DataFrame 校验
- 双容器 Docker 编排：`recorder-spot`（24 个 JPY 对）+ `recorder-umfut`（6 个 USDT 合约）+ `cloud-sync` sidecar
- 服务器别名更新：`nlogspot/nlogumfut/nhealthspot/nhealthumfut` 等分别管理

**第三方 L2 Depth 数据接入与转换管线**

- 新增 `data/tardis_downloader.py`：从 Tardis.dev 下载 L2 depth 数据（depthSnapshot + depth incremental），直接转换为 Narci 4 列 RAW 格式
- 新增 `data/format_converter.py`：合并 Tardis depth 与 Binance Vision aggTrades 为统一 RAW parquet，支持 nyx L2Reconstructor -> FV5 全 51 特征管线
- `main.py` 新增 `tardis` 和 `merge` 子命令
- 新增 `configs/tardis.yaml` 使用说明
- 完整管线：`download` (aggTrades) -> `tardis` (L2 depth) -> `merge` (合并) -> L2Reconstructor -> FV5

## 2026-03-07

**Binance 历史数据下载器扩展 + 云同步解耦与部署优化**

- 扩展 `data/download.py` 支持 spot + um_futures 双市场、6 个交易对 (BTC/ETH/SOL/BNB/XRP/DOGE)、6 个月日期范围
- 更新 `configs/downloader.yaml`：`market_type: "both"`，`start_date: "2025-09-01"`，`end_date: "auto"`

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
