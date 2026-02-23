import streamlit as st
import os
import sys

# 动态添加项目根目录到 sys.path，保证可以引入其它模块
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(current_dir)
if root_dir not in sys.path:
    sys.path.append(root_dir)

# 从拆分的文件中导入各自的渲染函数
from gui.panel_history import render as render_history
from gui.panel_l2_insight import render as render_l2_insight
from gui.panel_backtest import render as render_backtest
from gui.panel_settings import render as render_settings

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
        
        # 为 L2 录制目录增加纠偏
        alt_realtime_dir = os.path.join(self.base_path, "replay_buffer", "realtime", "l2")
        if os.path.exists(alt_realtime_dir):
            self.realtime_dir = alt_realtime_dir

    def run(self):
        tabs = st.tabs(["📊 L1 行情预览", "🔬 L2 盘口洞察", "🧪 强化版回测室", "🔧 系统设置"])
        
        with tabs[0]: 
            render_history(self.base_path, self.history_dir)
        with tabs[1]:
            render_l2_insight(self.base_path, self.realtime_dir)
        with tabs[2]: 
            render_backtest(self.base_path, self.history_dir, self.realtime_dir)
        with tabs[3]:
            render_settings(self.base_path, self.history_dir, self.realtime_dir)

if __name__ == "__main__":
    NarciDashboard().run()