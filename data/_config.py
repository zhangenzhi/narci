"""YAML 配置加载的单一入口(P2 冗余底座,2026-05-26)。

此前 ``yaml.safe_load(f).get(section, {})`` + 文件存在性判断散落在多个
模块(l2_recorder / download / backtest / ...),错误处理各不一致。
统一在这里。
"""

import os

import yaml


def load_config(path: str) -> dict | None:
    """读取并解析整个 YAML 配置;文件不存在返回 None。"""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config_section(path: str, section: str, default: dict | None = None) -> dict:
    """读取 YAML 并取出 ``section`` 子段;文件缺失或无该段时返回 ``default``。

    保持旧行为:缺失即回落到 ``default``(默认空 dict),不抛异常。
    """
    if default is None:
        default = {}
    cfg = load_config(path)
    if cfg is None:
        return default
    return cfg.get(section, default)
