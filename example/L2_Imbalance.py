import sys
import os
import math
import time

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
    高频微观策略 (纯净极致性能版 - 适配 FeatureBuilder)：
    策略本身不再负责任何特征和时间序列指标的推算。
    一切指标均由前置管线算出并直接提供 $O(1)$ 字典访问。
    """
    def __init__(self, symbol='BTCUSDT', imbalance_threshold=0.4, trade_qty=0.1):
        super().__init__()
        self.symbol = symbol.upper()
        
        self.imbalance_threshold = imbalance_threshold
        self.trade_qty = trade_qty
        
        # --- 生命周期参数 ---
        self.soft_hold_ticks = 10        # 1.0 秒：尝试 Maker 温和退场
        self.panic_hold_ticks = 30       # 3.0 秒：超时恐慌，无视成本 Taker 逃命
        
        self.take_profit_abs = 0.10      # 赚 $0.10 就立刻触发 Maker 跑路
        self.stop_loss_abs = 1.00        # 硬止损放大到 $1.00，留足空间给 Maker
        
        self.hold_ticks = 0             
        self.entry_price = 0.0          
        self.is_exiting = False          
        
        # 仅用于输出交易日志 (通过字典引用)
        self._current_ema_imb = 0.0
        self._current_buy_vol = 0.0
        self._current_sell_vol = 0.0

        self.prof = {
            'ticks': 0, 't_parse': 0.0, 't_phase1': 0.0, 't_phase2': 0.0
        }

    def _get_sig_info(self, reason='Entry'):
        return {
            'imbalance': self._current_ema_imb, 
            'buy_vol': self._current_buy_vol, 
            'sell_vol': self._current_sell_vol, 
            'reason': reason
        }

    def on_tick(self, tick):
        t_start = time.perf_counter()
        
        b_p_0 = tick.get('b_p_0') 
        a_p_0 = tick.get('a_p_0') 
        
        if b_p_0 is None or a_p_0 is None or math.isnan(b_p_0) or math.isnan(a_p_0):
            return

        # =========================================================
        # 🚀 极速读取已处理好的高级特征 (FeatureBuilder 产物)
        # 此时已经没有任何计算负荷，只是字典映射
        # =========================================================
        ema_imb = tick.get('ema_imbalance', 0.0)
        volatility_2s = tick.get('volatility_2s', 0.0)
        momentum_bullish = tick.get('momentum_bullish', False)
        momentum_bearish = tick.get('momentum_bearish', False)
        
        is_volatile = volatility_2s > 0.3
        
        self._current_ema_imb = ema_imb
        self._current_buy_vol = tick.get('taker_buy_vol', 0.0)
        self._current_sell_vol = tick.get('taker_sell_vol', 0.0)

        current_pos = self.broker.positions.get(self.symbol, 0.0)
        
        pending_buy_qty = 0.0
        pending_sell_qty = 0.0
        pending_orders = []
        for o in self.broker.active_orders.values():
            if o['symbol'] == self.symbol:
                pending_orders.append(o)
                if o['side'] == 'BUY': pending_buy_qty += o['qty']
                elif o['side'] == 'SELL': pending_sell_qty += o['qty']

        t_parse_end = time.perf_counter()

        # ==========================================
        # 阶段 1：持仓与混合退场 (Hybrid Exit) 管理
        # ==========================================
        if abs(current_pos) > 1e-8:
            self.hold_ticks += 1
            self.entry_price = self.broker.entry_prices.get(self.symbol, 0.0)

            current_price = b_p_0 if current_pos > 0 else a_p_0 
            
            if self.entry_price > 0:
                pnl_abs = (current_price - self.entry_price) if current_pos > 0 else (self.entry_price - current_price)
            else:
                pnl_abs = 0.0
            
            can_flip = self.hold_ticks >= 5
            is_flipped = can_flip and ((current_pos > 0 and ema_imb < -self.imbalance_threshold) or (current_pos < 0 and ema_imb > self.imbalance_threshold))
            
            # --- 1. TAKER 恐慌逃生 ---
            if pnl_abs <= -self.stop_loss_abs or is_flipped or self.hold_ticks >= self.panic_hold_ticks:
                self.broker.cancel_all_orders(self.symbol)
                
                reason = "StopLoss(Taker)" if pnl_abs <= -self.stop_loss_abs else ("SignalFlip(Taker)" if is_flipped else "PanicTimeExit(Taker)")
                
                if current_pos > 0:
                    self.broker.sell(self.symbol, abs(current_pos), use_l2=True, signal_info=self._get_sig_info(reason))
                else:
                    self.broker.buy(self.symbol, abs(current_pos), use_l2=True, signal_info=self._get_sig_info(reason))
                
                self.hold_ticks = 0
                self.entry_price = 0.0
                self.is_exiting = False
                
                t_p1_end = time.perf_counter()
                self.prof['ticks'] += 1
                self.prof['t_parse'] += (t_parse_end - t_start)
                self.prof['t_phase1'] += (t_p1_end - t_parse_end)
                return

            # --- 2. MAKER 软退场 ---
            maker_exit_reason = ""
            if pnl_abs >= self.take_profit_abs:
                maker_exit_reason = "TakeProfit(Maker)"
            elif self.hold_ticks >= self.soft_hold_ticks:
                maker_exit_reason = "SoftTimeExit(Maker)"
            elif abs(ema_imb) < (self.imbalance_threshold * 0.5):
                maker_exit_reason = "SignalDecay(Maker)"

            if maker_exit_reason or self.is_exiting:
                self.is_exiting = True 
                exit_side = 'SELL' if current_pos > 0 else 'BUY'
                exit_price = a_p_0 if current_pos > 0 else b_p_0 

                needs_update = True
                for o in pending_orders:
                    if o['side'] == exit_side:
                        if o['price'] == exit_price:
                            needs_update = False 
                        else:
                            self.broker.cancel_all_orders(self.symbol)
                            pending_orders = [] 
                        break

                if needs_update:
                    self.broker.place_limit_order(self.symbol, exit_side, exit_price, abs(current_pos), self._get_sig_info(maker_exit_reason or 'ChasingExit(Maker)'))
                
                t_p1_end = time.perf_counter()
                self.prof['ticks'] += 1
                self.prof['t_parse'] += (t_parse_end - t_start)
                self.prof['t_phase1'] += (t_p1_end - t_parse_end)
                return 
        else:
            self.hold_ticks = 0
            self.entry_price = 0.0
            self.is_exiting = False
            can_flip = True

        t_p1_end = time.perf_counter()

        # ==========================================
        # 阶段 2：信号生成与 Maker 追踪入场
        # ==========================================
        
        if is_volatile:
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
                        self.broker.place_limit_order(self.symbol, 'BUY', b_p_0, target_add_qty, self._get_sig_info('Entry(Maker)'))

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
                        self.broker.place_limit_order(self.symbol, 'SELL', a_p_0, target_add_qty, self._get_sig_info('Entry(Maker)'))
        else:
            if pending_orders and current_pos == 0:
                self.broker.cancel_all_orders(self.symbol)

        t_p2_end = time.perf_counter()
        
        self.prof['ticks'] += 1
        self.prof['t_parse'] += (t_parse_end - t_start)
        self.prof['t_phase1'] += (t_p1_end - t_parse_end)
        self.prof['t_phase2'] += (t_p2_end - t_p1_end)

    def on_finish(self):
        self.broker.cancel_all_orders(self.symbol)
        self.broker.close_all(use_l2=True)
        
        total_strat_time = self.prof['t_parse'] + self.prof['t_phase1'] + self.prof['t_phase2']
        if self.prof['ticks'] > 0 and total_strat_time > 0:
            print("\n" + "="*15 + " [策略内部 Profiler 性能报告] " + "="*15)
            print(f"⏱️ 总计 Tick 步数: {self.prof['ticks']:,}")
            print(f"⏱️ 策略层执行总耗时: {total_strat_time:.4f} 秒")
            print(f"   ├─ 数据解析与特征注入 (Parse) : {self.prof['t_parse']:.4f} 秒 ({self.prof['t_parse']/total_strat_time*100:.1f}%)")
            print(f"   ├─ 持仓与退场管理 (Phase1): {self.prof['t_phase1']:.4f} 秒 ({self.prof['t_phase1']/total_strat_time*100:.1f}%)")
            print(f"   └─ 追踪与入场管理 (Phase2): {self.prof['t_phase2']:.4f} 秒 ({self.prof['t_phase2']/total_strat_time*100:.1f}%)")
            print(f"⚡ 单个 Tick 策略层平均耗时: {(total_strat_time/self.prof['ticks']) * 1e6:.2f} 微秒 (μs)")
            print("="*60 + "\n")