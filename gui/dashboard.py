import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import os
import glob

# 页面配置
st.set_page_config(page_title="Binance 数据可视化终端", layout="wide")

class BinanceDashboard:
    def __init__(self, data_dir="./data/parquet"):
        self.data_dir = data_dir
        
    def get_available_files(self):
        """获取目录下所有的 parquet 文件"""
        files = glob.glob(os.path.join(self.data_dir, "*.parquet"))
        return sorted([os.path.basename(f) for f in files], reverse=True)

    @st.cache_data
    def load_data(_self, file_name):
        """加载数据并进行简单的预处理"""
        path = os.path.join(_self.data_dir, file_name)
        df = pd.read_parquet(path)
        # 确保时间戳已排序
        df = df.sort_values('timestamp')
        return df

    def render(self):
        st.title("📈 Binance aggTrades 数据分析面板")
        
        # 侧边栏：文件选择
        files = self.get_available_files()
        if not files:
            st.error(f"在 {self.data_dir} 未找到数据文件，请先运行 download.py")
            return
        
        selected_file = st.sidebar.selectbox("选择交易日期文件", files)
        resample_rule = st.sidebar.selectbox("K线聚合周期", ["1min", "5min", "15min", "1H"], index=0)
        
        # 加载数据
        df = self.load_data(selected_file)
        
        # 顶部指标卡
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("总成交笔数", f"{len(df):,}")
        with col2:
            st.metric("最高价", f"{df['price'].max():.2f}")
        with col3:
            st.metric("最低价", f"{df['price'].min():.2f}")
        with col4:
            st.metric("总成交量", f"{df['quantity'].sum():.2f}")

        # 数据处理：聚合为 OHLC
        df_ohlc = df.set_index('timestamp')['price'].resample(resample_rule).ohlc()
        df_vol = df.set_index('timestamp')['quantity'].resample(resample_rule).sum()
        df_plot = pd.concat([df_ohlc, df_vol], axis=1).dropna()

        # 绘制主图
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, 
                           vertical_spacing=0.03, subplot_titles=('价格 K线', '成交量'), 
                           row_width=[0.2, 0.7])

        # K线图
        fig.add_trace(go.Candlestick(
            x=df_plot.index,
            open=df_plot['open'],
            high=df_plot['high'],
            low=df_plot['low'],
            close=df_plot['close'],
            name="价格"
        ), row=1, col=1)

        # 成交量图
        fig.add_trace(go.Bar(
            x=df_plot.index,
            y=df_plot['quantity'],
            name="成交量",
            marker_color='rgba(0, 0, 255, 0.5)'
        ), row=2, col=1)

        fig.update_layout(
            height=800,
            xaxis_rangeslider_visible=False,
            template="plotly_dark",
            showlegend=False
        )
        
        st.plotly_chart(fig, use_container_width=True)

        # 详细数据预览
        with st.expander("查看原始成交数据 (前100条)"):
            st.dataframe(df.head(100), use_container_width=True)
            
        # 交易压力分析 (买方 vs 卖方)
        st.subheader("📊 市场多空分布")
        maker_counts = df['is_buyer_maker'].value_counts()
        
        # 这里的 logic: is_buyer_maker=True 代表卖方成交，False 代表买方成交
        pie_fig = go.Figure(data=[go.Pie(
            labels=['买方主动成交 (Taker Buy)', '卖方主动成交 (Taker Sell)'], 
            values=[maker_counts.get(False, 0), maker_counts.get(True, 0)],
            hole=.3,
            marker_colors=['#26a69a', '#ef5350']
        )])
        pie_fig.update_layout(template="plotly_dark")
        st.plotly_chart(pie_fig)

if __name__ == "__main__":
    dashboard = BinanceDashboard()
    dashboard.render()