"""ops 渲染层(薄 Streamlit;数据来自 probe_aws，解析/分类在 fleet）。

v1 四块:fleet 总览行 / per-venue 新鲜度 / WAL 落盘积压 / /health 明细。
cold-tier 落地(P3,ssh lustre1)另起 panel。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from ops import fleet

_BADGE = {"GREEN": "🟢", "RED": "🔴", "EMPTY": "⚪"}
_STATE_ICON = {"healthy": "🟢", "up": "🟢", "unhealthy": "🔴",
               "restarting": "🔴", "exited": "⏸", "created": "🟡", "unknown": "❔"}
_FRESH_ICON = {"fresh": "🟢", "stale": "🔴", "parked": "⏸", "unknown": "❔"}


def render_fleet(res: dict, stale_sec: int) -> None:
    name = res.get("fleet", "?").upper()
    if res.get("error"):
        st.error(f"**{name}** 探测失败:{res['error']}")
        return

    fresh = fleet.classify_freshness(res.get("freshness_raw", []), stale_sec=stale_sec)
    summ = fleet.summarize(res.get("containers", []), fresh)
    commit = res.get("commit", {})
    age = int(dt.datetime.now().timestamp() - res.get("ts", 0))

    # ── 总览行 ──
    st.markdown(
        f"### {_BADGE.get(summ['overall'],'⚪')} {name} "
        f"· EC2 `{res.get('ec2_state')}` · SSM `{res.get('ssm_ping')}` "
        f"· commit `{commit.get('sha','?')}` "
        f"· {summ['n_up']} up / {summ['n_parked']} parked / {summ['n_bad']} bad "
        f"· 探于 {age}s 前"
    )
    if summ["overall"] == "RED":
        msg = []
        if summ["bad_containers"]:
            msg.append("容器异常:" + ", ".join(summ["bad_containers"]))
        if summ["stale_venues"]:
            msg.append("陈旧 venue:" + ", ".join(summ["stale_venues"]))
        st.error("⚠️ " + " ｜ ".join(msg))

    # ── 容器 ──
    cs = res.get("containers", [])
    if cs:
        st.dataframe(
            pd.DataFrame([{"容器": c["name"], "": _STATE_ICON.get(c["state"], "❔"),
                           "状态": c["status"]} for c in cs]),
            width="stretch", hide_index=True)

    # ── ① 新鲜度 ──
    with st.expander(f"① Per-venue 新鲜度（{len([f for f in fresh if f['status']=='stale'])} stale）",
                     expanded=bool(summ["stale_venues"])):
        if fresh:
            df = pd.DataFrame([{
                "": _FRESH_ICON.get(f["status"], "❔"),
                "交易所": f.get("exchange"), "市场": f.get("market"), "交易对": f.get("symbol"),
                "状态": f["status"], "距今(s)": f.get("age_sec"), "shard 数": f.get("n_shards"),
                "最后录制": dt.datetime.fromtimestamp(f["last_shard_ts"]) if f.get("last_shard_ts") else None,
            } for f in fresh])
            st.dataframe(df, width="stretch", hide_index=True)
        else:
            st.caption("无 RAW 数据")

    # ── ② WAL 积压 ──
    wal = sorted(res.get("wal", []), key=lambda x: -x["pending_segments"])
    with st.expander(f"② WAL 落盘积压（{len(wal)} venue 有未合并段）"):
        st.caption("正常每 save_interval 合并清空;**持续非零/单调上涨 = 落盘或合并卡住**。"
                   "单次几十~几百段在 600s 合并周期内属正常。")
        if wal:
            st.dataframe(pd.DataFrame([{
                "交易所": w["exchange"], "市场": w["market"], "交易对": w["symbol"],
                "待合并段数": w["pending_segments"]} for w in wal]),
                width="stretch", hide_index=True)
        else:
            st.caption("无积压 ✅")

    # ── ③ /health ──
    h = res.get("health", [])
    with st.expander(f"③ /health 端口（{sum(1 for x in h if x['ok'])}/{len(h)} OK）"):
        if h:
            st.dataframe(pd.DataFrame([{
                "端口": x["port"], "": "🟢" if x["ok"] else "🔴", "HTTP": x["code"]} for x in h]),
                width="stretch", hide_index=True)
        else:
            st.caption("未配 health 端口（deploy/reco/.env 的 NARCI_*_HEALTH_PORTS）")
