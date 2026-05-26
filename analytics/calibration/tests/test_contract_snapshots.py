# -*- coding: utf-8 -*-
"""契约快照护栏(P0,2026-05-26)。

逐字冻结 nyx↔narci↔echo 之间的跨仓库契约 —— 任何改动都会让本文件变红,
逼出一次**有意识**的版本 bump + 下游协调,而不是无声漂移。

冻结对象(见 docs/REFACTOR_DESIGN.md §3 护栏):
  - features.FEATURE_NAMES(完整有序列表)+ FEATURES_VERSION
  - calibration.alpha_models 的 SAMPLING_MODES / TARGET_KINDS /
    MODEL_OUTPUT_UNITS / MANIFEST_SCHEMA_VERSION
  - calibration.schema 的 SCHEMA_VERSION + RAW_L2_SCHEMA + 三个事件的
    PARQUET_SCHEMA 字段(名/类型/nullable)

改动契约时:更新这里的期望值,同时 bump 对应 *_VERSION,并按
docs/INTERFACE_NARCI_NYX.md / INTERFACE_NARCI_ECHO.md 通知下游。
本文件不导入任何重 IO,纯常量比对,快且无外部依赖。
"""
from __future__ import annotations

from contracts.features import FEATURE_NAMES, FEATURES_VERSION
from contracts import manifest as am
from contracts import schema as S


# ------------------------------------------------------------------ #
# features 契约
# ------------------------------------------------------------------ #

EXPECTED_FEATURES_VERSION = "v6"

EXPECTED_FEATURE_NAMES = [
    "r_um", "r_um_2s", "r_um_5s", "r_um_10s",
    "um_imb_1s", "um_imb_30s_norm", "um_n_5s", "um_vol_5s",
    "um_flow_50ms", "um_flow_100ms", "um_flow_500ms", "um_flow_5s",
    "r_bj", "bj_imb_1s", "bj_flow_5s",
    "trade_intensity_burst_50ms",
    "r_cc_lag1", "r_cc_lag2", "cc_imb_1s",
    "basis_bj_bps", "um_x_basis", "basis_um_bps", "basis_um_bps_trade_proxy",
    "hour_sin", "hour_cos",
    "cc_flow_5s", "cc_flow_30s", "cc_imb_30s_norm",
    "cc_l2_top1_imb", "cc_l2_top5_imb",
    "cc_l2_imb_top1_5s", "cc_l2_imb_top5_5s",
    "cc_l2_micro_dev_bps", "cc_micro_dev_5s", "cc_l2_spread_bps",
    "bj_l2_top1_imb", "bj_l2_top5_imb", "l2_imb_diff",
]


def test_features_version_frozen():
    assert FEATURES_VERSION == EXPECTED_FEATURES_VERSION, (
        f"FEATURES_VERSION 从 {EXPECTED_FEATURES_VERSION!r} 变成 "
        f"{FEATURES_VERSION!r} —— 改特征必须同步更新本快照 + 通知 nyx 重训。"
    )


def test_feature_names_frozen():
    assert FEATURE_NAMES == EXPECTED_FEATURE_NAMES, (
        "FEATURE_NAMES(名称或顺序)变了。下游模型按本列表的索引 pin 输入,"
        "任何增删/重排都需 bump FEATURES_VERSION 并协调 nyx。\n"
        f"新增: {set(FEATURE_NAMES) - set(EXPECTED_FEATURE_NAMES)}\n"
        f"删除: {set(EXPECTED_FEATURE_NAMES) - set(FEATURE_NAMES)}"
    )


def test_feature_names_no_duplicates():
    assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))


# ------------------------------------------------------------------ #
# nyx↔narci manifest 契约(alpha_models)
# ------------------------------------------------------------------ #

EXPECTED_SAMPLING_MODES = {
    "1s_grid",
    "event_at_bj_trade",
    "event_at_book_update",
    "event_at_cc_trade",
    "event_at_simulated_maker_fill",
}

EXPECTED_TARGET_KINDS = {
    "bj_l2_microprice_log_return_1s",
    "bj_l2_mid_log_return_1s",
    "cc_l2_microprice_log_return_1s",
    "cc_l2_mid_log_return_1s",
    "cc_maker_conditional_fill_pnl_buy_τ1000ms",
    "cc_maker_conditional_fill_pnl_sell_τ1000ms",
    "cc_mid_event_log_return",
    "cc_trade_event_log_return",
    "fill_pnl_1s_log_return",
    "mid_10s_log_return",
    "mid_1s_log_return",
    "mid_30s_log_return",
    "mid_5s_log_return",
    "trade_10s_log_return",
    "trade_1s_log_return",
    "trade_30s_log_return",
    "trade_5s_log_return",
}

EXPECTED_MODEL_OUTPUT_UNITS = {"bps", "log_return"}


def test_sampling_modes_frozen():
    assert set(am.SAMPLING_MODES) == EXPECTED_SAMPLING_MODES, (
        "SAMPLING_MODES 变了。采样器抽象(痛点1)与 nyx manifest 都 pin 这个"
        "集合,改动需协调 nyx + narci 采样实现。\n"
        f"新增: {set(am.SAMPLING_MODES) - EXPECTED_SAMPLING_MODES}\n"
        f"删除: {EXPECTED_SAMPLING_MODES - set(am.SAMPLING_MODES)}"
    )


def test_target_kinds_frozen():
    assert set(am.TARGET_KINDS) == EXPECTED_TARGET_KINDS, (
        f"TARGET_KINDS 变了。\n新增: {set(am.TARGET_KINDS) - EXPECTED_TARGET_KINDS}\n"
        f"删除: {EXPECTED_TARGET_KINDS - set(am.TARGET_KINDS)}"
    )


def test_model_output_units_frozen():
    assert set(am.MODEL_OUTPUT_UNITS) == EXPECTED_MODEL_OUTPUT_UNITS


def test_manifest_schema_version_frozen():
    assert am.MANIFEST_SCHEMA_VERSION == "v1"


# ------------------------------------------------------------------ #
# echo↔narci 事件 schema 契约(schema.py)
# ------------------------------------------------------------------ #

EXPECTED_SCHEMA_VERSION = "v1.1"

# (name, arrow_type_str, nullable) —— 逐字段冻结。
EXPECTED_RAW_L2 = [
    ("timestamp", "int64", False), ("side", "int8", False),
    ("price", "double", False), ("quantity", "double", False),
]

EXPECTED_DECISION = [
    ("ts_ns", "int64", False), ("ts_wall_ns", "int64", False),
    ("event_type", "string", False), ("client_oid", "string", False),
    ("symbol", "string", False), ("mid_price", "double", False),
    ("best_bid", "double", False), ("best_ask", "double", False),
    ("top1_bid_qty", "double", False), ("top1_ask_qty", "double", False),
    ("spread_bps", "double", False), ("alpha_pred_bps", "double", True),
    ("alpha_features_hash", "string", False), ("alpha_source", "string", False),
    ("side", "string", False), ("price", "double", True),
    ("qty", "double", True), ("estimated_queue_position", "int32", False),
    ("estimated_queue_ahead_qty", "double", True), ("place_oid", "string", False),
    ("reason", "string", False),
]

EXPECTED_FILL = [
    ("ts_ns", "int64", False), ("ts_wall_ns", "int64", False),
    ("exchange_ts_ms", "int64", False), ("client_oid", "string", False),
    ("exchange_oid", "string", False), ("symbol", "string", False),
    ("side", "string", False), ("fill_price", "double", False),
    ("fill_qty", "double", False), ("is_maker", "bool", False),
    ("place_ts_ns", "int64", False), ("quote_age_ms", "double", False),
    ("mid_at_place", "double", False), ("mid_at_fill", "double", False),
    ("spread_at_fill_bps", "double", False), ("mid_t_plus_500ms", "double", True),
    ("mid_t_plus_1s", "double", True), ("mid_t_plus_5s", "double", True),
    ("mid_t_plus_30s", "double", True), ("estimated_queue_at_place", "int32", False),
    ("estimated_queue_at_fill", "int32", False), ("inventory_before", "double", False),
    ("inventory_after", "double", False), ("alpha_pred_bps_at_place", "double", True),
    ("alpha_realized_bps_post", "double", True),
]

EXPECTED_CANCEL = [
    ("ts_ns_request", "int64", False), ("ts_wall_ns_request", "int64", False),
    ("ts_ns_ack", "int64", False), ("cancel_latency_ms", "double", False),
    ("client_oid", "string", False), ("exchange_oid", "string", False),
    ("symbol", "string", False), ("place_ts_ns", "int64", False),
    ("quote_age_at_cancel_ms", "double", False), ("cancel_reason", "string", False),
    ("final_state", "string", False), ("qty_filled_before_cancel", "double", False),
    ("mid_at_cancel", "double", False),
]


def _fields(schema):
    return [(f.name, str(f.type), f.nullable) for f in schema]


def test_schema_version_frozen():
    assert S.SCHEMA_VERSION == EXPECTED_SCHEMA_VERSION, (
        f"SCHEMA_VERSION {S.SCHEMA_VERSION!r} != {EXPECTED_SCHEMA_VERSION!r};"
        " echo 日志 schema 改动必须 bump 并通知 echo。"
    )


def test_raw_l2_schema_frozen():
    assert _fields(S.RAW_L2_SCHEMA) == EXPECTED_RAW_L2


def test_decision_event_schema_frozen():
    assert _fields(S.DecisionEvent.PARQUET_SCHEMA) == EXPECTED_DECISION


def test_fill_event_schema_frozen():
    assert _fields(S.FillEvent.PARQUET_SCHEMA) == EXPECTED_FILL


def test_cancel_event_schema_frozen():
    assert _fields(S.CancelEvent.PARQUET_SCHEMA) == EXPECTED_CANCEL
