import sys
import os
import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from backtest.strategy import BaseStrategy
except ImportError:
    class BaseStrategy:
        def __init__(self): self.broker = None
        def on_tick(self, tick): pass
        def on_finish(self): pass

class L2ImbalanceStrategy(BaseStrategy):
    """
    高频微观策略 (终极纯做市 Pure Maker 版)：
    1. 入场：只挂 Maker 单，赚取免手续费入场。
    2. 出场：彻底废弃 Taker 市价单。即便止损或时间耗尽，也只在最优盘口挂 Maker 单。
    3. 追价机制：如果退场 Maker 单没被吃掉且盘口移动，立刻撤单并以新盘口价重挂。
    """
    def __init__(self, symbol='BTCUSDT', imbalance_threshold=0.3, trade_qty=0.1):
        super().__init__()
        self.symbol = symbol.upper()
        
        self.imbalance_threshold = imbalance_threshold
        self.trade_qty = trade_qty
        
        self.max_hold_ticks = 6          # 0.6 秒生存期
        self.take_profit_pct = 0.0001    # 万分之一微利止盈
        self.stop_loss_pct = 0.001       # 千分之一硬止损
        
        self.hold_ticks = 0             
        self.entry_price = 0.0          
        self.is_exiting = False          # 【新增】退场状态锁
        
        self.ema_imbalance = 0.0
        self.ema_alpha = 2.0 / (10 + 1)  # 10 Tick EMA

    def on_tick(self, tick):
        raw_imbalance = tick.get('imbalance', 0.0)
        taker_buy_vol = tick.get('taker_buy_vol', 0.0)
        taker_sell_vol = tick.get('taker_sell_vol', 0.0)
        
        b_p_0 = tick.get('b_p_0') 
        a_p_0 = tick.get('a_p_0') 
        
        if pd.isna(b_p_0) or pd.isna(a_p_0):
            return

        # --- 信号平滑过滤 (EMA) ---
        if self.ema_imbalance == 0.0:
            self.ema_imbalance = raw_imbalance
        else:
            self.ema_imbalance = self.ema_alpha * raw_imbalance + (1 - self.ema_alpha) * self.ema_imbalance
            
        ema_imb = round(self.ema_imbalance, 4)
        current_pos = self.broker.positions.get(self.symbol, 0.0)
        
        momentum_bullish = taker_buy_vol > taker_sell_vol and taker_buy_vol > 0
        momentum_bearish = taker_sell_vol > taker_buy_vol and taker_sell_vol > 0
        
        sig_info = {
            'imbalance': ema_imb, 
            'buy_vol': taker_buy_vol, 
            'sell_vol': taker_sell_vol, 
            'reason': 'Entry'
        }
        
        pending_orders = [o for o in self.broker.active_orders.values() if o['symbol'] == self.symbol]
        
        # ==========================================
        # 阶段 1：持仓与全 Maker 退场生命周期管理
        # ==========================================
        if abs(current_pos) > 1e-8:
            self.hold_ticks += 1
            self.entry_price = self.broker.entry_prices.get(self.symbol, 0.0)

            current_price = b_p_0 if current_pos > 0 else a_p_0 
            
            if self.entry_price > 0:
                pnl_pct = (current_price - self.entry_price) / self.entry_price if current_pos > 0 else (self.entry_price - current_price) / self.entry_price
            else:
                pnl_pct = 0.0
            
            exit_reason = ""
            can_flip = self.hold_ticks >= 5
            
            # --- 退场触发条件研判 ---
            if pnl_pct <= -self.stop_loss_pct:
                exit_reason = "StopLoss"
            elif pnl_pct >= self.take_profit_pct:
                exit_reason = "TakeProfit"
            elif self.hold_ticks >= self.max_hold_ticks:
                exit_reason = "TimeExit"
            elif abs(ema_imb) < (self.imbalance_threshold * 0.5):
                exit_reason = "SignalDecay"
            elif can_flip and ((current_pos > 0 and ema_imb < -self.imbalance_threshold) or (current_pos < 0 and ema_imb > self.imbalance_threshold)):
                exit_reason = "SignalFlip"
                
            # --- 纯 Maker 退场执行逻辑 (动态追单) ---
            if exit_reason or self.is_exiting:
                self.is_exiting = True # 一旦触发退场，锁定退场状态直到仓位归零
                
                exit_side = 'SELL' if current_pos > 0 else 'BUY'
                # 核心：无论止损还是止盈，永远挂在对立面的最优盘口做 Maker
                exit_price = a_p_0 if current_pos > 0 else b_p_0 

                needs_update = True
                for o in pending_orders:
                    if o['side'] == exit_side:
                        if o['price'] == exit_price:
                            needs_update = False # 已经挂在最优盘口，耐心等待被吃
                        else:
                            # 盘口移动了，我们被落在了后面，立刻撤单重挂 (Penny Jumping)
                            self.broker.cancel_all_orders(self.symbol)
                            pending_orders = [] 
                        break

                if needs_update:
                    exit_sig = {'imbalance': ema_imb, 'reason': exit_reason or 'ChasingExit'}
                    self.broker.place_limit_order(self.symbol, exit_side, exit_price, abs(current_pos), exit_sig)
                
                return # 退场状态中，不参与阶段 2 的新入场

        else:
            # 空仓状态重置
            self.hold_ticks = 0
            self.entry_price = 0.0
            self.is_exiting = False
            can_flip = True

        # ==========================================
        # 阶段 2：信号生成与 Maker 追踪入场
        # ==========================================
        pending_buy_qty = sum(o['qty'] for o in pending_orders if o['side'] == 'BUY')
        pending_sell_qty = sum(o['qty'] for o in pending_orders if o['side'] == 'SELL')
        
        # --- 做多逻辑 ---
        if ema_imb > self.imbalance_threshold and momentum_bullish:
            if current_pos < self.trade_qty:
                target_add_qty = round(self.trade_qty - current_pos - pending_buy_qty, 4)
                needs_update = False
                for order in pending_orders:
                    if order['side'] == 'BUY' and order['price'] != b_p_0:
                        needs_update = True
                        break
                        
                if needs_update:
                    self.broker.cancel_all_orders(self.symbol)
                    pending_orders = [] 
                    target_add_qty = round(self.trade_qty - current_pos, 4) 
                
                if target_add_qty > 0 and not pending_orders:
                    self.broker.place_limit_order(self.symbol, 'BUY', b_p_0, target_add_qty, sig_info)

        # --- 做空逻辑 ---
        elif ema_imb < -self.imbalance_threshold and momentum_bearish:
            if current_pos > -self.trade_qty:
                target_add_qty = round(self.trade_qty - abs(current_pos) - pending_sell_qty, 4)
                needs_update = False
                for order in pending_orders:
                    if order['side'] == 'SELL' and order['price'] != a_p_0:
                        needs_update = True
                        break
                        
                if needs_update:
                    self.broker.cancel_all_orders(self.symbol)
                    pending_orders = []
                    target_add_qty = round(self.trade_qty - abs(current_pos), 4)
                    
                if target_add_qty > 0 and not pending_orders:
                    self.broker.place_limit_order(self.symbol, 'SELL', a_p_0, target_add_qty, sig_info)

        # --- 信号消退逻辑 ---
        else:
            if pending_orders and current_pos == 0:
                self.broker.cancel_all_orders(self.symbol)

    def on_finish(self):
        self.broker.cancel_all_orders(self.symbol)
        # 最后兜底由于强制结算，只能保留市价
        self.broker.close_all(use_l2=True)