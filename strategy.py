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


class MyTrendStrategy(BaseStrategy):
    """
    一个简单的双均线或价格突破策略示例
    """
    def __init__(self, window_size=100):
        super().__init__()
        self.window_size = window_size
        self.prices = []

    def on_tick(self, tick):
        self.prices.append(tick['price'])
        if len(self.prices) > self.window_size:
            self.prices.pop(0)
            
            avg_price = np.mean(self.prices)
            current_price = tick['price']
            
            # 简单的策略逻辑：当前价格高于均线且无仓位时买入
            if current_price > avg_price * 1.001 and self.broker.position == 0:
                self.broker.buy(current_price, self.broker.cash, tick['timestamp'])
            
            # 当前价格低于均线且有仓位时卖出
            elif current_price < avg_price * 0.999 and self.broker.position > 0:
                self.broker.sell(current_price, self.broker.position, tick['timestamp'])