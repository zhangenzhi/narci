"""历史数据源工厂。"""

from .base import HistoricalSource
from .binance_vision import BinanceVisionSource
from .tardis import TardisSource

_REGISTRY = {
    "binance_vision": BinanceVisionSource,
    "tardis":         TardisSource,
}


def get_source(name: str, **kwargs) -> HistoricalSource:
    """按名加载历史数据源。"""
    key = name.lower()
    if key not in _REGISTRY:
        raise ValueError(
            f"Unknown historical source: {name!r}. "
            f"Available: {list(_REGISTRY.keys())}"
        )
    return _REGISTRY[key](**kwargs)


__all__ = ["HistoricalSource", "get_source",
           "BinanceVisionSource", "TardisSource"]
