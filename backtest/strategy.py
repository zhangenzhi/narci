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


