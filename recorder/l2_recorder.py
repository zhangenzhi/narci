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

from recorder.exchange import get_adapter, ExchangeAdapter
from recorder.wal import SegmentWAL, recover_orphans, write_parquet_atomic
from core.config import load_config_section
from core.io import load_parquet


class _WalBuffer:
    """兼容垫片:把旧契约 ``recorder.buffers[sym].extend(...)/.append(...)``
    转发进该 symbol 的 :class:`SegmentWAL` micro-buffer。

    保留以不破坏 bitbank / gmo 等 custom_stream adapter 的既有写法
    (见 ``ExchangeAdapter.custom_stream`` 契约 data/exchange/base.py)。
    """

    __slots__ = ("_wal",)

    def __init__(self, wal: "SegmentWAL"):
        self._wal = wal

    def extend(self, records) -> None:
        self._wal.append(records)

    def append(self, record) -> None:
        self._wal.append([record])


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
        # P1 WAL (2026-05-26): micro-flush 周期。事件先入内存 micro-buffer,
        # 每 wal_flush_interval_sec 原子写成一个 .segwal 段;save_loop 每
        # save_interval 把段合并成规范 RAW。崩溃最多丢一个 micro-buffer
        # (~秒级)而非一个 save_interval(线上 600s)。见 data/wal.py。
        self.wal_flush_interval = int(self.config.get("wal_flush_interval_sec", 2))
        # fsync 每个段(+ 目录)以抗掉电;测试/低端盘可关。
        self.wal_fsync = bool(self.config.get("wal_fsync", True))
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
        # WAL 段目录:与 realtime/ 平级的 wal/ 树,且段用 .segwal 扩展名 ——
        # 双重保证扫 *.parquet / *_RAW_* 的消费方(GUI os.walk、compactor
        # glob、main 正则)绝对看不到临时段。
        wal_root = os.path.join(os.path.dirname(os.path.normpath(base_dir)), "wal")
        self.wal_dir = os.path.join(
            wal_root, self.adapter.name, self.adapter.market_type, "l2"
        )

        # 5. 初始化状态机
        self.orderbooks = {s: {"bids": {}, "asks": {}} for s in self.symbols}
        # 每 symbol 一个段式 WAL;其内部 micro-buffer 即原来的 self.buffers。
        self.wals = {
            s: SegmentWAL(self.wal_dir, s, fsync=self.wal_fsync)
            for s in self.symbols
        }
        # 兼容垫片:旧契约 recorder.buffers[sym] 仍可用,转发进 WAL。
        self.buffers = {s: _WalBuffer(self.wals[s]) for s in self.symbols}
        self.pre_align_buffer = {s: [] for s in self.symbols}
        self.last_update_ids = {s: 0 for s in self.symbols}
        self.is_initialized = {s: False for s in self.symbols}
        self.stream_aligned = {s: False for s in self.symbols}
        self.running = True

        # 6. Alignment-loop circuit breaker (#85, 2026-05-24).
        # Prevents 2026-05-23 UM 28h silent death: recorder kept re-pulling
        # snapshots (128 attempts, 0 successful alignment, 0 落盘) because
        # WS stream `u` always ran ahead of REST `lastUpdateId+1`. With no
        # circuit breaker, the loop ran indefinitely + container appeared
        # healthy. Per-sym tracking: count re-snapshot attempts since last
        # successful alignment + wall-time since last_alignment_ok. When
        # either crosses threshold, exit so supervisord/docker restarts
        # the container — clean restart_count visibility + chance of fresh
        # WS connection succeeding. See:
        #   reco/docs/INCIDENTS/2026-05-23-um-alignment-loop-28h-silent-death.md
        self.alignment_max_retries = int(
            self.config.get("alignment_max_retries",
                            os.environ.get("NARCI_ALIGNMENT_MAX_RETRIES", 10))
        )
        self.alignment_max_duration_sec = int(
            self.config.get("alignment_max_duration_sec",
                            os.environ.get("NARCI_ALIGNMENT_MAX_DURATION_SEC", 600))
        )
        # Per-sym counters: # of "流偏移过大" retries since last successful
        # alignment. Reset to 0 when stream_aligned[sym] flips True.
        self.alignment_retry_count = {s: 0 for s in self.symbols}
        # Per-sym timestamp of last successful alignment (or process start
        # time when never aligned). Used for elapsed-time circuit-breaker.
        _t0 = time.time()
        self.last_alignment_ok_ts = {s: _t0 for s in self.symbols}

        # bitbank 等用 socket.io 的 adapter 不走 ws_url 路径,跳过构造。
        # 见 ExchangeAdapter.uses_custom_stream() 契约。
        if self.adapter.uses_custom_stream():
            self.ws_urls = []
        else:
            self.ws_urls = self.adapter.ws_urls(self.symbols, self.interval)

        print(f"🔧 配置完成 | 交易所: {self.adapter.name} | 市场: {self.adapter.market_type}")
        print(f"🔧 交易对: {[s.upper() for s in self.symbols]}")
        print(f"📂 数据目录: {self.save_dir}")
        if self.retain_days > 0:
            print(f"🗑️ 自动清理已启用: 保留最近 {self.retain_days} 天")

    @staticmethod
    def _load_config(path: str) -> dict:
        return load_config_section(path, "recorder")

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

            self.wals[sym].append(records)
            self.last_update_ids[sym] = last_id
            self.is_initialized[sym] = True
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 📸 {sym.upper()} 初始快照已就绪, ID: {last_id}")
        except Exception as e:
            print(f"❌ {sym.upper()} 初始化快照失败: {e}")

    def _maybe_trip_alignment_breaker(self, sym: str,
                                       retry_n: int, elapsed: float) -> None:
        """Circuit-breaker: if a symbol has been stuck unaligned past either
        retry-count or wall-time threshold, exit the recorder process.

        Rationale (#85, 2026-05-23 UM incident): when WS `u` consistently
        outruns REST `lastUpdateId+1`, the `_handle_depth` re-snapshot
        path loops forever. Container appears healthy (`state=running,
        restarts=0`) while 0 bytes land on disk for hours/days. By exiting
        on threshold, supervisord/docker restarts the container — fresh
        WS connection + visible `restart_count > 0` + per-symbol
        healthcheck flips 503. Better to die loud than zombie silent.

        Override action via env: NARCI_ALIGNMENT_BREAKER_ACTION ∈
          - "exit"  (default) — os._exit(2), let supervisord/docker restart
          - "log"   — only print loudly, keep retrying (debugging)

        Thresholds are config-overridable per recorder:
          - alignment_max_retries        (default 10)
          - alignment_max_duration_sec   (default 600 = 10 min)
        """
        retry_tripped = retry_n >= self.alignment_max_retries
        time_tripped = elapsed >= self.alignment_max_duration_sec
        if not (retry_tripped or time_tripped):
            return
        reason_bits = []
        if retry_tripped:
            reason_bits.append(f"retries={retry_n}>={self.alignment_max_retries}")
        if time_tripped:
            reason_bits.append(
                f"elapsed={elapsed:.0f}s>={self.alignment_max_duration_sec}s")
        reason = ", ".join(reason_bits)
        action = os.environ.get("NARCI_ALIGNMENT_BREAKER_ACTION", "exit").lower()
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] 🚨 {sym.upper()} "
            f"alignment-loop circuit-breaker TRIPPED ({reason}). "
            f"action={action}. See "
            f"reco/docs/INCIDENTS/2026-05-23-um-alignment-loop-28h-silent-death.md",
            flush=True,
        )
        if action == "exit":
            # P1: flush pending micro-buffers to WAL segments before the hard
            # exit so the in-flight data survives — recover_orphans() merges
            # them into RAW on the next start. Old behavior lost them.
            for s in self.symbols:
                try:
                    self.wals[s].flush()
                except Exception as e:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ {s.upper()} "
                          f"熔断前 WAL flush 失败: {e}", flush=True)
            # Hard exit; bypass asyncio cleanup so supervisord/docker
            # restart cycle is immediate + visible. Exit code 2 distinguishes
            # this from normal exit (0) and uncaught exception (typically 1).
            os._exit(2)
        # else "log" — fall through to re-snapshot; next attempt may trip
        # again but at least the operator gets repeated alerts in logs.

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
        self.wals[sym].append(records)

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
                            # trade 无条件入 WAL —— 即便 depth 尚未对齐,trade
                            # 也会被 save_loop 落盘(修复未对齐 symbol 的 trade
                            # 在内存无限堆积且永不落盘的旧 bug)。
                            self.wals[sym].append(records)

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
            for i, d in enumerate(combined):
                if self.adapter.try_align(self.last_update_ids[sym], d):
                    # 从对齐点起处理 d 及其后的全部事件,而非只处理对齐事件
                    # 就 return —— 旧逻辑会丢弃对齐事件之后已到达的有效事件。
                    for ev in combined[i:]:
                        self._process_depth_event(sym, ev)
                    self.stream_aligned[sym] = True
                    self.pre_align_buffer[sym] = []
                    # Reset circuit-breaker counters on success.
                    self.alignment_retry_count[sym] = 0
                    self.last_alignment_ok_ts[sym] = time.time()
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
                    self.alignment_retry_count[sym] += 1
                    retry_n = self.alignment_retry_count[sym]
                    elapsed = time.time() - self.last_alignment_ok_ts[sym]
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ {sym.upper()} 流偏移过大 "
                          f"(newest u={newest_u}, snapshot last_id+1={self.last_update_ids[sym]+1}, "
                          f"retry={retry_n}/{self.alignment_max_retries}, "
                          f"elapsed={elapsed:.0f}s/{self.alignment_max_duration_sec}s)，重新拉快照")
                    self.is_initialized[sym] = False
                    self.stream_aligned[sym] = False
                    self.pre_align_buffer[sym] = []
                    # Circuit breaker check: if either retry-count or
                    # elapsed time threshold crossed → exit so supervisord/
                    # docker restarts. Better to die loud (restart_count > 0
                    # + per-symbol healthcheck red) than silently zombie
                    # for 28h with container healthy.
                    self._maybe_trip_alignment_breaker(sym, retry_n, elapsed)
                    asyncio.create_task(self.init_symbol_snapshot(sym))
            return

        # 不需要对齐 或 已对齐 -> 直接处理
        if not self.adapter.needs_alignment():
            self.stream_aligned[sym] = True
        self._process_depth_event(sym, data)

    # ------------------------------------------------------------------ #
    # 落盘
    # ------------------------------------------------------------------ #

    async def wal_loop(self):
        """Micro-flush 循环:每 wal_flush_interval 把各 symbol 的内存
        micro-buffer 原子写成一个 .segwal 段。段很小(数秒数据),flush 同步
        在事件循环内执行即可(亚毫秒~毫秒级),不丢线程。崩溃最多丢最后一个
        未 flush 的 micro-buffer。
        """
        while self.running:
            await asyncio.sleep(self.wal_flush_interval)
            for sym in self.symbols:
                try:
                    self.wals[sym].flush()
                except Exception as e:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ {sym.upper()} "
                          f"WAL micro-flush 失败: {e}")

    def _merge_segments_to_raw(self, paths: list[str], raw_path: str) -> int:
        """[thread] 读取段列表 → 按序 concat → 原子写出 RAW。返回行数。

        纯文件 IO,不触碰任何被事件循环线程修改的状态(段已由 flush 写定;
        _buffer/_seq 只在循环线程里被 flush 改),故可安全丢进 to_thread。
        """
        frames = [load_parquet(p) for p in paths]
        df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
        write_parquet_atomic(df, raw_path, fsync=self.wal_fsync)
        return len(df)

    async def save_loop(self):
        """周期性把 WAL 段合并成规范 RAW + (可选)REST 刷新盘口。

        每 save_interval,对每个 symbol:
          1. flush 收尾内存 micro-buffer → 段。
          2. harvest 本 symbol 全部段 → 合并原子写出一个
             {SYMBOL}_RAW_*.parquet,成功后删段。RAW 文件名/格式/节奏与旧
             实现完全一致(消费方无感)。
          3. (可选)REST 重拉盘口,驱逐累计 dust(协议同 _refresh_book_via_rest)。
          4. 注入 side=3/4 全量快照进 micro-buffer,作为下一 interval 首段,
             使每个 RAW 文件自包含。

        相对旧实现的关键改进:
          - 原子写(.tmp→os.replace),崩溃不再留损坏 shard。
          - 不再以 stream_aligned 为前提:未对齐 symbol 的 trade 段照样落盘
            (修复内存堆积 + 全丢的旧 bug)。
          - 合并写失败时段不删,下轮 / 重启恢复时重试,不丢数据。
        """
        while self.running:
            await asyncio.sleep(self.save_interval)
            saved = []
            for sym in self.symbols:
                raw_path = await self._compact_symbol(sym)
                if raw_path:
                    saved.append(raw_path)
            if saved and self.retain_days > 0:
                await asyncio.to_thread(self._cleanup_old_files)

    async def _compact_symbol(self, sym: str) -> str | None:
        """把单个 symbol 的 WAL 段合并成一个 RAW,并为下一 interval 注入快照。

        返回写出的 RAW 路径(无段则 None)。从 save_loop 抽出以便单测。
        注意:**不以 stream_aligned 为前提** —— 未对齐 symbol 的 trade 段
        照样落盘;只有 side=3/4 快照注入需要对齐且 book 非空。
        """
        wal = self.wals[sym]
        # 1. 收尾 micro-buffer(同步;紧接 segment_paths 间无 await,
        #    wal_loop 无法插入)。
        try:
            wal.flush()
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ {sym.upper()} "
                  f"WAL flush 失败: {e}")
        paths = wal.segment_paths()

        # 2. 合并段 → RAW(读+concat+原子写在线程内,避免阻塞循环)。
        raw_path = None
        if paths:
            ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            candidate = os.path.join(
                self.save_dir, f"{sym.upper()}_RAW_{ts_str}.parquet"
            )
            try:
                n = await asyncio.to_thread(
                    self._merge_segments_to_raw, paths, candidate
                )
                wal.discard(paths)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 📥 {sym.upper()} "
                      f"原始数据固化 ({n} 行, {len(paths)} 段)")
                raw_path = candidate
            except Exception as e:
                print(f"❌ {sym.upper()} 段合并写盘失败: {e}（段保留,下轮/重启重试）")

        # 3. 可选 REST 刷新内存盘口。
        if self.snapshot_refresh_on_save:
            await self._refresh_book_via_rest(sym)

        # 4. 注入下一 interval 首段的全量快照 —— 仅在已对齐且 book 非空时,
        #    避免写出空/陈旧快照。
        book = self.orderbooks[sym]
        if self.stream_aligned[sym] and (book["bids"] or book["asks"]):
            now_ms = int(time.time() * 1000)
            snapshot_records = []
            for p, q in book["bids"].items():
                snapshot_records.append([now_ms, 3, float(p), float(q)])
            for p, q in book["asks"].items():
                snapshot_records.append([now_ms, 4, float(p), float(q)])
            wal.append(snapshot_records)
        return raw_path

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
        """关停时:flush 各 symbol 的 micro-buffer 收尾,再把全部段合并成
        RAW 并删段。即使这里失败/被打断,段仍在盘上,下次启动 recover_orphans
        会兜底恢复 —— 不丢数据。"""
        for sym in self.symbols:
            wal = self.wals[sym]
            try:
                wal.flush()
            except Exception as e:
                print(f"[SHUTDOWN] ⚠️ {sym.upper()} WAL flush 失败: {e}")
            paths = wal.segment_paths()
            if not paths:
                continue
            try:
                ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                raw_path = os.path.join(self.save_dir, f"{sym.upper()}_RAW_{ts_str}.parquet")
                n = self._merge_segments_to_raw(paths, raw_path)
                wal.discard(paths)
                print(f"[SHUTDOWN] 📥 {sym.upper()} 最终落盘完成 ({n} 行, {len(paths)} 段)")
            except Exception as e:
                print(f"[SHUTDOWN] ❌ {sym.upper()} 最终落盘失败: {e}（段保留,重启恢复）")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self):
        print(f"🚀 Narci Recorder 启动 | {self.adapter.name}/{self.adapter.market_type}")
        # 崩溃恢复:把上次进程残留的 WAL 段合并成 RAW 交还,零数据丢失。
        try:
            recover_orphans(self.wal_dir, self.save_dir, fsync=self.wal_fsync)
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ WAL 启动恢复失败: {e}")
        loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self._handle_shutdown(s)))

        # bitbank 等 socket.io adapter:adapter 自管整个 stream 生命周期
        # (REST snapshot + connect + room join + 消息分发 + 重连),recorder
        # 只需要起一个 task 等它跑。常规 WS adapter:每条 URL 一个 task,沿用旧
        # record_stream 逻辑。
        if self.adapter.uses_custom_stream():
            record_tasks = [
                asyncio.create_task(self.adapter.custom_stream(self))
            ]
        else:
            record_tasks = [
                asyncio.create_task(self.record_stream(url))
                for url in self.ws_urls
            ]
        save_task = asyncio.create_task(self.save_loop())
        wal_task = asyncio.create_task(self.wal_loop())

        await self._shutdown_event.wait()
        for t in record_tasks:
            t.cancel()
        save_task.cancel()
        wal_task.cancel()
        for t in record_tasks + [save_task, wal_task]:
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


if __name__ == "__main__":
    symbol_arg = sys.argv[1] if len(sys.argv) > 1 else None
    recorder = L2Recorder(symbol=symbol_arg)
    try:
        asyncio.run(recorder.start())
    except KeyboardInterrupt:
        print("\n🛑 用户停止")
        recorder.running = False
