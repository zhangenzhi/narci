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
    高频做市微观策略 (Pure HFT MM 架构)：
    1. 波动率自适应盈亏比 (Volatility-Scaled TP/SL)
    2. 信号分层 (Strong=Taker, Weak=Maker)
    3. 队列优先级保持 (No Micro-Chasing)
    4. 库存偏置控制 (Inventory Skewing)
    """
    def __init__(self, symbol='BTCUSDT', imbalance_threshold=0.4, trade_qty=0.1):
        super().__init__()
        self.symbol = symbol.upper()
        
        # 基础参数
        self.base_imbalance_threshold = imbalance_threshold
        self.strong_signal_multiplier = 1.5  # 强信号乘数 (例如 0.4 * 1.5 = 0.6 触发 Taker)
        self.trade_qty = trade_qty
        
        # --- 优先级 1: 压缩止损与自适应波动率 (Volatility Scaled) ---
        self.k_tp = 0.5           # TP 乘数
        self.k_sl = 1.0           # SL 乘数 (追求 1:2 左右的盈亏比结构)
        
        self.min_tp_abs = 0.05    # 最低锁盈底线 (5个Tick)
        self.min_sl_abs = 0.20    # 最低硬止损底线
        self.max_sl_abs = 0.50    # 绝对防暴毙硬止损封顶
        
        # 生命周期
        self.panic_hold_ticks = 40       # 超时硬切断放宽到 4 秒
        self.queue_tolerance = 0.05      # 队列容忍度: 价格跑偏超过 5 ticks 才允许重新挂单 (保队列优先级)
        
        self.hold_ticks = 0             
        self.entry_price = 0.0          
        self.is_exiting = False          
        
        self._current_ema_imb = 0.0
        self._current_buy_vol = 0.0
        self._current_sell_vol = 0.0

        self.prof = {'ticks': 0, 't_parse': 0.0, 't_phase1': 0.0, 't_phase2': 0.0}

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
        # O(1) 获取前置流水线生成的高级特征
        # =========================================================
        ema_imb = tick.get('ema_imbalance', 0.0)
        volatility_2s = tick.get('volatility_2s', 0.0)
        momentum_bullish = tick.get('momentum_bullish', False)
        momentum_bearish = tick.get('momentum_bearish', False)
        
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
        # 阶段 1：持仓与风控管理 (Exit & Risk)
        # ==========================================
        if abs(current_pos) > 1e-8:
            self.hold_ticks += 1
            self.entry_price = self.broker.entry_prices.get(self.symbol, 0.0)

            current_price = b_p_0 if current_pos > 0 else a_p_0 
            pnl_abs = (current_price - self.entry_price) if current_pos > 0 else (self.entry_price - current_price)
            
            # --- 自适应波动率计算 ---
            dynamic_tp = max(volatility_2s * self.k_tp, self.min_tp_abs)
            dynamic_sl = min(max(volatility_2s * self.k_sl, self.min_sl_abs), self.max_sl_abs)
            
            # --- 库存偏置导致的反转极易触发点 ---
            is_flipped = False
            if current_pos > 0 and ema_imb < -0.15: 
                is_flipped = True
            elif current_pos < 0 and ema_imb > 0.15:
                is_flipped = True
            
            # --- 1. TAKER 止损/反转/恐慌逃生 (坚决切断) ---
            if pnl_abs <= -dynamic_sl or is_flipped or self.hold_ticks >= self.panic_hold_ticks:
                self.broker.cancel_all_orders(self.symbol)
                reason = "StopLoss(Taker)" if pnl_abs <= -dynamic_sl else ("SignalFlip(Taker)" if is_flipped else "PanicTimeExit(Taker)")
                
                if current_pos > 0:
                    self.broker.sell(self.symbol, abs(current_pos), use_l2=True, signal_info=self._get_sig_info(reason))
                else:
                    self.broker.buy(self.symbol, abs(current_pos), use_l2=True, signal_info=self._get_sig_info(reason))
                
                self.hold_ticks = 0
                self.entry_price = 0.0
                self.is_exiting = False
                
                t_p1_end = time.perf_counter()
                self.prof['t_phase1'] += (t_p1_end - t_parse_end)
                return

            # --- 2. TAKER 强力止盈 (确保锁住 R/R 期望值) ---
            if pnl_abs >= dynamic_tp:
                self.broker.cancel_all_orders(self.symbol)
                # 盈利已满足动态波动期望，直接 Taker 锁定利润，拒绝幻想
                reason = f"TakeProfit(Taker)[TP={dynamic_tp:.2f}]"
                if current_pos > 0:
                    self.broker.sell(self.symbol, abs(current_pos), use_l2=True, signal_info=self._get_sig_info(reason))
                else:
                    self.broker.buy(self.symbol, abs(current_pos), use_l2=True, signal_info=self._get_sig_info(reason))
                
                self.hold_ticks = 0
                self.entry_price = 0.0
                self.is_exiting = False
                
                t_p1_end = time.perf_counter()
                self.prof['t_phase1'] += (t_p1_end - t_parse_end)
                return 

            # --- 3. MAKER 温和退出 (仅在无强烈方向时尝试排队出局) ---
            if abs(ema_imb) < 0.15:
                if not self.is_exiting:
                    self.is_exiting = True 
                    self.broker.cancel_all_orders(self.symbol)
                    
                exit_side = 'SELL' if current_pos > 0 else 'BUY'
                exit_price = a_p_0 if current_pos > 0 else b_p_0 

                # 队列保护逻辑：不要轻易撤单！
                needs_update = True
                for o in pending_orders:
                    if o['side'] == exit_side:
                        # 只有当市场价偏离我的挂单超过容忍度时，才撤单重排
                        if abs(o['price'] - exit_price) <= self.queue_tolerance:
                            needs_update = False 
                        else:
                            self.broker.cancel_all_orders(self.symbol)
                            pending_orders = [] 
                        break

                if needs_update:
                    self.broker.place_limit_order(self.symbol, exit_side, exit_price, abs(current_pos), self._get_sig_info('SoftExit(Maker)'))
                
                t_p1_end = time.perf_counter()
                self.prof['t_phase1'] += (t_p1_end - t_parse_end)
                return 
        else:
            self.hold_ticks = 0
            self.entry_price = 0.0
            self.is_exiting = False

        t_p1_end = time.perf_counter()

        # ==========================================
        # 阶段 2：信号判定与入场管理 (Entry Skewing)
        # ==========================================
        
        # --- 优先级 4: 库存惩罚机制 (Inventory Skew) ---
        skew = (current_pos / self.trade_qty) * 0.25
        # 做多门槛：如果有仓位，变高；如果没有，原样
        bull_threshold = self.base_imbalance_threshold + skew 
        # 做空门槛：如果有仓位(正)，变低(更容易触发反手空)
        bear_threshold = self.base_imbalance_threshold - skew 
        
        is_volatile = volatility_2s > 0.20 # 基础噪音过滤
        
        if is_volatile:
            # ---------------- BUY 逻辑 ----------------
            if ema_imb > bull_threshold and momentum_bullish:
                if current_pos < self.trade_qty:
                    target_add_qty = round(self.trade_qty - current_pos, 4)
                    
                    # 优先级 2：动能分离 (Strong vs Weak)
                    if ema_imb > bull_threshold * self.strong_signal_multiplier:
                        # 极端不平衡，买盘极强 -> TAKER 追击
                        self.broker.cancel_all_orders(self.symbol)
                        if target_add_qty > 0:
                            self.broker.buy(self.symbol, target_add_qty, use_l2=True, signal_info=self._get_sig_info('StrongEntry(Taker)'))
                    else:
                        # 一般不平衡 -> MAKER 挂单，守护队列
                        maker_qty = round(target_add_qty - pending_buy_qty, 4)
                        has_maker = False
                        for o in pending_orders:
                            if o['side'] == 'BUY':
                                has_maker = True
                                # 优先级 3：避免频繁撤单
                                if abs(o['price'] - b_p_0) > self.queue_tolerance:
                                    self.broker.cancel_all_orders(self.symbol)
                                    pending_orders = []
                                    has_maker = False
                                break
                                
                        if not has_maker and maker_qty > 0:
                            self.broker.place_limit_order(self.symbol, 'BUY', b_p_0, maker_qty, self._get_sig_info('WeakEntry(Maker)'))

            # ---------------- SELL 逻辑 ----------------
            elif ema_imb < -bear_threshold and momentum_bearish:
                if current_pos > -self.trade_qty:
                    target_add_qty = round(self.trade_qty - abs(current_pos), 4)
                    
                    if ema_imb < -bear_threshold * self.strong_signal_multiplier:
                        # 极端空头 -> TAKER 追击
                        self.broker.cancel_all_orders(self.symbol)
                        if target_add_qty > 0:
                            self.broker.sell(self.symbol, target_add_qty, use_l2=True, signal_info=self._get_sig_info('StrongEntry(Taker)'))
                    else:
                        maker_qty = round(target_add_qty - pending_sell_qty, 4)
                        has_maker = False
                        for o in pending_orders:
                            if o['side'] == 'SELL':
                                has_maker = True
                                if abs(o['price'] - a_p_0) > self.queue_tolerance:
                                    self.broker.cancel_all_orders(self.symbol)
                                    pending_orders = []
                                    has_maker = False
                                break
                                
                        if not has_maker and maker_qty > 0:
                            self.broker.place_limit_order(self.symbol, 'SELL', a_p_0, maker_qty, self._get_sig_info('WeakEntry(Maker)'))

        else:
            # 无波动死水行情，撤销做市排队
            if pending_orders and current_pos == 0:
                self.broker.cancel_all_orders(self.symbol)

        t_p2_end = time.perf_counter()
        
        self.prof['ticks'] += 1
        self.prof['t_parse'] += (t_parse_end - t_start)
        self.prof['t_phase2'] += (t_p2_end - t_p1_end)

    def on_finish(self):
        self.broker.cancel_all_orders(self.symbol)
        self.broker.close_all(use_l2=True)
        
        total_strat_time = self.prof['t_parse'] + self.prof['t_phase1'] + self.prof['t_phase2']
        if self.prof['ticks'] > 0 and total_strat_time > 0:
            print("\n" + "="*15 + " [策略内部 Profiler 性能报告] " + "="*15)
            print(f"⏱️ 总计 Tick 步数: {self.prof['ticks']:,}")
            print(f"⚡ 单个 Tick 策略层平均耗时: {(total_strat_time/self.prof['ticks']) * 1e6:.2f} 微秒 (μs)")
            print("="*60 + "\n")