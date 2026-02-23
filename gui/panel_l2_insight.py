import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import os

from data.l2_reconstruct import L2Reconstructor
from gui.utils import get_all_parquet_files

def render(base_path, realtime_dir):
    st.header("🔬 L2 盘口微观结构重建与可视化")
    
    all_raw_files = get_all_parquet_files(realtime_dir)
    # 仅筛选 RAW 录制文件
    raw_files = [f for f in all_raw_files if "RAW" in f.upper()]
    
    if not raw_files:
        st.warning(f"⚠️ 未在 {realtime_dir} 中找到任何包含 'RAW' 的 L2 录制文件。")
        return
        
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        selected_path = st.selectbox("选择要重建的 L2 原始数据文件", raw_files, format_func=lambda x: os.path.relpath(x, base_path))
    with col2:
        sample_interval = st.selectbox("重采样间隔 (毫秒)", [10, 50, 100, 500, 1000], index=2)
    with col3:
        depth_limit = st.slider("重建深度档位", min_value=5, max_value=20, value=10)

    # 缓存重建结果，避免每次交互都重新计算
    @st.cache_data
    def process_l2_data(filepath, interval, limit):
        recon = L2Reconstructor(depth_limit=limit)
        df = recon.generate_l2_dataset(filepath, sample_interval_ms=interval)
        if not df.empty:
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    
    if selected_path:
        with st.spinner("正在逐笔重建微观订单簿... 这可能需要几秒钟时间"):
            df_l2 = process_l2_data(selected_path, sample_interval, depth_limit)
            
        if df_l2.empty:
            st.error("❌ 重建失败：数据为空，或该文件中未发现初始快照 (Side 3/4)，无法重构盘口。")
            return
        
        st.success(f"✅ L2 盘口重建成功！共生成 **{len(df_l2)}** 个时间切片。")
        
        tab_price, tab_depth, tab_data = st.tabs(["📊 盘口价格与指标走势", "🧊 动态深度截面图", "📋 原始重构数据"])
        
        with tab_price:
            st.subheader("1. 盘口最优报价 (Top of Book)")
            fig_price = go.Figure()
            fig_price.add_trace(go.Scatter(x=df_l2['datetime'], y=df_l2['a_p_0'], mode='lines', name='Ask 1 (卖一)', line=dict(color='#FF4B4B', width=1.5)))
            fig_price.add_trace(go.Scatter(x=df_l2['datetime'], y=df_l2['b_p_0'], mode='lines', name='Bid 1 (买一)', line=dict(color='#00FF00', width=1.5)))
            fig_price.add_trace(go.Scatter(x=df_l2['datetime'], y=df_l2['mid_price'], mode='lines', name='Mid Price (中间价)', line=dict(color='yellow', width=1, dash='dot')))
            fig_price.update_layout(template="plotly_dark", height=400, hovermode="x unified", margin=dict(l=0, r=0, t=30, b=0))
            st.plotly_chart(fig_price, use_container_width=True)

            st.subheader("2. 盘口微观指标 (Imbalance & Spread)")
            fig_metrics = make_subplots(specs=[[{"secondary_y": True}]])
            fig_metrics.add_trace(go.Scatter(x=df_l2['datetime'], y=df_l2['imbalance'], mode='lines', name='Imbalance (买盘偏度)', line=dict(color='cyan', width=1.5)), secondary_y=False)
            fig_metrics.add_trace(go.Scatter(x=df_l2['datetime'], y=df_l2['spread'], mode='lines', name='Spread (价差)', line=dict(color='orange', width=1, dash='dot')), secondary_y=True)
            fig_metrics.update_layout(template="plotly_dark", height=400, hovermode="x unified", margin=dict(l=0, r=0, t=30, b=0))
            fig_metrics.update_yaxes(title_text="Imbalance (-1 to 1)", secondary_y=False, range=[-1.1, 1.1])
            fig_metrics.update_yaxes(title_text="Spread (Price Diff)", secondary_y=True)
            st.plotly_chart(fig_metrics, use_container_width=True)
            
        with tab_depth:
            st.subheader("3. 动态订单簿截面 (Orderbook Depth Chart)")
            row_idx = st.slider("滑动选择时间切片查看深度", 0, len(df_l2)-1, 0, format="第 %d 帧")
            row = df_l2.iloc[row_idx]
            
            bids_p, bids_q, asks_p, asks_q = [], [], [], []
            for i in range(depth_limit):
                if f'b_p_{i}' in row and not pd.isna(row[f'b_p_{i}']):
                    bids_p.append(row[f'b_p_{i}'])
                    bids_q.append(row[f'b_q_{i}'])
                if f'a_p_{i}' in row and not pd.isna(row[f'a_p_{i}']):
                    asks_p.append(row[f'a_p_{i}'])
                    asks_q.append(row[f'a_q_{i}'])
            
            # 买单需要逆序累加计算累计深度 (因为 b_p_0 最高，往后越来越低)
            bids_q_cum = np.cumsum(bids_q)
            asks_q_cum = np.cumsum(asks_q)
            
            fig_depth = go.Figure()
            fig_depth.add_trace(go.Scatter(x=bids_p, y=bids_q_cum, fill='tozeroy', mode='lines', name='Bids (买单累计)', line=dict(color='#00FF00', shape='hv')))
            fig_depth.add_trace(go.Scatter(x=asks_p, y=asks_q_cum, fill='tozeroy', mode='lines', name='Asks (卖单累计)', line=dict(color='#FF4B4B', shape='hv')))
            
            fig_depth.update_layout(
                title=f"快照时间: {row['datetime']}",
                xaxis_title="Price (价格)",
                yaxis_title="Cumulative Quantity (累计挂单量)",
                template="plotly_dark",
                height=500
            )
            st.plotly_chart(fig_depth, use_container_width=True)

        with tab_data:
            st.dataframe(df_l2.head(200), use_container_width=True)