"""Narci Reco Ops 控制台 — streamlit 入口（只读 fleet 监控）。

启动:`python main.py reco-gui`(或直接 streamlit run ops/dashboard.py)。
数据走 SSM 实时拉(每 fleet 一发批量探针),st.cache_data(ttl=60) 缓存 + 手动刷新。
设计:docs/design/RECO_OPS_GUI.md。
"""
from __future__ import annotations

import os
import sys

import streamlit as st

# repo root 上 path（streamlit run 时 cwd 不一定是 repo 根）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ops import config, probe_aws, panels  # noqa: E402

st.set_page_config(page_title="Narci Reco Ops", layout="wide", page_icon="🛰️")


@st.cache_data(ttl=60, show_spinner=False)
def _probe(name: str):
    """缓存 60s;刷新按钮 .clear() 强制重拉。"""
    return probe_aws.probe_fleet(name)


def main() -> None:
    st.title("🛰️ Narci Reco Ops")
    st.caption("reco 运维域只读看板 · SSM 实时拉 AWS fleet · 数据源 = EC2(无 gdrive/lustre,P1)")

    avail = config.available_fleets()
    if not avail:
        st.error("未找到 fleet 配置 —— deploy/reco/.env 缺 NARCI_JP/SG_INSTANCE_ID。")
        st.stop()

    c1, c2, c3 = st.columns([3, 1, 1])
    sel = c1.multiselect("Fleet", avail, default=avail)
    if c2.button("🔄 刷新", use_container_width=True):
        _probe.clear()
        st.rerun()
    stale_sec = c3.number_input("陈旧阈值(s)", min_value=300, max_value=7200, value=1500, step=100,
                                help="RAW 每 save_interval(默认600s)落一次 → 阈值应 >它(默认1500≈2.5×)")

    if not sel:
        st.info("选至少一个 fleet。")
        st.stop()

    with st.spinner(f"探测 {', '.join(s.upper() for s in sel)} (SSM ~6s/fleet)…"):
        results = [_probe(name) for name in sel]

    # 顶部跨-fleet 总览 strip(2 秒判断)+ 单一 legend
    panels.render_summary(results, int(stale_sec))
    st.divider()

    # 每 fleet 明细
    for res in results:
        panels.render_fleet(res, int(stale_sec))
        st.divider()


main()
