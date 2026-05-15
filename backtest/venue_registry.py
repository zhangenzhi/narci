"""
Venue-level metadata: fee schedule, trade-side decoding hint, supported order types.

跟 `symbol_spec.py` 配对使用:`SymbolSpec` 是 per-symbol 的 tick/lot/min_notional,
`VenueSpec` 是 per-exchange 的费率与撮合行为。回测 + calibration replay 按
`meta.json.exchange` 字段查表得到 `VenueSpec`,套到 `MakerSimBroker` /
`SpotBroker` / `SimulatedBroker` 上,避免拿 CC 默认费率跑 bitbank 数据。

Phase 1 scope (本文件):
  - 数据结构 + 注册表
  - 不重构 broker — 现有 maker_fee/taker_fee kwargs 路径保持不变
  - 用法见 helper `apply_to_broker()`,opt-in 接入

Phase 2 (跟 venue dispatch 整合):
  - `BaseBroker.__init__` 接 `venue=` 参数,自动从 registry 拉默认
  - 撮合层根据 `supports_post_only` 拒掉不支持 venue 的 order-type
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VenueSpec:
    """每个交易所的撮合参数 + 费率契约。

    fee 用 bps (basis points) 表示:1 bps = 0.01%。
    maker_fee_bps 可以为负 (rebate),例如 bitbank maker = -2 bps。
    """
    exchange: str                              # 例: "coincheck" / "bitbank"
    market_type: str = "spot"                  # spot / um_futures
    maker_fee_bps: float = 0.0                 # 负数 = rebate
    taker_fee_bps: float = 0.0
    supports_post_only: bool = False           # bitbank/Binance 支持;CC 不支持
    supports_ioc: bool = False                 # immediate-or-cancel
    supports_fok: bool = False                 # fill-or-kill
    # trade-side decoding: "buy_taker_pos" 表示 taker=buy 应 narci qty 取正 / 负
    # CC 用 "sell_taker_neg" (sell=主动卖 → qty 取负),bitbank 实测一致 ("sell_taker_pos"
    # 即 taker=sell → buyer maker → narci 正)。下方注解详见各 adapter。
    trade_sign_convention: str = "sell_taker_neg"
    # 默认 endpoint host (供 docs / debug 引用,实际 URL 仍走 adapter)
    rest_public_base: str = ""
    ws_url_hint: str = ""
    notes: str = ""


# ============================================================
# Registry — 添加新 venue 在这里登记
# ============================================================

VENUE_REGISTRY: dict[str, VenueSpec] = {
    "coincheck": VenueSpec(
        exchange="coincheck",
        market_type="spot",
        maker_fee_bps=0.0,
        taker_fee_bps=0.0,
        supports_post_only=False,
        supports_ioc=False,
        supports_fok=False,
        trade_sign_convention="sell_taker_neg",
        rest_public_base="https://coincheck.com",
        ws_url_hint="wss://ws-api.coincheck.com",
        notes="JPY 现货,主力 venue,零费率。trade 帧 list-of-lists 含 'sell' 字段标主动卖。",
    ),
    "bitbank": VenueSpec(
        exchange="bitbank",
        market_type="spot",
        maker_fee_bps=-2.0,                    # rebate
        taker_fee_bps=12.0,
        supports_post_only=True,
        supports_ioc=False,                    # bitbank 不支持 IOC,只 GTC / post_only
        supports_fok=False,
        trade_sign_convention="buy_taker_neg",  # taker=buy → seller maker → narci qty 取负
        rest_public_base="https://public.bitbank.cc",
        ws_url_hint="https://stream.bitbank.cc",
        notes="socket.io v2 (EIO=3) WS,深度 channel: depth_diff_<pair> + depth_whole_<pair>;"
              " 交易 channel: transactions_<pair>。maker rebate -2 bps 是核心利润源。",
    ),
    "binance_spot": VenueSpec(
        exchange="binance_spot",
        market_type="spot",
        maker_fee_bps=10.0,                    # 1 bps with BNB; 10 bps default
        taker_fee_bps=10.0,
        supports_post_only=True,
        supports_ioc=True,
        supports_fok=True,
        trade_sign_convention="buy_taker_neg",  # aggTrade m=true → maker side is buyer
        rest_public_base="https://api.binance.com",
        ws_url_hint="wss://stream.binance.com:9443",
        notes="USDT 现货,默认费率 10/10 bps;持有 BNB 抵扣后 7.5/7.5。",
    ),
    "binance_jp": VenueSpec(
        exchange="binance_jp",
        market_type="spot",
        maker_fee_bps=10.0,
        taker_fee_bps=10.0,
        supports_post_only=True,
        supports_ioc=True,
        supports_fok=True,
        trade_sign_convention="buy_taker_neg",
        rest_public_base="https://api.binance.co.jp",
        ws_url_hint="wss://data-stream.binance.vision",
        notes="JPY 现货走 Binance Japan,WS 仍走 binance.vision data-stream。",
    ),
    "binance_um_futures": VenueSpec(
        exchange="binance_um_futures",
        market_type="um_futures",
        maker_fee_bps=2.0,
        taker_fee_bps=5.0,
        supports_post_only=True,
        supports_ioc=True,
        supports_fok=True,
        trade_sign_convention="buy_taker_neg",
        rest_public_base="https://fapi.binance.com",
        ws_url_hint="wss://fstream.binance.com",
        notes="U 本位永续,2/5 bps 标准费率;UM 双 endpoint /public + /market 拆 depth/trade。",
    ),
    "bitflyer_spot": VenueSpec(
        exchange="bitflyer",
        market_type="spot",
        maker_fee_bps=15.0,                    # 0.15% spot 默认
        taker_fee_bps=15.0,
        supports_post_only=True,
        supports_ioc=True,
        supports_fok=True,
        trade_sign_convention="buy_taker_neg",
        rest_public_base="https://api.bitflyer.com",
        ws_url_hint="wss://ws.lightstream.bitflyer.com/json-rpc",
        notes="bitFlyer 普通现货,JSON-RPC over WebSocket;板深 ≈300 levels。"
              " 月成交量阶梯费率,15 bps 是入门价。",
    ),
    "bitflyer_fx": VenueSpec(
        exchange="bitflyer",
        market_type="fx",
        maker_fee_bps=0.0,                     # Lightning FX 当前无 maker 费
        taker_fee_bps=0.0,                     # 但有日内 SWAP 利息 (类 funding rate)
        supports_post_only=True,
        supports_ioc=True,
        supports_fok=True,
        trade_sign_convention="buy_taker_neg",
        rest_public_base="https://api.bitflyer.com",
        ws_url_hint="wss://ws.lightstream.bitflyer.com/json-rpc",
        notes="bitFlyer Lightning FX 永续 (FX_BTC_JPY only),日本最大 BTC 永续。"
              " 手续费 0/0,但有日内 SWAP 利息 (持仓每 4h 收取约 0.04% 类 funding)。"
              " 流动性比 spot 强,日成交是 bitFlyer 主战场。",
    ),
    "gmo_spot": VenueSpec(
        exchange="gmo",
        market_type="spot",
        maker_fee_bps=-1.0,                    # rebate -0.01% 成交占比 65% 以下
        taker_fee_bps=5.0,                     # 0.05%
        supports_post_only=False,              # GMO API 不支持 post_only,只 IOC
        supports_ioc=True,
        supports_fok=True,
        trade_sign_convention="buy_taker_neg",
        rest_public_base="https://api.coin.z.com/public",
        ws_url_hint="wss://api.coin.z.com/ws/public/v1",
        notes="GMO 現物 (single-asset symbol like BTC),WS orderbooks 每条都是全量"
              " snapshot (无 diff)。",
    ),
    "gmo_leverage": VenueSpec(
        exchange="gmo",
        market_type="leverage",
        maker_fee_bps=-1.0,                    # rebate (条件同 spot)
        taker_fee_bps=5.0,
        supports_post_only=False,
        supports_ioc=True,
        supports_fok=True,
        trade_sign_convention="buy_taker_neg",
        rest_public_base="https://api.coin.z.com/public",
        ws_url_hint="wss://api.coin.z.com/ws/public/v1",
        notes="GMO レバレッジ取引 (pair symbol BTC_JPY/ETH_JPY 等),最高 2x 杠杆,"
              " JPY 现金结算。深度 + 活跃度 > 現物,echo 团队偏好的主战场。"
              " WS orderbooks 同样是全量 snapshot。",
    ),
}


def get_venue(name: str) -> VenueSpec:
    """按 exchange 名查 VenueSpec。未知 venue 抛 KeyError。"""
    key = name.lower()
    if key not in VENUE_REGISTRY:
        raise KeyError(
            f"Unknown venue {name!r}. Registered: {sorted(VENUE_REGISTRY.keys())}"
        )
    return VENUE_REGISTRY[key]


def apply_to_broker(broker, exchange: str) -> None:
    """把 VenueSpec 的费率覆盖到一个已存在的 broker 实例上。

    使用场景:calibration replay 从 meta.json.exchange 拉 venue,套到默认构造
    出的 SpotBroker 上。比改 broker 构造函数侵入小。

    Phase 1 只覆盖费率;Phase 2 会扩展到 order-type 校验。
    """
    spec = get_venue(exchange)
    # bps → fraction
    broker.maker_fee = spec.maker_fee_bps / 1e4
    broker.taker_fee = spec.taker_fee_bps / 1e4
