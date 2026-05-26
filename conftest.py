"""Root conftest —— 测试体系的地基(P4.5,2026-05-26)。

它的存在让 pytest 把**仓库根**插入 sys.path,从而测试文件无论放在哪个
目录(`tests/recorder/`、`tests/analytics/`、历史的 `calibration/tests/`)
都能 `from data import ...` / `from features import ...` 等绝对导入 ——
narci 未作为包安装,此前靠 `calibration/tests/__init__.py` 的包链才把根带上
sys.path;有了本文件,测试可以脱离那条包链、提升为顶层 `tests/` 树。

测试组织目标(见 docs/design/REFACTOR_DESIGN.md §9 P4.5):按模块分目录
(`tests/recorder/` 已建),作为"读懂该模块行为与稳健性保证"的入口。
"""
