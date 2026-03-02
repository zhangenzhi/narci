# 💹 Narci Quant Terminal

**Narci** 是一个高性能的量化数据获取、微观结构重构与分析平台。系统专注于 Binance 市场的 Level 2 (L2) 订单簿高频采集、跨市场支持（现货/U本位合约）、冷热数据分离架构，以及提供基于 WebGL 加速的交互式可视化看板。

---

## 🌟 核心特性

* **🚀 多币种并发捕获 (Multi-Symbol Stream)**
通过单一 WebSocket 连接订阅多个交易对的 100ms 级深度快照与逐笔成交，极大降低网络延迟与 I/O 开销。
* **🧩 微观盘口重建 (L2 Reconstructor)**
支持通过增量事件还原毫秒级的 Orderbook 切片，并计算 Mid Price、Imbalance、Spread 等微观因子。
* **📐 L1 同盘数据校验 (Data Fidelity Check)**
独创的录制/归档分离机制。凌晨自动拉取 Binance 官方归档的历史数据，与本地重建的 L1 成交记录进行自动交叉对比，精准量化丢包率和体积差异。
* **🗄️ 冷热数据分离架构**
热数据每分钟高频落盘保证灾备安全；冷数据日级别合并 (PyArrow 驱动) 提供极致的读取分析体验。
* **📊 全景 UI 控制台 (Dashboard)**
内置基于 Streamlit 和 Plotly 的强交互面板。支持拖拽缩放的 L2 深度截面墙、微观因子视图与冷数据检索仓库。

---

## 🛠️ 安装与环境准备

本项目重度依赖 `pandas`, `pyarrow` 和 `streamlit` 以实现海量数据的分析与展现。

```bash
# 克隆仓库
git clone https://github.com/your-repo/narci.git
cd narci

# 安装依赖
pip install -r requirements.txt

```

---

## 🚀 快速启动指南

所有的核心功能都可以通过根目录下的中枢调度脚本 `main.py` 触发。

### 1. 启动图形化控制台 (Dashboard)

启动可视化看板，您可以在此预览数据、分析微观结构指标并查看冷数据仓库。

```bash
python main.py gui

```

### 2. 启动 L2 高频录制器 (Recorder)

录制器由 YAML 配置文件驱动。支持自定义市场类型 (`spot` 或 `um_futures`) 及币种列表。

* **常规启动**（默认加载 `configs/recorder.yaml`，适用于 U本位合约多币种）：
```bash
python main.py record

```


* **指定配置启动**（例如启动配置好的现货多币种录制器）：
```bash
python main.py record --config configs/spot_recorder.yaml

```


* **强制单币种覆盖**（临时挂一个新币种，不修改 YAML 文件）：
```bash
python main.py record --symbol DOGEUSDT

```



### 3. 数据归档与交叉验证 (Compactor)

录制器每分钟会产生碎片数据。该命令会将碎片聚合为 `_DAILY.parquet` 冷数据，并自动下载币安官方数据执行防漏单验证。（**注**：录制器在运行状态下，每天凌晨 00:05 也会自动触发此操作）

* **一键智能扫描所有缺漏天数并验证**：
```bash
python main.py compact --symbol ETHUSDT

```


* **指定特定日期进行强制修复与校验**：
```bash
python main.py compact --symbol ETHUSDT --date 2026-02-22

```



---

## 📂 目录结构说明

```text
narci/
├── configs/               # 配置文件目录
│   ├── recorder.yaml      # 默认多币种配置 (U本位合约)
│   └── spot_record.yaml   # 现货多币种配置示例
├── data/                  # 核心数据逻辑
│   ├── l2_recoder.py      # WS 异步接收引擎
│   ├── l2_reconstruct.py  # Orderbook 状态重构引擎
│   └── daily_compactor.py # 日级归档与官方校验引擎
├── gui/                   # 可视化控制台面板
│   ├── dashboard.py       # 看板主入口
│   ├── panel_l2_insight.py# L2 盘口同盘对比与深度展示
│   └── panel_cold_db.py   # 全局冷数据资产检索库
└── replay_buffer/         # 数据落盘存储区 (在 .gitignore 中忽略)
    ├── realtime/l2        # 1min 原始碎片及 DAILY 归档文件
    └── official_validation# 自动下载的币安官方 CSV 比对数据

```

---

## ⚠️ 重要注意事项

> **关于数据对比 (L1 校验) 的警告：**
> 请务必保证 `configs/` 中的 `market_type` (spot 或 um_futures) 与你的预期一致。因为不同市场的交易量天差地别。如果录制了现货数据却使用默认的合约校验器，会导致校验报告提示高达 **-90% 以上**的体积差异（跨市场对比错误）。


---

## 🔮 愿景 (Vision)

Narci 致力于打造一个**轻量级、高精度、低门槛**的量化研究基础设施。

我们的核心目标是：搞钱！