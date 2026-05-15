"""
交易所适配器工厂。

用法：
    from data.exchange import get_adapter
    adapter = get_adapter("binance", market_type="um_futures")
"""

from .base import ExchangeAdapter
from .binance import (BinanceAdapter, BinanceSpotAdapter,
                      BinanceUmFuturesAdapter, BinanceJpAdapter)
from .bitbank import BitbankAdapter
from .bitflyer import BitflyerAdapter
from .coincheck import CoincheckAdapter
from .gmo import GmoAdapter

_REGISTRY = {
    "binance": BinanceAdapter,
    "binance_spot": BinanceSpotAdapter,
    "binance_um_futures": BinanceUmFuturesAdapter,
    "binance_jp": BinanceJpAdapter,
    "bitbank": BitbankAdapter,
    "bitflyer": BitflyerAdapter,
    "coincheck": CoincheckAdapter,
    "gmo": GmoAdapter,
}


def get_adapter(name: str, market_type: str | None = None,
                **kwargs) -> ExchangeAdapter:
    """
    工厂方法：按交易所名加载适配器。

    参数:
        name:        交易所名 (如 "binance")
        market_type: 市场类型 (如 "spot" / "um_futures")，部分适配器需要
        kwargs:      透传给适配器构造函数（如自定义 ws_tpl / snapshot_tpl）
    """
    name_lower = name.lower()

    if name_lower == "binance":
        # Binance 适配器接受 market_type + 可选 ws_tpl / snapshot_tpl
        bn_kwargs = {k: v for k, v in kwargs.items()
                     if k in ("ws_tpl", "snapshot_tpl") and v is not None}
        return BinanceAdapter(market_type=market_type or "um_futures", **bn_kwargs)

    if name_lower == "binance_jp":
        jp_kwargs = {k: v for k, v in kwargs.items()
                     if k in ("ws_tpl", "snapshot_tpl") and v is not None}
        return BinanceJpAdapter(**jp_kwargs)

    if name_lower == "coincheck":
        cc_kwargs = {k: v for k, v in kwargs.items() if k in ()}
        return CoincheckAdapter(**cc_kwargs)

    if name_lower == "bitbank":
        bb_kwargs = {k: v for k, v in kwargs.items() if k in ()}
        return BitbankAdapter(**bb_kwargs)

    if name_lower == "bitflyer":
        # bitflyer 接 market_type='spot'|'fx',recorder save_dir 自动拆
        return BitflyerAdapter(market_type=market_type or "spot")

    if name_lower == "gmo":
        # gmo 接 market_type='spot'|'leverage';默认 leverage 因为是主战场
        return GmoAdapter(market_type=market_type or "leverage")

    if name_lower in _REGISTRY:
        return _REGISTRY[name_lower](**kwargs)

    raise ValueError(
        f"Unknown exchange adapter: {name!r}. "
        f"Available: {list(_REGISTRY.keys())}"
    )


__all__ = ["ExchangeAdapter", "get_adapter", "BinanceAdapter",
           "BitbankAdapter", "BitflyerAdapter", "CoincheckAdapter",
           "GmoAdapter"]
