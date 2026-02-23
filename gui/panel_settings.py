import streamlit as st

def render(root_dir, history_dir, realtime_dir):
    st.subheader("📁 目录配置")
    st.text(f"项目根目录: {root_dir}")
    st.text(f"L1 搜索路径: {history_dir}")
    st.text(f"L2 搜索路径: {realtime_dir}")