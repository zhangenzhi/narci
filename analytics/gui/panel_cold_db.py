import streamlit as st
import pandas as pd
import pyarrow.parquet as pq
import os
import re
from datetime import datetime

def format_size(size_bytes):
    """格式化文件大小为人类可读格式"""
    if size_bytes < 1024: return f"{size_bytes} B"
    elif size_bytes < 1024**2: return f"{size_bytes/1024:.2f} KB"
    elif size_bytes < 1024**3: return f"{size_bytes/(1024**2):.2f} MB"
    else: return f"{size_bytes/(1024**3):.2f} GB"

@st.cache_data(ttl=60) # 缓存60秒，避免频繁扫描磁盘
def scan_cold_database(base_path):
    """扫描全站数据目录，构建资产视图"""
    data = []
    
    # 扫描路径列表
    l1_dir = os.path.join(base_path, "replay_buffer")
    l2_dir = os.path.join(base_path, "data", "realtime", "l2")
    l2_alt_dir = os.path.join(base_path, "replay_buffer", "realtime", "l2")
    val_dir = os.path.join(base_path, "replay_buffer", "official_validation")
    
    dirs_to_scan = [l1_dir, l2_dir, l2_alt_dir, val_dir]
    
    for d in dirs_to_scan:
        if not os.path.exists(d): continue
        for root, _, files in os.walk(d):
            for f in files:
                if f.endswith(('.parquet', '.csv', '.zip')):
                    path = os.path.join(root, f)
                    size = os.path.getsize(path)
                    
                    dtype = "未知"
                    symbol = "UNKNOWN"
                    date_str = "未知"
                    
                    # 1. 官方交叉验证数据
                    if "official_validation" in path:
                        dtype = "官方交叉校验 (L1 CSV)" if f.endswith('.csv') else "官方校验包 (ZIP)"
                        m_sym = re.search(r'([a-zA-Z0-9]+)-aggTrades', f)
                        if m_sym: symbol = m_sym.group(1).upper()
                        m_date = re.search(r'(\d{4}-\d{2}-\d{2})', f)
                        if m_date: date_str = m_date.group(1)
                        
                    # 2. L2 每日聚合冷数据 (最高优)
                    elif "DAILY" in f.upper():
                        dtype = "L2 聚合冷数据 (DAILY)"
                        # 按 _RAW_ 切分得到完整 symbol（兼容 Coincheck ETH_JPY 格式）
                        symbol = f.split("_RAW_")[0].upper() if "_RAW_" in f else f.split("_")[0].upper()
                        m_date = re.search(r'(\d{8})', f)
                        if m_date: date_str = datetime.strptime(m_date.group(1), "%Y%m%d").strftime("%Y-%m-%d")

                    # 3. L2 原始碎片 (1min)
                    elif "RAW" in f.upper():
                        dtype = "L2 录制碎片 (1min RAW)"
                        symbol = f.split("_RAW_")[0].upper() if "_RAW_" in f else f.split("_")[0].upper()
                        m_date = re.search(r'(\d{8})', f)
                        if m_date: date_str = datetime.strptime(m_date.group(1), "%Y%m%d").strftime("%Y-%m-%d")
                        
                    # 4. L1 历史行情
                    else:
                        dtype = "L1 历史行情 (Parquet)"
                        m_sym = f.split('-')[0]
                        if m_sym.isalpha(): symbol = m_sym.upper()
                        m_date = re.search(r'(\d{4}-\d{2}-\d{2})', f)
                        if m_date: date_str = m_date.group(1)
                    
                    data.append({
                        "交易对": symbol,
                        "日期": date_str,
                        "类型": dtype,
                        "文件大小": size,
                        "格式化大小": format_size(size),
                        "文件名": f,
                        "路径": path
                    })
    
    return pd.DataFrame(data)

def render(base_path):
    st.header("🗄️ 冷数据仓库 (Cold Data Warehouse)")
    st.markdown("本面板用于统一盘点、检索和管理系统中所有的量化落盘数据，包括 L1 历史归档、L2 聚合特征与交叉校验数据。")
    
    with st.spinner("正在盘点全局存储资产..."):
        df_assets = scan_cold_database(base_path)
    
    if df_assets.empty:
        st.warning("⚠️ 数据库目前为空。尚未扫描到任何 L1、L2 或交叉校验数据。")
        return

    # ---------------- 顶层过滤与统计 ----------------
    col_sym, col_type, col_date = st.columns(3)
    with col_sym:
        symbols = ["全部"] + sorted(df_assets["交易对"].unique().tolist())
        sel_symbol = st.selectbox("🎯 交易对筛选", symbols)
    with col_type:
        types = ["全部"] + sorted(df_assets["类型"].unique().tolist())
        sel_type = st.selectbox("📂 数据类型筛选", types)
    with col_date:
        dates = ["全部"] + sorted(df_assets[df_assets["日期"] != "未知"]["日期"].unique().tolist(), reverse=True)
        sel_date = st.selectbox("📅 日期筛选", dates)
        
    # 动态应用过滤
    filtered_df = df_assets.copy()
    if sel_symbol != "全部": filtered_df = filtered_df[filtered_df["交易对"] == sel_symbol]
    if sel_type != "全部": filtered_df = filtered_df[filtered_df["类型"] == sel_type]
    if sel_date != "全部": filtered_df = filtered_df[filtered_df["日期"] == sel_date]

    # 统计卡片
    total_size_bytes = filtered_df["文件大小"].sum()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("匹配文件总数", f"{len(filtered_df):,} 个")
    c2.metric("冷数据总占用", format_size(total_size_bytes))
    c3.metric("包含交易对数量", f"{filtered_df['交易对'].nunique()} 个")
    
    # 隐藏纯数字大小列，优化展示
    display_df = filtered_df.drop(columns=["文件大小"]).reset_index(drop=True)

    st.divider()
    
    # ---------------- 数据资产表格 ----------------
    st.subheader("📋 资产检索列表")
    
    # 使用带有行选择的 st.dataframe
    selection_event = st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        selection_mode="single-row",
        on_select="rerun"
    )
    
    # ---------------- 极速数据预览 (Data Previewer) ----------------
    selected_indices = selection_event.selection.rows
    if selected_indices:
        selected_row = display_df.iloc[selected_indices[0]]
        selected_path = selected_row["路径"]
        selected_type = selected_row["类型"]
        
        st.markdown(f"### 🔍 数据质量预览: `{selected_row['文件名']}`")
        
        with st.expander("查看文件内部特征 (极速采样首部数据)", expanded=True):
            try:
                if selected_path.endswith('.parquet'):
                    # 利用 PyArrow 仅读取元数据和前 100 行，极度节省内存且瞬间完成
                    parquet_file = pq.ParquetFile(selected_path)
                    st.text(f"📊 Schema (数据结构): {parquet_file.schema}")
                    st.text(f"🔢 总行数: {parquet_file.metadata.num_rows:,} 行")
                    
                    df_preview = parquet_file.read_row_group(0).to_pandas().head(100)
                    # 转换常见的时间戳
                    for time_col in ['timestamp', 'datetime', 'transact_time']:
                        if time_col in df_preview.columns and pd.api.types.is_numeric_dtype(df_preview[time_col]):
                            is_ms = df_preview[time_col].max() > 1e11
                            df_preview[f"{time_col}_解析"] = pd.to_datetime(df_preview[time_col], unit='ms' if is_ms else 's')
                            
                    st.dataframe(df_preview, use_container_width=True)
                    
                elif selected_path.endswith('.csv'):
                    # 对于官方校验 CSV，直接抽取前 100 行
                    df_preview = pd.read_csv(selected_path, nrows=100)
                    st.text(f"🔢 预计官方包含大量历史成交记录...")
                    if 'transact_time' in df_preview.columns:
                        df_preview['transact_time_解析'] = pd.to_datetime(df_preview['transact_time'], unit='ms')
                    st.dataframe(df_preview, use_container_width=True)
                    
                elif selected_path.endswith('.zip'):
                    st.info("📦 这是一个原始的 ZIP 归档包，需解压后才能查看内部 CSV。")
                    
            except Exception as e:
                st.error(f"读取预览失败: {e}")