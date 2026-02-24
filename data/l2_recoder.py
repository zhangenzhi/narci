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
    def __init__(self, config_path="configs/recorder.yaml", symbol=None):
        """
        初始化录制器
        :param config_path: 配置文件路径
        :param symbol: 命令行参数覆盖配置文件的 symbol
        """
        # 兼容性处理：如果找不到 config，尝试当前目录或上级目录查找
        if not os.path.exists(config_path):
            if os.path.exists("recorder.yaml"):
                config_path = "recorder.yaml"
            elif os.path.exists("../configs/recorder.yaml"):
                config_path = "../configs/recorder.yaml"
        
        self.config = self._load_config(config_path)
        
        # 配置参数提取
        # 优先使用 override 参数，否则使用配置文件，最后使用默认值
        self.symbol = (symbol or self.config.get('symbol', 'ETHUSDT')).lower()
        self.interval = self.config.get('interval_ms', 100)
        self.save_dir = self.config.get('save_dir', './data/realtime/l2')
        self.save_interval = self.config.get('save_interval_sec', 60)
        self.retry_wait = self.config.get('retry', {}).get('wait_seconds', 5)
        
        # 组合流：深度增量 + 实时成交
        self.ws_url = f"wss://stream.binance.com:9443/stream?streams={self.symbol}@depth@{self.interval}ms/{self.symbol}@aggTrade"
        self.snapshot_url = f"https://api.binance.com/api/v3/depth?symbol={self.symbol.upper()}&limit=1000"
        
        self.buffer = []
        self.last_update_id = 0
        self.is_initialized = False
        self.stream_aligned = False # 标记流是否已对齐
        self.running = True
        
        os.makedirs(self.save_dir, exist_ok=True)
        print(f"🔧 配置加载完成 | 配置文件: {config_path}")
        print(f"🎯 目标: {self.symbol.upper()} | 频率: {self.interval}ms | 落盘间隔: {self.save_interval}s")
        print(f"📂 存储路径: {self.save_dir}")

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

    async def fetch_snapshot(self):
        """获取初始深度快照"""
        async with aiohttp.ClientSession() as session:
            async with session.get(self.snapshot_url) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"Snapshot request failed: {resp.status} {text}")
                data = await resp.json()
                self.last_update_id = data['lastUpdateId']
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 初始快照获取成功，LastUpdateId: {self.last_update_id}")
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

    async def save_loop(self):
        """滚动写盘逻辑"""
        print(f"💾 存储循环已就绪 (每 {self.save_interval} 秒落盘一次)")
        while self.running:
            await asyncio.sleep(self.save_interval)
            if not self.buffer:
                # 如果長時間沒有數據，打印提示
                if self.is_initialized and not self.stream_aligned:
                     print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ 正在等待数据流对齐...")
                continue
            
            # 原子操作：取出当前 buffer，置空原 buffer
            current_data = self.buffer
            self.buffer = []
            
            try:
                df = pd.DataFrame(current_data, columns=['timestamp', 'side', 'price', 'quantity'])
                
                # 文件名带时间戳
                filename = f"{self.symbol}_RAW_{datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
                path = os.path.join(self.save_dir, filename)
                
                # 异步写文件通常比较复杂，这里用同步写，量大时可考虑 run_in_executor
                df_to_save = df.copy()
                # 兼容不同精度的 timestamp (ms)
                df_to_save['timestamp_dt'] = pd.to_datetime(df_to_save['timestamp'], unit='ms')
                df_to_save.to_parquet(path, engine='pyarrow', compression='snappy', index=False)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 📥 数据固化完成: {filename} ({len(df)} rows)")
            except Exception as e:
                print(f"❌ 写入失败: {e}")
                # 写入失败尝试保留数据，避免丢失
                # self.buffer.extend(current_data) 

    async def _daily_compaction_task(self):
        """后台驻留协程：每天凌晨 00:05 自动触发对'昨天'的数据聚合与校验"""
        print(f"🕒 每日聚合与自动验证后台任务已启动...")
        while self.running:
            now = datetime.utcnow()
            # 计算距离明天 00:05:00 UTC 还有多少秒 (留出 5 分钟给官方生成归档文件的时间)
            tomorrow = now.date() + timedelta(days=1)
            next_run_time = datetime.combine(tomorrow, datetime.min.time()) + timedelta(minutes=5)
            sleep_seconds = (next_run_time - now).total_seconds()
            
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ 自动聚合与校验挂起中，将于 {sleep_seconds/3600:.1f} 小时后 (UTC 00:05) 触发...")
            
            try:
                # 采用分段 sleep 以便能够快速响应 self.running = False 的退出信号
                # 这里为了简化，直接 sleep，由 asyncio 的 cancellation 机制在外部结束它
                await asyncio.sleep(sleep_seconds)
            except asyncio.CancelledError:
                break
                
            if not self.running:
                break
                
            # 时间到，执行昨日数据的聚合
            yesterday = datetime.utcnow().date() - timedelta(days=1)
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 开始执行后台归档与交叉验证任务，目标日期: {yesterday}")
            
            # 定位官方数据存放的目录 (如 ./replay_buffer/official_validation)
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            official_dir = os.path.join(base_dir, "replay_buffer", "official_validation")
            
            try:
                compactor = DailyCompactor(
                    symbol=self.symbol, 
                    target_date=yesterday,
                    raw_dir=self.save_dir,
                    official_dir=official_dir
                )
                
                # 【极其重要】使用 to_thread 将阻塞的 I/O 运算丢入底层线程池，绝对不能阻塞这里的 WS 异步事件循环！
                await asyncio.to_thread(compactor.run)
            except Exception as e:
                print(f"❌ 后台归档任务发生严重异常: {e}")

    async def record_stream(self):
        """主录制循环"""
        while self.running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] WebSocket 连接成功: {self.ws_url}")
                    
                    # 1. 获取快照 (REST API)
                    # 必须在建立 WS 连接后尽快获取，减少 gap
                    snapshot_data = await self.fetch_snapshot()
                    snapshot_records = self.standardize_event('snapshot', snapshot_data)
                    self.buffer.extend(snapshot_records)
                    
                    self.is_initialized = True
                    self.stream_aligned = False
                    
                    async for message in ws:
                        raw_data = json.loads(message)
                        if 'stream' not in raw_data: continue
                        
                        stream_name = raw_data['stream']
                        data = raw_data['data']
                        
                        if 'depth' in stream_name:
                            u, U = data['u'], data['U']
                            # 顺序校验逻辑
                            if self.is_initialized:
                                if u <= self.last_update_id: 
                                    # 过期数据丢弃 (静默处理)
                                    continue
                                
                                # 检查是否衔接上了
                                if U <= self.last_update_id + 1 <= u:
                                    # 正常衔接 (U <= last_update_id + 1 且 u >= last_update_id + 1)
                                    if not self.stream_aligned:
                                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 深度流已与快照无缝对齐，开始实时缓冲数据...")
                                        self.stream_aligned = True
                                    pass
                                else:
                                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ 深度流同步偏移! Local Last ID: {self.last_update_id}, Stream U: {U}, u: {u}")
                                    # 出现 GAP，必须重置
                                    break
                            
                            records = self.standardize_event('depthUpdate', data)
                            self.buffer.extend(records)
                            self.last_update_id = u
                        
                        elif 'aggTrade' in stream_name:
                            records = self.standardize_event('aggTrade', data)
                            self.buffer.extend(records)
                        
            except Exception as e:
                print(f"❌ 连接断开或发生错误: {e}")
                print(f"⏳ {self.retry_wait}秒后重试...")
                self.is_initialized = False
                await asyncio.sleep(self.retry_wait)

    async def start(self):
        """启动录制器"""
        print(f"--- 启动 Binance 原始数据捕获器 ---")
        
        # 将每日压缩校验任务抛到后台，并保留句柄以便优雅退出
        compaction_task = asyncio.create_task(self._daily_compaction_task())
        
        try:
            await asyncio.gather(
                self.record_stream(),
                self.save_loop()
            )
        finally:
            # 当主协程被 KeyboardInterrupt 打断时，取消后台任务
            compaction_task.cancel()

if __name__ == "__main__":
    # 支持命令行参数覆盖 symbol
    # 用法: python data/l2_recoder.py [SYMBOL]
    import sys
    symbol_arg = sys.argv[1] if len(sys.argv) > 1 else None
    
    recorder = BinanceL2Recorder(symbol=symbol_arg)
    try:
        asyncio.run(recorder.start())
    except KeyboardInterrupt:
        print("\n🛑 用户手动停止录制，正在进行安全关闭...")
        recorder.running = False