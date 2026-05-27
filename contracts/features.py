"""narci.contracts.features —— 特征契约(名称 + 版本).

FEATURE_NAMES(有序,下游模型按索引 pin)+ FEATURES_VERSION(增删特征必 bump)。
nyx 训练 / echo 推理共享。P5 从 features/realtime.py 拆出。
"""

FEATURES_VERSION = "v6"


# ------------------------------------------------------------------ #
# Feature taxonomy — DO NOT change these names without bumping
# FEATURES_VERSION; downstream models pin to a specific list.
# ------------------------------------------------------------------ #


# Baseline (tested in OLS research, R² 12% on trade target)
BASELINE_FEATURES = [
    # UM trade-based
    "r_um", "r_um_2s", "r_um_5s", "r_um_10s",
    "um_imb_1s", "um_imb_30s_norm", "um_n_5s", "um_vol_5s",
    # UM signed trade flow (v4 — independent lead signal vs mid; ~50ms
    # extra horizon per nyx 2026-05-10 lead-lag study, peak +25ms /
    # 50% cliff +200ms). Σ(taker_buy_qty - taker_sell_qty) over window.
    "um_flow_50ms", "um_flow_100ms", "um_flow_500ms", "um_flow_5s",
    # BJ trade-based
    "r_bj", "bj_imb_1s", "bj_flow_5s",
    # CC trade intensity (v6 2026-05-15) — nyx 16-day LOO audit 显示
    # 这一个特征单独把 LGB R² 从 +6.81% 升到 +11.17%,IR 2.57→4.26
    # (全 16 个 variant 中 IR 最高);CC sample 点过去 50ms 窗口内 CC
    # trade count。机制类比 Hawkes self-exciting:50ms 内 5+ 笔 trade
    # 是 rare burst,强烈预示 informed flow 方向延续 1s。
    "trade_intensity_burst_50ms",
    # CC own
    "r_cc_lag1", "r_cc_lag2", "cc_imb_1s",
    # Basis
    #   basis_bj_bps = log(cc_mid / bj_mid) * 1e4
    #     CC ↔ BJ cross-venue, both JPY-denominated.
    #   um_x_basis = r_um * basis_bj_bps (interaction).
    #   basis_um_bps = log(um_mid / bs_mid) * 1e4  (v5 2026-05-14)
    #     UM perp ↔ BS (Binance global spot) USDT-USDT basis. The
    #     classic crypto perp-spot premium signal (positive = perp
    #     premium = contango, generally bullish-leaning carry).
    #     STRICT: NaN whenever the BS book hasn't bootstrapped — most
    #     pre-2026-05-13 days are Vision-backfilled trade-only (Vision
    #     bookTicker not available — see deploy/reco/donor/NARCI_DONOR_INTERFACE.md),
    #     so this is only finite on 2026-05-13+ live recording.
    #   basis_um_bps_trade_proxy = log(um_mid / bs_last_trade_price) * 1e4
    #     (v5 2026-05-14, addendum) Lossy fallback that lets the basis
    #     signal compute even on trade-only days. bs_last_trade alternates
    #     between bid and ask depending on which side the taker hit, so
    #     this has ~half-spread noise vs the strict mid version. nyx
    #     can use both: strict for clean signal where available, proxy
    #     when only aggTrades-backfilled days are in the training pool.
    "basis_bj_bps", "um_x_basis", "basis_um_bps", "basis_um_bps_trade_proxy",
]

# Tier 1 (hour-of-day + CC own flow)
TIER1_FEATURES = [
    "hour_sin", "hour_cos",
    "cc_flow_5s", "cc_flow_30s", "cc_imb_30s_norm",
]

# Tier 2 (CC L2 imbalance / microprice — gated by L2 reconstruction)
TIER2_FEATURES = [
    "cc_l2_top1_imb", "cc_l2_top5_imb",
    "cc_l2_imb_top1_5s", "cc_l2_imb_top5_5s",
    "cc_l2_micro_dev_bps", "cc_micro_dev_5s",
    "cc_l2_spread_bps",
    "bj_l2_top1_imb", "bj_l2_top5_imb", "l2_imb_diff",
]

FEATURE_NAMES = BASELINE_FEATURES + TIER1_FEATURES + TIER2_FEATURES
