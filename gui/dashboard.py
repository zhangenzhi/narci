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
import importlib.util

# 动态添加项目根目录到 sys.path 以便导入模块
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(current_dir)
if root_dir not in sys.path:
    sys.path.append(root_dir)

from data.l2_reconstruct import L2Reconstructor
from backtest.backtest import BacktestEngine
from backtest.strategy import MyTrendStrategy

st.set_page_config(page_title="Narci Quant Terminal", layout="wide", page_icon="💹")

class NarciDashboard:
    def __init__(self):
        self.base_path = root_dir
        self.history_dir = os.path.join(self.base_path, "replay_buffer", "parquet", "aggTrades")
        # 兼容 download.py 中配置的路径或默认路径
        if not os.path.exists(self.history_dir):
             self.history_dir = os.path.join(self.base_path, "data", "parquet", "aggTrades")
             
        self.realtime_dir = os.path.join(self.base_path, "data", "realtime", "l2")

    def get_files(self, directory, pattern="*.parquet"):
        if not os.path.exists(directory):
            return []
        # 递归搜索或单层搜索
        files = []
        for root, dirs, filenames in os.walk(directory):
            for filename in filenames:
                if filename.endswith(".parquet"):
                    files.append(os.path.join(root, filename))
        return sorted(files, reverse=True)

    def render_history_panel(self):
        st.header("📈 历史行情分析 (L1 Data)")
        files = self.get_files(self.history_dir)
        if not files:
            st.warning(f"未在 {self.history_dir} 找到数据，请先运行 download 命令。")
            return

        selected_path = st.selectbox("选择历史数据文件", files, format_func=lambda x: os.path.basename(x))
        
        if selected_path:
            with st.spinner("正在加载数据..."):
                df = pd.read_parquet(selected_path)
                df = df.sort_values('timestamp')
                
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("交易对", selected_path.split(os.sep)[-2])
            col2.metric("数据量", f"{len(df):,}")
            col3.metric("加权均价 (VWAP)", f"{(df['price']*df['quantity']).sum()/df['quantity'].sum():.2f}")
            col4.metric("时间跨度", f"{df['timestamp'].max() - df['timestamp'].min()}")

            # K线重采样
            rule = st.select_slider("K线周期", options=["1s", "1min", "5min", "15min", "1H"], value="5min")
            df_ohlc = df.set_index('timestamp')['price'].resample(rule).ohlc()
            df_vol = df.set_index('timestamp')['quantity'].resample(rule).sum()

            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.02)
            fig.add_trace(go.Candlestick(x=df_ohlc.index, open=df_ohlc['open'], high=df_ohlc['high'], 
                                         low=df_ohlc['low'], close=df_ohlc['close'], name='OHLC'), row=1, col=1)
            fig.add_trace(go.Bar(x=df_vol.index, y=df_vol, name='Volume'), row=2, col=1)
            fig.update_layout(height=600, xaxis_rangeslider_visible=False, template="plotly_dark")
            st.plotly_chart(fig, use_container_width=True)

    def render_realtime_panel(self):
        st.header("🔴 实时录制监控 (L2 Recorder)")
        files = self.get_files(self.realtime_dir)
        if not files:
            st.warning(f"未在 {self.realtime_dir} 找到录制数据，请先运行 l2record 命令。")
            return

        latest_file = st.selectbox("选择录制片段", files, format_func=lambda x: os.path.basename(x))
        
        if st.button("刷新数据状态"):
            st.rerun()

        if latest_file:
            df = pd.read_parquet(latest_file)
            st.info(f"文件路径: {latest_file}")
            
            # 统计不同类型的消息
            type_map = {0: 'Bid Diff', 1: 'Ask Diff', 2: 'Trade', 3: 'Snap Bid', 4: 'Snap Ask'}
            stats = df['side'].map(type_map).value_counts()
            
            c1, c2 = st.columns([1, 2])
            with c1:
                st.dataframe(stats, use_container_width=True)
            with c2:
                fig = px.pie(values=stats.values, names=stats.index, title="消息类型分布")
                st.plotly_chart(fig, use_container_width=True)

            st.subheader("最新原始消息流")
            st.dataframe(df.tail(20), use_container_width=True)

    def render_l2_reconstruct_panel(self):
        st.header("🧱 L2 订单簿重构与微观特征")
        st.markdown("该模块读取原始混合流（快照+增量），在内存中重建订单簿并计算特征。")
        
        files = self.get_files(self.realtime_dir)
        if not files:
            st.warning("需要录制的原始数据来进行重构。")
            return

        target_file = st.selectbox("选择原始数据源", files, key='l2_file', format_func=lambda x: os.path.basename(x))
        depth_limit = st.slider("重建深度 (Levels)", 5, 50, 20)
        sample_interval = st.number_input("采样间隔 (ms)", 100, 5000, 1000)

        if st.button("开始重建与计算"):
            with st.spinner("正在重放订单流 (这可能需要几秒钟)..."):
                recon = L2Reconstructor(depth_limit=depth_limit)
                # 使用 generate_l2_dataset 方法获取时序特征
                df_l2 = recon.generate_l2_dataset(target_file, sample_interval_ms=sample_interval)
            
            if not df_l2.empty:
                st.success(f"重建完成! 生成样本数: {len(df_l2)}")
                
                # 可视化 1: 盘口价格与 Imbalance
                fig1 = make_subplots(specs=[[{"secondary_y": True}]])
                fig1.add_trace(go.Scatter(x=df_l2['timestamp'], y=df_l2['mid_price'], name="Mid Price"), secondary_y=False)
                fig1.add_trace(go.Scatter(x=df_l2['timestamp'], y=df_l2['imbalance'], name="Order Imbalance", 
                                          line=dict(color='rgba(255, 100, 0, 0.5)')), secondary_y=True)
                fig1.update_yaxes(title_text="Price", secondary_y=False)
                fig1.update_yaxes(title_text="Imbalance (-1 to 1)", secondary_y=True)
                fig1.update_layout(title="中间价 vs 订单流不平衡度 (OFI)", template="plotly_dark")
                st.plotly_chart(fig1, use_container_width=True)

                # 可视化 2: 某一时刻的 Orderbook 形状
                st.subheader("订单簿快照透视")
                idx = st.slider("选择时间切片索引", 0, len(df_l2)-1, len(df_l2)//2)
                row = df_l2.iloc[idx]
                
                bids_x, bids_y = [], []
                asks_x, asks_y = [], []
                for i in range(depth_limit):
                    if f'b_p_{i}' in row:
                        bids_x.append(row[f'b_p_{i}'])
                        bids_y.append(row[f'b_q_{i}'])
                    if f'a_p_{i}' in row:
                        asks_x.append(row[f'a_p_{i}'])
                        asks_y.append(row[f'a_q_{i}'])
                
                fig2 = go.Figure()
                fig2.add_trace(go.Bar(x=bids_x, y=bids_y, name='Bids', marker_color='green'))
                fig2.add_trace(go.Bar(x=asks_x, y=asks_y, name='Asks', marker_color='red'))
                fig2.update_layout(title=f"Orderbook Depth @ {pd.to_datetime(row['timestamp'], unit='ms')}", 
                                   xaxis_title="Price", yaxis_title="Quantity", template="plotly_dark")
                st.plotly_chart(fig2, use_container_width=True)
            else:
                st.error("重建结果为空，可能是数据中缺失快照（Snapshot）导致无法初始化。")

    def render_backtest_panel(self):
        st.header("🧪 策略回测实验室")
        
        col1, col2 = st.columns(2)
        with col1:
            # 自动扫描 data/parquet 下的所有文件
            data_files = self.get_files(self.history_dir)
            # 也加入实时录制的文件用于回测
            realtime_files = self.get_files(self.realtime_dir)
            all_files = data_files + realtime_files
            
            selected_data = st.selectbox("选择回测数据", all_files, format_func=lambda x: f"[{'HIST' if 'aggTrades' in x else 'REAL'}] {os.path.basename(x)}")
        
        with col2:
            initial_cash = st.number_input("初始资金 ($)", 1000, 1000000, 10000)
            window_size = st.number_input("策略参数: 窗口大小", 10, 2000, 500)
            fee = st.number_input("手续费率", 0.0, 0.01, 0.001, format="%.4f")

        if st.button("🚀 运行回测"):
            if not selected_data or not os.path.exists(selected_data):
                st.error("文件不存在")
                return

            # 初始化策略和引擎
            strategy = MyTrendStrategy(window_size=int(window_size))
            engine = BacktestEngine(selected_data, strategy, initial_cash=initial_cash, fee_rate=fee)

            # 捕获标准输出
            output_buffer = io.StringIO()
            with contextlib.redirect_stdout(output_buffer):
                try:
                    engine.run()
                except Exception as e:
                    print(f"回测出错: {e}")
            
            # 解析结果
            logs = output_buffer.getvalue()
            st.text_area("回测日志", logs, height=300)
            
            # 可视化交易点（如果策略记录了）
            if hasattr(engine.broker, 'trades') and engine.broker.trades:
                trades_df = pd.DataFrame(engine.broker.trades)
                st.dataframe(trades_df)
                
                # 简单的盈亏曲线（近似）
                if 'equity' in logs:
                    # 如果需要精确的 Equity Curve，需要修改 BacktestEngine 记录每个 tick 的 equity
                    pass
                
                # 简单的买卖点标记图
                if not trades_df.empty:
                    try:
                        price_df = pd.read_parquet(selected_data).sort_values('timestamp')
                        # 降采样以绘图
                        price_df = price_df.iloc[::100] 
                        fig = go.Figure()
                        fig.add_trace(go.Scatter(x=price_df['timestamp'], y=price_df['price'], name='Price', line=dict(color='gray')))
                        
                        buys = trades_df[trades_df['side'] == 'BUY']
                        sells = trades_df[trades_df['side'] == 'SELL']
                        
                        fig.add_trace(go.Scatter(x=buys['time'], y=buys['price'], mode='markers', name='Buy', marker=dict(color='green', size=10, symbol='triangle-up')))
                        fig.add_trace(go.Scatter(x=sells['time'], y=sells['price'], mode='markers', name='Sell', marker=dict(color='red', size=10, symbol='triangle-down')))
                        
                        fig.update_layout(title="回测交易分布图", template="plotly_dark")
                        st.plotly_chart(fig, use_container_width=True)
                    except Exception as e:
                        st.warning(f"无法绘制交易图表: {e}")


    def run(self):
        tab1, tab2, tab3, tab4 = st.tabs(["📊 历史数据", "🔴 实时监控", "🧱 L2 重构", "🧪 策略回测"])
        
        with tab1:
            self.render_history_panel()
        with tab2:
            self.render_realtime_panel()
        with tab3:
            self.render_l2_reconstruct_panel()
        with tab4:
            self.render_backtest_panel()

if __name__ == "__main__":
    app = NarciDashboard()
    app.run()