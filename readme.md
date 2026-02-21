# Narci - 纯粹的高频交易回测框架

**Narci** 是一个专为加密货币量化交易者设计的轻量级**事件驱动**回测与实盘框架。它专注于高频交易（HFT）和微观市场结构（L1/L2 订单簿）策略的严谨验证与快速上线。

通过提供极速的模拟撮合引擎、精准的订单簿特征重构以及无缝切换实盘的架构，Narci 为做市策略和动量研究构建了一个从回测沙盒到实盘部署的高效通路。

---

## 📌 TODO List (V1.0 实盘上线路径)

为了用最快速度将量化模型跑通回测并真正“上线”交易，我们接下来的短期核心任务聚焦于以下三个阶段：

### 🧱 阶段一：模型整合与回测闭环 (Model & Backtest MVP)
* [ ] **模型工程化接入**：在 `strategy.py` 中增加对外部预训练模型（如 PyTorch/ONNX/LightGBM）的加载支持，将重构后的 L2 实时特征直接输入模型输出交易信号。
* [ ] **回测真实度矫正**：在 `SimulatedBroker` 中补充基础的固定滑点（Slippage）模型和最小变动价位（Tick Size）限制，确保回测收益在实盘中具有可落地性。
* [ ] **模型训练数据导出**：完善 `l2_reconstruct.py`，支持将清洗、重构后的高频特征矩阵与未来收益率标签（Label）一键导出为 CSV/Parquet，打通机器学习训练管线。

### ⚡ 阶段二：实盘交易流改造 (Live Trading Pipeline)
* [ ] **流式特征重构引擎**：改造目前的 `L2Reconstructor`（当前基于静态文件），使其能够直接对接 `l2_recorder.py` 的 WebSocket 内存队列，实现毫秒级的“边接收、边重构、边预测”。
* [ ] **实盘交易网关 (Live Broker)**：基于 `ccxt` 或 `binance-connector-python` 实现 `BinanceBroker`，替换回测环境的 `SimulatedBroker`，支持实盘环境的 API 鉴权与市价/限价单发送。
* [ ] **模拟盘沙盒 (Paper Trading)**：实现不消耗真实资金的模拟盘运行模式，利用实时行情和本地撮合测试整个上下游链路的稳定性。

### 🚀 阶段三：生产环境部署 (Production Deployment)
* [ ] **Docker 容器化**：编写 `Dockerfile` 和 `docker-compose.yml`，一键拉起包括数据接收、流式计算和策略执行在内的完整运行环境。
* [ ] **状态持久化与异常恢复**：引入轻量级本地数据库（如 SQLite）保存当前的持仓状态（Position）和活动订单（Active Orders），确保程序崩溃重启后能正确恢复交易状态。
* [ ] **实时告警推送**：集成 Telegram Bot 或 钉钉机器人，当策略发出交易信号、成交或发生系统级异常（如断网）时，实时推送到手机端。

---
> **Note:** 核心目标是确保从 `SimulatedBroker` 到 `LiveBroker` 的逻辑一致性。


## 🚀 快速开始

1.  **安装依赖**
    ```bash
    pip install pandas numpy streamlit plotly aiohttp websockets pyarrow requests
    ```

2.  **获取数据**
    ```bash
    # 下载历史数据
    python download.py
    
    # 或启动实时录制
    python l2_recoder.py
    ```

3.  **运行回测**
    ```bash
    python backtest.py
    ```

4.  **启动看板**
    ```bash
    streamlit run dashboard.py
    ```

---

## 🔮 愿景 (Vision)

Narci 致力于打造一个**轻量级、高精度、低门槛**的量化研究基础设施。

我们的核心目标是：搞钱！