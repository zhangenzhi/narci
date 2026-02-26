import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import os
import yaml
import time
import shutil

from backtest.backtest import JitBacktestEngine 
from example.L2_Imbalance import L2ImbalanceStrategy
from gui.utils import get_all_parquet_files

def render(base_path, default_history_dir, default_realtime_dir):
    st.header("📈 L2 高频微观回测实验室 (HFT 极速缓存版)")
    
    # ---------------------------------------------------------
    # 统一读取全新的回测配置 backtest.yaml
    # ---------------------------------------------------------
    config_path = os.path.join(base_path, "configs", "backtest.yaml")
    bt_cfg = {}
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            bt_cfg = yaml.safe_load(f).get('backtest', {})
    
    data_cfg = bt_cfg.get('data', {})
    broker_cfg = bt_cfg.get('broker', {})
    sys_cfg = bt_cfg.get('system', {})
    strat_cfg = bt_cfg.get('strategy', {})
    
    # 解析数据路径：优先读取 yaml，支持相对路径自动转为绝对路径
    raw_realtime = data_cfg.get('realtime_dir', default_realtime_dir)
    raw_history = data_cfg.get('history_dir', default_history_dir)
    
    realtime_dir = raw_realtime if os.path.isabs(raw_realtime) else os.path.abspath(os.path.join(base_path, raw_realtime))
    history_dir = raw_history if os.path.isabs(raw_history) else os.path.abspath(os.path.join(base_path, raw_history))
    
    initial_cash_cfg = float(broker_cfg.get('initial_cash', 10000.0))
    leverage_cfg = float(broker_cfg.get('leverage', 10.0))
    maker_fee_cfg = float(broker_cfg.get('maker_fee', 0.0000))
    taker_fee_cfg = float(broker_cfg.get('taker_fee', 0.0004))
    
    st.info(f"💡 **当前系统引擎配置** (读取自 `configs/backtest.yaml`):\n"
            f"- **数据根目录**: `{realtime_dir}`\n"
            f"- **初始资金**: ${initial_cash_cfg:,.2f}  |  **账户杠杆**: {leverage_cfg}x\n"
            f"- **Maker 费率**: {maker_fee_cfg * 100:.4f}%  |  **Taker 费率**: {taker_fee_cfg * 100:.4f}%")

    st.write("📌 **选择数据集 (自动支持 JIT 重构及本地特征缓存落盘)**")
    
    c_m1, c_m2 = st.columns(2)
    with c_m1:
        # 使用配置中的默认市场类型
        default_market = sys_cfg.get('default_market', 'um_futures')
        market_index = 0 if default_market == 'um_futures' else 1
        market_type = st.selectbox("🎯 数据来源市场", ["um_futures", "spot"], index=market_index, key="bt_market_type")
    
    market_dir = os.path.join(realtime_dir, market_type, "l2")
    if not os.path.exists(market_dir):
        os.makedirs(market_dir, exist_ok=True)
        
    cache_dir =os.path.join(realtime_dir, market_type, "features")
    
    all_files = get_all_parquet_files(market_dir)
    available_files = [f for f in all_files if f.endswith(".parquet") and "features" not in f]
    
    available_symbols = sorted(list(set([os.path.basename(f).split('_')[0].upper() for f in available_files])))
    with c_m2:
        selected_symbol = st.selectbox("🪙 交易对筛选", ["全部"] + available_symbols, key="bt_symbol_filter")
        
    if selected_symbol != "全部":
        available_files = sorted([f for f in available_files if os.path.basename(f).upper().startswith(selected_symbol)])
    else:
        available_files = sorted(available_files)
        
    if not available_files:
        st.warning(f"⚠️ 数据目录 {market_dir} 下暂无数据集。请先运行 Recorder 录制数据。")
        return
        
    with st.expander("💾 引擎特征缓存管理 (Feature Cache)"):
        st.write(f"当前缓存路径: `{cache_dir}`")
        if os.path.exists(cache_dir):
            cache_files = os.listdir(cache_dir)
            cache_size_mb = sum(os.path.getsize(os.path.join(cache_dir, f)) for f in cache_files) / (1024 * 1024)
            st.write(f"已缓存重构文件数: **{len(cache_files)}** 个  |  占用磁盘空间: **{cache_size_mb:.2f} MB**")
            if st.button("🧹 一键清空缓存 (清理后下次将重新计算)"):
                shutil.rmtree(cache_dir)
                os.makedirs(cache_dir)
                st.success("缓存已全部清空！")
                st.rerun()
        else:
            st.write("目前尚无任何缓存。")
    
    st.write(f"📂 **选择要进行回测的数据片段 (共 {len(available_files)} 个)**")
    
    file_records = []
    for f in available_files:
        display_name = os.path.basename(f).split('_RAW_')[-1].replace('.parquet', '') if '_RAW_' in f else os.path.basename(f)
        file_records.append({"文件标号 (按时间排序)": display_name, "_path": f})
        
    df_files = pd.DataFrame(file_records)
    selection_event = st.dataframe(
        df_files[["文件标号 (按时间排序)"]],
        hide_index=False,
        use_container_width=True,
        selection_mode="multi-row",
        on_select="rerun",
        height=200,
        key="bt_file_multi_selector"
    )
    
    selected_indices = selection_event.selection.rows
    current_selected_paths = [df_files.iloc[i]["_path"] for i in selected_indices]
    
    is_raw_data = any("RAW" in f.upper() for f in current_selected_paths)

    st.subheader("⚙️ 策略实例及参数设置")
    c1, c2, c3 = st.columns(3)
    with c1:
        selected_strategy = st.selectbox("核心策略", ["L2_Imbalance 极速波动率过滤版"], key="bt_strategy")
    with c2:
        # 提取配置的默认策略阈值
        def_imb = float(strat_cfg.get('default_imbalance_threshold', 0.4))
        imbalance_th = st.number_input("Imbalance 触发阈值", min_value=0.1, max_value=1.0, value=def_imb, step=0.05, key="bt_imb_th")
    with c3:
        def_qty = float(strat_cfg.get('default_trade_qty', 0.1))
        trade_qty = st.number_input("单次执行数量 (Qty)", min_value=0.001, max_value=10.0, value=def_qty, step=0.01, key="bt_trade_qty")

    if st.button("🚀 启动双向极速缓存回测", type="primary", use_container_width=True, key="bt_run_btn"):
        if not current_selected_paths:
            st.warning("请先在上方列表中至少选中一行文件。")
            return
            
        filename = os.path.basename(current_selected_paths[0])
        symbol = filename.split('_')[0].upper() if '_' in filename else "UNKNOWN"
        
        st.write(f"正在对目标资产 **{symbol}** 挂载极速回测引擎 (共载入 {len(current_selected_paths)} 个片段)...")
        
        strategy = L2ImbalanceStrategy(symbol=symbol, imbalance_threshold=imbalance_th, trade_qty=trade_qty)
        
        # 直接使用剥离到核心引擎模块中的 JitBacktestEngine
        engine = JitBacktestEngine(
            data_paths=current_selected_paths,
            strategy=strategy,
            symbol=symbol,
            is_raw=is_raw_data,
            init_cash=initial_cash_cfg,
            m_fee=maker_fee_cfg,
            t_fee=taker_fee_cfg,
            lev=leverage_cfg,
            c_dir=cache_dir,
            config_path=config_path # 透传 config path 给底层 Engine
        )
        
        with st.spinner("🧠 引擎运行中 (并发读取/缓存落盘/滑点撮合)..."):
            import io
            import sys
            old_stdout = sys.stdout
            new_stdout = io.StringIO()
            sys.stdout = new_stdout
            
            start_t = time.time()
            engine.run()
            elapsed_t = time.time() - start_t
            
            sys.stdout = old_stdout
            log_output = new_stdout.getvalue()
            
        st.success(f"回测执行完成！UI 渲染准备中...")
            
        df_trades = engine.broker.get_trade_history()
        df_equity = engine.broker.get_equity_history()
        state = engine.broker.get_state()
        
        if df_equity.empty:
            st.error("❌ 回测未生成资金曲线。可能是无满足条件的限价单成交。")
            with st.expander("点击展开查看引擎底层日志"):
                st.code(log_output, language="text")
            return
            
        df_equity['datetime'] = pd.to_datetime(df_equity['timestamp'], unit='ms')
        
        df_equity['cummax'] = df_equity['equity'].cummax()
        df_equity['drawdown'] = (df_equity['equity'] - df_equity['cummax']) / df_equity['cummax'] * 100
        max_drawdown = df_equity['drawdown'].min()
        
        initial_equity = float(initial_cash_cfg) 
        final_equity = float(state['total_equity'])
        total_return = (final_equity - initial_equity) / initial_equity * 100
        
        st.divider()
        st.subheader("📊 回测绩效评估 (Performance Report)")
        
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("初始资金", f"${initial_equity:,.2f}")
        m2.metric("最终权益", f"${final_equity:,.2f}", f"{total_return:.2f}%")
        m3.metric("最大回撤 (Max DD)", f"{max_drawdown:.2f}%")
        m4.metric("总交易次数", f"{len(df_trades)} 笔")
        m5.metric("累计消耗手续费", f"${df_trades['fee'].sum():,.2f}" if not df_trades.empty else "$0.00", delta_color="inverse")
            
        st.markdown("#### 📉 资金净值、动态回撤与交易分布")
        fig = make_subplots(
            rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06, 
            subplot_titles=("💰 账户总权益 (Total Equity)", "📉 动态回撤面积图 (Drawdown %)", "🤝 交易买卖离散点分布"),
            row_heights=[0.5, 0.2, 0.3]
        )
                            
        fig.add_trace(go.Scattergl(x=df_equity['datetime'], y=df_equity['equity'], mode='lines', name='Equity', line=dict(color='#00BFFF', width=2)), row=1, col=1)
        fig.add_trace(go.Scattergl(x=df_equity['datetime'], y=df_equity['drawdown'], fill='tozeroy', mode='lines', name='Drawdown', line=dict(color='rgba(255, 75, 75, 0.8)', width=1)), row=2, col=1)
        
        if not df_trades.empty:
            df_trades['datetime'] = pd.to_datetime(df_trades['timestamp'], unit='ms')
            buys = df_trades[df_trades['action'] == 'BUY']
            sells = df_trades[df_trades['action'] == 'SELL']
            
            fig.add_trace(go.Scattergl(x=buys['datetime'], y=buys['price'], mode='markers', name='Buy', marker=dict(color='#00FF00', symbol='triangle-up', size=8)), row=3, col=1)
            fig.add_trace(go.Scattergl(x=sells['datetime'], y=sells['price'], mode='markers', name='Sell', marker=dict(color='#FF4B4B', symbol='triangle-down', size=8)), row=3, col=1)
            
        fig.update_layout(template="plotly_dark", height=800, hovermode="x unified", margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig, use_container_width=True)
        
        if not df_trades.empty and 'realized_pnl' in df_trades.columns:
            pnl_trades = df_trades[df_trades['realized_pnl'] != 0].copy()
            if not pnl_trades.empty:
                st.markdown("#### 📊 单笔平仓盈亏分布 (Realized PnL Distribution)")
                pnl_trades['PnL_Type'] = pnl_trades['realized_pnl'].apply(lambda x: '盈利 (Profit)' if x > 0 else '亏损 (Loss)')
                fig_pnl = px.histogram(
                    pnl_trades, x="realized_pnl", color="PnL_Type",
                    color_discrete_map={'盈利 (Profit)': '#00FF00', '亏损 (Loss)': '#FF4B4B'}, nbins=50,
                    labels={'realized_pnl': '单笔盈亏金额 (USD)'}, opacity=0.8
                )
                fig_pnl.update_layout(template="plotly_dark", height=400, margin=dict(l=0, r=0, t=30, b=0), yaxis_title="交易笔数 (Count)", xaxis_title="盈亏金额 (USD)")
                st.plotly_chart(fig_pnl, use_container_width=True)
        
        st.subheader("📋 详细交易流水簿 (包含角色 MAKER/TAKER)")
        if not df_trades.empty:
            display_df = df_trades.copy()
            
            if 'signal_info' in display_df.columns:
                sig_df = display_df['signal_info'].apply(lambda x: pd.Series(x) if isinstance(x, dict) else pd.Series(dtype='float64'))
                display_df = pd.concat([display_df.drop('signal_info', axis=1), sig_df], axis=1)
                
            cols_to_show = ['datetime', 'symbol', 'action', 'role', 'quantity', 'price', 'imbalance', 'fee', 'realized_pnl', 'equity', 'reason']
            display_cols = [c for c in cols_to_show if c in display_df.columns]
            display_df = display_df[display_cols].copy()
            
            display_df['price'] = display_df['price'].map(lambda x: f"${x:,.4f}")
            display_df['fee'] = display_df['fee'].map(lambda x: f"${x:,.4f}")
            
            if 'realized_pnl' in display_df.columns:
                display_df['realized_pnl'] = display_df['realized_pnl'].map(lambda x: f"${x:,.2f}" if x != 0 else "-")
            if 'equity' in display_df.columns:
                display_df['equity'] = display_df['equity'].map(lambda x: f"${x:,.2f}")
                
            st.dataframe(display_df, use_container_width=True)

        st.markdown("#### 📜 引擎运行与性能分析日志")
        with st.expander("点击展开查看 Profiler 性能报告与底层撮合日志", expanded=False):
            st.code(log_output, language="text")