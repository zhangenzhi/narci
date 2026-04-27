"""
事件驱动 L2 回测引擎 (Phase 1)。

输入: RAW 4 列 parquet (timestamp, side, price, quantity) 一个或多个文件。
内核: numba @njit 编译的 orderbook 状态机 + 限价单 FIFO 队列匹配。
策略: backtest.strategy.EventStrategy (新接口)。

性能预期: 1M-3M events/s 量级（取决于策略 Python 开销和 trade 事件密度）。
"""
from __future__ import annotations

import os
import time
from typing import Iterable

import numpy as np
import pyarrow.dataset as ds

from .orderbook import Orderbook, SIDE_TRADE
from .broker import SpotBroker
from .strategy import EventStrategy


class EventBacktestEngine:
    def __init__(
        self,
        data_paths: str | Iterable[str],
        strategy: EventStrategy,
        broker: SpotBroker,
        symbol: str,
        depth: int = 20,
        max_orders: int = 256,
    ):
        if isinstance(data_paths, str):
            data_paths = [data_paths]
        self.data_paths = list(data_paths)
        self.strategy = strategy
        self.broker = broker
        self.symbol = symbol.upper()

        self.book = Orderbook(depth=depth, max_orders=max_orders)
        broker.orderbook = self.book
        strategy.broker = broker
        strategy.book = self.book

        self.stats = {"load_sec": 0.0, "loop_sec": 0.0, "n_events": 0,
                      "n_trades": 0, "n_fills": 0}

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

        t0 = time.time()
        for i in range(n):
            side_i = sd[i]
            price_i = px[i]
            qty_i = qy[i]
            ts_i = ts[i]

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
