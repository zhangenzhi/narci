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

# 动态引入每日聚合器
try:
    from data.daily_compactor import DailyCompactor
except ImportError:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from data.daily_compactor import DailyCompactor

class BinanceL2Recorder:
    def __init__(self, config_path="configs/um_future_recorder.yaml", symbol=None):
        """
        初始化录制器
        :param config_path: 配置文件路径
        :param symbol: 命令行参数，用于覆盖配置文件中的交易对
        """
        self.config = self._load_config(config_path)
        
        # 1. 交易对配置
        if symbol:
            self.symbols = [symbol.lower()]
        else:
            config_symbols = self.config.get('symbols', ['ETHUSDT'])
            self.symbols = [s.lower() for s in (config_symbols if isinstance(config_symbols, list) else [config_symbols])]
                
        # 2. 核心参数
        self.market_type = self.config.get('market_type', 'um_futures').lower()
        self.interval = self.config.get('interval_ms', 100)
        self.save_interval = self.config.get('save_interval_sec', 60)
        self.retry_wait = self.config.get('retry', {}).get('wait_seconds', 5)
        
        # 3. 存储路径隔离优化：按市场类型 (spot/um_futures) 分类存储
        # 解决之前现货和合约数据混杂在一个目录下的不合理设计
        base_save_dir = self.config.get('save_dir', './replay_buffer/realtime')
        self.save_dir = os.path.join(base_save_dir, self.market_type, 'l2')
        
        # 4. 路由端点配置
        endpoints = self.config.get('endpoints', {})
        m_endpoints = endpoints.get(self.market_type, {})
        
        if self.market_type == "spot":
            self.market_display = "Binance Spot (现货)"
            ws_tpl = m_endpoints.get('ws_url', "wss://stream.binance.com:9443/stream?streams={streams}")
            self.snapshot_template = m_endpoints.get('snapshot_url', "https://api.binance.com/api/v3/depth?symbol={symbol_upper}&limit=1000")
        else:
            self.market_display = "Binance U-Margined Futures (U本位合约)"
            ws_tpl = m_endpoints.get('ws_url', "wss://fstream.binance.com/stream?streams={streams}")
            self.snapshot_template = m_endpoints.get('snapshot_url', "https://fapi.binance.com/fapi/v1/depth?symbol={symbol_upper}&limit=1000")

        # 5. 构造订阅流
        stream_list = []
        for s in self.symbols:
            stream_list.append(f"{s}@depth@{self.interval}ms")
            stream_list.append(f"{s}@aggTrade")
        self.ws_url = ws_tpl.format(streams="/".join(stream_list))
        
        # 6. 状态管理
        self.buffers = {sym: [] for sym in self.symbols}
        self.pre_align_buffer = {sym: [] for sym in self.symbols} # 用于对齐的临时缓存
        self.last_update_ids = {sym: 0 for sym in self.symbols}
        self.is_initialized = {sym: False for sym in self.symbols} # 快照是否已获取
        self.stream_aligned = {sym: False for sym in self.symbols}  # 流是否已对齐
        self.running = True
        
        os.makedirs(self.save_dir, exist_ok=True)
        print(f"🔧 配置完成 | 交易对: {[s.upper() for s in self.symbols]} | 市场: {self.market_display}")
        print(f"📂 数据隔离路径: {self.save_dir}")

    def _load_config(self, path):
        if not os.path.exists(path): return {}
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f).get('recorder', {})

    async def fetch_snapshot(self, sym):
        """REST 请求全量深度快照"""
        url = self.snapshot_template.format(symbol_upper=sym.upper())
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise Exception(f"Snapshot failed: {resp.status}")
                return await resp.json()

    async def init_symbol_snapshot(self, sym):
        """异步初始化快照并存入 Buffer"""
        try:
            data = await self.fetch_snapshot(sym)
            records = self.standardize_event('snapshot', data)
            self.buffers[sym].extend(records)
            self.last_update_ids[sym] = data.get('lastUpdateId')
            self.is_initialized[sym] = True
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 📸 {sym.upper()} 快照已就绪, ID: {self.last_update_ids[sym]}")
        except Exception as e:
            print(f"❌ {sym.upper()} 初始化快照失败: {e}")

    def standardize_event(self, event_type, data):
        """标准化事件格式 (Side 0-4)"""
        records = []
        now_ms = int(time.time() * 1000)

        if event_type == 'depthUpdate':
            ts = data['E']
            for price, qty in data['b']: records.append([ts, 0, float(price), float(qty)])
            for price, qty in data['a']: records.append([ts, 1, float(price), float(qty)])
        elif event_type == 'aggTrade':
            ts = data['E']
            p, q = float(data['p']), float(data['q'])
            if data['m']: q = -q # 负数代表 Taker Sell
            records.append([ts, 2, p, q])
        elif event_type == 'snapshot':
            ts = now_ms 
            for p, q in data.get('bids', []): records.append([ts, 3, float(p), float(q)])
            for p, q in data.get('asks', []): records.append([ts, 4, float(p), float(q)])
        return records

    def process_depth_event(self, sym, data):
        """处理已对齐的深度事件"""
        u = data['u']
        if u <= self.last_update_ids[sym]: return
        records = self.standardize_event('depthUpdate', data)
        self.buffers[sym].extend(records)
        self.last_update_ids[sym] = u

    async def record_stream(self):
        """核心 WS 录制循环：实现缓存对齐逻辑"""
        while self.running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔌 WebSocket 已连接，启动缓存对齐机制...")
                    
                    # 重置状态
                    for sym in self.symbols:
                        self.is_initialized[sym] = False
                        self.stream_aligned[sym] = False
                        self.pre_align_buffer[sym] = []

                    # 为每个币种创建并行快照 Task
                    for sym in self.symbols:
                        asyncio.create_task(self.init_symbol_snapshot(sym))

                    async for message in ws:
                        msg = json.loads(message)
                        if 'stream' not in msg: continue
                        
                        s_name = msg['stream']
                        data = msg['data']
                        sym = s_name.split('@')[0].lower()
                        
                        if 'depth' in s_name:
                            # 1. 还没拿到快照：持续存入预对齐缓存
                            if not self.is_initialized[sym]:
                                self.pre_align_buffer[sym].append(data)
                                if len(self.pre_align_buffer[sym]) > 2000: self.pre_align_buffer[sym].pop(0)
                                continue
                            
                            # 2. 刚拿到快照，尚未对齐：执行回放查找
                            if not self.stream_aligned[sym]:
                                combined = self.pre_align_buffer[sym] + [data]
                                found = False
                                for d in combined:
                                    if d['U'] <= self.last_update_ids[sym] + 1 <= d['u']:
                                        self.process_depth_event(sym, d)
                                        self.stream_aligned[sym] = True
                                        self.pre_align_buffer[sym] = []
                                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ {sym.upper()} 深度流对齐成功！")
                                        found = True
                                        break
                                
                                if not found:
                                    if combined and combined[0]['U'] > self.last_update_ids[sym] + 1:
                                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ {sym.upper()} 流偏移过大，正在重连...")
                                        break
                                    continue
                            
                            # 3. 正常状态：处理增量
                            self.process_depth_event(sym, data)

                        elif 'aggTrade' in s_name:
                            records = self.standardize_event('aggTrade', data)
                            self.buffers[sym].extend(records)
                            
            except Exception as e:
                print(f"❌ 连接异常: {e}，{self.retry_wait}s 后重试...")
                await asyncio.sleep(self.retry_wait)

    async def save_loop(self):
        """后台落盘：使用线程池避免 I/O 阻塞事件循环"""
        while self.running:
            await asyncio.sleep(self.save_interval)
            for sym in self.symbols:
                if not self.buffers[sym] or not self.stream_aligned[sym]: continue
                
                # 原子提取数据
                data = self.buffers[sym]
                self.buffers[sym] = []
                
                try:
                    df = pd.DataFrame(data, columns=['timestamp', 'side', 'price', 'quantity'])
                    fname = f"{sym.upper()}_RAW_{datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
                    path = os.path.join(self.save_dir, fname)
                    
                    # 异步执行耗时的写盘操作
                    await asyncio.to_thread(df.to_parquet, path, engine='pyarrow', compression='snappy', index=False)
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📥 {sym.upper()} 固化成功 ({len(df)} 行)")
                except Exception as e:
                    print(f"❌ {sym.upper()} 写盘失败: {e}")

    async def _daily_compaction_task(self):
        """每日 UTC 00:05 自动归档昨天的数据"""
        while self.running:
            now = datetime.utcnow()
            target_time = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()) + timedelta(minutes=5)
            await asyncio.sleep((target_time - now).total_seconds())
            
            yesterday = datetime.utcnow().date() - timedelta(days=1)
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 启动定时任务: {yesterday} 数据归档与验证...")
            
            # 这里也适配了新的分级目录
            for sym in self.symbols:
                try:
                    compactor = DailyCompactor(
                        symbol=sym, target_date=yesterday, raw_dir=self.save_dir,
                        official_dir=os.path.join(os.getcwd(), "replay_buffer", "official_validation")
                    )
                    await asyncio.to_thread(compactor.run)
                except Exception as e:
                    print(f"❌ {sym.upper()} 归档异常: {e}")

    async def start(self):
        """启动录制器"""
        print(f"🚀 Narci Recorder 启动 | 模式: {self.market_display}")
        await asyncio.gather(
            self.record_stream(),
            self.save_loop(),
            self._daily_compaction_task()
        )

if __name__ == "__main__":
    symbol_arg = sys.argv[1] if len(sys.argv) > 1 else None
    recorder = BinanceL2Recorder(symbol=symbol_arg)
    try:
        asyncio.run(recorder.start())
    except KeyboardInterrupt:
        print("\n🛑 用户停止，正在关闭...")
        recorder.running = False