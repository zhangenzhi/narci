import asyncio
import json
import os
import signal
import time
import sys
import yaml
import pandas as pd
import aiohttp
import websockets
from datetime import datetime, timedelta

class BinanceL2Recorder:
    def __init__(self, config_path="configs/um_future_recorder.yaml", symbol=None):
        """
        初始化录制器
        """
        self.config = self._load_config(config_path)
        
        if symbol:
            self.symbols = [symbol.lower()]
        else:
            config_symbols = self.config.get('symbols', ['ETHUSDT'])
            self.symbols = [s.lower() for s in (config_symbols if isinstance(config_symbols, list) else [config_symbols])]
                
        self.market_type = self.config.get('market_type', 'um_futures').lower()
        self.interval = self.config.get('interval_ms', 100)
        self.save_interval = self.config.get('save_interval_sec', 60)
        self.retry_wait = self.config.get('retry', {}).get('wait_seconds', 5)
        
        base_save_dir = self.config.get('save_dir', './replay_buffer/realtime')
        self.save_dir = os.path.join(base_save_dir, self.market_type, 'l2')
        
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

        stream_list = []
        for s in self.symbols:
            stream_list.append(f"{s}@depth@{self.interval}ms")
            stream_list.append(f"{s}@aggTrade")
        self.ws_url = ws_tpl.format(streams="/".join(stream_list))
        
        # ---------------------------------------------------------
        # 💡 新增：内存盘口状态机 (In-memory Orderbook)
        # 用于维持最新的 L2 快照，实现写盘时的“无缝快照注入”
        # ---------------------------------------------------------
        self.orderbooks = {sym: {'bids': {}, 'asks': {}} for sym in self.symbols}
        
        self.buffers = {sym: [] for sym in self.symbols}
        self.pre_align_buffer = {sym: [] for sym in self.symbols} 
        self.last_update_ids = {sym: 0 for sym in self.symbols}
        self.is_initialized = {sym: False for sym in self.symbols} 
        self.stream_aligned = {sym: False for sym in self.symbols}  
        self.running = True
        
        os.makedirs(self.save_dir, exist_ok=True)
        print(f"🔧 配置完成 | 交易对: {[s.upper() for s in self.symbols]} | 市场: {self.market_display}")
        print(f"📂 数据隔离路径: {self.save_dir}")

    def _load_config(self, path):
        if not os.path.exists(path): return {}
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f).get('recorder', {})

    async def fetch_snapshot(self, sym):
        url = self.snapshot_template.format(symbol_upper=sym.upper())
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise Exception(f"Snapshot failed: {resp.status}")
                return await resp.json()

    async def init_symbol_snapshot(self, sym):
        try:
            data = await self.fetch_snapshot(sym)
            records = self.standardize_event('snapshot', data)
            
            # 💡 新增：初始化内存状态机
            self.orderbooks[sym] = {'bids': {}, 'asks': {}}
            for p, q in data.get('bids', []):
                self.orderbooks[sym]['bids'][float(p)] = float(q)
            for p, q in data.get('asks', []):
                self.orderbooks[sym]['asks'][float(p)] = float(q)

            self.buffers[sym].extend(records)
            self.last_update_ids[sym] = data.get('lastUpdateId')
            self.is_initialized[sym] = True
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 📸 {sym.upper()} 初始快照已就绪, ID: {self.last_update_ids[sym]}")
        except Exception as e:
            print(f"❌ {sym.upper()} 初始化快照失败: {e}")

    def standardize_event(self, event_type, data):
        records = []
        now_ms = int(time.time() * 1000)

        if event_type == 'depthUpdate':
            ts = data['E']
            for price, qty in data['b']: records.append([ts, 0, float(price), float(qty)])
            for price, qty in data['a']: records.append([ts, 1, float(price), float(qty)])
        elif event_type == 'aggTrade':
            ts = data['E']
            p, q = float(data['p']), float(data['q'])
            if data['m']: q = -q 
            records.append([ts, 2, p, q])
        elif event_type == 'snapshot':
            ts = now_ms 
            for p, q in data.get('bids', []): records.append([ts, 3, float(p), float(q)])
            for p, q in data.get('asks', []): records.append([ts, 4, float(p), float(q)])
        return records

    def process_depth_event(self, sym, data):
        u = data['u']
        if u <= self.last_update_ids[sym]: return
        
        # 1. 生成并追加增量记录到缓冲区
        records = self.standardize_event('depthUpdate', data)
        self.buffers[sym].extend(records)
        
        # 💡 2. 新增：更新内存状态机（维护最新盘口以供切割时注入）
        for price, qty in data['b']:
            p, q = float(price), float(qty)
            if q == 0:
                self.orderbooks[sym]['bids'].pop(p, None)  # 数量为0表示撤单，从状态机移除
            else:
                self.orderbooks[sym]['bids'][p] = q
                
        for price, qty in data['a']:
            p, q = float(price), float(qty)
            if q == 0:
                self.orderbooks[sym]['asks'].pop(p, None)
            else:
                self.orderbooks[sym]['asks'][p] = q
                
        self.last_update_ids[sym] = u

    async def record_stream(self):
        while self.running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔌 WebSocket 已连接，启动缓存对齐机制...")
                    
                    for sym in self.symbols:
                        self.is_initialized[sym] = False
                        self.stream_aligned[sym] = False
                        self.pre_align_buffer[sym] = []
                        # 💡 断线重连时，必须重置内存状态机
                        self.orderbooks[sym] = {'bids': {}, 'asks': {}}

                    for sym in self.symbols:
                        asyncio.create_task(self.init_symbol_snapshot(sym))

                    async for message in ws:
                        msg = json.loads(message)
                        if 'stream' not in msg: continue
                        
                        s_name = msg['stream']
                        data = msg['data']
                        sym = s_name.split('@')[0].lower()
                        
                        if 'depth' in s_name:
                            if not self.is_initialized[sym]:
                                self.pre_align_buffer[sym].append(data)
                                if len(self.pre_align_buffer[sym]) > 2000: self.pre_align_buffer[sym].pop(0)
                                continue
                            
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
                            
                            self.process_depth_event(sym, data)

                        elif 'aggTrade' in s_name:
                            records = self.standardize_event('aggTrade', data)
                            self.buffers[sym].extend(records)
                            
            except Exception as e:
                print(f"❌ 连接异常: {e}，{self.retry_wait}s 后重试...")
                await asyncio.sleep(self.retry_wait)

    async def save_loop(self):
        """后台落盘：加入特征生成(Feature Builder)在线双轨制流转"""
        while self.running:
            await asyncio.sleep(self.save_interval)
            for sym in self.symbols:
                if not self.buffers[sym] or not self.stream_aligned[sym]: continue
                
                # -------------------------------------------------------------
                # 💡 核心机制：原子提取与快照注入 (实现所有文件自包含 Self-contained)
                # -------------------------------------------------------------
                # 1. 提取老数据并清空 buffer (Python 单线程协程下，这是天然的原子操作)
                data = self.buffers[sym]
                self.buffers[sym] = []
                
                # 2. 内存盘口深拷贝与快照注入：
                # 提取数据的瞬间，立即把当前内存里最新的 orderbook 打包成 side=3/4 的事件
                # 并塞入新的空 buffer 中。这样下一个文件就会以一个全量快照作为开头！
                now_ms = int(time.time() * 1000)
                snapshot_records = []
                
                for p, q in self.orderbooks[sym]['bids'].items():
                    snapshot_records.append([now_ms, 3, float(p), float(q)])
                for p, q in self.orderbooks[sym]['asks'].items():
                    snapshot_records.append([now_ms, 4, float(p), float(q)])
                
                # 将快照作为未来 60 秒新数据的“基石”
                self.buffers[sym].extend(snapshot_records)
                
                try:
                    df = pd.DataFrame(data, columns=['timestamp', 'side', 'price', 'quantity'])
                    ts_str = datetime.now().strftime('%Y%m%d_%H%M%S')
                    fname = f"{sym.upper()}_RAW_{ts_str}.parquet"
                    path = os.path.join(self.save_dir, fname)
                    
                    # 异步执行耗时的 RAW 原始数据写盘操作
                    await asyncio.to_thread(df.to_parquet, path, engine='pyarrow', compression='snappy', index=False)
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📥 {sym.upper()} 原始数据固化 ({len(df)} 行)")
                    
                except Exception as e:
                    print(f"❌ {sym.upper()} 写盘失败: {e}")

    async def _flush_all_buffers(self):
        """安全关闭前将所有内存数据强制落盘"""
        for sym in self.symbols:
            if not self.buffers[sym]:
                continue
            data = self.buffers[sym]
            self.buffers[sym] = []
            try:
                df = pd.DataFrame(data, columns=['timestamp', 'side', 'price', 'quantity'])
                ts_str = datetime.now().strftime('%Y%m%d_%H%M%S')
                fname = f"{sym.upper()}_RAW_{ts_str}.parquet"
                path = os.path.join(self.save_dir, fname)
                df.to_parquet(path, engine='pyarrow', compression='snappy', index=False)
                print(f"[SHUTDOWN] 📥 {sym.upper()} 最终落盘完成 ({len(df)} 行)")
            except Exception as e:
                print(f"[SHUTDOWN] ❌ {sym.upper()} 最终落盘失败: {e}")

    async def start(self):
        print(f"🚀 Narci Recorder 启动 | 模式: {self.market_display}")

        # 注册信号处理，容器环境下 ECS 发送 SIGTERM 要求优雅退出
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self._handle_shutdown(s)))

        await asyncio.gather(
            self.record_stream(),
            self.save_loop()
        )

    async def _handle_shutdown(self, sig):
        print(f"\n🛑 收到信号 {sig.name}，正在执行安全关闭...")
        self.running = False
        await self._flush_all_buffers()
        print("✅ 所有缓冲区已落盘，进程即将退出。")
        asyncio.get_running_loop().stop()

if __name__ == "__main__":
    symbol_arg = sys.argv[1] if len(sys.argv) > 1 else None
    recorder = BinanceL2Recorder(symbol=symbol_arg)
    try:
        asyncio.run(recorder.start())
    except KeyboardInterrupt:
        print("\n🛑 用户停止，正在关闭...")
        recorder.running = False