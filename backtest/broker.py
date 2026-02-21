class SimulatedBroker:
    """
    模拟撮合引擎
    """
    def __init__(self, initial_cash=10000.0, fee_rate=0.001):
        self.cash = initial_cash
        self.fee_rate = fee_rate
        self.position = 0.0
        self.equity = initial_cash
        self.trades = []
        self.history = []

    def buy(self, price, amount_usd, timestamp):
        if self.cash >= amount_usd:
            fee = amount_usd * self.fee_rate
            net_amount = amount_usd - fee
            qty = net_amount / price
            self.position += qty
            self.cash -= amount_usd
            self.trades.append({'time': timestamp, 'side': 'BUY', 'price': price, 'qty': qty, 'fee': fee})
            return True
        return False

    def sell(self, price, qty, timestamp):
        if self.position >= qty:
            revenue = qty * price
            fee = revenue * self.fee_rate
            self.cash += (revenue - fee)
            self.position -= qty
            self.trades.append({'time': timestamp, 'side': 'SELL', 'price': price, 'qty': qty, 'fee': fee})
            return True
        return False

    def update_equity(self, current_price):
        self.equity = self.cash + (self.position * current_price)