"""ops 渲染层（薄 Streamlit;数据来自 probe_aws，解析/分类在 fleet）。

可视化(经 design-critique + accessibility-review 迭代):
- 顶部跨-fleet 总览 strip(HTML 彩色大块,一屏 2 秒判断;色 + 形双编码,不靠 st-key CSS)
- 每 fleet 卡片:状态徽章 + metric 行 + 模块色块按钮板(点击下钻)+ stale 自动展开 + 底部元信息
- 状态用**形状不同**的字符(✓/✕/⏸/!)+ 颜色,避免红绿色盲只靠色相(WCAG 1.4.1)
- 色块对比度经核:绿/红/琥珀过 AA;灰加深至 #5c636b 留余量
cold-tier 落地(P3,ssh lustre1)另起 panel。
"""
from __future__ import annotations

import datetime as dt
import html
import re

import pandas as pd
import streamlit as st

from ops import fleet

# health -> (色, 形状字符)。形状区分 → 红绿色盲也能辨(WCAG 1.4.1)
_TILE = {
    "ok": ("#1a7f37", "✓"), "bad": ("#cf222e", "✕"),
    "parked": ("#5c636b", "⏸"), "warn": ("#9a6700", "!"),
}
_OVERALL = {  # overall -> (色, 形状, 文案)
    "GREEN": ("#1a7f37", "✓", "运行正常"),
    "RED": ("#cf222e", "✕", "需要关注"),
    "AMBER": ("#9a6700", "!", "滞后"),
    "EMPTY": ("#5c636b", "·", "无数据"),
}
_COLD_ICON = {"ok": "✓", "lag": "!", "stale": "✕", "missing": "✕", "parked": "⏸"}
_ROW_BG = {
    "fresh": "", "ok": "", "unknown": "",
    "stale": "background-color: rgba(255,75,75,.22)",
    "missing": "background-color: rgba(255,75,75,.22)",
    "lag": "background-color: rgba(250,176,5,.18)",
    "parked": "background-color: rgba(130,130,130,.18)",
    "bad": "background-color: rgba(255,75,75,.22)",
}
_FRESH_ICON = {"fresh": "✓", "stale": "✕", "parked": "⏸", "unknown": "?"}


def _safe(s: str) -> str:
    return re.sub(r"[^0-9a-zA-Z_]", "_", s)


def _short(name: str) -> str:
    return name.split("recorder-", 1)[1] if "recorder-" in name else name.replace("narci-", "")


def _row_style(col):
    def _f(row):
        return [_ROW_BG.get(str(row.get(col, "")), "")] * len(row)
    return _f


def _digest(res: dict, stale_sec: int):
    """算一个 fleet 的 (fresh 分类, summary)。

    parked venue(recorder 容器已 exited,如 GMO)即时豁免,不误判 stale 触发 RED。
    """
    containers = res.get("containers", [])
    parked = fleet.parked_venues(containers)
    fresh = fleet.classify_freshness(res.get("freshness_raw", []), stale_sec=stale_sec,
                                     parked_venues=parked)
    return fresh, fleet.summarize(containers, fresh)


# ───────────────────────── 顶部总览 strip ───────────────────────── #

def render_summary(results: list[dict], stale_sec: int) -> None:
    st.markdown("##### Fleet 总览")
    cards = []
    for res in results:
        name = res.get("fleet", "?").upper()
        if res.get("error"):
            color, icon, word, sub = "#5c636b", "?", "探测失败", html.escape(res["error"][:60])
        else:
            _, summ = _digest(res, stale_sec)
            color, icon, word = _OVERALL.get(summ["overall"], ("#5c636b", "·", ""))
            sub = (f"{summ['n_up']} up · {summ['n_parked']} parked · "
                   f"{summ['n_bad']} bad · {len(summ['stale_venues'])} stale")
        cards.append(
            f"<div style='flex:1;min-width:200px;background:{color};border-radius:12px;"
            f"padding:14px 18px;color:#fff'>"
            f"<div style='font-size:13px;opacity:.85'>{html.escape(name)}</div>"
            f"<div style='font-size:26px;font-weight:700;line-height:1.2'>{icon} {word}</div>"
            f"<div style='font-size:13px;opacity:.9;margin-top:2px'>{sub}</div></div>")
    st.markdown(
        "<div style='display:flex;gap:12px;flex-wrap:wrap'>" + "".join(cards) + "</div>",
        unsafe_allow_html=True)
    st.caption("模块色块:✓正常(绿) · ✕异常/陈旧(红) · ⏸PARKED(灰) · !未知(黄) —— 点色块看明细")


# ───────────────────────── tile CSS ───────────────────────── #

def _inject_tile_css(tiles: list[tuple[str, str]]) -> None:
    rules = []
    for key, healthv in tiles:
        c = _TILE.get(healthv, ("#5c636b", ""))[0]
        rules.append(
            f".st-key-{key} button{{background-color:{c};border-color:{c};color:#fff;font-weight:600}}"
            f".st-key-{key} button:hover{{filter:brightness(1.12);border-color:#fff;color:#fff}}")
    st.markdown("<style>" + "".join(rules) + "</style>", unsafe_allow_html=True)


def _freshness_table(rows: list[dict], compact: bool = False) -> None:
    cols = ([{"": _FRESH_ICON.get(r["status"], "?"), "交易对": r.get("symbol"),
              "status": r["status"], "距今": r.get("age_sec"), "shard": r.get("n_shards")}
             for r in rows] if compact else
            [{"": _FRESH_ICON.get(r["status"], "?"), "交易所": r.get("exchange"),
              "市场": r.get("market"), "交易对": r.get("symbol"), "status": r["status"],
              "距今": r.get("age_sec"), "shard": r.get("n_shards")} for r in rows])
    df = pd.DataFrame(cols)
    st.dataframe(df.style.apply(_row_style("status"), axis=1), width="stretch", hide_index=True,
                 column_config={"status": None,
                                "距今": st.column_config.NumberColumn(format="%d s")})


def _venue_detail(res: dict, container: dict, fresh: list[dict]) -> None:
    name = container["name"]
    st.markdown(f"**{name}** — `{container['status']}`")
    ex, mkt = fleet.container_venue(name)
    if ex is None:
        st.caption("非 recorder 模块(无 venue 数据),仅容器状态。")
        return
    rows = [f for f in fresh if f.get("exchange") == ex and f.get("market") == mkt]
    wal = [w for w in res.get("wal", []) if w.get("exchange") == ex and w.get("market") == mkt]
    c1, c2 = st.columns(2)
    with c1:
        st.caption(f"① {ex}/{mkt} 新鲜度({sum(1 for r in rows if r['status']=='stale')} stale / {len(rows)})")
        _freshness_table(rows, compact=True) if rows else st.caption("无 RAW")
    with c2:
        st.caption(f"② WAL 待合并段({len(wal)})")
        if wal:
            df = pd.DataFrame([{"交易对": w["symbol"], "段数": w["pending_segments"]} for w in wal])
            st.dataframe(df, width="stretch", hide_index=True,
                         column_config={"段数": st.column_config.ProgressColumn(
                             format="%d", min_value=0, max_value=max(300, df["段数"].max()))})
        else:
            st.caption("无积压 ✅")


# ───────────────────────── Coverage 进度条(当日 144 桶)───────────────────────── #

_COV_CSS = """<style>
.cov-row{display:flex;align-items:center;gap:8px;margin:3px 0;font-family:monospace;font-size:11.5px}
.cov-label{min-width:240px}
.cov-bar{display:inline-block;background:rgba(255,255,255,.06);border-radius:2px;line-height:0;font-size:0;vertical-align:middle}
.cov-bar span{display:inline-block;width:4px;height:16px}
.cov-bar span.g{background:#1a7f37}
.cov-bar span.r{background:#cf222e}
.cov-scale{display:flex;gap:8px;margin-top:6px;font-family:monospace}
.cov-scale .pad{min-width:240px}
.cov-scale .marks span{display:inline-block;width:96px;font-size:10px;opacity:.55}
</style>"""
_BIT_HTML = {"1": '<span class="g"></span>', "x": '<span class="r"></span>', "0": "<span></span>"}


def _coverage_bar_html(bitmap: str) -> str:
    cells = "".join(_BIT_HTML.get(c, "<span></span>") for c in bitmap)
    return f'<div class="cov-bar">{cells}</div>'


def _render_coverage(coverage: list, n_buckets: int, day: str) -> None:
    """每 (ex/mkt/sym) 一行 st.markdown 调用(避免一坨大 HTML 噎住 streamlit)。"""
    st.markdown(f"##### 📅 {day} UTC 录制覆盖({n_buckets} 桶 × {1440 // n_buckets} 分钟)")
    st.caption("🟢 有 shard · ⬛ 缺失 · 🔴 损坏(.corrupted)  ·  按时间从左 00:00 到右 23:59 排布")
    st.markdown(_COV_CSS, unsafe_allow_html=True)
    rows = sorted(coverage, key=lambda r: (r.get("exchange", ""), r.get("market", ""),
                                           r.get("symbol", "")))
    for r in rows:
        bm = r.get("bitmap", "0" * n_buckets)
        present, bad = bm.count("1"), bm.count("x")
        pct = round(100 * (present + bad) / n_buckets, 1)
        bad_html = f' · <span style="color:#cf222e">坏 {bad}</span>' if bad else ""
        label = (f'<span style="opacity:.9">{r["exchange"]}/{r["market"]}/<b>{r["symbol"]}</b></span>'
                 f'<span style="opacity:.55"> &nbsp;{present}/{n_buckets} ({pct}%){bad_html}</span>')
        st.markdown(
            f'<div class="cov-row"><div class="cov-label">{label}</div>{_coverage_bar_html(bm)}</div>',
            unsafe_allow_html=True)
    scale_marks = "".join(f'<span>{h:02d}:00</span>' for h in range(0, 24, 4))
    st.markdown(
        f'<div class="cov-scale"><div class="pad"></div><div class="marks">{scale_marks}</div></div>',
        unsafe_allow_html=True)


# ───────────────────────── Cold-tier(lustre1,全局)───────────────────────── #

_COLD_HIST_CSS = """<style>
.coldh-row{display:flex;align-items:center;gap:8px;margin:3px 0;font-family:monospace;font-size:11.5px}
.coldh-label{min-width:200px}
.coldh-bar{display:inline-block;background:rgba(255,255,255,.06);border-radius:2px;line-height:0;font-size:0;vertical-align:middle}
.coldh-bar span{display:inline-block;width:8px;height:18px}
.coldh-bar span.g{background:#1a7f37}
.coldh-bar span.y{background:#9a6700}
.coldh-bar span.r{background:#cf222e}
.coldh-bar span.x{background:#7a1620}
.coldh-bar span.p{background:#5c636b}
.coldh-scale{display:flex;gap:8px;margin-top:4px;font-family:monospace}
.coldh-scale .pad{min-width:200px}
.coldh-scale .marks span{display:inline-block;font-size:10px;opacity:.55}
</style>"""
_COLD_BIT_HTML = {"1": '<span class="g"></span>', "?": '<span class="y"></span>',
                  "0": '<span class="r"></span>', "x": '<span class="x"></span>',
                  "p": '<span class="p"></span>'}


def _cold_history_bar(history: str) -> str:
    cells = "".join(_COLD_BIT_HTML.get(c, "<span></span>") for c in history)
    return f'<div class="coldh-bar">{cells}</div>'


def render_cold(cold_res: dict, parked_venues: set, today_yyyymmdd: str) -> None:
    import datetime as dt
    with st.container(border=True):
        if cold_res.get("error"):
            st.subheader("❄️ Cold-tier 落地")
            st.warning(f"cold 探测不可用(lustre1):{cold_res['error']}")
            return
        N_DAYS = 90
        hist = fleet.classify_cold_history(cold_res.get("rows", []), today_yyyymmdd,
                                           parked_venues, n_days=N_DAYS)
        # 总览:任何 active venue 近 N 天有 '0' 就 RED;只有 '?' 就 AMBER;全 ok/parked 就 GREEN
        any_red = any("0" in r["history"] or "x" in r["history"] for r in hist)
        any_lag = any("?" in r["history"] for r in hist)
        overall = "RED" if any_red else ("AMBER" if any_lag else "GREEN" if hist else "EMPTY")
        color, icon, word = _OVERALL.get(overall, ("#5c636b", "·", ""))
        st.subheader(f"❄️ {icon} Cold-tier 落地 · 近 {N_DAYS} 天 · {word}")
        age = int(dt.datetime.now().timestamp() - cold_res.get("ts", 0))
        st.caption(f"每 venue 每日 DAILY 是否已落 lustre1 · 🟢 已落 · 🟡 今/昨 lag · "
                   f"🔴 缺(真 gap)· ⬛ PARKED · 🟥 corrupted · 探于 {age}s 前 · ssh→HPC")
        if any_red:
            reds = [f'{r["exchange"]}/{r["market"]}({r["history"].count("0")}天)'
                    for r in hist if "0" in r["history"]]
            st.error("⚠️ 缺 DAILY:" + ", ".join(reds))
        if not hist:
            st.caption("cold 下无 venue 目录")
            return
        st.markdown(_COLD_HIST_CSS, unsafe_allow_html=True)
        for r in hist:
            h = r["history"]
            green = h.count("1"); yellow = h.count("?"); red = h.count("0")
            corrupt = h.count("x"); parked = h.count("p")
            label = (f'<span style="opacity:.9">{r["exchange"]}/<b>{r["market"]}</b></span>'
                     f'<span style="opacity:.55"> &nbsp;{green}/{N_DAYS}')
            if red: label += f' · <span style="color:#cf222e">缺{red}</span>'
            if yellow: label += f' · <span style="color:#9a6700">lag{yellow}</span>'
            if parked: label += f' · 停{parked}'
            if corrupt: label += f' · <span style="color:#7a1620">坏{corrupt}</span>'
            label += "</span>"
            st.markdown(
                f'<div class="coldh-row"><div class="coldh-label">{label}</div>'
                f'{_cold_history_bar(h)}</div>', unsafe_allow_html=True)
        # 时间刻度:每 30 天一标(N_DAYS 天 × 8px = 720px;240px / 标)
        cell_px = 8
        n_marks = 3
        mark_w = (N_DAYS * cell_px) // n_marks
        labels_html = "".join(
            f'<span style="display:inline-block;width:{mark_w}px">{N_DAYS - i*(N_DAYS//n_marks)}d 前</span>'
            for i in range(n_marks))
        labels_html += '<span style="display:inline-block">今天</span>'
        st.markdown(
            f'<div class="coldh-scale"><div class="pad"></div><div class="marks">{labels_html}</div></div>',
            unsafe_allow_html=True)


_CS_ICON = {"ok": "✓", "bad": "✕", "stale": "!", "unknown": "?"}


def _render_cloudsync_line(res: dict) -> None:
    """fleet 卡片内的 cloud-sync 推送腿健康行(来自主探针日志,零 gdrive quota)。"""
    cs = res.get("cloudsync") or {}
    if not cs.get("n_cycles") and not cs.get("in_progress"):
        st.caption("☁️ 推送(cloud-sync):无日志(容器刚起/未配?)")
        return
    h = fleet.cloudsync_health(cs)
    rc, age, dur = cs.get("last_rc"), cs.get("last_done_age"), cs.get("last_dur_sec")
    agem = "—" if age is None else f"{int(age)//60}min 前"
    durm = "" if not dur else f" · 耗时 {int(dur)//60}min"
    tail = f" · 进行中" if cs.get("in_progress") else ""
    msg = f"上次 rc={rc} · {agem}{durm} · 近5轮 rc={cs.get('recent_rcs')}{tail}"
    if h == "bad":
        st.error(f"☁️ 推送腿异常:rc={rc}（124=rclone 超时卡住,2026-05-21 那类）· {agem}")
    elif h == "stale":
        st.warning(f"☁️ 推送腿:{int(age)//60}min 没成功了,可能卡住 · 近5轮 rc={cs.get('recent_rcs')}")
    else:
        st.caption(f"☁️ 推送(cloud-sync){_CS_ICON[h]}:{msg}")


# ───────────────────────── 每 fleet 卡片 ───────────────────────── #

def render_fleet(res: dict, stale_sec: int) -> None:
    name = res.get("fleet", "?").upper()
    with st.container(border=True):
        if res.get("error"):
            st.subheader(f"✕ {name}")
            st.error(f"探测失败:{res['error']}")
            return

        fresh, summ = _digest(res, stale_sec)
        color, icon, word = _OVERALL.get(summ["overall"], ("#5c636b", "·", ""))
        st.subheader(f"{icon} {name} · {word}")

        n_stale = len(summ["stale_venues"])
        m = st.columns(5)
        m[0].metric("容器 up", summ["n_up"])
        m[1].metric("PARKED", summ["n_parked"])
        m[2].metric("异常容器", summ["n_bad"],
                    delta=None if not summ["n_bad"] else f"+{summ['n_bad']}", delta_color="inverse")
        m[3].metric("陈旧 venue", n_stale,
                    delta=None if not n_stale else f"+{n_stale}", delta_color="inverse")
        oldest = max((f["age_sec"] for f in fresh
                      if f["status"] in ("fresh", "stale") and f["age_sec"] is not None), default=None)
        m[4].metric("最旧活跃数据", "—" if oldest is None else f"{int(oldest)}s")

        # RED:横幅 + 自动展开 stale 明细(不必点击)
        if summ["overall"] == "RED":
            parts = []
            if summ["bad_containers"]:
                parts.append("容器异常:" + ", ".join(summ["bad_containers"]))
            if summ["stale_venues"]:
                parts.append("陈旧 venue:" + ", ".join(summ["stale_venues"]))
            st.error("⚠️ " + " ｜ ".join(parts))
            stale_rows = [f for f in fresh if f["status"] == "stale"]
            if stale_rows:
                st.markdown("**陈旧 venue 明细(自动展开)**")
                _freshness_table(stale_rows)

        # cloud-sync 推送腿(EC2→gdrive)健康——来自容器日志,零 gdrive quota
        _render_cloudsync_line(res)

        # 模块色块按钮板
        cs = res.get("containers", [])
        tiles = [(f"tile_{res['fleet']}_{_safe(c['name'])}", fleet.tile_health(c, fresh), c)
                 for c in cs]
        _inject_tile_css([(k, h) for k, h, _ in tiles])
        sel_key = f"sel_{res['fleet']}"
        ncol = min(4, len(tiles)) or 1
        cols = st.columns(ncol)
        for i, (key, healthv, c) in enumerate(tiles):
            ic = _TILE.get(healthv, ("", "?"))[1]
            if cols[i % ncol].button(f"{ic} {_short(c['name'])}", key=key, use_container_width=True):
                st.session_state[sel_key] = c["name"]

        # 选中模块明细
        sel_c = next((c for c in cs if c["name"] == st.session_state.get(sel_key)), None)
        if sel_c:
            with st.container(border=True):
                _venue_detail(res, sel_c, fresh)
        else:
            st.caption("👆 点一个模块色块查看该 venue 的逐 symbol 明细。")

        # 元信息 + 全量,降权放底部
        h = res.get("health", [])
        h_ok = sum(1 for x in h if x["ok"]); h_tot = len(h)
        st.caption(f"EC2 `{res.get('ec2_state')}` · SSM `{res.get('ssm_ping')}` · "
                   f"commit `{res.get('commit',{}).get('sha','?')}` · "
                   f"/health {h_ok}/{h_tot} OK · "
                   f"探于 {int(dt.datetime.now().timestamp() - res.get('ts', 0))}s 前 · "
                   f"`{res.get('instance_id','')}`")
        day = res.get("day", "")
        cov = res.get("coverage") or []
        with st.expander(f"📅 今日 UTC {day} 录制覆盖({len(cov)} symbol)"):
            # 旧 freshness 全量表已删:它跟 coverage 条(每 symbol 一行 144 桶)+
            # 点色块下钻的逐 symbol 明细 100% 重复。这里只留 coverage 条。
            if cov and day:
                try:
                    _render_coverage(cov, res.get("n_buckets", 144), day)
                except Exception as e:
                    st.warning(f"coverage 渲染失败:{e}")
            else:
                st.caption("无 coverage 数据")
