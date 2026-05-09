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

    # 动态适配 L2 原始数据目录，支持新旧两种路径结构
    # 新结构: replay_buffer/realtime/{exchange}/{market_type}/l2/
    # 旧结构（向后兼容）: replay_buffer/realtime/{market_type}/l2/
    candidate_dirs = []
    base = os.path.join(current_dir, "replay_buffer", "realtime")
    if os.path.isdir(base):
        for entry in os.listdir(base):
            p = os.path.join(base, entry)
            if not os.path.isdir(p):
                continue
            # 新结构: {exchange}/{market}/l2
            for sub in os.listdir(p):
                l2 = os.path.join(p, sub, "l2")
                if os.path.isdir(l2):
                    candidate_dirs.append(l2)
            # 旧结构: {market}/l2
            l2_legacy = os.path.join(p, "l2")
            if os.path.isdir(l2_legacy):
                candidate_dirs.append(l2_legacy)
    # 兜底
    for legacy in [
        os.path.join(current_dir, "replay_buffer", "realtime", "l2"),
        os.path.join(current_dir, "data", "realtime", "l2"),
    ]:
        if os.path.isdir(legacy) and legacy not in candidate_dirs:
            candidate_dirs.append(legacy)

    raw_dirs = candidate_dirs

    official_dir = os.path.join(current_dir, "replay_buffer", "official_validation")
    cold_dir = os.path.join(current_dir, "replay_buffer", "cold")
    retain_days = int(os.environ.get("NARCI_RETAIN_DAYS", "7"))

    if not raw_dirs:
        print(f"❌ 原始数据目录不存在，已检查以下路径:")
        for d in candidate_dirs:
            print(f"  - {d}")
        return

    # 如果 symbol=ALL：按 raw_dir 分组发现，避免跨交易所同名 symbol collision
    # （例如 BTCJPY 同时存在 binance/spot 全球版 和 binance_jp/spot 日本版，
    # 之前只 break 在第一个匹配 dir 上，第二个 dir 永远 silently 跳过）
    if symbol.upper() == "ALL":
        per_dir_syms: dict[str, list[str]] = {}
        for raw_dir in raw_dirs:
            local = set()
            for f in os.listdir(raw_dir):
                m = re.match(r"([A-Za-z0-9_]+)_RAW_\d{8}_\d{6}\.parquet$", f)
                if m:
                    local.add(m.group(1).upper())
            if local:
                per_dir_syms[raw_dir] = sorted(local)

        if not per_dir_syms:
            print("⚠️ 未发现任何交易对的 RAW 文件。")
            return

        total = sum(len(v) for v in per_dir_syms.values())
        print(f"🔍 [自动发现] {len(per_dir_syms)} 个 raw_dir，共 {total} 个 (sym × venue) 组合")
        for raw_dir, syms in per_dir_syms.items():
            print(f"  📂 {raw_dir} → {syms}")

        for raw_dir, syms in per_dir_syms.items():
            for sym in syms:
                _compact_one_dir(sym, target_date, raw_dir, official_dir,
                                 cold_dir, retain_days)
        return

    # 单 symbol 模式：遍历所有匹配的 raw_dir，每个都跑一遍
    matched_dirs = []
    for d in raw_dirs:
        files_in_dir = os.listdir(d)
        if any(re.match(rf"{symbol}_RAW_", f, re.IGNORECASE) for f in files_in_dir):
            matched_dirs.append(d)

    if not matched_dirs:
        print(f"⚠️ 未在任何数据目录中找到 {symbol} 的 RAW 文件。")
        return
    if len(matched_dirs) > 1:
        print(f"ℹ️ {symbol} 在 {len(matched_dirs)} 个 raw_dir 中存在，全部 compact:")
        for d in matched_dirs:
            print(f"  📂 {d}")

    for raw_dir in matched_dirs:
        _compact_one_dir(symbol, target_date, raw_dir, official_dir,
                         cold_dir, retain_days)
    return


def _compact_one_dir(symbol, target_date, raw_dir, official_dir, cold_dir, retain_days):
    """Compact one (symbol, raw_dir) combination across all dates with fragments."""
    from data.daily_compactor import DailyCompactor
    symbol = symbol.upper()

    # 扫描指定交易对的 1min RAW 文件
    files = os.listdir(raw_dir)
    # 修复：加入 re.IGNORECASE，忽略大小写，兼容 recorder 生成的 ethusdt_RAW...
    date_pattern = re.compile(rf"{symbol}_RAW_(\d{{8}})_\d{{6}}\.parquet", re.IGNORECASE)
    
    dates_found = set()
    file_counts = {}
    
    # 自动 case-fix：只在 full prefix 仅大小写不同时才 rename
    # （避免 split('_')[0] 把多 token 符号误删 - 历史上 BTC_JPY 被 split
    # 成 "BTC" 然后 prepend 整个 "BTC_JPY"，把文件改成 BTC_JPY_jpy_RAW_...）
    full_prefix_pattern = re.compile(
        rf"^([A-Za-z0-9_]+)_RAW_\d{{8}}_\d{{6}}\.parquet$"
    )
    for f in files:
        match = date_pattern.match(f)
        if match:
            m_full = full_prefix_pattern.match(f)
            if m_full:
                full_prefix = m_full.group(1)
                if (full_prefix != symbol.upper()
                        and full_prefix.upper() == symbol.upper()):
                    new_f = symbol.upper() + f[len(full_prefix):]
                    os.rename(os.path.join(raw_dir, f),
                              os.path.join(raw_dir, new_f))
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
        target_dt = datetime.strptime(d_str, "%Y%m%d").date()

        # 推断 save_interval_sec：从文件名 HHMMSS 的相邻间隔取中位数
        times_sec = []
        for f in files:
            m = re.match(rf"{symbol}_RAW_{d_str}_(\d{{6}})\.parquet", f, re.IGNORECASE)
            if m:
                hms = m.group(1)
                times_sec.append(int(hms[:2]) * 3600 + int(hms[2:4]) * 60 + int(hms[4:6]))
        times_sec.sort()
        if len(times_sec) >= 2:
            diffs = sorted(times_sec[i + 1] - times_sec[i] for i in range(len(times_sec) - 1))
            interval_sec = max(1, diffs[len(diffs) // 2])
            expected = max(1, round(86400 / interval_sec))
            interval_label = f"{interval_sec}s"
        else:
            expected = count
            interval_label = "?"
        completion_rate = (count / expected) * 100 if expected else 0.0

        print(f"\n📅 【处理日期】: {target_dt.strftime('%Y-%m-%d')}")
        print(f"📦 【碎片完整度】: 发现 {count}/{expected} 个 {interval_label} 切片 (完整率: {completion_rate:.1f}%)")

        if completion_rate < 99.0 and expected > 1:
            print(f"⚠️ 警告: 存在部分时段掉线或缺失！(约 {expected - count} 个 {interval_label} 切片缺失)")
            
        # 从 raw_dir 推断 exchange + market_type (仅对新结构有效)
        #   replay_buffer/realtime/{exchange}/{market}/l2/
        path_parts = os.path.normpath(raw_dir).split(os.sep)
        exchange, market_type = None, "um_futures"
        if len(path_parts) >= 3 and path_parts[-1] == "l2":
            market_type = path_parts[-2]
            if len(path_parts) >= 4 and path_parts[-3] != "realtime":
                exchange = path_parts[-3]

        # 只有 Binance 能用 Binance Vision 做交叉校验；其他交易所传 None 跳过
        source = None
        if exchange in (None, "binance"):
            try:
                from data.historical import get_source
                source = get_source("binance_vision")
            except Exception:
                source = None

        compactor = DailyCompactor(
            symbol=symbol.upper(),
            target_date=target_dt,
            raw_dir=raw_dir,
            official_dir=official_dir,
            cold_dir=cold_dir,
            retain_days=retain_days,
            source=source,
            market_type=market_type,
            exchange=exchange,
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

    # 5. Download 命令
    parser_download = subparsers.add_parser("download", help="从 Binance Vision 批量下载历史数据 (spot + futures)")
    parser_download.add_argument("--config", type=str, default="configs/downloader.yaml", help="下载器配置文件路径")

    # 6. Build-Cache 命令
    parser_cache = subparsers.add_parser("build-cache", help="离线聚合 RAW 数据并调用 FeatureBuilder 构建特征缓存")
    parser_cache.add_argument("--market", type=str, default="um_futures", help="市场类型 (默认: um_futures)")
    parser_cache.add_argument("--symbol", type=str, default="ETHUSDT", help="交易对 (默认: ETHUSDT，输入 ALL 处理所有)")
    parser_cache.add_argument("--dir", type=str, default=os.path.join(current_dir, "replay_buffer", "realtime"), help="基础数据目录")

    # 7. Tardis Download 命令
    parser_tardis = subparsers.add_parser("tardis", help="从 Tardis.dev 下载 L2 depth 数据")
    parser_tardis.add_argument("--symbol", type=str, required=True, help="交易对 (如 ETHUSDT)")
    parser_tardis.add_argument("--start", type=str, required=True, help="起始日期 (如 2025-09-01)")
    parser_tardis.add_argument("--end", type=str, required=True, help="结束日期 (如 2025-09-30)")
    parser_tardis.add_argument("--market", type=str, default="um_futures", help="市场类型 (默认: um_futures)")
    parser_tardis.add_argument("--output-dir", type=str, default=os.path.join(current_dir, "replay_buffer", "tardis"), help="输出目录")

    # 8. Merge 命令 (合并 Tardis depth + Binance aggTrades -> Narci RAW)
    parser_merge = subparsers.add_parser("merge", help="合并 Tardis depth 与 Binance aggTrades 为 Narci RAW 格式")
    parser_merge.add_argument("--symbol", type=str, default=None, help="交易对 (不填则自动发现所有)")
    parser_merge.add_argument("--start", type=str, default=None, help="起始日期")
    parser_merge.add_argument("--end", type=str, default=None, help="结束日期")
    parser_merge.add_argument("--market", type=str, default="um_futures", help="市场类型 (默认: um_futures)")
    parser_merge.add_argument("--tardis-dir", type=str, default=os.path.join(current_dir, "replay_buffer", "tardis"), help="Tardis 数据目录")
    parser_merge.add_argument("--aggtrades-dir", type=str, default=os.path.join(current_dir, "replay_buffer", "parquet"), help="aggTrades 数据目录")
    parser_merge.add_argument("--output-dir", type=str, default=os.path.join(current_dir, "replay_buffer", "merged", "um_futures", "l2"), help="合并输出目录")

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
    elif args.command == "download":
        from data.download import BinanceDownloader
        downloader = BinanceDownloader(config_path=args.config)
        downloader.run()
    elif args.command == "build-cache":
        cmd_build_cache(args.market, args.symbol, args.dir)
    elif args.command == "tardis":
        from data.tardis_downloader import TardisDownloader
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        dl = TardisDownloader(output_dir=args.output_dir)
        files = dl.download_range(args.symbol, args.start, args.end, args.market)
        print(f"\n{'='*50}")
        print(f"Tardis download complete: {len(files)} files saved")
    elif args.command == "merge":
        from data.format_converter import FormatConverter
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        converter = FormatConverter(
            tardis_dir=args.tardis_dir,
            aggtrades_dir=args.aggtrades_dir,
            output_dir=args.output_dir,
        )
        if args.symbol and args.start and args.end:
            files = converter.merge_range(args.symbol, args.start, args.end, args.market)
        else:
            files = converter.scan_and_merge_all(args.market)
        print(f"\n{'='*50}")
        print(f"Merge complete: {len(files)} RAW files created")
    else:
        parser.print_help()

if __name__ == "__main__":
    main()