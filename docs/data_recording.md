# 数据录制系统

Narci 的数据录制系统负责从 Binance 实时采集 L2 订单簿深度与 L1 逐笔成交数据，并提供离线历史数据下载、日级归档聚合与交叉验证能力。

---

## 架构概览

```
数据录制系统
├── l2_recorder.py      # WebSocket 实时高频录制引擎 (核心)
├── cloud_sync.py       # 独立云同步守护进程 (rclone)
├── l2_reconstruct.py   # L2 盘口状态重构引擎 (Reconstructor)
├── download.py         # 离线历史数据批量下载器
├── daily_compactor.py  # 日级归档聚合与官方交叉验证
├── validator.py        # 数据质量校验工具
└── third_party.py      # 第三方数据源管道 (CryptoChassis)
```

---

## 1. L2 高频录制器 (`l2_recorder.py`)

### 1.1 功能定位

通过单一 WebSocket 连接并发订阅多个交易对的 **100ms 级深度增量 + 逐笔成交 (aggTrade)** 流，实现毫秒级盘口数据的持续采集与落盘。

### 1.2 核心类：`BinanceL2Recorder`

**初始化参数**：
- `config_path`: YAML 配置文件路径，默认 `configs/um_future_recorder.yaml`
- `symbol`: 可选，临时覆盖配置，仅录制单一交易对

**配置文件字段** (`recorder` 节点下)：
| 字段 | 说明 | 默认值 |
|------|------|--------|
| `symbols` | 交易对列表 | `['ETHUSDT']` |
| `market_type` | 市场类型 (`spot` / `um_futures`) | `um_futures` |
| `interval_ms` | 深度推送间隔(ms) | `100` |
| `save_interval_sec` | 落盘周期(秒) | `600` |
| `save_dir` | 数据存储根目录 | `./replay_buffer/realtime` |
| `retain_days` | 自动清理天数 (0=不清理) | `7` |

### 1.3 数据编码协议 (Side 编码)

录制器将所有事件统一编码为 `[timestamp, side, price, quantity]` 四列结构：

| Side | 含义 | 来源 |
|------|------|------|
| 0 | Bid 增量更新 (买盘挂单变化) | `depthUpdate` 事件 `b` 字段 |
| 1 | Ask 增量更新 (卖盘挂单变化) | `depthUpdate` 事件 `a` 字段 |
| 2 | L1 逐笔成交 | `aggTrade` 事件，quantity 为负表示主动卖出 |
| 3 | Bid 全量快照 | REST API 快照 / 落盘时内存状态机注入 |
| 4 | Ask 全量快照 | REST API 快照 / 落盘时内存状态机注入 |

### 1.4 核心机制

#### 深度流对齐 (Snapshot-Stream Alignment)

WebSocket 深度增量流必须与 REST 快照的 `lastUpdateId` 对齐后才能正确使用。录制器的对齐流程：

1. 连接 WebSocket 后，先将深度事件缓存到 `pre_align_buffer`
2. 异步请求 REST 全量快照，获取 `lastUpdateId`
3. 在缓存中找到满足 `U <= lastUpdateId + 1 <= u` 的事件
4. 从该事件开始正式处理深度更新

#### 内存盘口状态机 (In-memory Orderbook)

录制器在内存中维护每个交易对的最新盘口状态 (`self.orderbooks`)，作用是：

- 每次落盘时（每 60 秒），先原子提取当前 buffer 数据
- 立即将内存中最新的 orderbook 打包为 side=3/4 的快照事件
- 注入到下一个周期的 buffer 开头

这保证了每个 1min parquet 文件都是**自包含的** (Self-contained)——任何单个文件都以全量快照开头，可以独立重建完整盘口。

#### 落盘策略

- 每 `save_interval_sec` (默认 600 秒 = 10 分钟) 执行一次异步写盘
- 文件命名格式：`{SYMBOL}_RAW_{YYYYMMDD}_{HHMMSS}.parquet`
- 存储路径：`{save_dir}/{market_type}/l2/`
- 使用 PyArrow + Snappy 压缩
- 每日产生 144 个文件 (vs 原 1440 个)，每个文件是独立的并行重建单元

#### 本地文件自动清理

配置 `retain_days` 后，录制器在每次落盘后自动删除超过指定天数的本地 parquet 文件：

- 基于文件修改时间判断，与文件名无关
- 仅清理 `save_dir` 下的 `.parquet` 文件
- 配合 rclone 推送使用：数据已上云后释放本地磁盘
- 设为 `0` 禁用清理 (默认 `7` 天)

### 1.5 使用方式

```bash
# 默认配置启动 (U本位合约)
python main.py record

# 指定配置文件 (现货)
python main.py record --config configs/spot_recorder.yaml

# 临时覆盖为单币种
python main.py record --symbol DOGEUSDT
```

### 1.6 断线重连

连接异常时自动重连（间隔由 `retry.wait_seconds` 配置），重连后：
- 重置所有交易对的对齐状态
- 清空内存状态机
- 重新请求快照并执行对齐流程

### 1.7 优雅关闭 (Graceful Shutdown)

录制器支持信号级安全关闭，适配容器环境 (ECS SIGTERM)：

- 注册 `SIGTERM` / `SIGINT` 信号处理器
- 收到信号后立即停止录制循环
- 调用 `_flush_all_buffers()` 将所有交易对的内存缓冲区强制落盘
- 确保无数据丢失后退出进程

---

## 2. 独立云同步守护进程 (`cloud_sync.py`)

### 2.1 功能定位

与录制器完全解耦的独立进程，定时将本地数据目录通过 `rclone copy` 推送至远端存储（如 Google Drive）。录制器只负责写盘，云同步只负责上传，两者互不阻塞。

### 2.2 核心类：`CloudSyncDaemon`

**参数**：
| 参数 | 说明 | 默认值 |
|------|------|--------|
| `local_dir` | 本地待同步目录 | `/app/replay_buffer/realtime` |
| `remote` | rclone 远端路径 | 环境变量 `NARCI_RCLONE_REMOTE` |
| `interval` | 同步间隔（秒） | `300` |
| `transfers` | rclone 并发上传数 | `4` |

### 2.3 运行机制

- 定时循环执行 `rclone copy`，使用 `--no-traverse` 跳过远端目录扫描
- 单次同步设有超时保护（等于 `interval` 时长），超时则跳过本轮
- 支持 `SIGTERM` / `SIGINT` 信号优雅退出（可中断的 sleep）
- rclone 未安装时记录错误日志但不崩溃

### 2.4 部署方式

**Docker sidecar（推荐）**：

在 `docker-compose.yaml` 中，`cloud-sync` 服务使用 `rclone/rclone` 官方镜像，通过共享的 named volume (`narci-data`) 以只读方式挂载 recorder 的数据目录：

```yaml
services:
  recorder:
    volumes:
      - narci-data:/app/replay_buffer    # 读写
  cloud-sync:
    image: rclone/rclone:latest
    volumes:
      - narci-data:/data:ro              # 只读
```

环境变量配置：
| 变量 | 说明 |
|------|------|
| `NARCI_RCLONE_REMOTE` | rclone 远端路径（如 `gdrive:/narci_raw`） |
| `RCLONE_GDRIVE_TOKEN` | Google Drive OAuth token JSON |
| `RCLONE_GDRIVE_FOLDER_ID` | Google Drive 目标文件夹 ID |
| `SYNC_INTERVAL` | 同步间隔秒数（默认 300） |

**非 Docker 独立运行**：

```bash
# 通过 main.py 子命令
python main.py cloud-sync --remote gdrive:/narci_raw --interval 300

# 直接运行模块
python -m data.cloud_sync --local-dir ./replay_buffer/realtime --remote gdrive:/narci_raw
```

### 2.5 解耦优势

- **录制零干扰**：rclone 网络慢或超时不会延迟落盘周期
- **独立生命周期**：recorder 和 cloud-sync 可各自重启、升级
- **频率独立**：录制间隔（10min）和同步间隔（5min）可独立调整
- **镜像精简**：recorder 容器不再需要安装 rclone

---

## 3. 离线历史数据下载器 (`download.py`)

### 3.1 功能定位

从 Binance Vision 官方数据归档站批量下载历史 aggTrades 数据，转换为 Parquet 格式存储。

### 3.2 核心类：`BinanceDownloader`

**配置文件字段** (`downloader` 节点下)：
| 字段 | 说明 |
|------|------|
| `symbols` | 交易对列表 |
| `data_types` | 数据类型列表 (如 `aggTrades`) |
| `date_range.start_date` | 起始日期 |
| `date_range.end_date` | 结束日期 (`auto` 表示至昨天) |
| `max_workers` | 并发下载线程数 |
| `base_dir` | 存储根目录 |

### 3.3 处理流程

1. 根据配置生成任务队列 (交易对 x 日期 x 数据类型)
2. 断点续传检查：目标 Parquet 已存在则跳过
3. 流式下载 ZIP 文件 (内存安全)
4. 解压 CSV 并标准化时间戳 (自动识别秒/毫秒/微秒)
5. 可选数据校验 (`BinanceDataValidator`)
6. 转换为 Parquet 并清理临时文件

### 3.4 存储结构

```
replay_buffer/parquet/
└── aggTrades/
    ├── BTCUSDT/
    │   ├── BTCUSDT-2026-01-01.parquet
    │   └── ...
    └── ETHUSDT/
        └── ...
```

---

## 4. 日级归档与交叉验证 (`daily_compactor.py`)

### 4.1 功能定位

将录制器产生的 144 个 10min 碎片文件聚合为单个 DAILY 大文件，并自动下载币安官方 aggTrades 数据进行交叉对比，量化丢包率。

### 4.2 核心类：`DailyCompactor`

**参数**：
- `symbol`: 交易对
- `target_date`: 目标日期 (datetime.date)
- `raw_dir`: 1min 碎片文件目录
- `official_dir`: 官方校验数据存放目录
- `cold_dir`: 冷数据归档目录 (供 rclone 同步到 Google Drive)，为 `None` 则不移动
- `retain_days`: 保留最近 N 天的 1min 碎片，更早的在 compact 后自动清理 (默认 7 天)

### 4.3 工作流程

```
compact_small_files() → download_official_agg_trades() → validate_data() → archive_to_cold() → cleanup_old_fragments()
     聚合碎片                下载官方数据                  交叉比对           冷数据归档            碎片清理
```

**聚合阶段**：
- 使用 PyArrow Dataset 引擎极速合并所有碎片
- 按 `[timestamp, side]` 排序（side 降序确保快照优先）
- 输出文件：`{SYMBOL}_RAW_{YYYYMMDD}_DAILY.parquet`
- 保留原始碎片不删除

**校验阶段**：
- 从聚合文件中提取 side=2 (L1 成交) 记录
- 与官方 aggTrades CSV 比对笔数和总成交量
- 输出差异报告（笔数差、体积差百分比）

**冷数据归档阶段** (`archive_to_cold`)：
- 将 DAILY 文件复制到 `cold_dir` 目录
- 该目录作为 rclone 同步源，自动上传到 Google Drive
- 已存在的冷数据文件自动跳过

**碎片清理阶段** (`cleanup_old_fragments`)：
- 仅当 DAILY 文件已成功生成后才执行
- 清理超过 `retain_days` 天的 1min 碎片文件
- 释放本地磁盘空间，防止碎片无限膨胀

### 4.4 使用方式

```bash
# 智能扫描所有缺漏天数
python main.py compact --symbol ETHUSDT

# 指定日期强制修复
python main.py compact --symbol ETHUSDT --date 2026-02-22
```

### 4.5 执行方式

> **注意**：云服务器仅负责数据录制，compact / validate 等计算密集型操作已从云端移除。
> 请在本地机器上通过 `rclone` 或 `scp` 拉取 1min 碎片后，在本地执行聚合与校验。

```bash
# 本地执行: 从云端拉取数据后进行聚合与校验
python main.py compact --symbol ETHUSDT

# 本地执行: 处理所有交易对
python main.py compact --symbol ALL
```

---

## 5. 数据校验器 (`validator.py`)

### 5.1 核心类：`BinanceDataValidator`

提供以下校验能力：

| 方法 | 功能 |
|------|------|
| `calculate_md5()` | 计算文件 MD5 哈希 |
| `verify_checksum()` | 与币安官方 CHECKSUM 文件比对 |
| `validate_dataframe()` | DataFrame 业务逻辑校验 |
| `scan_directory()` | 批量扫描目录下所有文件 |

**`validate_dataframe` 校验项**：
- 价格/数量合法性 (> 0)
- 时间戳范围与预期日期匹配
- 时间戳单位自动识别 (秒/毫秒/微秒)
- 年份合理性检查 (防止单位误判导致超出公元 3000 年)

---

## 6. 第三方数据源管道 (`third_party.py`)

### 6.1 核心类：`CryptoDataPipeline`

从 CryptoChassis API 批量拉取历史深度 (market-depth) 和逐笔成交 (trade) 数据，聚合为 1 秒级宽表并落盘为 Parquet。

**初始化参数**：
- `base_dir`: 本地数据存储根目录 (默认 `./crypto_data`)

**目录结构**：
```
{base_dir}/
├── raw/              # 原始 csv.gz 文件
│   ├── market-depth/ # 深度快照
│   └── trade/        # 逐笔成交
└── processed/        # 聚合后的 1s 级宽表 Parquet
    └── {symbol}/
        └── {symbol}_features_{date}.parquet
```

### 6.2 处理流程 (`process_daily_data`)

1. 读取当天的 Trade CSV，按秒聚合 `taker_buy_vol` / `taker_sell_vol`
2. 读取当天的 Depth CSV，解析字符串格式的多档挂单 (price_qty 以 `|` 和 `_` 分隔)
3. 按秒对齐合并 (Merge)，计算 `mid_price` 和 `spread`
4. 输出为 Parquet 格式

### 6.3 使用方式

```python
pipeline = CryptoDataPipeline(base_dir="./quant_data")
pipeline.run_pipeline(
    symbols=["btc-usdt", "eth-usdt"],
    start_date="2025-03-04",
    end_date="2026-03-04"
)
```

---

## 7. 数据流向总结

```
                   ┌────────────── 云服务器 (Docker Compose) ───────────────┐
                   │                                                        │
                   │  ┌─ recorder 容器 ──────────┐                          │
                   │  │  Binance WebSocket        │                         │
                   │  │         │                  │                         │
                   │  │         ▼                  │                         │
                   │  │  BinanceL2Recorder         │                         │
                   │  │  (纯采集 + 本地落盘)       │                         │
                   │  │         │                  │                         │
                   │  │         ▼ (每10分钟)       │                         │
                   │  │  10min RAW Parquet 文件    │                         │
                   │  └────────────┬───────────────┘                        │
                   │               │ narci-data (共享 volume)               │
                   │  ┌────────────▼───────────────┐                        │
                   │  │  cloud-sync 容器 (sidecar)  │                        │
                   │  │  rclone/rclone 官方镜像     │                        │
                   │  │  定时 rclone copy (:ro)     │                        │
                   │  │         │                   │                        │
                   │  │         ▼                   │                        │
                   │  │  Google Drive               │                        │
                   │  │  (gdrive:/narci_raw)        │                        │
                   │  └────────────────────────────┘                        │
                   └────────────────────────────────────────────────────────┘

                   ┌─────────────────── 本地机器 ────────────────────┐
                   │                                                 │
                   │  rclone copy / sync 拉取                       │
                   │         │                                       │
                   │         ▼                                       │
                   │    daily_compactor 聚合 (并行处理 10min 文件)    │
                   │         │                                       │
                   │         ▼                                       │
                   │    DAILY Parquet 冷数据  ←─对比─→ 币安官方数据   │
                   │         │                                       │
                   │         ▼                                       │
                   │    GUI / L2 盘口洞察 / 回测引擎                  │
                   └─────────────────────────────────────────────────┘

  CryptoChassis API (第三方)
       │
       ▼
  CryptoDataPipeline ──→ 1s 级宽表 Parquet (processed/)
```
