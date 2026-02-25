import argparse
import os
import sys
import re
import asyncio
import subprocess
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
    from data.l2_recoder import BinanceL2Recorder
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
    """
    try:
        from data.daily_compactor import DailyCompactor
    except ImportError:
        print("❌ 无法导入 DailyCompactor。请确保 data/daily_compactor.py 文件存在。")
        return
        
    # 动态适配 L2 原始数据目录，优先适配 replay_buffer 下的路径
    raw_dir_replay = os.path.join(current_dir, "replay_buffer", "realtime", "l2")
    raw_dir_data = os.path.join(current_dir, "data", "realtime", "l2")
    
    raw_dir = raw_dir_replay if os.path.exists(raw_dir_replay) else raw_dir_data
    
    official_dir = os.path.join(current_dir, "replay_buffer", "official_validation")
    
    if not os.path.exists(raw_dir):
        print(f"❌ 原始数据目录不存在，已检查以下路径:\n  - {raw_dir_replay}\n  - {raw_dir_data}")
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
            # 这确保了文件系统的一致性，并保证 DailyCompactor 里的 glob 能正确检索。
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
        # 一天 24 小时 * 60 分钟 = 1440 份
        completion_rate = (count / 1440) * 100
        target_dt = datetime.strptime(d_str, "%Y%m%d").date()
        
        print(f"\n📅 【处理日期】: {target_dt.strftime('%Y-%m-%d')}")
        print(f"📦 【碎片完整度】: 发现 {count}/1440 个 1min 文件 (完整率: {completion_rate:.1f}%)")
        
        if completion_rate < 99.0:
            print(f"⚠️ 警告: 存在部分时段掉线或缺失！(缺失约 {1440 - count} 分钟的数据)")
            
        compactor = DailyCompactor(
            symbol=symbol.upper(), # 严格传入大写
            target_date=target_dt,
            raw_dir=raw_dir,
            official_dir=official_dir
        )
        
        # 触发核心执行流：聚合 -> 下载官方历史 -> L1/L2 交叉校验
        compactor.run()
        print("=" * 60)
        
    print("\n✅ 所有批次数据聚合与交叉验证完毕！请前往 GUI [冷数据仓库] 查看全盘资产。")

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

    args = parser.parse_args()

    if args.command == "gui":
        cmd_gui()
    elif args.command == "record":
        cmd_record(args.config, args.symbol)
    elif args.command == "compact":
        cmd_compact(args.symbol, args.date)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()