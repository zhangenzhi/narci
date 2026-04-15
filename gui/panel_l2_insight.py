import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import os
import time
from datetime import datetime
import pyarrow.dataset as ds
import pyarrow as pa
import pyarrow.parquet as pq

from data.l2_reconstruct import L2Reconstructor
from gui.utils import get_all_parquet_files

def render(base_path, realtime_dir):
    st.header("🔬 L2 盘口微观结构重建与可视化")
    
    # 初始化 session state 用于存储已导入的路径，防止页面刷新时数据丢失
    if "insight_selected_paths" not in st.session_state:
        st.session_state.insight_selected_paths = tuple()
    
    # --- 1. 物理隔离适配：选择交易所 + 市场 + 交易对 ---
    # 目录结构: realtime/{exchange}/{market}/l2/   (新)
    #           realtime/{market}/l2/              (旧版兼容)
    exchanges = []
    if os.path.isdir(realtime_dir):
        for e in sorted(os.listdir(realtime_dir)):
            if os.path.isdir(os.path.join(realtime_dir, e)):
                exchanges.append(e)

    c_m0, c_m1, c_m2 = st.columns(3)
    with c_m0:
        exchange = st.selectbox("🏪 交易所", exchanges or ["binance"])
    with c_m1:
        # 动态发现该交易所下的市场
        exch_dir = os.path.join(realtime_dir, exchange) if exchange else realtime_dir
        markets = []
        if os.path.isdir(exch_dir):
            for m in sorted(os.listdir(exch_dir)):
                if os.path.isdir(os.path.join(exch_dir, m, "l2")):
                    markets.append(m)
        market_type = st.selectbox("🎯 市场类型", markets or ["um_futures", "spot"])

    # 构建实际数据目录（支持三种结构）
    candidates = [
        os.path.join(realtime_dir, exchange, market_type, "l2"),  # 新
        os.path.join(realtime_dir, market_type, "l2"),            # 旧
        realtime_dir,                                              # 兜底
    ]
    market_dir = next((d for d in candidates if os.path.isdir(d)), realtime_dir)

    all_raw_files = get_all_parquet_files(market_dir)
    
    # 拆分 SYMBOL_RAW_YYYYMMDD_HHMMSS.parquet：按 _RAW_ 切分得到完整 symbol（兼容 ETH_JPY 这种含下划线的 Coincheck 格式）
    available_symbols = sorted(set(
        os.path.basename(f).split("_RAW_")[0].upper()
        for f in all_raw_files if "_RAW_" in f.upper()
    ))
    with c_m2:
        selected_symbol = st.selectbox("🪙 交易对筛选", ["全部"] + available_symbols)
        
    raw_files = sorted([f for f in all_raw_files if "RAW" in f.upper()])
    if selected_symbol != "全部":
        # 用 lowercase 前缀匹配，兼容 Coincheck 的 btc_jpy 和 Binance 的 BTCUSDT
        raw_files = [f for f in raw_files
                     if os.path.basename(f).split("_RAW_")[0].upper() == selected_symbol]
    
    if not raw_files:
        st.warning(f"⚠️ 在目录 {market_dir} 中未找到匹配的 L2 录制文件。")
        return
        
    st.write(f"📌 **当前检索范围: {market_type} / {selected_symbol} (共 {len(raw_files)} 个碎片)**")
    st.info("💡 **操作提示**：\n"
            "1. **单选**：直接点击行。\n"
            "2. **范围连选**：点击起始行，按住键盘 `Shift` 键，再点击结束行。\n"
            "3. **间隔多选**：按住 `Ctrl` (Mac 为 `Cmd`) 键点击多行。\n"
            "4. **快捷全选**：点击表格内部后按 `Ctrl + A` (Mac 为 `Cmd + A`)。也可以直接点击右下角的【全选并导入】按钮。")
    
    # 构造文件列表的 DataFrame
    file_records = []
    for f in raw_files:
        display_name = os.path.basename(f).split('_RAW_')[-1].replace('.parquet', '')
        file_records.append({"时间段标号": display_name, "_path": f})
    
    df_files = pd.DataFrame(file_records)
    
    # 使用 st.dataframe 原生的多行选中功能 (支持 Shift 连选)
    selection_event = st.dataframe(
        df_files[["时间段标号"]], # 隐藏实际路径列，仅展示标号
        hide_index=False,
        use_container_width=True,
        selection_mode="multi-row",
        on_select="rerun",
        height=250 # 固定高度，内容多时可滚动
    )
    
    # 提取被选中的行索引，并映射回实际文件路径
    selected_indices = selection_event.selection.rows
    current_selected_paths = [df_files.iloc[i]["_path"] for i in selected_indices]
        
    col1, col2, col3, col4 = st.columns([1, 1, 1, 1])
    with col1:
        sample_interval = st.selectbox("重采样间隔 (毫秒)", [10, 50, 100, 500, 1000], index=2)
    with col2:
        depth_limit = st.slider("重建深度档位", min_value=5, max_value=20, value=10)
    with col3:
        st.write("") # 占位，对齐按钮
        st.write("")
        # 导入按钮：点击后将选中的文件列表存入 session_state 并触发后续处理
        if st.button("📥 导入选定范围", type="primary", use_container_width=True):
            if not current_selected_paths:
                st.warning("请先在上方列表中至少选中一行文件。")
            else:
                st.session_state.insight_selected_paths = tuple(current_selected_paths)
    with col4:
        st.write("") # 占位，对齐按钮
        st.write("")
        # 快捷全选按钮：无视当前选中状态，直接拉取全部列表加载
        if st.button("📑 全选并导入全部", type="secondary", use_container_width=True):
            st.session_state.insight_selected_paths = tuple(df_files["_path"].tolist())

    # --- 核心性能重构方法：统一处理 L2 和 L1 ---
    @st.cache_data(show_spinner=False)
    def process_data_v2(filepaths, interval, limit):
        if not filepaths: return pd.DataFrame(), pd.DataFrame()
        
        # 内部创建 UI 占位符，执行完毕后清空
        ui_box = st.empty()
        with ui_box.container():
            st.markdown("##### ⚙️ L2/L1 特征流极速重构引擎 (纯内存计算)")
            c_text, c_prog = st.columns([2, 3])
            _status_text = c_text.empty()
            _progress_bar = c_prog.progress(0.0)
        
        start_time = time.time()
        
        # [Step 1] 极速聚合
        _status_text.markdown("**[1/2]** 📂 正在通过 PyArrow 引擎聚合内存数据并执行原子级时间轴对齐...")
        _progress_bar.progress(0.3)
        dataset = ds.dataset(list(filepaths), format="parquet")
        table = dataset.to_table(columns=['timestamp', 'side', 'price', 'quantity'])
        df_raw = table.to_pandas().sort_values(by=['timestamp', 'side'], ascending=[True, False]).reset_index(drop=True)
        
        _progress_bar.progress(0.6)
        _status_text.markdown("**[2/2]** 🧠 正在执行 L2/L1 内存流重构与并行解析...")
        
        recon = L2Reconstructor(depth_limit=limit)
        
        # 提取 L1 数据
        df_l1_raw = df_raw[df_raw['side'] == 2].copy()
        df_l1_raw['is_buyer_maker'] = df_l1_raw['quantity'] < 0
        df_l1_raw['side_label'] = df_l1_raw['is_buyer_maker'].map({True: 'SELL (主动卖)', False: 'BUY (主动买)'})
        df_l1_raw['quantity'] = df_l1_raw['quantity'].abs()
        df_l1_raw['datetime'] = pd.to_datetime(df_l1_raw['timestamp'], unit='ms')

        # 执行 L2 内存流重建
        df_l2_raw = recon.process_dataframe(df_raw, sample_interval_ms=interval)
        if not df_l2_raw.empty:
            df_l2_raw['datetime'] = pd.to_datetime(df_l2_raw['timestamp'], unit='ms')
            
        elapsed = time.time() - start_time
        _progress_bar.progress(1.0)
        _status_text.markdown(f"**✅ 重构完成！** 耗时: **{elapsed:.2f}s** | 采样点: **{len(df_l2_raw):,}**")
        
        time.sleep(1)
        ui_box.empty()
        
        return df_l2_raw, df_l1_raw

    # 缓存：官方数据加载器
    @st.cache_data(show_spinner=False)
    def load_official_l1(filepath):
        if filepath == "无 (仅查看重构)": return pd.DataFrame()
        df = pd.read_parquet(filepath)
        
        # 兼容不同命名的时间戳字段
        time_col = 'timestamp' if 'timestamp' in df.columns else ('datetime' if 'datetime' in df.columns else None)
        if not time_col: return pd.DataFrame()
            
        if not pd.api.types.is_datetime64_any_dtype(df[time_col]):
            # 如果是毫秒级时间戳则转换
            is_ms = str(df[time_col].iloc[0]).isdigit() and int(df[time_col].iloc[0]) > 1e11
            df[time_col] = pd.to_datetime(df[time_col], unit='ms' if is_ms else None)
        
        if time_col != 'datetime':
            df['datetime'] = df[time_col]
            
        return df.sort_values('datetime').reset_index(drop=True)

    # 根据导入按钮的状态 (session_state) 进行数据处理
    process_paths = st.session_state.insight_selected_paths

    if process_paths:
        st.divider()
        
        # 开始执行数据重构
        df_l2, df_l1 = process_data_v2(process_paths, sample_interval, depth_limit)
            
        if df_l2.empty:
            st.error("❌ 重建失败：数据为空，或该文件中未发现初始快照 (Side 3/4)，无法重构盘口。")
            return
        
        st.success(f"✅ L2 跨文件合并重建成功！共串联 **{len(process_paths)}** 个文件。生成 **{len(df_l2)}** 个时间切片，提取出 **{len(df_l1)}** 笔 L1 交易。")
        
        # 选项卡
        tab_price, tab_depth, tab_l1, tab_data = st.tabs([
            "📊 盘口价格与指标走势", 
            "🧊 动态深度截面图", 
            "🔄 L1 逐笔成交同盘对比", 
            "📋 原始重构数据"
        ])
        
        with tab_price:
            st.subheader("1. 盘口最优报价 (Top of Book)")
            fig_price = go.Figure()
            # 开启 WebGL 加速 (Scattergl) 应对海量点
            fig_price.add_trace(go.Scattergl(x=df_l2['datetime'], y=df_l2['a_p_0'], mode='lines', name='Ask 1 (卖一)', line=dict(color='#FF4B4B', width=1.5)))
            fig_price.add_trace(go.Scattergl(x=df_l2['datetime'], y=df_l2['b_p_0'], mode='lines', name='Bid 1 (买一)', line=dict(color='#00FF00', width=1.5)))
            fig_price.add_trace(go.Scattergl(x=df_l2['datetime'], y=df_l2['mid_price'], mode='lines', name='Mid Price (中间价)', line=dict(color='yellow', width=1, dash='dot')))
            fig_price.update_layout(template="plotly_dark", height=400, hovermode="x unified", margin=dict(l=0, r=0, t=30, b=0))
            st.plotly_chart(fig_price, use_container_width=True)

            st.subheader("2. 盘口微观指标 (Imbalance & Spread)")
            fig_metrics = make_subplots(specs=[[{"secondary_y": True}]])
            fig_metrics.add_trace(go.Scattergl(x=df_l2['datetime'], y=df_l2['imbalance'], mode='lines', name='Imbalance (买盘偏度)', line=dict(color='cyan', width=1.5)), secondary_y=False)
            fig_metrics.add_trace(go.Scattergl(x=df_l2['datetime'], y=df_l2['spread'], mode='lines', name='Spread (价差)', line=dict(color='orange', width=1, dash='dot')), secondary_y=True)
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
            
            bids_q_cum = np.cumsum(bids_q)
            asks_q_cum = np.cumsum(asks_q)
            
            fig_depth = go.Figure()
            fig_depth.add_trace(go.Scattergl(x=bids_p, y=bids_q_cum, fill='tozeroy', mode='lines', name='Bids (买单累计)', line=dict(color='#00FF00', shape='hv')))
            fig_depth.add_trace(go.Scattergl(x=asks_p, y=asks_q_cum, fill='tozeroy', mode='lines', name='Asks (卖单累计)', line=dict(color='#FF4B4B', shape='hv')))
            
            fig_depth.update_layout(
                title=f"快照时间: {row['datetime']}",
                xaxis_title="Price (价格)",
                yaxis_title="Cumulative Quantity (累计挂单量)",
                template="plotly_dark",
                height=500
            )
            st.plotly_chart(fig_depth, use_container_width=True)
            
        with tab_l1:
            st.subheader("4. 还原的 L1 实时成交与官方历史一致性校验")
            
            # 定位官方历史目录
            history_dir = os.path.join(base_path, "replay_buffer", "parquet")
            if not os.path.exists(history_dir):
                history_dir = os.path.join(base_path, "replay_buffer")
            official_files = get_all_parquet_files(history_dir)
            
            off_file_options = ["无 (仅查看重构)"] + official_files
            selected_off_file = st.selectbox(
                "⚖️ 选取对应时间段的官方 L1 数据 (AggTrades) 注入对比",
                off_file_options,
                format_func=lambda x: x if x == "无 (仅查看重构)" else os.path.relpath(x, base_path)
            )
            
            if df_l1.empty:
                st.warning("⚠️ 此 L2 文件中未发现成交记录 (side=2)。可能由于该时间段内无成交，或未采集 aggTrade。")
            else:
                # 载入官方历史文件
                df_off_full = load_official_l1(selected_off_file)
                
                # 获取整体时间范围并构建滑块
                min_time = df_l1['datetime'].min().to_pydatetime()
                max_time = df_l1['datetime'].max().to_pydatetime()

                selected_time_range = st.slider(
                    "⌛ 拖动滑块截取特定时间段进行分析",
                    min_value=min_time,
                    max_value=max_time,
                    value=(min_time, max_time),
                    format="HH:mm:ss"
                )

                # 动态过滤选定时间范围内的数据
                mask_l1 = (df_l1['datetime'] >= selected_time_range[0]) & (df_l1['datetime'] <= selected_time_range[1])
                f_df_l1 = df_l1.loc[mask_l1]
                
                mask_l2 = (df_l2['datetime'] >= selected_time_range[0]) & (df_l2['datetime'] <= selected_time_range[1])
                f_df_l2 = df_l2.loc[mask_l2]
                
                if not df_off_full.empty:
                    mask_off = (df_off_full['datetime'] >= selected_time_range[0]) & (df_off_full['datetime'] <= selected_time_range[1])
                    f_df_off = df_off_full.loc[mask_off]
                else:
                    f_df_off = pd.DataFrame()

                # ---------------- 指标计算与渲染 ----------------
                if not f_df_off.empty:
                    rec_count = len(f_df_l1)
                    off_count = len(f_df_off)
                    rec_vol = f_df_l1['quantity'].sum() if not f_df_l1.empty else 0
                    off_vol = f_df_off['quantity'].sum() if not f_df_off.empty else 0
                    
                    diff_count = rec_count - off_count
                    diff_vol = rec_vol - off_vol
                    vol_diff_pct = (diff_vol / off_vol * 100) if off_vol > 0 else 0
                    
                    st.markdown("#### 📐 数据一致性评分板 (Data Fidelity)")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("官方历史笔数", f"{off_count:,} 笔")
                    c2.metric("重构解析笔数", f"{rec_count:,} 笔", f"{diff_count:,} 笔 (偏差)", delta_color="inverse")
                    c3.metric("官方历史成交量", f"{off_vol:.4f}")
                    c4.metric("重构解析成交量", f"{rec_vol:.4f}", f"{diff_vol:.4f} ({vol_diff_pct:.3f}%)", delta_color="inverse")
                    
                    if diff_count == 0 and abs(diff_vol) < 1e-5:
                        st.success("🎉 完美匹配！当前重构的 L1 逐笔数据与币安官方历史数据 **100% 吻合**。无掉包漏单。")
                    else:
                        st.warning("⚠️ 存在差异。常见原因：WebSocket 断线重连漏收、边界毫秒级截断、或者官方归档时间存在细微时差。请通过下方的“累计成交量”曲线判断差值的发生时间。")
                else:
                    t_buys = f_df_l1[f_df_l1['side_label'] == 'BUY (主动买)']
                    t_sells = f_df_l1[f_df_l1['side_label'] == 'SELL (主动卖)']
                    c1, c2, c3 = st.columns(3)
                    c1.metric("选定范围成交笔数", f"{len(f_df_l1):,} 笔")
                    c2.metric("选定主动买入 (Taker Buy)", f"{t_buys['quantity'].sum():.4f}")
                    c3.metric("选定主动卖出 (Taker Sell)", f"{t_sells['quantity'].sum():.4f}")
                
                # ---------------- 图表绘制：双层联合视图 ----------------
                st.markdown("#### 📈 同盘轨迹叠加视图")
                fig_l1 = make_subplots(
                    rows=2, cols=1, shared_xaxes=True, 
                    vertical_spacing=0.08, 
                    subplot_titles=("价格分布离散点 (上方) vs 官方连续线 (底层)", "累计成交量轨迹 (Cumulative Volume)"),
                    row_heights=[0.7, 0.3]
                )
                
                # --- 子图 1: 价格分布 ---
                t_buys = f_df_l1[f_df_l1['side_label'] == 'BUY (主动买)']
                t_sells = f_df_l1[f_df_l1['side_label'] == 'SELL (主动卖)']
                
                # 绘制官方数据作为基线 (存在的话)
                if not f_df_off.empty:
                    fig_l1.add_trace(go.Scattergl(
                        x=f_df_off['datetime'], y=f_df_off['price'], 
                        mode='lines', name='Official Price (官方价格基线)', 
                        line=dict(color='rgba(0, 191, 255, 0.6)', width=2)
                    ), row=1, col=1)

                # 绘制重构买卖散点
                fig_l1.add_trace(go.Scattergl(
                    x=t_buys['datetime'], y=t_buys['price'], 
                    mode='markers', name='Recon Buy (重构主动买)', 
                    marker=dict(color='#00FF00', symbol='triangle-up', size=6, opacity=0.7)
                ), row=1, col=1)
                
                fig_l1.add_trace(go.Scattergl(
                    x=t_sells['datetime'], y=t_sells['price'], 
                    mode='markers', name='Recon Sell (重构主动卖)', 
                    marker=dict(color='#FF4B4B', symbol='triangle-down', size=6, opacity=0.7)
                ), row=1, col=1)
                
                # 叠加中间价作为盘口背景
                if not f_df_l2.empty:
                    fig_l1.add_trace(go.Scattergl(
                        x=f_df_l2['datetime'], y=f_df_l2['mid_price'], 
                        mode='lines', name='Mid Price (盘口参考)', 
                        line=dict(color='rgba(255, 255, 255, 0.4)', width=1, dash='dot')
                    ), row=1, col=1)
                
                # --- 子图 2: 累计成交量追踪 ---
                f_df_l1_sorted = f_df_l1.sort_values('datetime')
                fig_l1.add_trace(go.Scattergl(
                    x=f_df_l1_sorted['datetime'], y=f_df_l1_sorted['quantity'].cumsum(), 
                    mode='lines', name='Recon CumVol (重构累计量)', 
                    line=dict(color='#FFA500', width=2)
                ), row=2, col=1)
                
                if not f_df_off.empty:
                    f_df_off_sorted = f_df_off.sort_values('datetime')
                    fig_l1.add_trace(go.Scattergl(
                        x=f_df_off_sorted['datetime'], y=f_df_off_sorted['quantity'].cumsum(), 
                        mode='lines', name='Official CumVol (官方累计量)', 
                        line=dict(color='#00BFFF', width=2, dash='dash')
                    ), row=2, col=1)
                
                fig_l1.update_layout(
                    template="plotly_dark", 
                    height=650, 
                    hovermode="x unified",
                    margin=dict(l=0, r=0, t=30, b=0)
                )
                st.plotly_chart(fig_l1, use_container_width=True)
                
                st.dataframe(f_df_l1[['datetime', 'side_label', 'price', 'quantity']].tail(100), use_container_width=True)

        with tab_data:
            st.dataframe(df_l2.head(200), use_container_width=True)