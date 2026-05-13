"""
通用 L2 盘口录制器（交易所无关）。

依赖 data.exchange.ExchangeAdapter 完成所有交易所特有逻辑：
  - WebSocket URL 构造
  - REST 快照拉取
  - 原生消息 -> Narci 4 列格式的翻译
  - 盘口对齐（如 Binance 的 U/u 机制）

录制器本身只负责：
  - WS 连接与重连
  - 缓冲管理
  - 原子快照注入（使每个落盘文件自包含）
  - 定时落盘 + 退役清理

落盘目录：{save_dir}/{exchange}/{market_type}/l2/
文件名：  {SYMBOL}_RAW_{YYYYMMDD}_{HHMMSS}.parquet
"""

import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime

import pandas as pd
import yaml

from data.exchange import get_adapter, ExchangeAdapter


class L2Recorder:
    def __init__(self, config_path: str = "configs/um_future_recorder.yaml",
                 symbol: str | None = None,
                 adapter: ExchangeAdapter | None = None):
        self.config = self._load_config(config_path)

        # 1. 加载适配器
        if adapter is None:
            exchange_name = self.config.get("exchange", "binance")
            market_type = self.config.get("market_type", "um_futures")
            endpoints = self.config.get("endpoints", {}).get(market_type, {})
            adapter = get_adapter(
                exchange_name,
                market_type=market_type,
                ws_tpl=endpoints.get("ws_url"),
                snapshot_tpl=endpoints.get("snapshot_url"),
            )
        self.adapter = adapter

        # 2. 交易对：优先命令行 > 配置
        if symbol:
            raw_syms = [symbol]
        else:
            cfg_syms = self.config.get("symbols", ["ETHUSDT"])
            raw_syms = cfg_syms if isinstance(cfg_syms, list) else [cfg_syms]
        self.symbols = [s.lower() for s in raw_syms]

        # 3. 基本配置
        self.interval = self.config.get("interval_ms", 100)
        self.save_interval = self.config.get("save_interval_sec", 60)
        self.retry_wait = self.config.get("retry", {}).get("wait_seconds", 5)
        self.retain_days = self.config.get("retain_days", 0)
        # P1-A fix 2026-05-08: opt-in REST snapshot refresh in save_loop.
        # When True, save_loop fetches a fresh REST snapshot before
        # writing each periodic snapshot batch, evicting accumulated dust
        # from the in-memory book at the source. Default False preserves
        # legacy behavior. See data/l2_recorder.py save_loop docstring.
        self.snapshot_refresh_on_save = bool(
            self.config.get("snapshot_refresh_on_save", False)
        )
        # 2026-05-13: per-WS channel-staleness watchdog. Catches silent
        # channel deaths (the 05-08 CC orderbook channel hung for 15h
        # while the trade channel kept pushing). On any WS, if no event
        # of the expected type arrives within the threshold, force-close
        # the connection so the outer retry loop reconnects + re-aligns.
        # check_interval is how often we wake up to test staleness; the
        # threshold is the actual budget.
        self.watchdog_check_interval_sec = int(
            self.config.get("watchdog_check_interval_sec", 5)
        )
        self.depth_stale_threshold_sec = int(
            self.config.get("depth_stale_threshold_sec", 180)
        )
        self.trade_stale_threshold_sec = int(
            self.config.get("trade_stale_threshold_sec", 1800)
        )

        # 4. 落盘路径：{save_dir}/{exchange}/{market_type}/l2/
        base_dir = self.config.get("save_dir", "./replay_buffer/realtime")
        self.save_dir = os.path.join(
            base_dir, self.adapter.name, self.adapter.market_type, "l2"
        )
        os.makedirs(self.save_dir, exist_ok=True)

        # 5. 初始化状态机
        self.orderbooks = {s: {"bids": {}, "asks": {}} for s in self.symbols}
        self.buffers = {s: [] for s in self.symbols}
        self.pre_align_buffer = {s: [] for s in self.symbols}
        self.last_update_ids = {s: 0 for s in self.symbols}
        self.is_initialized = {s: False for s in self.symbols}
        self.stream_aligned = {s: False for s in self.symbols}
        self.running = True

        self.ws_urls = self.adapter.ws_urls(self.symbols, self.interval)

        print(f"🔧 配置完成 | 交易所: {self.adapter.name} | 市场: {self.adapter.market_type}")
        print(f"🔧 交易对: {[s.upper() for s in self.symbols]}")
        print(f"📂 数据目录: {self.save_dir}")
        if self.retain_days > 0:
            print(f"🗑️ 自动清理已启用: 保留最近 {self.retain_days} 天")

    @staticmethod
    def _load_config(path: str) -> dict:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f).get("recorder", {})

    # ------------------------------------------------------------------ #
    # 快照初始化
    # ------------------------------------------------------------------ #

    async def init_symbol_snapshot(self, sym: str):
        try:
            data = await self.adapter.fetch_snapshot(sym)
            last_id, records = self.adapter.parse_snapshot(data)

            self.orderbooks[sym] = {"bids": {}, "asks": {}}
            for p, q in data.get("bids", []):
                self.orderbooks[sym]["bids"][float(p)] = float(q)
            for p, q in data.get("asks", []):
                self.orderbooks[sym]["asks"][float(p)] = float(q)

            self.buffers[sym].extend(records)
            self.last_update_ids[sym] = last_id
            self.is_initialized[sym] = True
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 📸 {sym.upper()} 初始快照已就绪, ID: {last_id}")
        except Exception as e:
            print(f"❌ {sym.upper()} 初始化快照失败: {e}")

    # ------------------------------------------------------------------ #
    # 事件处理
    # ------------------------------------------------------------------ #

    def _process_depth_event(self, sym: str, data: dict):
        # 仅当交易所有 update_id 机制时才做去重（Binance U/u）
        if self.adapter.needs_alignment():
            u = self.adapter.get_update_id(data)
            if u <= self.last_update_ids[sym]:
                return
            self.last_update_ids[sym] = u

        records = self.adapter.standardize_event("depth", data)
        self.buffers[sym].extend(records)

        # 更新内存状态机（0=bid, 1=ask）
        for rec in records:
            _, side, price, qty = rec
            book = self.orderbooks[sym]["bids" if side == 0 else "asks"]
            if qty == 0:
                book.pop(price, None)
            else:
                book[price] = qty

    async def record_stream(self, url: str):
        import websockets  # 惰性导入
        # 多 WS 时，只有 depth 流的重连需要重置 orderbook 状态机 + 重新拉 snapshot；
        # 单纯 trade 流（如 Binance UM /market）的重连不能动 depth alignment 状态。
        # 判定规则：含 @aggTrade 且不含 @depth 的 URL 是「trade-only」（只 UM
        # /market 这一种）；其他都视为 depth-bearing（含 Coincheck 这类没有
        # URL 流参数、靠 subscribe message 订阅 orderbook 的 case）。
        trade_only = "@aggTrade" in url and "@depth" not in url
        depth_only = "@depth" in url and "@aggTrade" not in url
        carries_depth = not trade_only
        # Watchdog expectations: which event types should this WS deliver?
        # Used to enable per-type staleness checks (catches silent channel
        # death — e.g. CC 2026-05-08 orderbook channel hung 15h while
        # trade channel kept pushing).
        expects_depth = not trade_only
        expects_trade = not depth_only
        label = url.split("?")[0].rsplit("/", 2)[-2]  # 'public' / 'market' / 'stream'
        while self.running:
            try:
                async with websockets.connect(url) as ws:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔌 WS 已连接 [{label}]")

                    if carries_depth:
                        for sym in self.symbols:
                            self.is_initialized[sym] = False
                            self.stream_aligned[sym] = False
                            self.pre_align_buffer[sym] = []
                            self.orderbooks[sym] = {"bids": {}, "asks": {}}

                    # 发送订阅消息（Coincheck 等需要主动订阅的交易所）
                    for sub in self.adapter.subscribe_messages(self.symbols):
                        await ws.send(json.dumps(sub))
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] 📡 已订阅 {sub}")

                    if carries_depth:
                        for sym in self.symbols:
                            asyncio.create_task(self.init_symbol_snapshot(sym))

                    # Watchdog state — reset on every (re)connect.
                    last_depth_msg_t = time.time()
                    last_trade_msg_t = time.time()
                    forced_reconnect_reason: str | None = None

                    while self.running:
                        try:
                            message = await asyncio.wait_for(
                                ws.recv(),
                                timeout=self.watchdog_check_interval_sec,
                            )
                        except asyncio.TimeoutError:
                            message = None

                        now = time.time()

                        # Per-channel staleness check. Break to outer retry
                        # loop on stale; the websockets `async with` will
                        # cleanly close before reconnect.
                        if expects_depth:
                            idle = now - last_depth_msg_t
                            if idle > self.depth_stale_threshold_sec:
                                forced_reconnect_reason = (
                                    f"depth 静默 {idle:.0f}s > "
                                    f"{self.depth_stale_threshold_sec}s"
                                )
                                break
                        if expects_trade:
                            idle = now - last_trade_msg_t
                            if idle > self.trade_stale_threshold_sec:
                                forced_reconnect_reason = (
                                    f"trade 静默 {idle:.0f}s > "
                                    f"{self.trade_stale_threshold_sec}s"
                                )
                                break

                        if message is None:
                            continue

                        msg = json.loads(message)
                        sym, event_type, data = self.adapter.parse_message(msg)

                        if sym is None or event_type is None:
                            continue
                        if sym not in self.symbols:
                            continue

                        if event_type == "depth":
                            last_depth_msg_t = now
                            await self._handle_depth(sym, data)
                        elif event_type == "trade":
                            last_trade_msg_t = now
                            records = self.adapter.standardize_event("trade", data)
                            self.buffers[sym].extend(records)

                    if forced_reconnect_reason:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                              f"⚠️ WS [{label}] {forced_reconnect_reason}，强制重连")

            except Exception as e:
                print(f"❌ WS [{label}] 异常: {e}，{self.retry_wait}s 后重试...")
                await asyncio.sleep(self.retry_wait)

    async def _handle_depth(self, sym: str, data: dict):
        # 未拉到快照 -> 先缓冲
        if not self.is_initialized[sym]:
            self.pre_align_buffer[sym].append(data)
            if len(self.pre_align_buffer[sym]) > 2000:
                self.pre_align_buffer[sym].pop(0)
            return

        # 需要对齐的交易所（如 Binance）走 U/u 对齐
        if self.adapter.needs_alignment() and not self.stream_aligned[sym]:
            combined = self.pre_align_buffer[sym] + [data]
            for d in combined:
                if self.adapter.try_align(self.last_update_ids[sym], d):
                    self._process_depth_event(sym, d)
                    self.stream_aligned[sym] = True
                    self.pre_align_buffer[sym] = []
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ {sym.upper()} 深度流对齐成功")
                    return

            # 对齐失败：判断 stream 是否已经走过 snapshot 的对齐窗口。
            # 检查 NEWEST 事件 (combined[-1].u) 而不是 oldest，因为
            # P1-A REST 重拉之后 pre_align_buffer 可能含 await 期间累积的
            # 旧事件 (u < snapshot.lastUpdateId+1)；用 combined[0].u 检查
            # 永远是 stale-event 的 u，re-snapshot trigger 永远不 fire，
            # symbol 卡死在 stream_aligned=False 直到 WS 重连。
            # 用 NEWEST 才能正确识别「stream 已 overshoot snapshot」的情况。
            if combined:
                newest_u = self.adapter.get_update_id(combined[-1])
                if newest_u > self.last_update_ids[sym] + 1:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ {sym.upper()} 流偏移过大 (newest u={newest_u}, snapshot last_id+1={self.last_update_ids[sym]+1})，重新拉快照")
                    self.is_initialized[sym] = False
                    self.stream_aligned[sym] = False
                    self.pre_align_buffer[sym] = []
                    asyncio.create_task(self.init_symbol_snapshot(sym))
            return

        # 不需要对齐 或 已对齐 -> 直接处理
        if not self.adapter.needs_alignment():
            self.stream_aligned[sym] = True
        self._process_depth_event(sym, data)

    # ------------------------------------------------------------------ #
    # 落盘
    # ------------------------------------------------------------------ #

    async def save_loop(self):
        """Periodic disk flush + (optional) REST snapshot refresh.

        Each save_interval:
          1. Drain the in-flight diff buffer for each symbol.
          2. If snapshot_refresh_on_save=True: REST-fetch a fresh order
             book and atomically install it as the new in-memory book.
             Discards dust accumulated when WS delete events were missed.
             Re-aligns the WS stream the same way a reconnect would —
             setting is_initialized=False during the await means concurrent
             depth events buffer in pre_align_buffer instead of mutating
             the about-to-be-replaced book.
          3. Inject a side=3/4 snapshot batch (from the now-fresh book) at
             the head of the next cycle's buffer so each parquet file is
             self-contained.
          4. Write the drained buffer to disk.

        REST refresh failure is non-fatal: log + fall through to dump the
        in-memory book (legacy behavior). One operator-facing failure mode
        is rate limiting; with 600s interval × ~6 symbols × ~50ms per call,
        Binance UM 2400/min limit has 4 orders of magnitude headroom.
        """
        while self.running:
            await asyncio.sleep(self.save_interval)
            saved = []

            for sym in self.symbols:
                if not self.buffers[sym] or not self.stream_aligned[sym]:
                    continue

                # Step 1: atomic buffer drain
                data = self.buffers[sym]
                self.buffers[sym] = []

                # Step 2: optional REST refresh of the in-memory book.
                if self.snapshot_refresh_on_save:
                    await self._refresh_book_via_rest(sym)

                # Step 3: inject side=3/4 snapshot batch from current book.
                now_ms = int(time.time() * 1000)
                snapshot_records = []
                for p, q in self.orderbooks[sym]["bids"].items():
                    snapshot_records.append([now_ms, 3, float(p), float(q)])
                for p, q in self.orderbooks[sym]["asks"].items():
                    snapshot_records.append([now_ms, 4, float(p), float(q)])
                self.buffers[sym].extend(snapshot_records)

                try:
                    df = pd.DataFrame(data, columns=["timestamp", "side", "price", "quantity"])
                    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                    fname = f"{sym.upper()}_RAW_{ts_str}.parquet"
                    path = os.path.join(self.save_dir, fname)
                    await asyncio.to_thread(
                        df.to_parquet, path, engine="pyarrow",
                        compression="snappy", index=False,
                    )
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📥 {sym.upper()} 原始数据固化 ({len(df)} 行)")
                    saved.append(path)
                except Exception as e:
                    print(f"❌ {sym.upper()} 写盘失败: {e}")

            if saved and self.retain_days > 0:
                await asyncio.to_thread(self._cleanup_old_files)

    async def _refresh_book_via_rest(self, sym: str) -> None:
        """REST-refresh a single symbol's in-memory book.

        Mirrors the WS reconnect protocol so depth events arriving
        during the REST round-trip don't get lost or trample the
        about-to-be-replaced book:

        1. Set is_initialized=False, stream_aligned=False, clear
           pre_align_buffer. From this point, concurrent _handle_depth
           callbacks buffer events in pre_align_buffer (see
           _handle_depth's `not self.is_initialized[sym]` branch).
        2. Await fetch_snapshot. Yields control to record_stream which
           may push events into pre_align_buffer.
        3. Atomically install the new book + last_update_id (no await
           in this block). Set is_initialized=True so the next depth
           event triggers re-alignment using the buffered events
           captured during step 2.

        On REST failure, restore is_initialized/stream_aligned to True
        so the existing in-memory book continues to be used (legacy
        path) — we lose the dust eviction this cycle but don't drop
        the recording.
        """
        self.is_initialized[sym] = False
        self.stream_aligned[sym] = False
        self.pre_align_buffer[sym] = []
        try:
            fresh = await asyncio.wait_for(
                self.adapter.fetch_snapshot(sym), timeout=10
            )
            new_book = {"bids": {}, "asks": {}}
            for p, q in fresh.get("bids", []):
                new_book["bids"][float(p)] = float(q)
            for p, q in fresh.get("asks", []):
                new_book["asks"][float(p)] = float(q)
            last_id, _ = self.adapter.parse_snapshot(fresh)
            # Atomic install — no await between these lines.
            self.orderbooks[sym] = new_book
            self.last_update_ids[sym] = last_id
            self.is_initialized[sym] = True
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 {sym.upper()} REST 重拉 snapshot ({len(new_book['bids'])} bids / {len(new_book['asks'])} asks, last_id={last_id})")
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ {sym.upper()} REST 重拉失败: {e}, 继续用内存 book")
            # Restore to legacy state — no fresh book installed, but
            # the existing in-memory book is still valid and aligned.
            self.is_initialized[sym] = True
            self.stream_aligned[sym] = True

    def _cleanup_old_files(self):
        cutoff = time.time() - self.retain_days * 86400
        removed = 0
        try:
            for fname in os.listdir(self.save_dir):
                if not fname.endswith(".parquet"):
                    continue
                fpath = os.path.join(self.save_dir, fname)
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    removed += 1
            if removed:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 🗑️ 已清理 {removed} 个过期文件 (>{self.retain_days}天)")
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ 文件清理失败: {e}")

    async def _flush_all_buffers(self):
        for sym in self.symbols:
            if not self.buffers[sym]:
                continue
            data = self.buffers[sym]
            self.buffers[sym] = []
            try:
                df = pd.DataFrame(data, columns=["timestamp", "side", "price", "quantity"])
                ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                fname = f"{sym.upper()}_RAW_{ts_str}.parquet"
                path = os.path.join(self.save_dir, fname)
                df.to_parquet(path, engine="pyarrow", compression="snappy", index=False)
                print(f"[SHUTDOWN] 📥 {sym.upper()} 最终落盘完成 ({len(df)} 行)")
            except Exception as e:
                print(f"[SHUTDOWN] ❌ {sym.upper()} 最终落盘失败: {e}")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self):
        print(f"🚀 Narci Recorder 启动 | {self.adapter.name}/{self.adapter.market_type}")
        loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self._handle_shutdown(s)))

        record_tasks = [
            asyncio.create_task(self.record_stream(url))
            for url in self.ws_urls
        ]
        save_task = asyncio.create_task(self.save_loop())

        await self._shutdown_event.wait()
        for t in record_tasks:
            t.cancel()
        save_task.cancel()
        for t in record_tasks + [save_task]:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        await self._flush_all_buffers()
        print("✅ 所有缓冲区已落盘")

    async def _handle_shutdown(self, sig):
        if self._shutdown_event.is_set():
            return
        print(f"\n🛑 收到信号 {sig.name}，执行安全关闭...")
        self.running = False
        self._shutdown_event.set()


# 向后兼容的别名（保留旧名以不破坏外部引用）
BinanceL2Recorder = L2Recorder


if __name__ == "__main__":
    symbol_arg = sys.argv[1] if len(sys.argv) > 1 else None
    recorder = L2Recorder(symbol=symbol_arg)
    try:
        asyncio.run(recorder.start())
    except KeyboardInterrupt:
        print("\n🛑 用户停止")
        recorder.running = False
