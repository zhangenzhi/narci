import asyncio
import json
import os
import time
import pandas as pd
import aiohttp
import websockets
from datetime import datetime

class BinanceL2Recorder:
    def __init__(self, symbol="ETHUSDT", interval_ms=100, save_dir="./data/realtime/l2"):
        self.symbol = symbol.lower()
        self.interval = interval_ms
        self.save_dir = save_dir
        # 组合流：深度增量 + 实时成交
        self.ws_url = f"wss://stream.binance.com:9443/stream?streams={self.symbol}@depth@{self.interval}ms/{self.symbol}@aggTrade"
        self.snapshot_url = f"https://api.binance.com/api/v3/depth?symbol={symbol.upper()}&limit=1000"
        
        self.buffer = []
        self.last_update_id = 0
        self.is_initialized = False
        self.running = True
        
        os.makedirs(self.save_dir, exist_ok=True)

    async def fetch_snapshot(self):
        """获取初始深度快照"""
        async with aiohttp.ClientSession() as session:
            async with session.get(self.snapshot_url) as resp:
                data = await resp.json()
                self.last_update_id = data['lastUpdateId']
                print(f"[{datetime.now()}] 初始快照获取成功，LastUpdateId: {self.last_update_id}")
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
            # WebSocket 增量流使用缩写 'b' 和 'a'
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
            # REST API 快照使用全称 'bids' 和 'asks'
            ts = now_ms 
            for price, qty in data.get('bids', []):
                records.append([ts, 3, float(price), float(qty)])
            for price, qty in data.get('asks', []):
                records.append([ts, 4, float(price), float(qty)])
            
        return records

    async def save_loop(self, interval_sec=60):
        """滚动写盘逻辑"""
        while self.running:
            await asyncio.sleep(interval_sec)
            if not self.buffer:
                continue
            
            current_data = self.buffer
            self.buffer = []
            
            df = pd.DataFrame(current_data, columns=['timestamp', 'side', 'price', 'quantity'])
            
            filename = f"{self.symbol}_RAW_{datetime.now().strftime('%Y%m%d_%H%M')}.parquet"
            path = os.path.join(self.save_dir, filename)
            
            try:
                df_to_save = df.copy()
                df_to_save['timestamp_dt'] = pd.to_datetime(df_to_save['timestamp'], unit='ms')
                df_to_save.to_parquet(path, engine='pyarrow', compression='snappy', index=False)
                print(f"[{datetime.now()}] 原始数据已固化: {filename} ({len(df)} rows)")
            except Exception as e:
                print(f"写入失败: {e}")

    async def record_stream(self):
        """主录制循环"""
        while self.running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    print(f"[{datetime.now()}] 建立 WebSocket 链接...")
                    
                    # 1. 获取快照
                    snapshot_data = await self.fetch_snapshot()
                    # 修正：快照逻辑使用 'snapshot' 类型
                    snapshot_records = self.standardize_event('snapshot', snapshot_data)
                    self.buffer.extend(snapshot_records)
                    
                    self.is_initialized = True
                    
                    async for message in ws:
                        raw_data = json.loads(message)
                        stream_name = raw_data['stream']
                        data = raw_data['data']
                        
                        if 'depth' in stream_name:
                            u, U = data['u'], data['U']
                            # 顺序校验逻辑
                            if self.is_initialized:
                                if u <= self.last_update_id: continue
                                if U <= self.last_update_id + 1 <= u:
                                    print(f"[{datetime.now()}] 深度流已与快照完成对齐。")
                                    self.is_initialized = False
                                else:
                                    print(f"同步偏移，尝试重连...")
                                    break
                            elif U != self.last_update_id + 1:
                                print(f"数据序列中断!")
                                break
                            
                            records = self.standardize_event('depthUpdate', data)
                            self.buffer.extend(records)
                            self.last_update_id = u
                        
                        elif 'aggTrade' in stream_name:
                            records = self.standardize_event('aggTrade', data)
                            self.buffer.extend(records)
                        
            except Exception as e:
                print(f"网络异常: {e}, 5秒后重试...")
                await asyncio.sleep(5)

    async def start(self):
        """启动录制器"""
        print(f"--- 启动 Binance 原始数据捕获器 ({self.symbol}) ---")
        await asyncio.gather(
            self.record_stream(),
            self.save_loop()
        )

if __name__ == "__main__":
    recorder = BinanceL2Recorder(symbol="ETHUSDT")
    try:
        asyncio.run(recorder.start())
    except KeyboardInterrupt:
        print("录制已停止")