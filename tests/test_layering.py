"""分层 import 红线(P5,2026-05-26)。

把 narci 的三层边界**钉死为测试**(替代物理拆包/拆仓):

    core  <  recorder  <  analytics

规则:一个模块只能 import 同层或**下层**;**严禁向上 import**
(recorder 不得 import analytics;core 不得 import recorder/analytics)。
这给到"降爆炸半径"(研究/回测的改动无法悄悄拖进 24/7 录制器)与"职责清晰"
(下面的 LAYER_* 清单就是分层文档),无需真正搬目录。

层成员(按 docs/design/REFACTOR_DESIGN.md §8.2):
  - core      : 跨层共享的格式/契约/工具(无业务依赖)
  - recorder  : 实时采集 + 历史摄取 + 数据策展(pandas/pyarrow/websockets/requests)
  - analytics : 重建/采样/特征/模拟/校准/研究/GUI(+ numpy/lightgbm/torch/streamlit)

新增 data/ 文件必须在此归层,否则本测试失败 —— 强制做一次有意识的分层决策。
"""
from __future__ import annotations

import ast
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- 层成员清单(相对仓库根的路径 / 目录前缀)---------------------------- #

CORE_FILES = {
    "core/io.py", "core/config.py", "core/symbol_spec.py",
}
# contracts/ 是对外契约层(schema/manifest/features),与 core 同为底层(无业务依赖)
CORE_DIR_PREFIXES = ("core/", "contracts/")
RECORDER_FILES = {"deploy/healthcheck.py"}
RECORDER_DIR_PREFIXES = ("recorder/",)
ANALYTICS_FILES = set()
ANALYTICS_DIR_PREFIXES = ("analytics/",)

ORDER = {"core": 0, "recorder": 1, "analytics": 2}


def _rel(path: str) -> str:
    return os.path.relpath(path, REPO).replace("\\", "/")


def file_layer(rel: str) -> str | None:
    if rel in CORE_FILES or rel.startswith(CORE_DIR_PREFIXES):
        return "core"
    if rel in RECORDER_FILES or rel.startswith(RECORDER_DIR_PREFIXES):
        return "recorder"
    if rel in ANALYTICS_FILES or rel.startswith(ANALYTICS_DIR_PREFIXES):
        return "analytics"
    return None


def module_layer(mod: str) -> str | None:
    """把 import 目标模块名映射到层。"""
    top = mod.split(".")[0]
    if top in ("core", "contracts"):
        return "core"
    if top == "analytics":
        return "analytics"
    if top == "recorder":
        return "recorder"
    return None  # 三方库 / stdlib —— 不约束


def _iter_layered_py():
    for base, _, files in os.walk(REPO):
        if "__pycache__" in base or "/tests" in base or base.endswith("tests"):
            continue
        for fn in files:
            if not fn.endswith(".py") or fn == "__init__.py":
                continue
            rel = _rel(os.path.join(base, fn))
            yield rel, os.path.join(base, fn)


def test_data_files_all_classified():
    """每个分层目录(core/contracts/recorder/analytics)下的 *.py 都必须归层,
    逼新文件做分层决策。"""
    unclassified = []
    for rel, _ in _iter_layered_py():
        top = rel.split("/")[0]
        if top in ("core", "contracts", "recorder", "analytics") and file_layer(rel) is None:
            unclassified.append(rel)
    assert not unclassified, (
        "这些文件未在 tests/test_layering.py 归层,请把它们加入 "
        f"CORE/RECORDER/ANALYTICS 之一:\n  " + "\n  ".join(unclassified)
    )


def test_no_upward_cross_layer_imports():
    """core/recorder 模块不得 import 上层模块。"""
    violations = []
    for rel, path in _iter_layered_py():
        L = file_layer(rel)
        if L not in ("core", "recorder"):
            continue
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        targets = []
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                targets.append(n.module)
            elif isinstance(n, ast.Import):
                targets.extend(a.name for a in n.names)
        for mod in targets:
            tl = module_layer(mod)
            if tl in ORDER and ORDER[tl] > ORDER[L]:
                violations.append(f"{rel} [{L}] → {mod} [{tl}]")
    assert not violations, (
        "检测到向上跨层 import(下层依赖上层,违反 core<recorder<analytics):\n  "
        + "\n  ".join(violations)
    )
