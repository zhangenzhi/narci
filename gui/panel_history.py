import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import os
from gui.utils import get_all_parquet_files

def render(base_path, history_dir):
    st.header("📈 历史行情预览 (L1 Data)")
    all_files = get_all_parquet_files(history_dir)
    
    if not all_files:
        st.warning(f"⚠️ 文件夹 {history_dir} 中未找到任何数据。")
        return
        
    selected_path = st.selectbox("选择预览文件", all_files, format_func=lambda x: os.path.relpath(x, base_path))
    
    if selected_path:
        with st.spinner("加载数据中..."):
            df = pd.read_parquet(selected_path).sort_values('timestamp')
        
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("数据条数", f"{len(df):,}")
        col2.metric("最高价", f"{df['price'].max():.2f}")
        col3.metric("最低价", f"{df['price'].min():.2f}")
        col4.metric("平均成交量", f"{df['quantity'].mean():.4f}")

        rule = st.select_slider("可视化聚合周期", options=["1s", "10s", "1min", "5min"], value="1min")
        # 确保时间戳是 datetime
        if not pd.api.types.is_datetime64_any_dtype(df['timestamp']):
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            
        df_ohlc = df.set_index('timestamp')['price'].resample(rule).ohlc()
        
        fig = go.Figure(data=[go.Candlestick(
            x=df_ohlc.index, open=df_ohlc['open'], high=df_ohlc['high'], 
            low=df_ohlc['low'], close=df_ohlc['close'], name='K-Line'
        )])
        fig.update_layout(height=500, template="plotly_dark", xaxis_rangeslider_visible=False, title=f"价格走势 ({rule})")
        st.plotly_chart(fig, use_container_width=True)