"""录制健康监控面板(薄 Streamlit 层;逻辑在 recorder_health 数据层)。

给 narci + reco 管理录制器:一眼看 ① 哪些 (venue,symbol) 录制停了(新鲜度)、
② 哪些静默丢了数据(cold-tier gap 报告)、③ WAL 落盘有没有卡(.segwal 积压)。
"""
import os

import pandas as pd
import streamlit as st

from analytics.gui.recorder_health import (
    scan_freshness, scan_gap_reports, scan_wal_backlog, health_summary,
)


def render(base_path):
    st.subheader("📡 录制健康监控")
    st.caption("narci + reco 共用:实时新鲜度 / 静默丢数据(gap) / WAL 落盘积压。"
               "数据源 = replay_buffer/{realtime,cold,wal}。")

    rb = os.path.join(base_path, "replay_buffer")
    realtime_root = os.path.join(rb, "realtime")
    cold_root = os.path.join(rb, "cold")
    wal_root = os.path.join(rb, "wal")

    stale_sec = st.slider(
        "陈旧阈值(秒)", min_value=300, max_value=3600, value=1500, step=100,
        help="RAW 每 save_interval_sec(默认 600s)落一次盘,故阈值应 > 它(默认 1500≈2.5×)",
    )

    fresh = scan_freshness(realtime_root, stale_sec=stale_sec)
    summ = health_summary(fresh)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("交易对", summ["n_symbols"])
    c2.metric("新鲜", summ["n_fresh"])
    c3.metric("陈旧", summ["n_stale"])
    c4.metric("总体", summ["overall"])
    if summ["overall"] == "RED":
        st.error("⚠️ 疑似录制停止(陈旧):" + ", ".join(summ["stale"]))
    elif summ["overall"] == "GREEN":
        st.success("✅ 所有交易对录制新鲜")
    else:
        st.info("未发现录制数据 —— 检查 replay_buffer/realtime/ 路径")

    # ① 实时新鲜度
    st.markdown("#### ① 实时新鲜度(最新 RAW shard)")
    if fresh:
        df = pd.DataFrame(fresh)
        df["最后录制"] = pd.to_datetime(df["last_shard_ts"], unit="s")
        df = df.rename(columns={"exchange": "交易所", "market": "市场", "symbol": "交易对",
                                "status": "状态", "age_sec": "距今(s)", "n_shards": "shard 数"})
        st.dataframe(df[["交易所", "市场", "交易对", "状态", "距今(s)", "shard 数", "最后录制"]],
                     use_container_width=True, hide_index=True)
    else:
        st.write("—")

    # ② WAL 落盘积压
    st.markdown("#### ② WAL 落盘积压(.segwal 残留)")
    wal = scan_wal_backlog(wal_root)
    if wal:
        st.warning("存在未合并的 WAL 段。正常每个 save_interval 会合并清空;**持续非零 = 落盘/合并卡住**。")
        st.dataframe(pd.DataFrame(wal).rename(
            columns={"exchange": "交易所", "market": "市场", "symbol": "交易对",
                     "pending_segments": "待合并段数"}),
            use_container_width=True, hide_index=True)
    else:
        st.write("无积压 ✅")

    # ③ Gap 检测
    st.markdown("#### ③ Gap 检测(cold-tier — 无 U/u 序列 venue 的丢数据信号)")
    gaps = scan_gap_reports(cold_root)
    if gaps:
        df = pd.DataFrame(gaps)
        st.caption("覆盖率低 / gap 多 / 最大 gap 大 = 录制有洞。depth 与 trade 分流(orderbook 可能单独死)。")
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.write("无 gap 报告(compact 后对 Coincheck 等无官方归档 venue 生成;或尚未 compact)")
