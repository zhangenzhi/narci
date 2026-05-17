"""
GMO Coin 交易所适配器 (現物 + レバレッジ)。

GMO 有两套独立市场:
  - **現物 (spot)**:single-asset symbols `BTC`,`ETH`,`BCH`,`LTC`,`XRP`,
    `XLM`,`BAT`,`ATOM`,`XYM`,`MONA`,`DOT`,`LINK`,`MKR`,`DAI`,`OMG`,`FCR2`
  - **レバレッジ (leverage)**:pair symbols `BTC_JPY`,`ETH_JPY`,`BCH_JPY`,
    `LTC_JPY`,`XRP_JPY`,`DOT_JPY`,`SOL_JPY`。**这是 GMO 主战场**,
    深度/活跃度高于現物;最高 2x 杠杆,JPY 现金结算

WS 行为关键差异 (跟 Binance/CC/bitFlyer 都不同):
  - **orderbooks channel 每条推送都是完整盘口快照** (不是 diff)。
  - 没有 incremental delta 流。

这意味着不能走 ws_url + parse_message + state machine 的标准路径
(state machine 假设 side=0/1 是 incremental update),需要自管 book
状态 reset。所以走 `custom_stream` hook,跟 bitbank 同样的 escape hatch。

WS 协议:plain WebSocket JSON,无 socket.io。
  - URL: wss://api.coin.z.com/ws/public/v1
  - Subscribe: {"command":"subscribe","channel":"orderbooks","symbol":"BTC_JPY"}
              {"command":"subscribe","channel":"trades","symbol":"BTC_JPY","option":"TAKER_ONLY"}

侧编码 (narci 标准):
  - orderbooks 推送 (full snapshot) → side 3 (bid snap) / 4 (ask snap),
    伴随 in-memory book 状态重置
  - trades 推送 → side 2,qty 符号:
      side="BUY"  → taker buy  → seller maker → qty 取负
      side="SELL" → taker sell → buyer  maker → qty 取正

参考 echo `tests/ws_compare_jp.py:148-176` 的 GMO 协议笔记 + 上行 comment
"gmo orderbooks channel pushes full top-N snapshot per update"。
"""

import asyncio
import json
import time
from datetime import datetime, timezone

from .base import ExchangeAdapter


class GmoAdapter(ExchangeAdapter):
    name = "gmo"

    WS_URL = "wss://api.coin.z.com/ws/public/v1"
    REST_ORDERBOOKS = "https://api.coin.z.com/public/v1/orderbooks"

    def __init__(self, market_type: str = "leverage"):
        if market_type not in ("spot", "leverage"):
            raise ValueError(f"gmo market_type 必须是 'spot' 或 'leverage',got {market_type!r}")
        self.market_type = market_type

    # ------------------------------------------------------------------ #
    # WS (ws_url 不会被 recorder 调,custom_stream 接管)
    # ------------------------------------------------------------------ #

    def ws_url(self, symbols: list[str], interval_ms: int = 100) -> str:
        return self.WS_URL          # sentinel,custom_stream 自己用

    def subscribe_messages(self, symbols: list[str]) -> list[dict]:
        return []                   # custom_stream 内部 send

    def uses_custom_stream(self) -> bool:
        return True

    # ------------------------------------------------------------------ #
    # REST snapshot
    # ------------------------------------------------------------------ #

    async def fetch_snapshot(self, symbol: str) -> dict:
        """GET /public/v1/orderbooks?symbol=<sym> → {bids:[{price,size}...], asks:[...]}"""
        import aiohttp
        native = self.to_native(symbol)
        url = f"{self.REST_ORDERBOOKS}?symbol={native}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"gmo snapshot failed: HTTP {resp.status}")
                payload = await resp.json()
        # GMO wraps {status:0, data:{bids:[{price,size}...], asks:[...]}, responsetime:"..."}
        data = payload.get("data", {})
        return {
            "bids": [[float(b["price"]), float(b["size"])] for b in data.get("bids", [])],
            "asks": [[float(a["price"]), float(a["size"])] for a in data.get("asks", [])],
            "timestamp": payload.get("responsetime"),
        }

    def parse_snapshot(self, data: dict) -> tuple[int, list[list]]:
        records = self.standardize_event("snapshot", data)
        return int(time.time() * 1000), records

    # ------------------------------------------------------------------ #
    # 消息解析 / 翻译 (custom_stream 内部用 standardize_event 翻 trade,
    # depth 直接在 custom_stream 内 inline 处理为了同时 reset 状态机)
    # ------------------------------------------------------------------ #

    def parse_message(self, msg) -> tuple[str | None, str | None, dict | None]:
        # custom_stream 不会调用本方法,留实现仅为 ABC 合规 + 单测可达。
        if not isinstance(msg, dict):
            return None, None, None
        ch = msg.get("channel")
        sym = (msg.get("symbol") or "").lower()
        if ch == "orderbooks":
            return sym, "snapshot", msg
        if ch == "trades":
            return sym, "trade", {"trades": [msg]}
        return None, None, None

    def standardize_event(self, event_type: str, data: dict,
                          now_ms: int | None = None) -> list[list]:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        records: list[list] = []

        if event_type == "snapshot":
            ts = self._parse_ts(data.get("timestamp")) or now_ms
            for b in data.get("bids", []):
                if isinstance(b, dict):
                    records.append([ts, 3, float(b["price"]), float(b["size"])])
                else:
                    records.append([ts, 3, float(b[0]), float(b[1])])
            for a in data.get("asks", []):
                if isinstance(a, dict):
                    records.append([ts, 4, float(a["price"]), float(a["size"])])
                else:
                    records.append([ts, 4, float(a[0]), float(a[1])])

        elif event_type == "trade":
            for t in data.get("trades", []):
                try:
                    ts = self._parse_ts(t.get("timestamp")) or now_ms
                    p = float(t["price"])
                    q = float(t["size"])
                    if (t.get("side") or "").upper() == "BUY":
                        q = -q
                    records.append([ts, 2, p, q])
                except (KeyError, TypeError, ValueError):
                    continue

        return records

    # ------------------------------------------------------------------ #
    # custom_stream — plain WS,自管 book reset 因为 GMO 推全量 snapshot
    # ------------------------------------------------------------------ #

    async def custom_stream(self, recorder) -> None:
        """整接管 GMO stream。逻辑:
          1. 外层 while recorder.running 重连 loop
          2. 每轮 REST snapshot init (装 orderbooks 状态机首版)
          3. websockets.connect + send 每个 sym 的 2 个 subscribe
          4. 消息分发:
             - orderbooks: full snapshot → reset book + 写 side=3/4 records
             - trades: 写 side=2 records
          5. Watchdog task — depth/trade 静默阈值跟 record_stream 一致
          6. 保活循环,断连 / shutdown / watchdog 触发 → 外层重连
        """
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("gmo custom_stream 需要 websockets 库") from exc

        while recorder.running:
            watchdog_task: asyncio.Task | None = None
            ws_state = {
                "last_depth_msg_t": 0.0,
                "last_trade_msg_t": 0.0,
                "forced_reconnect_reason": None,
            }
            ws = None
            try:
                # 1. REST snapshot 初始化
                for sym in recorder.symbols:
                    await recorder.init_symbol_snapshot(sym)

                # 2. WS connect + subscribe
                ws = await websockets.connect(self.WS_URL)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔌 gmo WS 已连接")

                for sym in recorder.symbols:
                    native = self.to_native(sym)
                    await ws.send(json.dumps({
                        "command": "subscribe",
                        "channel": "orderbooks",
                        "symbol": native,
                    }))
                    await ws.send(json.dumps({
                        "command": "subscribe",
                        "channel": "trades",
                        "symbol": native,
                        "option": "TAKER_ONLY",
                    }))
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📡 已订阅 "
                          f"orderbooks+trades for {native}")

                # 初始化 watchdog 时间戳
                now = time.time()
                ws_state["last_depth_msg_t"] = now
                ws_state["last_trade_msg_t"] = now

                # 3. watchdog task
                # 注:循环条件只看 recorder.running,不查 ws 状态。websockets v14+
                # 返回的 ClientConnection 没有 .closed 属性 (只有 .state),旧写法
                # `not ws.closed` 会 AttributeError 让 watchdog 静默死亡 → 主 loop
                # 永久挂在 async for raw in ws 上 → 无数据 + 无重连 + 无 log。
                # incident: reco/docs/INCIDENTS/2026-05-17-gmo-watchdog-silent-death.md
                # 整段再包 try/except 作为最后一道防御,任何异常至少有 print。
                async def _watchdog_loop():
                    while recorder.running:
                        try:
                            await asyncio.sleep(recorder.watchdog_check_interval_sec)
                        except asyncio.CancelledError:
                            return
                        try:
                            now = time.time()
                            idle_d = now - ws_state["last_depth_msg_t"]
                            idle_t = now - ws_state["last_trade_msg_t"]
                            reason: str | None = None
                            if idle_d > recorder.depth_stale_threshold_sec:
                                reason = (f"depth 静默 {idle_d:.0f}s > "
                                          f"{recorder.depth_stale_threshold_sec}s")
                            elif idle_t > recorder.trade_stale_threshold_sec:
                                reason = (f"trade 静默 {idle_t:.0f}s > "
                                          f"{recorder.trade_stale_threshold_sec}s")
                            if reason:
                                ws_state["forced_reconnect_reason"] = reason
                                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                                      f"⚠️ gmo {reason},强制重连")
                                try:
                                    await ws.close()
                                except Exception:
                                    pass
                                return
                        except Exception as e:
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                                  f"❌ gmo watchdog 异常: {type(e).__name__}: {e}")
                            return

                watchdog_task = asyncio.create_task(_watchdog_loop())

                # 4. 消息 loop
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    ch = msg.get("channel", "")
                    sym_native = msg.get("symbol", "")
                    # GMO subscribe 啥就推啥 (实测 "BTC" / "BTC_JPY" 全大写,
                    # 跟 recorder.symbols 从 yaml 大写读入一致)。to_std 走 .upper()
                    # 而不是 .lower(),否则会全 mismatch、消息全丢。
                    # incident: reco/docs/INCIDENTS/2026-05-16-gmo-silent-drop.md
                    rec_sym = self.to_std(sym_native)
                    if not ch or rec_sym not in recorder.symbols:
                        continue

                    now = time.time()
                    if ch == "orderbooks":
                        ws_state["last_depth_msg_t"] = now
                        # Full snapshot — reset book + 写 side=3/4 records
                        ts = self._parse_ts(msg.get("timestamp")) or int(now * 1000)
                        book = recorder.orderbooks[rec_sym]
                        book["bids"].clear()
                        book["asks"].clear()
                        records: list[list] = []
                        for b in msg.get("bids", []):
                            p, q = float(b["price"]), float(b["size"])
                            book["bids"][p] = q
                            records.append([ts, 3, p, q])
                        for a in msg.get("asks", []):
                            p, q = float(a["price"]), float(a["size"])
                            book["asks"][p] = q
                            records.append([ts, 4, p, q])
                        recorder.buffers[rec_sym].extend(records)
                    elif ch == "trades":
                        ws_state["last_trade_msg_t"] = now
                        ts = self._parse_ts(msg.get("timestamp")) or int(now * 1000)
                        try:
                            p = float(msg["price"])
                            q = float(msg["size"])
                            if (msg.get("side") or "").upper() == "BUY":
                                q = -q
                            recorder.buffers[rec_sym].append([ts, 2, p, q])
                        except (KeyError, TypeError, ValueError):
                            continue

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                      f"❌ gmo custom_stream 异常: {e},"
                      f"{recorder.retry_wait}s 后重连")
                await asyncio.sleep(recorder.retry_wait)
            finally:
                if watchdog_task is not None and not watchdog_task.done():
                    watchdog_task.cancel()
                    try:
                        await watchdog_task
                    except (asyncio.CancelledError, Exception):
                        pass
                if ws is not None:
                    # close 在 ClientConnection 上幂等,不需要先查状态
                    # (旧版 ws.closed 在 v14+ 没了,见 watchdog 注释)
                    try:
                        await ws.close()
                    except Exception:
                        pass

    # ------------------------------------------------------------------ #
    # 对齐 (无 update-id;snapshot 每条覆盖)
    # ------------------------------------------------------------------ #

    def needs_alignment(self) -> bool:
        return False

    # ------------------------------------------------------------------ #
    # 符号规范化:
    #   spot:     BTC <-> BTC
    #   leverage: BTC_JPY <-> BTC_JPY
    # GMO 自己 API 用什么 narci 就用什么,无 dash/lower 转换。
    # ------------------------------------------------------------------ #

    def to_native(self, std_symbol: str) -> str:
        return std_symbol.upper()

    def to_std(self, native_symbol: str) -> str:
        return native_symbol.upper()

    # ------------------------------------------------------------------ #
    # 工具:GMO timestamp 是 ISO8601 with Z
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_ts(s: str | None) -> int:
        """GMO timestamp e.g. '2026-05-15T03:30:00.123Z' → ms epoch。失败返 0。"""
        if not s:
            return 0
        try:
            # Python 3.11+ supports Z suffix in fromisoformat;older needs strip
            s2 = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s2)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except (ValueError, AttributeError):
            return 0
