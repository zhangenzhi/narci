"""
Coincheck 交易所适配器（现货）。

参考：
  - REST base: https://coincheck.com
  - WebSocket public: wss://ws-api.coincheck.com
  - Channels: "{pair}-trades", "{pair}-orderbook"
  - Rate limit: 新单 4 req/s, 订单查询 1 req/s（全局）

⚠️ 待实测 orderbook channel 的实际负载格式（增量 / 全量）。
    订阅方式为 client 发送:
      {"type":"subscribe","channel":"btc_jpy-orderbook"}
    服务端推送形如:
      ["btc_jpy", {"bids": [["price","qty"], ...], "asks": [...], "last_update_at": "..."}]

  - 若每条推送是【完整盘口】 → side=3/4 全量
  - 若是【增量】 → side=0/1（qty=0 撤单）
  本实现先按「增量」处理（Coincheck 文档称 "orderbook diff"），
  首次收到时附加一条全量快照（side=3/4）供重构器定位。
"""

import time

from .base import ExchangeAdapter


class CoincheckAdapter(ExchangeAdapter):
    name = "coincheck"
    market_type = "spot"

    WS_URL = "wss://ws-api.coincheck.com"
    REST_ORDERBOOK = "https://coincheck.com/api/order_books"

    def __init__(self):
        # Coincheck 单个 WS 连接需订阅多个 channel，消息流混合
        # 订阅命令在连接后由 recorder 发送（见 record_stream）
        self._seen_snapshot: set[str] = set()

    # ------------------------------------------------------------------ #
    # WebSocket（Coincheck 需要连接后主动 subscribe，URL 本身无 stream 参数）
    # ------------------------------------------------------------------ #

    def ws_url(self, symbols: list[str], interval_ms: int = 100) -> str:
        return self.WS_URL

    def subscribe_messages(self, symbols: list[str]) -> list[dict]:
        """recorder 连接成功后需发送的订阅消息列表。"""
        msgs = []
        for s in symbols:
            pair = self.to_native(s)
            msgs.append({"type": "subscribe", "channel": f"{pair}-trades"})
            msgs.append({"type": "subscribe", "channel": f"{pair}-orderbook"})
        return msgs

    async def fetch_snapshot(self, symbol: str) -> dict:
        """通过 REST 拉取全量盘口 (GET /api/order_books?pair=eth_jpy)。"""
        import aiohttp
        pair = self.to_native(symbol)
        url = f"{self.REST_ORDERBOOK}?pair={pair}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Coincheck snapshot failed: HTTP {resp.status}")
                return await resp.json()

    def parse_snapshot(self, data: dict) -> tuple[int, list[list]]:
        records = self.standardize_event("snapshot", data)
        # Coincheck 无 lastUpdateId；用时间戳近似
        return int(time.time() * 1000), records

    # ------------------------------------------------------------------ #
    # 消息解析
    # ------------------------------------------------------------------ #

    def parse_message(self, msg) -> tuple[str | None, str | None, dict | None]:
        """
        Coincheck 推送格式（实测 2026-04）:
          Orderbook:  [pair_str, {"bids":[...], "asks":[...], "last_update_at":"..."}]
          Trades:     [[ts_s, trade_id, pair, rate, amount, order_type, fill_id, fill_id, null], ...]
                      (list-of-lists; 同一消息内的多笔成交都是同一 pair)
        """
        if not isinstance(msg, list) or not msg:
            return None, None, None

        first = msg[0]

        # Orderbook: first is pair string, second is dict with 'bids'
        if isinstance(first, str) and len(msg) >= 2 \
                and isinstance(msg[1], dict) and "bids" in msg[1]:
            return first, "depth", msg[1]

        # Trades: first is itself a list, field [2] is pair
        if isinstance(first, list) and len(first) >= 6:
            pair = str(first[2]).lower()
            return pair, "trade", {"trades": msg}

        return None, None, None

    def standardize_event(self, event_type: str, data: dict,
                          now_ms: int | None = None) -> list[list]:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        records = []

        if event_type == "depth":
            # 增量：qty=0 撤单
            for p, q in data.get("bids", []):
                records.append([now_ms, 0, float(p), float(q)])
            for p, q in data.get("asks", []):
                records.append([now_ms, 1, float(p), float(q)])

        elif event_type == "trade":
            # Coincheck trades 批量打包，data = {"trades": [[ts_s, id, pair, rate, amount, type, ...], ...]}
            for t in data.get("trades", []):
                try:
                    # t[0] = unix seconds string；转 ms
                    ts_ms = int(float(t[0]) * 1000) if t[0] else now_ms
                    rate = float(t[3])
                    amount = float(t[4])
                    # order_type: "buy" = 主动买 (taker buy), "sell" = 主动卖 (seller maker → qty 负)
                    if t[5] == "sell":
                        amount = -amount
                    records.append([ts_ms, 2, rate, amount])
                except (IndexError, TypeError, ValueError):
                    continue

        elif event_type == "snapshot":
            for p, q in data.get("bids", []):
                records.append([now_ms, 3, float(p), float(q)])
            for p, q in data.get("asks", []):
                records.append([now_ms, 4, float(p), float(q)])

        return records

    # ------------------------------------------------------------------ #
    # 对齐（Coincheck 无 U/u 机制，REST 快照 + 紧接 WS 增量即可）
    # ------------------------------------------------------------------ #

    def needs_alignment(self) -> bool:
        return False

    # ------------------------------------------------------------------ #
    # 符号规范化：ETH-JPY <-> eth_jpy
    # ------------------------------------------------------------------ #

    def to_native(self, std_symbol: str) -> str:
        return std_symbol.replace("-", "_").lower()

    def to_std(self, native_symbol: str) -> str:
        return native_symbol.upper().replace("_", "-")
