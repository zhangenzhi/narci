"""
事件驱动的 L2 订单簿状态机 + 限价单 FIFO 队列匹配。

输入: RAW 4 列事件流 (timestamp, side, price, quantity)，side 编码同 ABC：
  0 = bid update (qty=0 表示删除)
  1 = ask update
  2 = aggTrade  (qty 正负=做市方: 正=buyer maker / 卖方主动 / 命中 bid 侧；
                                 负=seller maker / 买方主动 / 命中 ask 侧)
  3 = bid snapshot (与 0 同语义，仅标记 recorder 周期性快照)
  4 = ask snapshot

撮合内核 numba @njit，外壳 Python。订单簿用定深度 numpy 数组 (默认 20 档)。

队列模型：限价单挂入时记录 queue_ahead = 当时该价位同侧已挂的总体积。
aggTrade 命中我们的价位时按 FIFO 消减 queue_ahead；耗尽后多出来的体积
按比例填我们的剩余 qty (实现层面：单订单一次性 take，多订单按 oid 顺序)。
"""
from __future__ import annotations

import numpy as np
import numba

DEPTH = 20
MAX_ORDERS = 256

SIDE_BID = 0
SIDE_ASK = 1
SIDE_TRADE = 2
SIDE_BID_SNAP = 3
SIDE_ASK_SNAP = 4

ORDER_BUY = 0
ORDER_SELL = 1


@numba.njit(cache=True, fastmath=True)
def _book_apply(prices, qtys, n_arr, price, qty, descending):
    """
    更新一档：存在则 update/delete (qty=0)；不存在则按排序插入。
    descending=True for bids（best bid 在 [0]）；False for asks。
    超过 depth 的尾部档位被丢弃。
    """
    n = n_arr[0]
    cap = prices.shape[0]

    # 查找已有档
    found = -1
    for i in range(n):
        if prices[i] == price:
            found = i
            break

    if found >= 0:
        if qty == 0.0:
            for i in range(found, n - 1):
                prices[i] = prices[i + 1]
                qtys[i] = qtys[i + 1]
            prices[n - 1] = 0.0
            qtys[n - 1] = 0.0
            n_arr[0] = n - 1
        else:
            qtys[found] = qty
        return

    if qty == 0.0:
        return

    # 插入位置
    pos = n
    if descending:
        for i in range(n):
            if price > prices[i]:
                pos = i
                break
    else:
        for i in range(n):
            if price < prices[i]:
                pos = i
                break

    if pos >= cap:
        return  # 超出深度，丢

    end = n + 1 if n < cap else cap
    for i in range(end - 1, pos, -1):
        prices[i] = prices[i - 1]
        qtys[i] = qtys[i - 1]
    prices[pos] = price
    qtys[pos] = qty
    if n < cap:
        n_arr[0] = n + 1


@numba.njit(cache=True, fastmath=True)
def _qty_at_level(prices, qtys, n, price):
    for i in range(n):
        if prices[i] == price:
            return qtys[i]
    return 0.0


@numba.njit(cache=True, fastmath=True)
def _match_trade(
    trade_price, trade_qty, is_buyer_maker,
    order_oid, order_side, order_price, order_qty, order_queue_ahead, order_active,
    fill_oids, fill_qtys, fill_prices, fill_n,
):
    """
    aggTrade 撮合限价单。is_buyer_maker=True 表示卖方主动命中 bid 侧 → 命中 BUY 限价单。
    返回更新后的 fill 数。
    """
    remaining = trade_qty
    n = fill_n
    cap = order_oid.shape[0]
    for j in range(cap):
        if not order_active[j]:
            continue
        # 价位+方向必须匹配；价格必须严格相等（FIFO 模型只看精确档）
        if order_price[j] != trade_price:
            continue
        if is_buyer_maker:
            if order_side[j] != ORDER_BUY:
                continue
        else:
            if order_side[j] != ORDER_SELL:
                continue
        if remaining <= 0.0:
            break

        qa = order_queue_ahead[j]
        if qa >= remaining:
            order_queue_ahead[j] = qa - remaining
            remaining = 0.0
        else:
            excess = remaining - qa
            order_queue_ahead[j] = 0.0
            fill = excess if excess < order_qty[j] else order_qty[j]
            order_qty[j] -= fill
            fill_oids[n] = order_oid[j]
            fill_qtys[n] = fill
            fill_prices[n] = trade_price
            n += 1
            remaining = excess - fill
            if order_qty[j] <= 1e-10:
                order_active[j] = False
    return n


class Orderbook:
    """事件驱动 L2 + 限价单 FIFO 队列。Python 外壳，热点函数 njit。"""

    def __init__(self, depth: int = DEPTH, max_orders: int = MAX_ORDERS):
        self.depth = depth
        self.max_orders = max_orders

        self.bid_prices = np.zeros(depth, dtype=np.float64)
        self.bid_qtys = np.zeros(depth, dtype=np.float64)
        self.ask_prices = np.zeros(depth, dtype=np.float64)
        self.ask_qtys = np.zeros(depth, dtype=np.float64)
        self.n_bid = np.zeros(1, dtype=np.int64)
        self.n_ask = np.zeros(1, dtype=np.int64)

        self.order_oid = np.zeros(max_orders, dtype=np.int64)
        self.order_side = np.zeros(max_orders, dtype=np.int8)
        self.order_price = np.zeros(max_orders, dtype=np.float64)
        self.order_qty = np.zeros(max_orders, dtype=np.float64)
        self.order_queue_ahead = np.zeros(max_orders, dtype=np.float64)
        self.order_active = np.zeros(max_orders, dtype=np.bool_)

        # 单次 trade 事件里收集到的 fills 缓冲（外层每个 trade 后 flush）
        self._fill_oids = np.zeros(max_orders, dtype=np.int64)
        self._fill_qtys = np.zeros(max_orders, dtype=np.float64)
        self._fill_prices = np.zeros(max_orders, dtype=np.float64)

        self._next_slot_hint = 0

    def apply_event(self, side: int, price: float, qty: float):
        """更新 orderbook 状态。aggTrade 不改本侧 book，由 match_trade 单独处理。"""
        if side == SIDE_BID or side == SIDE_BID_SNAP:
            _book_apply(self.bid_prices, self.bid_qtys, self.n_bid, price, qty, True)
        elif side == SIDE_ASK or side == SIDE_ASK_SNAP:
            _book_apply(self.ask_prices, self.ask_qtys, self.n_ask, price, qty, False)
        # side==2 (trade) 不动 book，由 match_trade 处理

    def match_trade(self, price: float, signed_qty: float):
        """处理一个 aggTrade 事件，返回 [(oid, fill_qty, fill_price), ...] 列表。
        signed_qty 正=buyer maker (命中 bid)，负=seller maker (命中 ask)。"""
        is_buyer_maker = signed_qty > 0.0
        trade_qty = abs(signed_qty)
        n = _match_trade(
            price, trade_qty, is_buyer_maker,
            self.order_oid, self.order_side, self.order_price,
            self.order_qty, self.order_queue_ahead, self.order_active,
            self._fill_oids, self._fill_qtys, self._fill_prices, 0,
        )
        if n == 0:
            return ()
        out = [(int(self._fill_oids[i]), float(self._fill_qtys[i]), float(self._fill_prices[i]))
               for i in range(n)]
        return out

    def best_bid(self) -> float:
        return float(self.bid_prices[0]) if self.n_bid[0] > 0 else 0.0

    def best_ask(self) -> float:
        return float(self.ask_prices[0]) if self.n_ask[0] > 0 else 0.0

    def mid(self) -> float:
        if self.n_bid[0] > 0 and self.n_ask[0] > 0:
            return 0.5 * (self.bid_prices[0] + self.ask_prices[0])
        return 0.0

    def place_limit_order(self, oid: int, side: int, price: float, qty: float) -> bool:
        """挂入一笔限价单。返回 False 表示订单簿已满或马上自成交（aggressive）拒绝。"""
        # 防 aggressive cross：BUY 价 >= best ask 或 SELL 价 <= best bid 直接拒绝（Phase 1 简化）
        if side == ORDER_BUY:
            ba = self.best_ask()
            if ba > 0.0 and price >= ba:
                return False
            qa = _qty_at_level(self.bid_prices, self.bid_qtys, self.n_bid[0], price)
        else:
            bb = self.best_bid()
            if bb > 0.0 and price <= bb:
                return False
            qa = _qty_at_level(self.ask_prices, self.ask_qtys, self.n_ask[0], price)

        # 找一个空 slot
        slot = -1
        for i in range(self._next_slot_hint, self.max_orders):
            if not self.order_active[i]:
                slot = i
                break
        if slot < 0:
            for i in range(0, self._next_slot_hint):
                if not self.order_active[i]:
                    slot = i
                    break
        if slot < 0:
            return False

        self.order_oid[slot] = oid
        self.order_side[slot] = side
        self.order_price[slot] = price
        self.order_qty[slot] = qty
        self.order_queue_ahead[slot] = qa
        self.order_active[slot] = True
        self._next_slot_hint = slot + 1
        return True

    def cancel_order(self, oid: int) -> bool:
        for i in range(self.max_orders):
            if self.order_active[i] and self.order_oid[i] == oid:
                self.order_active[i] = False
                self._next_slot_hint = i
                return True
        return False

    def sweep_marketable(self, side: int, price_cap: float, qty: float):
        """
        Walk acceptable levels for a marketable limit order. Mutates the book
        (decrements level qtys, removes empty levels). Returns (filled_qty, vwap).

        side: ORDER_BUY 走 ask 侧，仅吃 price <= price_cap 的档；
              ORDER_SELL 走 bid 侧，仅吃 price >= price_cap 的档。
        """
        if side == ORDER_BUY:
            prices = self.ask_prices
            qtys = self.ask_qtys
            n_arr = self.n_ask
        else:
            prices = self.bid_prices
            qtys = self.bid_qtys
            n_arr = self.n_bid

        n = n_arr[0]
        remaining = qty
        total_filled = 0.0
        total_cost = 0.0
        # 从 [0] 顶档开始走（ask 升序、bid 降序），只要价格还在限价范围内
        i = 0
        while i < n_arr[0] and remaining > 1e-12:
            p = prices[i]
            if side == ORDER_BUY and p > price_cap:
                break
            if side == ORDER_SELL and p < price_cap:
                break
            available = qtys[i]
            fill = available if available < remaining else remaining
            total_filled += fill
            total_cost += fill * p
            remaining -= fill
            new_q = available - fill
            if new_q <= 1e-12:
                # 删档：左移
                cur_n = n_arr[0]
                for k in range(i, cur_n - 1):
                    prices[k] = prices[k + 1]
                    qtys[k] = qtys[k + 1]
                prices[cur_n - 1] = 0.0
                qtys[cur_n - 1] = 0.0
                n_arr[0] = cur_n - 1
                # 不递增 i，下一档已经左移到当前位置
            else:
                qtys[i] = new_q
                i += 1
        if total_filled <= 0:
            return 0.0, 0.0
        return total_filled, total_cost / total_filled
