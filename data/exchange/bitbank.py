"""
bitbank 交易所适配器 (现货,JPY)。

**Phase 1 scaffold** —— WS path 不完整,recorder integration 留给 Phase 2。
当前 adapter 可用于:
  - 单元测试 (parse_message / standardize_event / to_native)
  - REST snapshot fetch (`fetch_orderbook_snapshot`)
  - 配套 `VenueRegistry` 拿费率 (maker -2 bps / taker 12 bps)

不可用于:
  - `python main.py record --config configs/bitbank_recorder.yaml` —— ws_url() 会抛
    NotImplementedError,因为 bitbank 走 socket.io v2 (EIO=3),narci recorder 现在
    只支持裸 `websockets.connect()`。Phase 2 决定怎么接 (扩 ABC vs 加 special case
    vs 分叉 recorder)。

参考:
  - bitbank API docs: https://github.com/bitbankinc/bitbank-api-docs
  - REST public: https://public.bitbank.cc/<pair>/{depth,ticker,transactions}
  - REST private: https://api.bitbank.cc/v1/...
  - WS: https://stream.bitbank.cc (socket.io v2, rooms = `depth_diff_<pair>` /
    `depth_whole_<pair>` / `transactions_<pair>`)
  - echo team 已实现 socket.io 客户端,见 echo/echo/exchange/bitbank.py (SHA 6569402)

侧编码 (narci 标准):
  - depth_diff_<pair> → side 0 (bid) / 1 (ask),qty=0 = 撤单
  - depth_whole_<pair> → side 3 (bid snap) / 4 (ask snap)
  - transactions_<pair> → side 2,qty 符号:
      taker=buy  (买方主动) → seller maker → qty 取负
      taker=sell (卖方主动) → buyer maker  → qty 取正
"""

import time

from .base import ExchangeAdapter


class BitbankAdapter(ExchangeAdapter):
    name = "bitbank"
    market_type = "spot"

    WS_URL = "https://stream.bitbank.cc"          # socket.io upgrades from http(s)
    REST_PUBLIC_BASE = "https://public.bitbank.cc"

    def __init__(self):
        pass

    # ------------------------------------------------------------------ #
    # WebSocket (Phase 2 — 暂不接入 recorder)
    # ------------------------------------------------------------------ #

    def ws_url(self, symbols: list[str], interval_ms: int = 100) -> str:
        raise NotImplementedError(
            "bitbank WS 走 socket.io v2 (EIO=3),narci 当前 l2_recorder.py 只支持裸 "
            "websockets.connect()。Phase 2 决定如何接入 (扩 ExchangeAdapter ABC 加 "
            "custom_stream hook,或 recorder 加 socket.io 特例分支)。"
            "参考 echo/echo/exchange/bitbank.py 的 socketio.AsyncClient 用法。"
        )

    def subscribe_messages(self, symbols: list[str]) -> list[dict]:
        # bitbank 的 "subscribe" 是 socket.io `join-room` emit,不是 JSON 消息;
        # 接入 recorder 时由专门的 socket.io handler 发出。这里返回空,recorder
        # 现行流程不会调用本 adapter。
        return []

    # ------------------------------------------------------------------ #
    # REST snapshot (公开,无需签名)
    # ------------------------------------------------------------------ #

    async def fetch_snapshot(self, symbol: str) -> dict:
        """GET /<pair>/depth → {bids:[[p,q]...], asks:[...], timestamp:int}."""
        import aiohttp
        pair = self.to_native(symbol)
        url = f"{self.REST_PUBLIC_BASE}/{pair}/depth"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"bitbank snapshot failed: HTTP {resp.status}")
                payload = await resp.json()
        # bitbank wraps {success, data:{bids, asks, timestamp}}
        return payload.get("data", payload)

    def parse_snapshot(self, data: dict) -> tuple[int, list[list]]:
        records = self.standardize_event("snapshot", data)
        ts = int(data.get("timestamp") or time.time() * 1000)
        return ts, records

    # ------------------------------------------------------------------ #
    # WS 消息解析 (Phase 2 由 socket.io handler 喂入)
    # ------------------------------------------------------------------ #

    def parse_message(self, msg) -> tuple[str | None, str | None, dict | None]:
        """识别 bitbank socket.io 消息 (room_name + message.data 结构)。

        消息形状 (socket.io 框架剥掉后):
          {"room_name": "depth_diff_btc_jpy",
           "message":   {"data": {"a":[[p,q]...], "b":[...], "t":int}}}
          {"room_name": "depth_whole_btc_jpy",
           "message":   {"data": {"bids":[...], "asks":[...], "timestamp":int}}}
          {"room_name": "transactions_btc_jpy",
           "message":   {"data": {"transactions":[{...}, ...]}}}
        """
        if not isinstance(msg, dict):
            return None, None, None
        room = msg.get("room_name", "")
        data = (msg.get("message") or {}).get("data") or {}
        if not room or not isinstance(data, dict):
            return None, None, None

        # 抽出 pair: depth_diff_btc_jpy → btc_jpy
        for prefix, evt in (
            ("depth_diff_",        "depth"),
            ("depth_whole_",       "snapshot"),
            ("transactions_",      "trade"),
        ):
            if room.startswith(prefix):
                pair = room[len(prefix):]
                return pair, evt, data
        return None, None, None

    def standardize_event(self, event_type: str, data: dict,
                          now_ms: int | None = None) -> list[list]:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        records: list[list] = []

        if event_type == "depth":
            # bitbank depth_diff: data = {"a":[[p,q]...], "b":[...], "t":int_ms}
            ts = int(data.get("t") or now_ms)
            for p, q in data.get("b", []):
                records.append([ts, 0, float(p), float(q)])
            for p, q in data.get("a", []):
                records.append([ts, 1, float(p), float(q)])

        elif event_type == "snapshot":
            # depth_whole 或 REST snapshot: {"bids":[...], "asks":[...], "timestamp":int}
            ts = int(data.get("timestamp") or data.get("t") or now_ms)
            for p, q in data.get("bids", []):
                records.append([ts, 3, float(p), float(q)])
            for p, q in data.get("asks", []):
                records.append([ts, 4, float(p), float(q)])

        elif event_type == "trade":
            # transactions: {"transactions": [{executed_at, price, amount, side, ...}]}
            for tx in data.get("transactions", []):
                try:
                    ts = int(tx.get("executed_at") or now_ms)
                    px = float(tx.get("price"))
                    qty = float(tx.get("amount"))
                    # bitbank "side": taker 方向。narci 约定:
                    #   taker=buy  → seller maker → qty 取负
                    #   taker=sell → buyer maker  → qty 取正
                    if (tx.get("side") or "").lower() == "buy":
                        qty = -qty
                    records.append([ts, 2, px, qty])
                except (KeyError, TypeError, ValueError):
                    continue

        return records

    # ------------------------------------------------------------------ #
    # 对齐 (bitbank 无 update-id;REST snapshot + WS depth_whole 自带定锚)
    # ------------------------------------------------------------------ #

    def needs_alignment(self) -> bool:
        return False

    # ------------------------------------------------------------------ #
    # 符号规范化:BTC_JPY <-> btc_jpy
    # ------------------------------------------------------------------ #

    def to_native(self, std_symbol: str) -> str:
        return std_symbol.replace("-", "_").lower()

    def to_std(self, native_symbol: str) -> str:
        return native_symbol.upper()
