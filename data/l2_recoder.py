import asyncio
import json
import os
import time
import sys
import yaml
import pandas as pd
import aiohttp
import websockets
from datetime import datetime, timedelta

# 动态引入每日聚合器，解决可能存在的路径问题
try:
    from data.daily_compactor import DailyCompactor
except ImportError:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from data.daily_compactor import DailyCompactor

class BinanceL2Recorder:
    def __init__(self, config_path="configs/um_feature_recorder.yaml", symbol=None):
        """
        初始化录制器
        :param config_path: 配置文件路径
        :param symbol: 命令行参数覆盖配置文件的 symbol
        """
        # 移除回退逻辑，没找到就是没找到
        self.config = self._load_config(config_path)
        
        # 配置参数提取
        # 如果命令行提供了 symbol，则单挂该交易对，否则读取 yaml 里的列表
        if symbol:
            self.symbols = [symbol.lower()]
        else:
            config_symbols = self.config.get('symbols', ['ETHUSDT'])
            if isinstance(config_symbols, str):
                self.symbols = [config_symbols.lower()]
            else:
                self.symbols = [s.lower() for s in config_symbols]
                
        self.market_type = self.config.get('market_type', 'um_futures').lower()
        self.interval = self.config.get('interval_ms', 100)
        self.save_dir = self.config.get('save_dir', './data/realtime/l2')
        self.save_interval = self.config.get('save_interval_sec', 60)
        self.retry_wait = self.config.get('retry', {}).get('wait_seconds', 5)
        
        # 获取 endpoints 配置
        endpoints_config = self.config.get('endpoints', {})
        market_endpoints = endpoints_config.get(self.market_type, {})
        
        # 根据市场类型动态路由端点 (提供默认模板，支持 {streams} 聚合多流注入)
        if self.market_type == "spot":
            self.market_display = "Binance Spot (现货)"
            default_ws = "wss://stream.binance.com:9443/stream?streams={streams}"
            default_rest = "https://api.binance.com/api/v3/depth?symbol={symbol_upper}&limit=1000"
        elif self.market_type == "um_futures":
            self.market_display = "Binance U-Margined Futures (U本位合约)"
            default_ws = "wss://fstream.binance.com/stream?streams={streams}"
            default_rest = "https://fapi.binance.com/fapi/v1/depth?symbol={symbol_upper}&limit=1000"
        else:
            raise ValueError(f"不支持的市场类型: {self.market_type}。请在配置中使用 'spot' 或 'um_futures'")
            
        # 优先从 yaml 读取模板，否则使用默认模板
        ws_template = market_endpoints.get('ws_url', default_ws)
        self.snapshot_template = market_endpoints.get('snapshot_url', default_rest)

        # 组合流：将多个交易对的流合并到同一个 ws url 中
        stream_list = []
        for s in self.symbols:
            stream_list.append(f"{s}@depth@{self.interval}ms")
            stream_list.append(f"{s}@aggTrade")
            
        self.ws_url = ws_template.format(streams="/".join(stream_list))
        
        # 为每个交易对维护独立的数据缓存和状态
        self.buffers = {sym: [] for sym in self.symbols}
        self.last_update_ids = {sym: 0 for sym in self.symbols}
        self.is_initialized = {sym: False for sym in self.symbols}
        self.stream_aligned = {sym: False for sym in self.symbols}
        self.running = True
        
        os.makedirs(self.save_dir, exist_ok=True)
        print(f"🔧 配置加载完成 | 配置文件: {config_path}")
        print(f"🎯 目标交易对: {[s.upper() for s in self.symbols]} | 市场: {self.market_display} | 频率: {self.interval}ms")
        print(f"📂 落盘间隔: {self.save_interval}s | 存储路径: {self.save_dir}")

    def _load_config(self, path):
        if not os.path.exists(path):
            print(f"⚠️ 配置文件 {path} 不存在，将使用默认参数。")
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f).get('recorder', {})
        except Exception as e:
            print(f"❌ 读取配置文件失败: {e}，将使用默认参数。")
            return {}

    async def fetch_snapshot(self, sym):
        """获取指定交易对的初始深度快照"""
        url = self.snapshot_template.format(symbol_upper=sym.upper())
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"Snapshot request failed for {sym.upper()}: {resp.status} {text}")
                data = await resp.json()
                print(f"[{datetime.now().strftime('%H:%M:%S')}] {sym.upper()} 初始快照获取成功，LastUpdateId: {data.get('lastUpdateId')}")
                return data

    def standardize_event(self, event_type, data):
        """
        标准化事件格式
        side 定义:
        0: Depth Bid (Diff)
        1: Depth Ask (Diff)
        2: Trade (AggTrade)
        3: Snapshot Bid (Initial)
        4: Snapshot Ask (Initial)
        """
        records = []
        now_ms = int(time.time() * 1000)

        if event_type == 'depthUpdate':
            ts = data['E']
            for price, qty in data['b']:
                records.append([ts, 0, float(price), float(qty)])
            for price, qty in data['a']:
                records.append([ts, 1, float(price), float(qty)])
        
        elif event_type == 'aggTrade':
            ts = data['E']
            price, qty = float(data['p']), float(data['q'])
            if data['m']: qty = -qty # 负数代表卖方主动 (Taker Sell)
            records.append([ts, 2, price, qty])

        elif event_type == 'snapshot':
            ts = now_ms 
            for price, qty in data.get('bids', []):
                records.append([ts, 3, float(price), float(qty)])
            for price, qty in data.get('asks', []):
                records.append([ts, 4, float(price), float(qty)])
            
        return records

    async def save_loop(self):
        """滚动写盘逻辑：为每个交易对独立生成文件"""
        print(f"💾 存储循环已就绪 (每 {self.save_interval} 秒落盘一次)")
        while self.running:
            await asyncio.sleep(self.save_interval)
            
            for sym in self.symbols:
                if not self.buffers[sym]:
                    if self.is_initialized[sym] and not self.stream_aligned[sym]:
                         print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ {sym.upper()} 正在等待数据流对齐...")
                    continue
                
                # 原子操作
                current_data = self.buffers[sym]
                self.buffers[sym] = []
                
                try:
                    df = pd.DataFrame(current_data, columns=['timestamp', 'side', 'price', 'quantity'])
                    
                    filename = f"{sym.upper()}_RAW_{datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
                    path = os.path.join(self.save_dir, filename)
                    
                    df_to_save = df.copy()
                    df_to_save['timestamp_dt'] = pd.to_datetime(df_to_save['timestamp'], unit='ms')
                    df_to_save.to_parquet(path, engine='pyarrow', compression='snappy', index=False)
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📥 {sym.upper()} 固化完成: {filename} ({len(df)} rows)")
                except Exception as e:
                    print(f"❌ {sym.upper()} 写入失败: {e}")

    async def _daily_compaction_task(self):
        """后台驻留协程：每天凌晨 00:05 自动触发对'昨天'的数据聚合与校验"""
        print(f"🕒 每日聚合与自动验证后台任务已启动...")
        while self.running:
            now = datetime.utcnow()
            tomorrow = now.date() + timedelta(days=1)
            next_run_time = datetime.combine(tomorrow, datetime.min.time()) + timedelta(minutes=5)
            sleep_seconds = (next_run_time - now).total_seconds()
            
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ 自动聚合校验挂起中，将于 {sleep_seconds/3600:.1f} 小时后 (UTC 00:05) 触发...")
            
            try:
                await asyncio.sleep(sleep_seconds)
            except asyncio.CancelledError:
                break
                
            if not self.running:
                break
                
            yesterday = datetime.utcnow().date() - timedelta(days=1)
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 开始执行后台归档与交叉验证任务，目标日期: {yesterday}")
            
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            official_dir = os.path.join(base_dir, "replay_buffer", "official_validation")
            
            # 对所有订阅的交易对逐个执行归档和校验
            for sym in self.symbols:
                try:
                    compactor = DailyCompactor(
                        symbol=sym, 
                        target_date=yesterday,
                        raw_dir=self.save_dir,
                        official_dir=official_dir
                    )
                    await asyncio.to_thread(compactor.run)
                except Exception as e:
                    print(f"❌ {sym.upper()} 后台归档任务发生严重异常: {e}")

    async def record_stream(self):
        """主录制循环"""
        while self.running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] WebSocket 连接成功，开始并发拉取全量快照...")
                    
                    # 并发获取所有交易对的快照
                    async def init_sym(sym):
                        snapshot_data = await self.fetch_snapshot(sym)
                        records = self.standardize_event('snapshot', snapshot_data)
                        self.buffers[sym].extend(records)
                        self.last_update_ids[sym] = snapshot_data.get('lastUpdateId')
                        self.is_initialized[sym] = True
                        self.stream_aligned[sym] = False
                        
                    await asyncio.gather(*(init_sym(sym) for sym in self.symbols))
                    
                    async for message in ws:
                        raw_data = json.loads(message)
                        if 'stream' not in raw_data: continue
                        
                        stream_name = raw_data['stream']
                        data = raw_data['data']
                        
                        # 提取当前消息对应的交易对
                        sym = stream_name.split('@')[0].lower()
                        if sym not in self.symbols: continue
                        
                        if 'depth' in stream_name:
                            u, U = data['u'], data['U']
                            if self.is_initialized[sym]:
                                if u <= self.last_update_ids[sym]: 
                                    continue
                                
                                if U <= self.last_update_ids[sym] + 1 <= u:
                                    if not self.stream_aligned[sym]:
                                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ {sym.upper()} 深度流已无缝对齐，开始缓冲...")
                                        self.stream_aligned[sym] = True
                                    pass
                                else:
                                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ {sym.upper()} 深度流同步偏移! Local: {self.last_update_ids[sym]}, U: {U}, u: {u}")
                                    break # 某一个交易对发生断档，重连整个WS以保安全
                            
                            records = self.standardize_event('depthUpdate', data)
                            self.buffers[sym].extend(records)
                            self.last_update_ids[sym] = u
                        
                        elif 'aggTrade' in stream_name:
                            records = self.standardize_event('aggTrade', data)
                            self.buffers[sym].extend(records)
                        
            except Exception as e:
                print(f"❌ 连接断开或发生错误: {e}")
                print(f"⏳ {self.retry_wait}秒后重试...")
                for sym in self.symbols:
                    self.is_initialized[sym] = False
                await asyncio.sleep(self.retry_wait)

    async def start(self):
        """启动录制器"""
        print(f"--- 启动 Binance 原始数据捕获器 ({self.market_display}) ---")
        
        compaction_task = asyncio.create_task(self._daily_compaction_task())
        
        try:
            await asyncio.gather(
                self.record_stream(),
                self.save_loop()
            )
        finally:
            compaction_task.cancel()

if __name__ == "__main__":
    import sys
    symbol_arg = sys.argv[1] if len(sys.argv) > 1 else None
    
    recorder = BinanceL2Recorder(symbol=symbol_arg)
    try:
        asyncio.run(recorder.start())
    except KeyboardInterrupt:
        print("\n🛑 用户手动停止录制，正在进行安全关闭...")
        recorder.running = False