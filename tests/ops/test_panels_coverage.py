"""ops.panels coverage 进度条测试 + 回归稳健性守卫。

2026-05-28 实测过一次"~1MB HTML 单 st.markdown 调用噎住 streamlit / 看板不出"
事故,改成"CSS 类 + 按 symbol 单调用"修复。本测试把那个失败模式钉死:
- _coverage_bar_html 纯函数:bitmap 解码到 HTML 结构正确。
- _render_coverage 在大规模 coverage(100 symbol)下:
  * 每条 st.markdown 调用都 < 64KB(防再有人把它合回大 blob)。
  * 至少 N+ 条调用(确保按 symbol 拆分,不是一坨)。
  * 不抛异常。
"""
from __future__ import annotations

from ops import panels


def test_coverage_bar_html_encodes_each_bit():
    bm = "1" * 10 + "0" * 100 + "x" * 5 + "1" * 29  # 共 144
    html = panels._coverage_bar_html(bm)
    assert html.count('class="g"') == 39                # 10 + 29 个有 shard
    assert html.count('class="r"') == 5                 # 5 个 corrupted
    assert html.count("<span") == 144                   # 每桶一个 span
    assert 'class="cov-bar"' in html


def test_render_coverage_per_call_size_bounded(monkeypatch):
    """**回归守卫**:不得有任何 > 64KB 的 st.markdown 单调用。
    历史:一次性发 1MB HTML 把 streamlit 渲染噎住,看板整面板不出。"""
    md_calls, cap_calls = [], []
    monkeypatch.setattr(panels.st, "markdown", lambda s, **kw: md_calls.append(s))
    monkeypatch.setattr(panels.st, "caption", lambda s, **kw: cap_calls.append(s))

    n_syms = 100   # 远超真实(~47)
    coverage = [{"exchange": "binance", "market": "spot", "symbol": f"SYM{i:03d}",
                 "bitmap": "1" * 144} for i in range(n_syms)]
    panels._render_coverage(coverage, 144, "20260528")

    max_sz = max(len(s) for s in md_calls)
    assert max_sz < 64 * 1024, (
        f"单条 st.markdown ~{max_sz/1024:.1f}KB 超 64KB —— 大概率噎住 streamlit;"
        f"应拆分成按 symbol 单独调用 + CSS 类(见 _COV_CSS)")
    # 标题 + caption 不算,核心 bar 行至少 n_syms 条 + CSS + scale
    assert len(md_calls) >= n_syms + 2, (
        f"得到 {len(md_calls)} 条 markdown 调用,期望 >= {n_syms + 2}(每 symbol 一条 + "
        f"CSS + 时间刻度);若数量骤减说明又被合成大 blob 了")


def test_render_coverage_no_exception_on_edge_cases(monkeypatch):
    monkeypatch.setattr(panels.st, "markdown", lambda *a, **kw: None)
    monkeypatch.setattr(panels.st, "caption", lambda *a, **kw: None)
    # 空 coverage / 缺 bitmap 字段 / 含 corrupted / 短 bitmap 都不该炸
    panels._render_coverage([], 144, "20260528")
    panels._render_coverage(
        [{"exchange": "x", "market": "spot", "symbol": "S"}],   # 无 bitmap → fallback "0"*144
        144, "20260528")
    panels._render_coverage(
        [{"exchange": "x", "market": "spot", "symbol": "S", "bitmap": "1xx0" * 36}],  # 含坏
        144, "20260528")
