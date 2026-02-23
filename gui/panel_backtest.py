import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import os
import io
import contextlib
from datetime import date

from data.l2_reconstruct import L2Reconstructor
from backtest.backtest import BacktestEngine
from example.L2_Imbalance import ImbalanceStrategy
from example.L1_Trend import MyTrendStrategy
from gui.utils import get_filtered_files

def run_process(files, strategy, mode, initial_cash, fee):
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

def render(base_path, history_dir, realtime_dir):
    st.header("🧪 强化版策略回测实验室")
    
    c1, c2 = st.columns([1, 1])
    with c1:
        st.subheader("1. 数据源与范围配置")
        data_mode = st.radio("回测模式", ["L1 历史趋势 (aggTrades)", "L2 微观特征 (Realtime RAW)"])
        
        # 币种选择
        symbol_input = st.text_input("交易对筛选 (例如 ETHUSDT)", value="ETHUSDT").strip().upper()
        
        # 日期范围选择
        d_range = st.date_input("选择回测日期范围", [date(2026, 2, 1), date(2026, 2, 28)])
        
        selected_files = []
        if len(d_range) == 2:
            start, end = d_range
            target_dir = history_dir if "L1" in data_mode else realtime_dir
            selected_files = get_filtered_files(target_dir, start, end, symbol=symbol_input)
            
            if selected_files:
                st.success(f"📂 匹配到 {len(selected_files)} 个 {symbol_input} 数据文件")
                with st.expander("查看待处理文件列表"):
                    for f in selected_files:
                        st.text(f"- {os.path.relpath(f, base_path)}")
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
        
        run_process(selected_files, strategy, data_mode, initial_cash, fee)