"""
Binance 交易所适配器（现货 + U 本位合约）。

将原 l2_recorder.py 中散落的 Binance 特有逻辑集中到此处。
"""

import time

from .base import ExchangeAdapter


class BinanceAdapter(ExchangeAdapter):
    """
    Binance 通用适配器。通过 market_type 区分现货和 U 本位合约。

    两种市场差异仅在 URL 前缀：
      - spot:       stream.binance.com:9443 + api.binance.com
      - um_futures: fstream.binance.com    + fapi.binance.com
    其他（消息格式、U/u 对齐机制、字段名）完全一致。
    """

    name = "binance"

    _ENDPOINTS = {
        "spot": {
            "ws":       "wss://stream.binance.com:9443/stream?streams={streams}",
            "snapshot": "https://api.binance.com/api/v3/depth?symbol={symbol_upper}&limit=1000",
        },
        "um_futures": {
            "ws":       "wss://fstream.binance.com/stream?streams={streams}",
            "snapshot": "https://fapi.binance.com/fapi/v1/depth?symbol={symbol_upper}&limit=1000",
        },
    }

    def __init__(self, market_type: str = "um_futures",
                 ws_tpl: str | None = None, snapshot_tpl: str | None = None):
        self.market_type = market_type.lower()
        if self.market_type not in self._ENDPOINTS:
            raise ValueError(f"Unsupported Binance market_type: {market_type}")

        defaults = self._ENDPOINTS[self.market_type]
        self.ws_tpl = ws_tpl or defaults["ws"]
        self.snapshot_tpl = snapshot_tpl or defaults["snapshot"]

    # ------------------------------------------------------------------ #
    # WebSocket
    # ------------------------------------------------------------------ #

    def ws_url(self, symbols: list[str], interval_ms: int = 100) -> str:
        streams = []
        for s in symbols:
            streams.append(f"{s.lower()}@depth@{interval_ms}ms")
            streams.append(f"{s.lower()}@aggTrade")
        return self.ws_tpl.format(streams="/".join(streams))

    async def fetch_snapshot(self, symbol: str) -> dict:
        import aiohttp  # 惰性导入，避免离线场景下无 aiohttp 也能使用纯解析逻辑
        url = self.snapshot_tpl.format(symbol_upper=symbol.upper())
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Snapshot failed: HTTP {resp.status}")
                return await resp.json()

    def parse_snapshot(self, data: dict) -> tuple[int, list[list]]:
        last_id = data.get("lastUpdateId", 0)
        records = self.standardize_event("snapshot", data)
        return last_id, records

    def parse_message(self, msg: dict
                     ) -> tuple[str | None, str | None, dict | None]:
        if "stream" not in msg:
            return None, None, None
        stream = msg["stream"]
        data = msg["data"]
        symbol = stream.split("@")[0].lower()

        if "depth" in stream:
            return symbol, "depth", data
        if "aggTrade" in stream:
            return symbol, "trade", data
        return symbol, None, data

    def standardize_event(self, event_type: str, data: dict,
                          now_ms: int | None = None) -> list[list]:
        records = []
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        if event_type == "depth":
            ts = data["E"]
            for price, qty in data["b"]:
                records.append([ts, 0, float(price), float(qty)])
            for price, qty in data["a"]:
                records.append([ts, 1, float(price), float(qty)])

        elif event_type == "trade":
            ts = data["E"]
            p = float(data["p"])
            q = float(data["q"])
            if data["m"]:
                q = -q  # buyer is maker -> 主动卖，qty 取负
            records.append([ts, 2, p, q])

        elif event_type == "snapshot":
            ts = now_ms
            for p, q in data.get("bids", []):
                records.append([ts, 3, float(p), float(q)])
            for p, q in data.get("asks", []):
                records.append([ts, 4, float(p), float(q)])

        return records

    # ------------------------------------------------------------------ #
    # U/u 对齐机制
    # ------------------------------------------------------------------ #

    def needs_alignment(self) -> bool:
        return True

    def try_align(self, snapshot_update_id: int, event: dict) -> bool:
        """Binance 规则：第一个满足 U <= lastUpdateId+1 <= u 的事件即对齐点。"""
        return event["U"] <= snapshot_update_id + 1 <= event["u"]

    def get_update_id(self, event: dict) -> int:
        return event["u"]

    # ------------------------------------------------------------------ #
    # 符号规范化
    # ------------------------------------------------------------------ #

    def to_native(self, std_symbol: str) -> str:
        # ETH-USDT -> ethusdt
        return std_symbol.replace("-", "").lower()

    def to_std(self, native_symbol: str) -> str:
        # 简单启发式：常见 quote 后缀切开
        s = native_symbol.upper()
        for quote in ("USDT", "USDC", "BUSD", "BTC", "ETH", "JPY"):
            if s.endswith(quote) and len(s) > len(quote):
                return f"{s[:-len(quote)]}-{quote}"
        return s


class BinanceSpotAdapter(BinanceAdapter):
    def __init__(self, **kwargs):
        super().__init__(market_type="spot", **kwargs)


class BinanceUmFuturesAdapter(BinanceAdapter):
    def __init__(self, **kwargs):
        super().__init__(market_type="um_futures", **kwargs)


class BinanceJpAdapter(BinanceAdapter):
    """
    Binance Japan 适配器（日本 IP 可用）。

    WS 用 data-stream.binance.vision（纯行情端点，不受日本 IP 地理封锁）。
    REST snapshot 用 api.binance.com（从日本 IP 可访问 /api/v3 端点）。
    消息格式与国际版完全一致，复用 BinanceAdapter 的全部解析逻辑。
    """

    name = "binance_jp"

    _ENDPOINTS = {
        "spot": {
            "ws":       "wss://data-stream.binance.vision/stream?streams={streams}",
            "snapshot": "https://api.binance.com/api/v3/depth?symbol={symbol_upper}&limit=1000",
        },
    }

    def __init__(self, **kwargs):
        kwargs.pop("market_type", None)
        super().__init__(market_type="spot", **kwargs)
        defaults = self._ENDPOINTS["spot"]
        self.ws_tpl = kwargs.get("ws_tpl") or defaults["ws"]
        self.snapshot_tpl = kwargs.get("snapshot_tpl") or defaults["snapshot"]
