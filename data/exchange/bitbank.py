"""
bitbank 交易所适配器 (现货,JPY)。

WS transport 走 **socket.io v2 (EIO=3)**,不同于 Binance/CC 的裸 WebSocket。
通过 `ExchangeAdapter.uses_custom_stream()` + `custom_stream(recorder)` hook
接入 narci L2Recorder。

参考:
  - bitbank API docs: https://github.com/bitbankinc/bitbank-api-docs
  - REST public: https://public.bitbank.cc/<pair>/{depth,ticker,transactions}
  - REST private: https://api.bitbank.cc/v1/...
  - WS: https://stream.bitbank.cc (socket.io v2, rooms = `depth_diff_<pair>` /
    `depth_whole_<pair>` / `transactions_<pair>`)
  - echo team 同协议实现见 echo/echo/exchange/bitbank.py (SHA 6569402),
    不过 echo 走 yield-based async generator,这里直接喂 narci recorder buffer。

侧编码 (narci 标准):
  - depth_diff_<pair> → side 0 (bid) / 1 (ask),qty=0 = 撤单
  - depth_whole_<pair> → side 3 (bid snap) / 4 (ask snap)
  - transactions_<pair> → side 2,qty 符号:
      taker=buy  (买方主动) → seller maker → qty 取负
      taker=sell (卖方主动) → buyer maker  → qty 取正
"""

import asyncio
import time
from datetime import datetime

from .base import ExchangeAdapter


class BitbankAdapter(ExchangeAdapter):
    name = "bitbank"
    market_type = "spot"

    WS_URL = "https://stream.bitbank.cc"          # socket.io upgrades from http(s)
    REST_PUBLIC_BASE = "https://public.bitbank.cc"

    def __init__(self):
        pass

    # ------------------------------------------------------------------ #
    # WebSocket — bitbank 走 socket.io,通过 custom_stream hook 接入。
    # ws_url() 这条传统路径不会被 recorder 调用,留 sentinel 仅为 ABC 合规。
    # ------------------------------------------------------------------ #

    def ws_url(self, symbols: list[str], interval_ms: int = 100) -> str:
        # 不会被 recorder 调用 (uses_custom_stream=True 走另一条路径),仅为
        # ABC abstract method 合规。手测 / 文档引用看这里。
        return self.WS_URL

    def subscribe_messages(self, symbols: list[str]) -> list[dict]:
        # bitbank 的 "subscribe" 是 socket.io `join-room` emit,不是 JSON 消息。
        # custom_stream 内部用 sio.emit('join-room', ...) 发送,所以这里返回空。
        return []

    def uses_custom_stream(self) -> bool:
        return True

    async def custom_stream(self, recorder) -> None:
        """通过 socket.io v2 接管 bitbank stream 生命周期。

        逻辑:
          1. 外层 while recorder.running 重连 loop
          2. 每轮:为每个 symbol 调 recorder.init_symbol_snapshot (REST 拉
             初始 book,落 side=3/4 records 进 buffer,装 orderbooks 状态机)
          3. socket.io connect + 每个 symbol join 3 rooms
          4. on_message → parse_message → 按 event_type 分发到 recorder
             已有的 _handle_depth / buffer.extend 路径
          5. 保活循环,收到 disconnect 或 recorder.running=False 退出

        watchdog (depth_stale_threshold) 暂不实现 —— socket.io 自带 heartbeat
        + 自动重连可以处理传输层死亡。bitbank 真出现"channel 静默而连接活"的
        case (类似 CC 0508 事件) 再补 Phase 3。
        """
        try:
            import socketio  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "bitbank custom_stream 需要 python-socketio。"
                "请 pip install 'python-socketio[asyncio_client]>=5.0'"
            ) from exc

        while recorder.running:
            sio: socketio.AsyncClient | None = None
            try:
                # 1. REST snapshot — 每个 symbol 一次,装初始 book + 写
                # side=3/4 records 进 buffer。recorder 现有方法直接复用。
                for sym in recorder.symbols:
                    await recorder.init_symbol_snapshot(sym)

                # 2. socket.io client (reconnection=True 让库自管小故障重连)
                sio = socketio.AsyncClient(
                    reconnection=True,
                    reconnection_attempts=0,        # forever
                    reconnection_delay=1,
                    reconnection_delay_max=30,
                    logger=False,
                    engineio_logger=False,
                )

                # 3. join rooms on connect — 库 reconnect 时这个 handler 会再触发
                @sio.event
                async def connect():
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                          f"🔌 bitbank socket.io 已连接")
                    for sym in recorder.symbols:
                        pair = self.to_native(sym)
                        for room in (f"depth_whole_{pair}",
                                     f"depth_diff_{pair}",
                                     f"transactions_{pair}"):
                            await sio.emit("join-room", room)
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                                  f"📡 已订阅 {room}")

                @sio.event
                async def disconnect():
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                          f"⚠️ bitbank socket.io 断连,等待自动重连")

                # 4. 消息分发
                @sio.on("message")
                async def on_message(payload):
                    pair, event_type, data = self.parse_message(payload)
                    if not pair or not event_type:
                        return
                    if pair not in recorder.symbols:
                        return

                    if event_type == "depth":
                        await recorder._handle_depth(pair, data)
                    elif event_type == "trade":
                        records = self.standardize_event("trade", data)
                        recorder.buffers[pair].extend(records)
                    elif event_type == "snapshot":
                        # depth_whole 推送 → side=3/4 records 直接落 buffer,
                        # 不重置 orderbooks 状态机 (depth_diff 继续在原 book 上 apply)
                        records = self.standardize_event("snapshot", data)
                        recorder.buffers[pair].extend(records)

                # 5. 建立连接
                await sio.connect(
                    self.WS_URL,
                    transports=["websocket"],
                    socketio_path="/socket.io",
                )

                # 6. 保活循环 — socket.io 在后台处理 callback,这里只 sleep
                # 等 recorder shutdown 或 sio 永久断连
                while recorder.running and sio.connected:
                    await asyncio.sleep(1)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                      f"❌ bitbank custom_stream 异常: {e},"
                      f"{recorder.retry_wait}s 后重连")
                await asyncio.sleep(recorder.retry_wait)
            finally:
                if sio is not None and sio.connected:
                    try:
                        await sio.disconnect()
                    except Exception:
                        pass

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
