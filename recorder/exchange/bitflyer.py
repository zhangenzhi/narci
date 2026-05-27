"""
bitFlyer 交易所适配器 (现货 + Lightning FX 永续)。

bitFlyer 有两套独立市场,各自独立盘口:
  - **Spot**:`BTC_JPY` / `ETH_JPY` / `XRP_JPY` / `ETH_BTC` / `BCH_JPY` 等
  - **Lightning FX**:`FX_BTC_JPY` 永续合约 (日本最大 BTC 永续之一,有日内
    SWAP 利息类似 funding rate)。**只有 BTC**,没有其它 alt FX。

通过 `market_type` 参数区分:
  - `market_type='spot'`     → channel `lightning_board_<sym>`
  - `market_type='fx'`       → channel `lightning_board_FX_BTC_JPY` 等
两种 market 下 channel 命名格式一致,只是 symbol prefix 不同 (`BTC_JPY` vs
`FX_BTC_JPY`),所以一个 adapter 处理两种,recorder save_dir 自动拆到
`bitflyer/spot/l2/` vs `bitflyer/fx/l2/`。

WS 协议:JSON-RPC over plain WebSocket (`wss://ws.lightstream.bitflyer.com/json-rpc`)。
不需要 socket.io,走 narci 现有的 ws_url + parse_message 标准路径。

侧编码 (narci 标准):
  - lightning_board_<sym> depth delta → side 0 (bid) / 1 (ask),qty=0 = 撤单
  - REST /v1/board snapshot → side 3 (bid snap) / 4 (ask snap)
  - lightning_executions_<sym> → side 2,qty 符号:
      side="BUY"  → taker buy  → seller maker → qty 取负
      side="SELL" → taker sell → buyer  maker → qty 取正

参考 echo team `tests/ws_compare_jp.py:109-117` 的 bitFlyer 协议笔记。
"""

import time

from .base import ExchangeAdapter


class BitflyerAdapter(ExchangeAdapter):
    name = "bitflyer"
    # market_type 在 __init__ 里设,不是 class attr (默认 spot)

    WS_URL = "wss://ws.lightstream.bitflyer.com/json-rpc"
    REST_BOARD = "https://api.bitflyer.com/v1/board"

    def __init__(self, market_type: str = "spot"):
        if market_type not in ("spot", "fx"):
            raise ValueError(f"bitflyer market_type 必须是 'spot' 或 'fx',got {market_type!r}")
        self.market_type = market_type

    # ------------------------------------------------------------------ #
    # WebSocket — JSON-RPC subscribe
    # ------------------------------------------------------------------ #

    def ws_url(self, symbols: list[str], interval_ms: int = 100) -> str:
        return self.WS_URL

    def subscribe_messages(self, symbols: list[str]) -> list[dict]:
        """每个 symbol 订阅 2 channel:board (depth deltas) + executions (trades)。

        snapshot channel `lightning_board_snapshot_*` 不订阅,首批 snapshot 走
        REST /v1/board (跟 Binance/CC 的模式一致)。
        """
        msgs = []
        for i, s in enumerate(symbols):
            native = self.to_native(s)
            msgs.append({
                "method": "subscribe",
                "params": {"channel": f"lightning_board_{native}"},
                "id": 2 * i + 1,
            })
            msgs.append({
                "method": "subscribe",
                "params": {"channel": f"lightning_executions_{native}"},
                "id": 2 * i + 2,
            })
        return msgs

    async def fetch_snapshot(self, symbol: str) -> dict:
        """GET /v1/board?product_code=<sym> → {mid_price, bids:[{price,size}...], asks:[...]}"""
        import aiohttp
        native = self.to_native(symbol)
        url = f"{self.REST_BOARD}?product_code={native}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"bitflyer snapshot failed: HTTP {resp.status}")
                data = await resp.json()
        # bitFlyer 返回 {mid_price, bids:[{price,size}...], asks:[...]} —— 跟
        # WS payload 字段一致;但 l2_recorder.init_symbol_snapshot 要求 fetch
        # 返回的 dict 里 bids/asks 是 [[p,q]...] 形式 (list-of-list,跟 CC/bitbank
        # 一致),所以这里展开成那个形状。
        return {
            "bids": [[float(b["price"]), float(b["size"])] for b in data.get("bids", [])],
            "asks": [[float(a["price"]), float(a["size"])] for a in data.get("asks", [])],
            "mid_price": data.get("mid_price"),
        }

    def parse_snapshot(self, data: dict) -> tuple[int, list[list]]:
        records = self.standardize_event("snapshot", data)
        return int(time.time() * 1000), records

    # ------------------------------------------------------------------ #
    # WS 消息解析
    # ------------------------------------------------------------------ #

    def parse_message(self, msg) -> tuple[str | None, str | None, dict | None]:
        """识别 bitFlyer JSON-RPC channelMessage。

        消息形状:
          {"jsonrpc":"2.0","method":"channelMessage",
           "params":{"channel":"lightning_board_BTC_JPY","message":{...}}}
          {"jsonrpc":"2.0","method":"channelMessage",
           "params":{"channel":"lightning_executions_BTC_JPY","message":[{...},...]}}
        """
        if not isinstance(msg, dict):
            return None, None, None
        if msg.get("method") != "channelMessage":
            return None, None, None
        params = msg.get("params") or {}
        ch = params.get("channel", "")
        data = params.get("message")
        if not ch or data is None:
            return None, None, None

        # 抽出 symbol:lightning_board_BTC_JPY → BTC_JPY
        #                lightning_board_FX_BTC_JPY → FX_BTC_JPY
        for prefix, evt in (
            ("lightning_board_snapshot_", "snapshot"),
            ("lightning_board_",          "depth"),
            ("lightning_executions_",     "trade"),
        ):
            if ch.startswith(prefix):
                sym_native = ch[len(prefix):]
                # 把 list (trades) 包装成 {"trades": [...]} 跟 CC adapter 一致
                if evt == "trade" and isinstance(data, list):
                    return sym_native.lower(), evt, {"trades": data}
                if isinstance(data, dict):
                    return sym_native.lower(), evt, data
        return None, None, None

    def standardize_event(self, event_type: str, data: dict,
                          now_ms: int | None = None) -> list[list]:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        records: list[list] = []

        if event_type == "depth":
            # lightning_board: {"mid_price":..., "bids":[{"price","size"}...], "asks":[...]}
            for b in data.get("bids", []):
                records.append([now_ms, 0, float(b["price"]), float(b["size"])])
            for a in data.get("asks", []):
                records.append([now_ms, 1, float(a["price"]), float(a["size"])])

        elif event_type == "snapshot":
            # REST /v1/board 已经被 fetch_snapshot 展平成 [[p,q]...] 形状;
            # WS lightning_board_snapshot_ 推送是原始 {price,size} 形状。
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            for b in bids:
                if isinstance(b, dict):
                    records.append([now_ms, 3, float(b["price"]), float(b["size"])])
                else:  # [p, q]
                    records.append([now_ms, 3, float(b[0]), float(b[1])])
            for a in asks:
                if isinstance(a, dict):
                    records.append([now_ms, 4, float(a["price"]), float(a["size"])])
                else:
                    records.append([now_ms, 4, float(a[0]), float(a[1])])

        elif event_type == "trade":
            for t in data.get("trades", []):
                try:
                    # exec_date 是 ISO8601 "2026-05-15T03:30:00.123";解析成 ms
                    ed = t.get("exec_date") or ""
                    if ed:
                        # ms 精度 — bitFlyer 给到 3 位小数
                        from datetime import datetime, timezone
                        # bitFlyer 返回的时间不带 timezone,统一按 UTC 解析
                        dt = datetime.fromisoformat(ed.rstrip("Z").replace("Z", ""))
                        ts = int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
                    else:
                        ts = now_ms
                    px = float(t["price"])
                    qty = float(t["size"])
                    # side: "BUY"=taker buy → seller maker → qty 取负
                    if (t.get("side") or "").upper() == "BUY":
                        qty = -qty
                    records.append([ts, 2, px, qty])
                except (KeyError, TypeError, ValueError):
                    continue

        return records

    # ------------------------------------------------------------------ #
    # 无 update-id 对齐 (REST snapshot + WS delta,跟 CC 一样)
    # ------------------------------------------------------------------ #

    def needs_alignment(self) -> bool:
        return False

    # ------------------------------------------------------------------ #
    # 符号规范化:
    #   spot: BTC-JPY <-> BTC_JPY
    #   fx:   FX-BTC-JPY <-> FX_BTC_JPY
    # bitFlyer 自己 API 用 underscore,narci 标准用 dash。
    # ------------------------------------------------------------------ #

    def to_native(self, std_symbol: str) -> str:
        return std_symbol.replace("-", "_").upper()

    def to_std(self, native_symbol: str) -> str:
        return native_symbol.upper().replace("_", "-")
