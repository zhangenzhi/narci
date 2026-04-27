"""
事件驱动 L2 回测引擎 (Phase 1 + 2 + 部分 3)。

输入: RAW 4 列 parquet (timestamp, side, price, quantity) 一个或多个文件。
内核: numba @njit 编译的 orderbook 状态机 + 限价单 FIFO 队列匹配。
策略: backtest.strategy.EventStrategy (新接口)。

Phase 2/3 增强:
- order_latency_ms: 订单提交到入 book 的延迟，模拟真实 API RTT
- symbol_spec:      tick/lot/min_notional 校验
- post_only=False:  marketable limit 立即吃流动性，剩余挂入 book

性能预期: 单线程 700K-1M events/s (取决于策略 Python 开销).
"""
from __future__ import annotations

import heapq
import os
import time
from typing import Iterable

import numpy as np
import pyarrow.dataset as ds

from .orderbook import Orderbook, SIDE_TRADE
from .broker import SpotBroker
from .strategy import EventStrategy
from .symbol_spec import SymbolSpec, get_spec


class EventBacktestEngine:
    def __init__(
        self,
        data_paths: str | Iterable[str],
        strategy: EventStrategy,
        broker: SpotBroker,
        symbol: str,
        depth: int = 20,
        max_orders: int = 256,
        order_latency_ms: int = 0,
        symbol_spec: SymbolSpec | None = None,
    ):
        if isinstance(data_paths, str):
            data_paths = [data_paths]
        self.data_paths = list(data_paths)
        self.strategy = strategy
        self.broker = broker
        self.symbol = symbol.upper()
        self.order_latency_ms = int(order_latency_ms)
        self.symbol_spec = symbol_spec if symbol_spec is not None else get_spec(self.symbol)

        self.book = Orderbook(depth=depth, max_orders=max_orders)
        broker.orderbook = self.book
        broker.engine = self  # broker.place_limit_order / cancel_order 通过它访问 latency + spec
        strategy.broker = broker
        strategy.book = self.book

        # 待执行操作堆: (exec_ts, seq, op_type, args_tuple)
        self._pending: list = []
        self._pending_seq = 0
        self._current_ts = 0

        self.stats = {"load_sec": 0.0, "loop_sec": 0.0, "n_events": 0,
                      "n_trades": 0, "n_fills": 0,
                      "n_pending_place": 0, "n_pending_cancel": 0,
                      "n_rejected": 0}

    def _queue_op(self, op_type: str, *args):
        """broker.place_limit_order / cancel_order 在有 latency 时调到这里。"""
        exec_ts = self._current_ts + self.order_latency_ms
        self._pending_seq += 1
        heapq.heappush(self._pending, (exec_ts, self._pending_seq, op_type, args))
        if op_type == "place":
            self.stats["n_pending_place"] += 1
        elif op_type == "cancel":
            self.stats["n_pending_cancel"] += 1

    def _drain_pending(self, now_ts: int):
        """ts 推进到 now_ts 时，把所有 exec_ts <= now_ts 的待执行 op 实际作用到 book/broker。"""
        broker = self.broker
        while self._pending and self._pending[0][0] <= now_ts:
            _, _, op_type, args = heapq.heappop(self._pending)
            if op_type == "place":
                oid, side, price, qty, post_only = args
                # 订单可能在 latency 期内被 cancel 了
                if oid not in broker.active_orders:
                    continue
                broker._submit_to_book(oid, side, price, qty, post_only)
            elif op_type == "cancel":
                oid, = args
                order = broker.active_orders.pop(oid, None)
                if order is None:
                    continue
                self.book.cancel_order(oid)

    def _load(self):
        t0 = time.time()
        dataset = ds.dataset(self.data_paths, format="parquet")
        table = dataset.to_table(columns=["timestamp", "side", "price", "quantity"])
        # 排序：timestamp 升序，同 ts 内 side 降序 (snapshot 3/4 -> trade 2 -> incremental 1/0)
        # 让快照先建 book，再处理同 ts 的增量与 trade
        df = table.to_pandas()
        df = df.sort_values(["timestamp", "side"], ascending=[True, False]).reset_index(drop=True)
        self.stats["load_sec"] = time.time() - t0
        self.stats["n_events"] = len(df)

        ts = df["timestamp"].to_numpy(dtype=np.int64, copy=False)
        sd = df["side"].to_numpy(dtype=np.int8, copy=False)
        px = df["price"].to_numpy(dtype=np.float64, copy=False)
        qy = df["quantity"].to_numpy(dtype=np.float64, copy=False)
        return ts, sd, px, qy

    def run(self):
        ts, sd, px, qy = self._load()
        n = len(ts)
        print(f"[EventEngine] {n:,} events from {len(self.data_paths)} file(s) "
              f"(load {self.stats['load_sec']:.2f}s)")

        # 局部变量缓存（避免每事件重 attr lookup）
        book = self.book
        apply_event = book.apply_event
        match_trade = book.match_trade
        mid_fn = book.mid
        broker = self.broker
        broker_apply_fill = broker.apply_fill
        broker_cur_price = broker.current_price
        on_event = self.strategy.on_event
        on_fill = self.strategy.on_fill
        symbol = self.symbol
        n_trades = 0
        n_fills = 0

        # 局部缓存（latency 路径用）
        pending_heap = self._pending
        drain_pending = self._drain_pending if self.order_latency_ms > 0 else None

        t0 = time.time()
        for i in range(n):
            side_i = sd[i]
            price_i = px[i]
            qty_i = qy[i]
            ts_i = ts[i]

            self._current_ts = ts_i
            # latency 模式：先 drain 应该已生效的 pending op，再处理本事件
            if drain_pending is not None and pending_heap:
                drain_pending(ts_i)

            apply_event(side_i, price_i, qty_i)

            if side_i == SIDE_TRADE:
                n_trades += 1
                fills = match_trade(price_i, qty_i)
                for oid, fqty, fprice in fills:
                    broker_apply_fill(oid, fqty, fprice, True)
                    on_fill(oid, fqty, fprice)
                    n_fills += 1

            broker.current_timestamp = ts_i
            m = mid_fn()
            if m > 0.0:
                broker_cur_price[symbol] = m
            on_event(ts_i, side_i, price_i, qty_i)

        self.stats["loop_sec"] = time.time() - t0
        self.stats["n_trades"] = n_trades
        self.stats["n_fills"] = n_fills

        broker.record_equity()
        self.strategy.on_finish()

        rate = n / self.stats["loop_sec"] if self.stats["loop_sec"] > 0 else 0
        print(f"[EventEngine] loop {self.stats['loop_sec']:.2f}s "
              f"({rate:,.0f} events/s) | trades {n_trades:,} fills {n_fills}")

    def summary(self) -> dict:
        eq_start = self.broker.initial_cash
        eq_end = self.broker.get_equity()
        ret = (eq_end - eq_start) / eq_start if eq_start > 0 else 0.0
        return {
            "events": self.stats["n_events"],
            "trades": self.stats["n_trades"],
            "fills": self.stats["n_fills"],
            "load_sec": round(self.stats["load_sec"], 3),
            "loop_sec": round(self.stats["loop_sec"], 3),
            "events_per_sec": round(self.stats["n_events"] / max(self.stats["loop_sec"], 1e-9)),
            "equity_start": eq_start,
            "equity_end": round(eq_end, 4),
            "return_pct": round(ret * 100, 4),
        }
