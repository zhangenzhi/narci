import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import os
import glob
import sys
import io
import contextlib

# 动态添加项目根目录到 sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(current_dir)
if root_dir not in sys.path:
    sys.path.append(root_dir)

from data.l2_reconstruct import L2Reconstructor
from backtest.backtest import BacktestEngine
# 导入示例中的策略逻辑
from example.L2_Imbalance import ImbalanceStrategy
from example.L1_Trend import MyTrendStrategy

st.set_page_config(page_title="Narci Quant Terminal", layout="wide", page_icon="💹")

class NarciDashboard:
    def __init__(self):
        self.base_path = root_dir
        # L1 数据存放路径 (download.py 默认生成路径)
        self.history_dir = os.path.join(self.base_path, "replay_buffer", "parquet", "aggTrades")
        # L2 数据存放路径 (l2_recorder.py 默认生成路径)
        self.realtime_dir = os.path.join(self.base_path, "data", "realtime", "l2")
        
        # 路径自动纠偏：如果默认路径不存在，尝试递归搜索 parquet 根目录
        if not os.path.exists(self.history_dir):
             self.history_dir = os.path.join(self.base_path, "replay_buffer", "parquet")
        if not os.path.exists(self.realtime_dir):
             self.realtime_dir = os.path.join(self.base_path, "replay_buffer", "realtime", "l2")

    def get_files(self, directory):
        if not os.path.exists(directory):
            return []
        files = []
        for root, _, filenames in os.walk(directory):
            for filename in filenames:
                if filename.endswith(".parquet"):
                    files.append(os.path.join(root, filename))
        return sorted(files, reverse=True)

    def render_history_panel(self):
        st.header("📈 历史行情预览 (L1 Data)")
        files = self.get_files(self.history_dir)
        if not files:
            st.warning("⚠️ 未找到 L1 历史数据，请先运行 'python main.py download' 下载数据。")
            return
            
        selected_path = st.selectbox("选择历史数据文件", files, format_func=lambda x: os.path.basename(x))
        if selected_path:
            with st.spinner("加载数据中..."):
                df = pd.read_parquet(selected_path).sort_values('timestamp')
            
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("数据条数", f"{len(df):,}")
            col2.metric("最高价", f"{df['price'].max():.2f}")
            col3.metric("最低价", f"{df['price'].min():.2f}")
            col4.metric("平均成交量", f"{df['quantity'].mean():.4f}")

            rule = st.select_slider("可视化聚合周期", options=["1s", "10s", "1min", "5min"], value="1min")
            df_ohlc = df.set_index('timestamp')['price'].resample(rule).ohlc()
            
            fig = go.Figure(data=[go.Candlestick(
                x=df_ohlc.index, open=df_ohlc['open'], high=df_ohlc['high'], 
                low=df_ohlc['low'], close=df_ohlc['close'], name='K-Line'
            )])
            fig.update_layout(height=500, template="plotly_dark", xaxis_rangeslider_visible=False, title=f"价格走势 ({rule})")
            st.plotly_chart(fig, use_container_width=True)

    def render_backtest_panel(self):
        st.header("🧪 策略回测实验室")
        st.markdown("支持 L1 (aggTrades) 趋势策略与 L2 (RAW) 订单簿微观策略。")
        
        c1, c2 = st.columns([1, 1])
        with c1:
            st.subheader("1. 数据源配置")
            data_mode = st.radio("回测模式", ["L1 历史回测 (趋势策略)", "L2 实时回测 (微观特征)"])
            
            if data_mode == "L1 历史回测 (趋势策略)":
                files = self.get_files(self.history_dir)
                strategy_options = ["趋势跟踪 (MyTrendStrategy)"]
            else:
                files = self.get_files(self.realtime_dir)
                strategy_options = ["盘口不平衡 (ImbalanceStrategy)"]
            
            selected_data = st.selectbox("选择回测文件", files, format_func=lambda x: os.path.basename(x))
            selected_strat_name = st.selectbox("选择加载策略", strategy_options)
            
        with c2:
            st.subheader("2. 核心参数调整")
            initial_cash = st.number_input("初始资金 (USD)", 1000, 1000000, 10000)
            fee = st.number_input("费率 (例如 0.0001 代表 0.01%)", 0.0, 0.01, 0.0001, format="%.4f")
            
            # 根据策略动态显示参数
            if selected_strat_name == "趋势跟踪 (MyTrendStrategy)":
                window = st.slider("均线窗口大小 (Window)", 10, 2000, 500)
                strategy = MyTrendStrategy(window_size=window)
            else:
                st.info("💡 L2 策略回测前会自动执行订单簿重构逻辑。")
                in_threshold = st.slider("入场阈值 (Imbalance > X)", 0.0, 1.0, 0.3)
                out_threshold = st.slider("出场阈值 (Imbalance < Y)", -1.0, 0.0, -0.1)
                strategy = ImbalanceStrategy(entry_threshold=in_threshold, exit_threshold=out_threshold)

        if st.button("🚀 启动回测引擎", type="primary", use_container_width=True):
            if not selected_data:
                st.error("❌ 错误：未选择任何数据文件。")
                return

            with st.spinner("正在处理数据并执行回测..."):
                final_data_path = selected_data
                
                # 处理 L2 数据的重构流程
                if data_mode == "L2 实时回测 (微观特征)":
                    recon = L2Reconstructor(depth_limit=10)
                    # 高频策略通常采样频率更高，这里固定 100ms
                    df_feat = recon.generate_l2_dataset(selected_data, sample_interval_ms=100)
                    if df_feat.empty:
                        st.error("❌ 重构失败：原始数据中未发现有效的 Snapshot 快照。")
                        return
                    df_feat['price'] = df_feat['mid_price']
                    final_data_path = "temp_gui_bt_l2.parquet"
                    df_feat.to_parquet(final_data_path, index=False)

                # 初始化回测引擎
                engine = BacktestEngine(final_data_path, strategy, initial_cash=initial_cash, fee_rate=fee)
                
                # 捕获回测过程中的标准输出
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    try:
                        engine.run()
                    except Exception as e:
                        st.error(f"回测运行时发生崩溃: {e}")
                
                # 渲染结果
                st.success("✅ 回测任务已完成！")
                
                # 布局结果展示
                res_col1, res_col2 = st.columns([1, 2])
                with res_col1:
                    st.subheader("📊 统计报告")
                    st.code(output.getvalue())
                
                with res_col2:
                    if engine.broker.trades:
                        trades_df = pd.DataFrame(engine.broker.trades)
                        st.subheader("📈 交易分布与价格曲线")
                        
                        # 加载价格数据进行绘图
                        df_price = pd.read_parquet(final_data_path).sort_values('timestamp')
                        # 如果数据点太多，进行降采样绘图以提升性能
                        if len(df_price) > 5000:
                            df_price = df_price.iloc[::max(1, len(df_price)//5000)]
                            
                        fig = go.Figure()
                        fig.add_trace(go.Scatter(x=df_price['timestamp'], y=df_price['price'], 
                                                 name='Market Price', line=dict(color='gray', width=1), opacity=0.6))
                        
                        buys = trades_df[trades_df['side'] == 'BUY']
                        sells = trades_df[trades_df['side'] == 'SELL']
                        
                        fig.add_trace(go.Scatter(x=buys['time'], y=buys['price'], mode='markers', 
                                                 name='BUY Signal', marker=dict(color='#00FF00', symbol='triangle-up', size=12)))
                        fig.add_trace(go.Scatter(x=sells['time'], y=sells['price'], mode='markers', 
                                                 name='SELL Signal', marker=dict(color='#FF4B4B', symbol='triangle-down', size=12)))
                        
                        fig.update_layout(title="Backtest Execution Map", template="plotly_dark", height=600, 
                                          xaxis_title="Time", yaxis_title="Price (USD)")
                        st.plotly_chart(fig, use_container_width=True)
                        
                        st.subheader("📜 成交明细")
                        st.dataframe(trades_df, use_container_width=True)
                    else:
                        st.warning("⚠️ 策略在当前参数下未产生任何交易。请尝试调整阈值或窗口大小。")

                # 清理临时文件
                if os.path.exists("temp_gui_bt_l2.parquet"):
                    os.remove("temp_gui_bt_l2.parquet")

    def run(self):
        tabs = st.tabs(["📊 行情预览", "🧱 L2 特征提取", "🧪 策略回测实验室"])
        with tabs[0]: 
            self.render_history_panel()
        with tabs[1]: 
            st.info("💡 L2 重构逻辑已集成到回测面板中。您可以在此预览原始数据质量。")
            # 这里可以保留之前的 L2 重构展示代码，如果需要独立预览的话
        with tabs[2]: 
            self.render_backtest_panel()

if __name__ == "__main__":
    NarciDashboard().run()