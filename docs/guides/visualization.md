# 可视化系统

Narci 内置基于 Streamlit + Plotly (WebGL 加速) 的交互式可视化控制台，提供 L1 行情预览、L2 盘口微观分析、冷数据仓库与系统设置四大面板。

---

## 架构概览

```
可视化系统 (analytics/gui/)          # P5 后位于 analytics 层
├── dashboard.py        # 看板主入口与 Tab 路由
├── panel_history.py    # L1 历史行情预览面板
├── panel_l2_insight.py # L2 盘口微观结构洞察面板
├── panel_cold_db.py    # 冷数据仓库面板
├── panel_settings.py   # 系统设置面板
└── utils.py            # 公共工具函数
```

> 回测面板 `panel_backtest.py` 已在 P4 删除(GUI 不再做回测;回测见
> `docs/guides/backtesting.md`)。

启动方式：

```bash
python main.py gui
# 等价于: streamlit run analytics/gui/dashboard.py
```

---

## 1. 主控制台 (`dashboard.py`)

### 1.1 NarciDashboard

控制台主类，负责：
- 初始化 L1 / L2 数据目录路径（含自动路径纠偏）
- 创建四个 Tab 并路由到各面板的 `render()` 函数

**Tab 布局**：
| Tab | 面板 | 功能 |
|-----|------|------|
| 📊 L1 行情预览 | `panel_history` | 历史 aggTrades K 线 |
| 🔬 L2 盘口洞察 | `panel_l2_insight` | 盘口重构与微观分析 |
| 🗄️ 冷数据仓库 | `panel_cold_db` | 全局数据资产管理 |
| 🔧 系统设置 | `panel_settings` | 路径配置 |

---

## 2. L1 历史行情预览 (`panel_history.py`)

### 功能

- 递归扫描 `replay_buffer/parquet/` 下所有 Parquet 文件
- 选择文件后展示基础统计指标（数据条数、最高价、最低价、平均成交量）
- 提供可调聚合周期 (1s / 10s / 1min / 5min)
- 使用 Plotly Candlestick 渲染 K 线图（暗色主题）

### 数据要求

输入 Parquet 文件需包含 `timestamp` 和 `price` 列。时间戳支持自动格式转换。

---

## 3. L2 盘口微观结构洞察 (`panel_l2_insight.py`)

### 3.1 功能概览

最核心的分析面板，提供 L2 盘口重建、微观因子可视化与官方数据交叉校验。

### 3.2 数据选择与导入

1. **市场类型选择**：`um_futures` / `spot`
2. **交易对筛选**：自动从文件名解析可用交易对
3. **多文件选择**：支持 Shift 连选、Ctrl 多选、Ctrl+A 全选
4. **导入方式**：
   - 「导入选定范围」：按选中行导入
   - 「全选并导入全部」：一键全量导入

### 3.3 数据处理流程

```
选定 RAW 文件列表
       │
       ▼ (PyArrow Dataset 聚合)
  全局排序后的 DataFrame
       │
       ├──→ L2Reconstructor → 100ms 采样盘口切片 (df_l2)
       │
       └──→ 提取 side=2 → L1 逐笔成交记录 (df_l1)
```

处理结果使用 `@st.cache_data` 缓存，相同文件组合不重复计算。

### 3.4 四个可视化子 Tab

#### Tab 1: 盘口价格与指标走势

两张图表：
- **Top of Book**：Ask1 (红) / Bid1 (绿) / Mid Price (黄虚线) 的时序走势
- **微观指标**：Imbalance 买盘偏度 (双 Y 轴) + Spread 价差叠加

全部使用 `Scattergl` (WebGL 加速) 渲染，支持百万级数据点流畅缩放。

#### Tab 2: 动态深度截面图

- 通过滑块选择任意时间帧
- 展示该帧的完整订单簿截面 (Bids/Asks 累计挂单量)
- 阶梯填充图，直观呈现买卖深度对比

#### Tab 3: L1 逐笔成交同盘对比

- **时间范围滑块**：可拖拽截取任意时段
- **数据一致性评分板**：对比重构数据与官方数据的笔数/成交量差异
- **双层联合视图**：
  - 上层：价格离散点 (买/卖分色) + 官方价格基线 + Mid Price 参考
  - 下层：累计成交量轨迹对比 (重构 vs 官方)
- 支持导入官方 L1 历史 Parquet 进行实时交叉校验

#### Tab 4: 原始重构数据

展示前 200 行重构后的 DataFrame 原始数据。

---

## 4. 冷数据仓库 (`panel_cold_db.py`)

### 4.1 功能概览

统一盘点系统中所有落盘数据资产，包括 L1 历史、L2 录制碎片、DAILY 聚合文件与官方交叉校验数据。

### 4.2 数据扫描

自动扫描以下目录（60 秒缓存）：
- `replay_buffer/` (L1 历史行情)
- `data/realtime/l2/` (L2 录制)
- `replay_buffer/realtime/l2/` (L2 录制备选路径)
- `replay_buffer/official_validation/` (官方校验数据)

### 4.3 数据分类

| 类型标签 | 识别规则 |
|----------|----------|
| L2 聚合冷数据 (DAILY) | 文件名含 `DAILY` |
| L2 录制碎片 (1min RAW) | 文件名含 `RAW` |
| 官方交叉校验 (L1 CSV) | 路径含 `official_validation`，后缀 `.csv` |
| L1 历史行情 (Parquet) | 其他 `.parquet` 文件 |

### 4.4 交互功能

- **三维过滤器**：按交易对 / 数据类型 / 日期筛选
- **统计卡片**：文件总数、冷数据总占用、交易对数量
- **资产检索列表**：可点击任意行进入预览
- **数据质量预览**：
  - Parquet 文件：展示 Schema、总行数、首 100 行数据
  - CSV 文件：展示首 100 行并自动转换时间戳
  - ZIP 文件：提示需解压

---

## 5. 系统设置 (`panel_settings.py`)

展示当前系统的目录配置：
- 项目根目录
- L1 搜索路径
- L2 搜索路径

---

## 6. 公共工具 (`utils.py`)

### `get_all_parquet_files(directory)`

递归获取指定目录下所有 `.parquet` 文件的完整路径列表。

### `get_filtered_files(directory, start_date, end_date, symbol=None)`

按日期范围和交易对筛选 Parquet 文件，支持 `YYYY-MM-DD` 和 `YYYYMMDD` 两种日期格式。

---

## 7. 可视化技术要点

- **WebGL 加速**：所有大数据量图表使用 `Scattergl` 替代 `Scatter`，支持百万点级流畅交互
- **Streamlit 缓存**：数据处理函数使用 `@st.cache_data` 装饰器，避免重复计算
- **暗色主题**：统一使用 `plotly_dark` 模板
- **Session State**：关键交互状态（如已导入文件列表）通过 `st.session_state` 跨刷新保持
- **PyArrow 极速预览**：冷数据仓库仅读取 Parquet 元数据和首个 Row Group 的前 100 行，内存占用极低
