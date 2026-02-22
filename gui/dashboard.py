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
import re
from datetime import datetime, date

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
        self.history_dir = os.path.join(self.base_path, "replay_buffer", "parquet")
        # L2 数据存放路径 (l2_recorder.py 默认生成路径)
        self.realtime_dir = os.path.join(self.base_path, "data", "realtime", "l2")
        
        # 路径自动纠偏：确保能找到数据
        if not os.path.exists(self.history_dir):
             self.history_dir = os.path.join(self.base_path, "replay_buffer")
        
        # 为 L2 录制目录增加纠偏，优先查找 replay_buffer 下的录制目录
        alt_realtime_dir = os.path.join(self.base_path, "replay_buffer", "realtime", "l2")
        if os.path.exists(alt_realtime_dir):
            self.realtime_dir = alt_realtime_dir

    def get_all_parquet_files(self, directory):
        """递归获取目录下所有 parquet 文件及其完整路径"""
        if not os.path.exists(directory):
            return []
        
        parquet_files = []
        for root, _, filenames in os.walk(directory):
            for filename in filenames:
                if filename.endswith(".parquet"):
                    parquet_files.append(os.path.join(root, filename))
        return parquet_files

    def get_filtered_files(self, directory, start_date, end_date, symbol=None):
        """根据日期范围和币种筛选 Parquet 文件"""
        all_paths = self.get_all_parquet_files(directory)
        filtered = []
        
        for f_path in all_paths:
            filename = os.path.basename(f_path)
            
            # 1. 币种过滤 (如果指定了 symbol)
            if symbol and symbol.upper() not in filename.upper() and symbol.upper() not in f_path.upper():
                continue
                
            try:
                # 2. 日期过滤: 
                # 匹配 YYYY-MM-DD (L1) 或 YYYYMMDD (L2 RAW)
                date_match = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
                if date_match:
                    file_date = datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
                else:
                    # 尝试匹配无横线的格式 20260222
                    date_match_raw = re.search(r'(\d{8})', filename)
                    if date_match_raw:
                        file_date = datetime.strptime(date_match_raw.group(1), "%Y%m%d").date()
                    else:
                        continue

                if start_date <= file_date <= end_date:
                    filtered.append(f_path)
            except Exception:
                continue
                
        # 按文件名排序，确保回测时间顺序
        return sorted(filtered)

    def render_history_panel(self):
        st.header("📈 历史行情预览 (L1 Data)")
        all_files = self.get_all_parquet_files(self.history_dir)
        
        if not all_files:
            st.warning(f"⚠️ 文件夹 {self.history_dir} 中未找到任何数据。")
            return
            
        selected_path = st.selectbox("选择预览文件", all_files, format_func=lambda x: os.path.relpath(x, self.base_path))
        
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
        st.header("🧪 强化版策略回测实验室")
        
        c1, c2 = st.columns([1, 1])
        with c1:
            st.subheader("1. 数据源与范围配置")
            data_mode = st.radio("回测模式", ["L1 历史趋势 (aggTrades)", "L2 微观特征 (Realtime RAW)"])
            
            # 新增：币种选择，用于精准过滤路径
            symbol_input = st.text_input("交易对筛选 (例如 ETHUSDT)", value="ETHUSDT").strip().upper()
            
            # 日期范围选择
            today = date.today()
            # 默认范围设为包含你数据的日期
            d_range = st.date_input("选择回测日期范围", [date(2026, 2, 1), date(2026, 2, 28)])
            
            selected_files = []
            if len(d_range) == 2:
                start, end = d_range
                target_dir = self.history_dir if "L1" in data_mode else self.realtime_dir
                selected_files = self.get_filtered_files(target_dir, start, end, symbol=symbol_input)
                
                if selected_files:
                    st.success(f"📂 匹配到 {len(selected_files)} 个 {symbol_input} 数据文件")
                    with st.expander("查看待处理文件列表"):
                        for f in selected_files:
                            st.text(f"- {os.path.relpath(f, self.base_path)}")
                else:
                    st.error(f"📍 在 {target_dir} 下未找到 {symbol_input} 的匹配数据")
                    st.info("提示：请检查文件名是否包含 YYYY-MM-DD 或 YYYYMMDD 格式。")

            strategy_options = ["趋势跟踪 (MyTrendStrategy)"] if "L1" in data_mode else ["盘口不平衡 (ImbalanceStrategy)"]
            selected_strat_name = st.selectbox("选择加载策略", strategy_options)
            
        with c2:
            st.subheader("2. 交易参数设置")
            initial_cash = st.number_input("初始资金 (USD)", 1000, 1000000, 10000)
            fee = st.number_input("费率 (例如 0.0001 代表 0.01%)", 0.0, 0.01, 0.0001, format="%.4f")
            
            st.divider()
            if "MyTrendStrategy" in selected_strat_name:
                window = st.slider("均线窗口大小", 10, 2000, 500)
                strategy = MyTrendStrategy(window_size=window)
            else:
                in_threshold = st.slider("入场阈值 (Imbalance > X)", 0.0, 1.0, 0.3)
                out_threshold = st.slider("出场阈值 (Imbalance < Y)", -1.0, 0.0, -0.1)
                strategy = ImbalanceStrategy(entry_threshold=in_threshold, exit_threshold=out_threshold)

        if st.button("🚀 启动跨文件回测引擎", type="primary", use_container_width=True):
            if not selected_files:
                st.error("❌ 无法开始：未选中任何有效文件。")
                return
            
            self.run_process(selected_files, strategy, data_mode, initial_cash, fee)

    def run_process(self, files, strategy, mode, initial_cash, fee):
        """执行回测处理流"""
        with st.spinner("正在串联数据并执行回测..."):
            final_data_source = files
            
            if "L2" in mode:
                recon = L2Reconstructor(depth_limit=10)
                all_feats = []
                progress_text = "正在重构 L2 特征..."
                my_bar = st.progress(0, text=progress_text)
                
                for i, f_path in enumerate(files):
                    df_feat = recon.generate_l2_dataset(f_path, sample_interval_ms=100)
                    if not df_feat.empty:
                        all_feats.append(df_feat)
                    my_bar.progress((i + 1) / len(files), text=f"{progress_text} ({i+1}/{len(files)})")
                
                if not all_feats:
                    st.error("❌ 重构失败：所选文件中未发现有效的订单簿快照数据。")
                    return
                
                combined_df = pd.concat(all_feats).sort_values('timestamp')
                combined_df['price'] = combined_df['mid_price']
                
                final_data_source = "temp_multi_bt_l2.parquet"
                combined_df.to_parquet(final_data_source, index=False)

            # 初始化回测引擎
            engine = BacktestEngine(final_data_source, strategy, initial_cash=initial_cash, fee_rate=fee)
            
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                try:
                    engine.run()
                except Exception as e:
                    st.error(f"回测运行时发生崩溃: {e}")
            
            st.success(f"✅ 回测完成！处理了 {len(files)} 个文件。")
            
            res_col1, res_col2 = st.columns([1, 2])
            with res_col1:
                st.subheader("📊 统计报告")
                st.code(output.getvalue())
            
            with res_col2:
                if engine.broker.trades:
                    trades_df = pd.DataFrame(engine.broker.trades)
                    st.subheader("📈 交易分布预览")
                    
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=trades_df['time'], y=trades_df['price'], mode='markers+lines',
                                             name='Price At Trade', line=dict(color='gold', width=1)))
                    
                    buys = trades_df[trades_df['side'] == 'BUY']
                    sells = trades_df[trades_df['side'] == 'SELL']
                    
                    fig.add_trace(go.Scatter(x=buys['time'], y=buys['price'], mode='markers', 
                                             name='BUY', marker=dict(color='#00FF00', symbol='triangle-up', size=10)))
                    fig.add_trace(go.Scatter(x=sells['time'], y=sells['price'], mode='markers', 
                                             name='SELL', marker=dict(color='#FF4B4B', symbol='triangle-down', size=10)))
                    
                    fig.update_layout(template="plotly_dark", height=500, title="Trade Execution Timeline")
                    st.plotly_chart(fig, use_container_width=True)
                    
                    st.subheader("📜 最近成交明细")
                    st.dataframe(trades_df.tail(100), use_container_width=True)
                else:
                    st.warning("⚠️ 策略在当前日期范围内未产生任何交易。")

            if os.path.exists("temp_multi_bt_l2.parquet"):
                os.remove("temp_multi_bt_l2.parquet")

    def run(self):
        tabs = st.tabs(["📊 行情预览", "🧪 强化版回测室", "🔧 系统设置"])
        with tabs[0]: 
            self.render_history_panel()
        with tabs[1]: 
            self.render_backtest_panel()
        with tabs[2]:
            st.subheader("📁 目录配置")
            st.text(f"项目根目录: {root_dir}")
            st.text(f"L1 搜索路径: {self.history_dir}")
            st.text(f"L2 搜索路径: {self.realtime_dir}")

if __name__ == "__main__":
    NarciDashboard().run()