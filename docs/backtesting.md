# 回测系统

Narci 的回测系统基于 L2 订单簿微观数据驱动，提供事件级 Tick-by-Tick 回测能力，支持 Maker/Taker 双角色撮合、杠杆合约仿真与特征缓存加速。

---

## 架构概览

```
回测系统
├── backtest.py         # 回测引擎 (BacktestEngine / JitBacktestEngine)
├── broker.py           # 模拟撮合引擎 (SimulatedBroker)
├── strategy.py         # 策略基类 (BaseStrategy)
├── feature_builder.py  # 特征工程构建器 (FeatureBuilder)
└── example/
    ├── L1_Trend.py     # L1 趋势策略示例
    └── L2_Imbalance.py # L2 高频做市策略示例
```

---

## 1. 回测引擎 (`backtest.py`)

### 1.1 BacktestEngine (基础引擎)

事件驱动的 Tick-by-Tick 回测引擎，每个 tick 按顺序执行：

1. 从 tick 中提取多档盘口数据 (b_p_0..N, a_p_0..N)
2. 调用 `broker.update_l2()` 更新盘口并撮合挂单
3. 调用 `strategy.on_tick()` 执行策略逻辑
4. 每 1000 tick 记录一次资金快照

**初始化参数**：
| 参数 | 说明 |
|------|------|
| `data_paths` | 数据文件路径列表 |
| `strategy` | 策略实例 (继承 BaseStrategy) |
| `symbol` | 交易对 |
| `config_path` | broker 配置文件路径 |

**性能探针**：引擎内置耗时分析，自动统计数据加载、结构转换、Broker 撮合、策略运算、资金快照各阶段耗时。

### 1.2 JitBacktestEngine (进阶引擎)

继承 `BacktestEngine`，增加以下能力：

#### JIT 实时盘口重构

当输入数据为 RAW 碎片 (包含 side 0~4 的原始事件) 时，引擎自动执行：

1. **PyArrow 并发读取**：使用 `pyarrow.dataset` 极速加载多个文件
2. **全局内存排序**：按 `[timestamp, side]` 排序 (side 降序确保快照先处理)
3. **L2 盘口重构**：调用 `L2Reconstructor` 以 100ms 采样步长还原盘口
4. **特征衍生**：调用 `FeatureBuilder.build_offline()` 生成高级因子

#### 特征缓存机制

- 对输入文件列表计算 MD5 哈希作为缓存键
- 首次处理后将结果落盘为 Parquet 缓存
- 相同文件组合再次回测时直接命中缓存，秒级启动
- 缓存路径：`{realtime_dir}/{market_type}/features/`

**额外初始化参数**：
| 参数 | 说明 |
|------|------|
| `is_raw` | 是否为 RAW 原始数据 (需 JIT 重构) |
| `init_cash` | 初始资金 |
| `m_fee` / `t_fee` | Maker/Taker 费率 |
| `lev` | 杠杆倍数 |
| `c_dir` | 特征缓存目录 |

---

## 2. 模拟撮合引擎 (`broker.py`)

### 2.1 核心类：`SimulatedBroker`

U 本位双向合约仿真撮合引擎，支持 Maker 限价单与 Taker 市价单双轨制。

**初始化参数**：
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `initial_cash` | 10000.0 | 初始保证金 |
| `maker_fee` | 0.0002 | 限价单手续费率 (0.02%) |
| `taker_fee` | 0.0004 | 市价单手续费率 (0.04%) |
| `leverage` | 10.0 | 杠杆倍数 |

### 2.2 市价单 (Taker)

```python
broker.buy(symbol, quantity, use_l2=True, signal_info=None)
broker.sell(symbol, quantity, use_l2=True, signal_info=None)
```

- `use_l2=True`：启用多档深度穿透吃单 (Market Sweep)，逐档消耗流动性并计算加权均价
- `use_l2=False`：使用中间价直接成交
- 自动保证金校验：可用保证金不足时自动缩减下单量

### 2.3 限价单 (Maker)

```python
oid = broker.place_limit_order(symbol, side, price, qty, signal_info=None)
broker.cancel_all_orders(symbol)
```

**撮合逻辑** (每次 `update_l2` 更新盘口时触发)：
- 买单成交条件：市场最优卖价 (best_ask) <= 挂单价
- 卖单成交条件：市场最优买价 (best_bid) >= 挂单价
- 成交后按 Maker 费率计费

### 2.4 持仓管理

```python
broker.get_equity()           # 总权益 = 现金 + 未实现盈亏
broker.get_available_margin() # 可用保证金 = 权益 - 已用保证金
broker.get_state()            # 完整账户状态快照
broker.close_all()            # 强制市价全平
```

**账本流转规则**：
- 同向交易 → 加仓，加权平均入场价
- 异向交易 → 先平后开，计算已实现盈亏并转入现金

### 2.5 数据输出

```python
broker.get_trade_history()   # 交易流水 DataFrame
broker.get_equity_history()  # 资金曲线 DataFrame
```

交易流水包含字段：`timestamp, symbol, action, role(MAKER/TAKER), quantity, price, fee, realized_pnl, equity, signal_info`

---

## 3. 策略框架 (`strategy.py`)

### 3.1 BaseStrategy

所有策略必须继承此基类并实现两个回调方法：

```python
class BaseStrategy:
    def __init__(self):
        self.broker = None   # 由引擎自动注入
        self.data = None     # 可选：策略可挂载外部数据引用

    def on_tick(self, tick):
        """每个 tick 调用，tick 为包含盘口与特征的字典"""
        pass

    def on_finish(self):
        """回测结束后回调"""
        pass
```

### 3.2 Tick 数据结构

策略 `on_tick` 接收的 tick 字典包含以下字段：

**基础盘口字段** (由 L2Reconstructor 生成)：
| 字段 | 说明 |
|------|------|
| `timestamp` | 毫秒级时间戳 |
| `mid_price` | 中间价 |
| `imbalance` | Top-5 深度加权买卖不平衡度 [-1, 1] |
| `spread` | 买卖价差 |
| `taker_buy_vol` | 当期主动买入成交量 |
| `taker_sell_vol` | 当期主动卖出成交量 |
| `b_p_0..N` / `b_q_0..N` | 买盘各档价格/数量 |
| `a_p_0..N` / `a_q_0..N` | 卖盘各档价格/数量 |

**高级特征字段** (由 FeatureBuilder 衍生)：
| 字段 | 说明 |
|------|------|
| `ema_imbalance` | 指数移动平均不平衡度 |
| `volatility_2s` | 2 秒窗口价格波动率 |
| `momentum_bullish` | 买方动能标志 |
| `momentum_bearish` | 卖方动能标志 |

---

## 4. 特征工程 (`feature_builder.py`)

### 4.1 核心类：`FeatureBuilder`

负责将 L2 截面数据转化为高级量化因子。

**配置参数** (可通过 `configs/backtest.yaml` 的 `backtest.features` 节点设置)：
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ema_span` | 10 | EMA 不平衡度的跨度 |
| `vol_window` | 20 | 波动率计算窗口 |

### 4.2 离线模式 (`build_offline`)

对整个 DataFrame 进行向量化特征计算：
- `ema_imbalance`：`imbalance` 列的 EMA 平滑
- `volatility_2s`：`mid_price` 的差分绝对值
- `momentum_bullish/bearish`：买卖成交量方向判断

### 4.3 在线模式 (`build_online`)

逐 tick 增量计算，维护内部状态机，适用于实盘场景。

### 4.4 端到端流水线 (`build_from_raw_files`)

```
多文件并发加载 → 全局排序 → L2 盘口重构 (100ms) → 特征衍生 → 输出 DataFrame
```

---

## 5. 策略示例

### 5.1 L2 Imbalance 高频做市策略 (`example/L2_Imbalance.py`)

一个完整的高频做市策略实现，核心逻辑：

**入场逻辑** (阶段 2)：
- 当 `ema_imbalance > 阈值` 且 `momentum_bullish` 为 True 时考虑做多
- 信号强度分层：
  - 强信号 (imbalance > 阈值 x 1.5)：Taker 市价追击
  - 弱信号：Maker 限价挂单，维护队列优先级
- 库存偏置 (Inventory Skewing)：持仓越大，同方向开仓门槛越高

**风控逻辑** (阶段 1)：
- 自适应止盈/止损：基于实时波动率 (`volatility_2s`) 动态调整
- 止损 = `volatility_2s * k_sl` (封顶 0.50)
- 止盈 = `volatility_2s * k_tp` (底线 0.05)
- 信号反转检测：持多时 imbalance 转负则 Taker 切仓
- 超时硬切：持仓超过 40 tick (约 4 秒) 强制退出
- 温和退出：信号中性时 Maker 挂单排队出局

**队列保护**：价格偏离不超过容忍度 (`queue_tolerance`) 时不撤单重排，保持 Maker 队列优先级。

### 5.2 L1 趋势策略 (`example/L1_Trend.py`)

基于均线突破的简单策略示例，适用于 L1 aggTrades 数据。

---

## 6. 命令行与 CLI 缓存构建

### 离线特征缓存构建

```bash
# 默认构建 ETHUSDT 的 U本位合约特征缓存
python main.py build-cache

# 指定交易对和市场
python main.py build-cache --symbol BTCUSDT --market spot

# 处理所有交易对
python main.py build-cache --symbol ALL
```

该命令会调用 `FeatureBuilder.build_from_raw_files()` 并将结果固化到 `backtest_cache/` 目录。GUI 回测面板使用 `features/` 目录存放 JIT 缓存，两者缓存文件命名规则一致 (MD5 哈希)，可互相命中。

---

## 7. 完整回测流程

```
                 RAW Parquet 碎片
                       │
         ┌─────────────┤ (is_raw=True?)
         │             │
    命中缓存?      JitBacktestEngine
    ├─ Yes → 直接加载   ├─ PyArrow 并发读取
    └─ No              ├─ L2Reconstructor 重构盘口
                       ├─ FeatureBuilder 衍生因子
                       └─ 缓存落盘
                       │
                       ▼
              100ms 采样 Feature DataFrame
                       │
                       ▼
            BacktestEngine 事件循环
            ┌──────────────────────┐
            │  for tick in ticks:  │
            │    broker.update_l2()│  ← 更新盘口 + 撮合限价单
            │    strategy.on_tick()│  ← 策略决策
            │    record_equity()   │  ← 资金快照
            └──────────────────────┘
                       │
                       ▼
              交易流水 + 资金曲线 + 绩效报告
```
