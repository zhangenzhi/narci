# 数据录制系统

Narci 的数据录制系统负责从 Binance 实时采集 L2 订单簿深度与 L1 逐笔成交数据，并提供离线历史数据下载、日级归档聚合与交叉验证能力。

---

## 架构概览

```
数据录制系统
├── l2_recorder.py      # WebSocket 实时高频录制引擎 (核心)
├── download.py         # 离线历史数据批量下载器
├── daily_compactor.py  # 日级归档聚合与官方交叉验证
└── validator.py        # 数据质量校验工具
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
| `save_interval_sec` | 落盘周期(秒) | `60` |
| `save_dir` | 数据存储根目录 | `./replay_buffer/realtime` |

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

- 每 `save_interval_sec` (默认 60 秒) 执行一次异步写盘
- 文件命名格式：`{SYMBOL}_RAW_{YYYYMMDD}_{HHMMSS}.parquet`
- 存储路径：`{save_dir}/{market_type}/l2/`
- 使用 PyArrow + Snappy 压缩

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

---

## 2. 离线历史数据下载器 (`download.py`)

### 2.1 功能定位

从 Binance Vision 官方数据归档站批量下载历史 aggTrades 数据，转换为 Parquet 格式存储。

### 2.2 核心类：`BinanceDownloader`

**配置文件字段** (`downloader` 节点下)：
| 字段 | 说明 |
|------|------|
| `symbols` | 交易对列表 |
| `data_types` | 数据类型列表 (如 `aggTrades`) |
| `date_range.start_date` | 起始日期 |
| `date_range.end_date` | 结束日期 (`auto` 表示至昨天) |
| `max_workers` | 并发下载线程数 |
| `base_dir` | 存储根目录 |

### 2.3 处理流程

1. 根据配置生成任务队列 (交易对 x 日期 x 数据类型)
2. 断点续传检查：目标 Parquet 已存在则跳过
3. 流式下载 ZIP 文件 (内存安全)
4. 解压 CSV 并标准化时间戳 (自动识别秒/毫秒/微秒)
5. 可选数据校验 (`BinanceDataValidator`)
6. 转换为 Parquet 并清理临时文件

### 2.4 存储结构

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

## 3. 日级归档与交叉验证 (`daily_compactor.py`)

### 3.1 功能定位

将录制器产生的 1440 个 1min 碎片文件聚合为单个 DAILY 大文件，并自动下载币安官方 aggTrades 数据进行交叉对比，量化丢包率。

### 3.2 核心类：`DailyCompactor`

**参数**：
- `symbol`: 交易对
- `target_date`: 目标日期 (datetime.date)
- `raw_dir`: 1min 碎片文件目录
- `official_dir`: 官方校验数据存放目录

### 3.3 工作流程

```
compact_small_files()  →  download_official_agg_trades()  →  validate_data()
     聚合碎片                  下载官方数据                   交叉比对
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

### 3.4 使用方式

```bash
# 智能扫描所有缺漏天数
python main.py compact --symbol ETHUSDT

# 指定日期强制修复
python main.py compact --symbol ETHUSDT --date 2026-02-22
```

### 3.5 自动触发

录制器运行状态下，每天凌晨 00:05 会自动触发前一天的归档与校验。

---

## 4. 数据校验器 (`validator.py`)

### 4.1 核心类：`BinanceDataValidator`

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

## 5. 数据流向总结

```
Binance WebSocket
       │
       ▼
  BinanceL2Recorder (实时采集)
       │
       ▼ (每60秒落盘)
  1min RAW Parquet 碎片
       │
       ▼ (daily_compactor 聚合)
  DAILY Parquet 冷数据  ←──对比──→  币安官方 aggTrades
       │
       ▼
  GUI 冷数据仓库 / L2 盘口洞察 / 回测引擎
```
