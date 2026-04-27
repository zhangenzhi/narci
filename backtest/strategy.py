import numpy as np

class BaseStrategy:
    """
    策略基类，用户需继承此类并实现抽象方法
    """
    def __init__(self):
        self.broker = None
        self.data = None

    def on_tick(self, tick):
        """
        每当有一个新的 tick 产生时调用
        """
        pass

    def on_finish(self):
        """
        回测结束后的回调
        """
        pass


class EventStrategy:
    """
    事件驱动策略基类，配 EventBacktestEngine 使用。
    与 BaseStrategy 的区别：直接吃 RAW 事件，知道 orderbook 状态，
    限价单成交通过 on_fill 回调，不再需要 wide-format tick dict。
    """
    def __init__(self):
        self.broker = None      # SpotBroker (or subclass)
        self.book = None        # backtest.orderbook.Orderbook

    def on_event(self, ts: int, event_type: int, price: float, qty: float):
        """
        对每个 RAW 事件调用一次。
        event_type: 0=bid_update, 1=ask_update, 2=trade, 3=bid_snap, 4=ask_snap
        策略可读 self.book.best_bid()/best_ask()/mid() 获取最新盘口。
        """
        pass

    def on_fill(self, oid: int, qty: float, price: float):
        """限价单（部分）成交回调。oid = place_limit_order 返回值。"""
        pass

    def on_finish(self):
        pass
