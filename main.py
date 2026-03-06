import argparse
import os
import sys
import re
import asyncio
import subprocess
import time
import hashlib
from datetime import datetime

# 确保能正确引入内部模块
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

def cmd_gui():
    """启动 Streamlit 交互式控制台"""
    print("🚀 正在启动 Narci Quant Terminal (GUI)...")
    gui_path = os.path.join(current_dir, "gui", "dashboard.py")
    subprocess.run(["streamlit", "run", gui_path])

def cmd_record(config_path, symbol):
    """启动 WebSocket L2 高频录制器"""
    from data.l2_recorder import BinanceL2Recorder
    recorder = BinanceL2Recorder(config_path=config_path, symbol=symbol)
    try:
        asyncio.run(recorder.start())
    except KeyboardInterrupt:
        print("\n🛑 用户手动停止录制，正在进行安全关闭...")
        recorder.running = False

def cmd_compact(symbol, target_date=None):
    """
    手动扫描 L2 1min 数据库，检查天级别聚合与补全，
    最后完成与官方离线历史数据的交叉验证。
    支持 --symbol ALL 自动发现所有交易对。
    """
    try:
        from data.daily_compactor import DailyCompactor
    except ImportError:
        print("❌ 无法导入 DailyCompactor。请确保 data/daily_compactor.py 文件存在。")
        return

    # 动态适配 L2 原始数据目录，扫描所有市场类型子目录
    candidate_dirs = [
        os.path.join(current_dir, "replay_buffer", "realtime", "um_futures", "l2"),
        os.path.join(current_dir, "replay_buffer", "realtime", "spot", "l2"),
        os.path.join(current_dir, "replay_buffer", "realtime", "l2"),
        os.path.join(current_dir, "data", "realtime", "l2"),
    ]
    raw_dirs = [d for d in candidate_dirs if os.path.exists(d)]

    official_dir = os.path.join(current_dir, "replay_buffer", "official_validation")
    cold_dir = os.path.join(current_dir, "replay_buffer", "cold")
    retain_days = int(os.environ.get("NARCI_RETAIN_DAYS", "7"))

    if not raw_dirs:
        print(f"❌ 原始数据目录不存在，已检查以下路径:")
        for d in candidate_dirs:
            print(f"  - {d}")
        return

    # 如果 symbol=ALL，自动发现所有交易对并逐个处理
    if symbol.upper() == "ALL":
        discovered = set()
        for raw_dir in raw_dirs:
            for f in os.listdir(raw_dir):
                m = re.match(r"([A-Za-z0-9]+)_RAW_", f)
                if m:
                    discovered.add(m.group(1).upper())
        if not discovered:
            print("⚠️ 未发现任何交易对的 RAW 文件。")
            return
        print(f"🔍 [自动发现] 共检测到 {len(discovered)} 个交易对: {sorted(discovered)}")
        for sym in sorted(discovered):
            cmd_compact(sym, target_date)
        return

    # 在所有候选目录中查找该交易对的文件
    raw_dir = None
    for d in raw_dirs:
        files_in_dir = os.listdir(d)
        if any(re.match(rf"{symbol}_RAW_", f, re.IGNORECASE) for f in files_in_dir):
            raw_dir = d
            break

    if raw_dir is None:
        print(f"⚠️ 未在任何数据目录中找到 {symbol} 的 RAW 文件。")
        return

    # 扫描指定交易对的 1min RAW 文件
    files = os.listdir(raw_dir)
    # 修复：加入 re.IGNORECASE，忽略大小写，兼容 recorder 生成的 ethusdt_RAW...
    date_pattern = re.compile(rf"{symbol}_RAW_(\d{{8}})_\d{{6}}\.parquet", re.IGNORECASE)
    
    dates_found = set()
    file_counts = {}
    
    for f in files:
        match = date_pattern.match(f)
        if match:
            # 自动修复机制：如果发现由于 recorder 导致的小写前缀，自动将文件重命名为大写。
            actual_prefix = f.split('_')[0]
            if actual_prefix != symbol.upper():
                new_f = f.replace(actual_prefix, symbol.upper(), 1)
                os.rename(os.path.join(raw_dir, f), os.path.join(raw_dir, new_f))
                f = new_f
                
            d_str = match.group(1)
            dates_found.add(d_str)
            file_counts[d_str] = file_counts.get(d_str, 0) + 1
            
    # 如果指定了特定日期，则过滤
    if target_date:
        d_str = target_date.replace("-", "") # 兼容 2026-02-22 或 20260222 格式
        if d_str not in dates_found:
            print(f"⚠️ 数据库中未找到日期为 {target_date} 的 {symbol} 1min 原始碎片。")
            return
        dates_found = {d_str}
        
    if not dates_found:
        print(f"⚠️ 未在 {raw_dir} 中找到任何 {symbol} 的 1min 原始碎片文件。")
        return
        
    print(f"\n🔍 [全盘扫描] 共发现 {len(dates_found)} 天的 L2 记录数据。开始执行一致性聚合校验...")
    print(f"📂 扫描路径: {raw_dir}")
    print("=" * 60)
    
    for d_str in sorted(dates_found):
        count = file_counts[d_str]
        completion_rate = (count / 1440) * 100
        target_dt = datetime.strptime(d_str, "%Y%m%d").date()
        
        print(f"\n📅 【处理日期】: {target_dt.strftime('%Y-%m-%d')}")
        print(f"📦 【碎片完整度】: 发现 {count}/1440 个 1min 文件 (完整率: {completion_rate:.1f}%)")
        
        if completion_rate < 99.0:
            print(f"⚠️ 警告: 存在部分时段掉线或缺失！(缺失约 {1440 - count} 分钟的数据)")
            
        compactor = DailyCompactor(
            symbol=symbol.upper(),
            target_date=target_dt,
            raw_dir=raw_dir,
            official_dir=official_dir,
            cold_dir=cold_dir,
            retain_days=retain_days
        )
        
        compactor.run()
        print("=" * 60)
        
    print("\n✅ 所有批次数据聚合与交叉验证完毕！请前往 GUI [冷数据仓库] 查看全盘资产。")

def cmd_build_cache(market_type, symbol, base_dir):
    """
    离线特征缓存构建管线 (Feature Cache)
    调用 FeatureBuilder 一键生成并固化高级盘口特征
    """
    print("🏗️ 启动离线特征缓存 (Feature Cache) 构建管线...")
    
    # 延迟导入重型数据处理库
    try:
        from data.feature_builder import FeatureBuilder
        from gui.utils import get_all_parquet_files
    except ImportError as e:
        print(f"❌ 导入依赖失败: {e}。请确保依赖项已安装，并在项目根目录下运行。")
        return

    symbol = symbol.upper()
    market_dir = os.path.join(base_dir, market_type, "l2")
    cache_dir = os.path.join(market_dir, "backtest_cache")
    
    if not os.path.exists(market_dir):
        print(f"❌ 错误: 数据目录 {market_dir} 不存在。")
        return

    all_files = get_all_parquet_files(market_dir)
    raw_files = [f for f in all_files if f.endswith(".parquet") and "backtest_cache" not in f and "RAW" in f.upper()]
    
    if symbol != "ALL":
        raw_files = [f for f in raw_files if os.path.basename(f).upper().startswith(symbol)]
        
    raw_files = sorted(raw_files)
    if not raw_files:
        print(f"⚠️ 没有找到 {market_type} 市场下 {symbol} 的 RAW 数据文件。请先运行 record 收集数据。")
        return

    # 哈希计算对齐 UI 保证命中率
    file_names = "".join(sorted([os.path.basename(p) for p in raw_files]))
    hash_str = hashlib.md5(file_names.encode('utf-8')).hexdigest()[:8]
    symbol_prefix = symbol if symbol != "ALL" else "MIXED"
    cache_filename = f"{symbol_prefix}_100ms_merged_{hash_str}.parquet"
    cache_file_path = os.path.join(cache_dir, cache_filename)

    if os.path.exists(cache_file_path):
        print(f"⚡ 命中已有缓存！该批次文件此前已处理完毕，无需重复构建。\n📍 路径: {cache_file_path}")
        return
        
    print(f"⏳ 开始并发读取 {len(raw_files)} 个 {symbol} 的 RAW 碎片文件...")
    t_start = time.time()
    
    try:
        # 使用 FeatureBuilder 封装好的高级流水线处理文件
        fb = FeatureBuilder()
        reconstructed_df = fb.build_from_raw_files(raw_files)
        
        # 落盘
        if not reconstructed_df.empty:
            os.makedirs(cache_dir, exist_ok=True)
            reconstructed_df.to_parquet(cache_file_path, engine='pyarrow', compression='snappy')
            
            elapsed = time.time() - t_start
            print(f"✅ 特征缓存构建成功！总耗时: {elapsed:.2f} 秒")
            print(f"💾 缓存已固化至: {cache_file_path}")
            print(f"💡 现在您可以运行 `python main.py gui` 打开面板，点击【全选并导入全部】，回测将在 1 秒内瞬间启动！")
        else:
            print("❌ 构建失败，重构后数据切片为空。")
            
    except Exception as e:
        print(f"❌ 构建过程中发生致命错误: {e}")

def main():
    parser = argparse.ArgumentParser(description="Narci 量化系统核心调度引擎")
    subparsers = parser.add_subparsers(dest="command", help="可用命令列表")

    # 1. GUI 命令
    parser_gui = subparsers.add_parser("gui", help="启动可视化交互控制台 (Dashboard)")

    # 2. Record 命令
    parser_record = subparsers.add_parser("record", help="启动实时 L2 盘口及逐笔数据录制 (支持多币种流)")
    parser_record.add_argument("--config", type=str, default="configs/um_future_recorder.yaml", help="配置文件路径 (默认: configs/um_future_recorder.yaml)")
    parser_record.add_argument("--symbol", type=str, default=None, help="可选：临时覆盖配置文件，仅录制指定的单一交易对 (如 BTCUSDT)")

    # 3. Compact/Validate 命令
    parser_compact = subparsers.add_parser("compact", help="手动触发 L2 小文件日级别聚合与交叉验证")
    parser_compact.add_argument("--symbol", type=str, default="ETHUSDT", help="交易对名称，如 ETHUSDT")
    parser_compact.add_argument("--date", type=str, default=None, help="指定验证日期 (如 2026-02-22)，不填则扫描所有存在碎片的天数")

    # 4. Cloud-Sync 命令
    parser_sync = subparsers.add_parser("cloud-sync", help="启动独立的 rclone 云同步守护进程")
    parser_sync.add_argument("--local-dir", type=str, default=os.path.join(current_dir, "replay_buffer", "realtime"), help="本地数据目录")
    parser_sync.add_argument("--remote", type=str, default=os.environ.get("NARCI_RCLONE_REMOTE", ""), help="rclone 远端路径 (如 gdrive:/narci_raw)")
    parser_sync.add_argument("--interval", type=int, default=300, help="同步间隔秒数 (默认 300)")

    # 5. Build-Cache 命令
    parser_cache = subparsers.add_parser("build-cache", help="离线聚合 RAW 数据并调用 FeatureBuilder 构建特征缓存")
    parser_cache.add_argument("--market", type=str, default="um_futures", help="市场类型 (默认: um_futures)")
    parser_cache.add_argument("--symbol", type=str, default="ETHUSDT", help="交易对 (默认: ETHUSDT，输入 ALL 处理所有)")
    parser_cache.add_argument("--dir", type=str, default=os.path.join(current_dir, "replay_buffer", "realtime"), help="基础数据目录")

    args = parser.parse_args()

    if args.command == "gui":
        cmd_gui()
    elif args.command == "record":
        cmd_record(args.config, args.symbol)
    elif args.command == "compact":
        cmd_compact(args.symbol, args.date)
    elif args.command == "cloud-sync":
        from data.cloud_sync import CloudSyncDaemon
        if not args.remote:
            print("❌ 未指定远端路径。请设置 --remote 或 NARCI_RCLONE_REMOTE 环境变量。")
            return
        daemon = CloudSyncDaemon(local_dir=args.local_dir, remote=args.remote, interval=args.interval)
        daemon.run()
    elif args.command == "build-cache":
        cmd_build_cache(args.market, args.symbol, args.dir)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()