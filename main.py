import argparse
import sys
import os
import asyncio

# 定义命令处理函数
def run_downloader(args):
    print(">>> 启动历史数据下载模块...")
    from data.download import BinanceDownloader
    # 假设 config 路径可以从 args 传入，这里默认
    downloader = BinanceDownloader(config_path="./configs/downloader.yaml")
    downloader.run()

def run_l2_recorder(args):
    print(f">>> 启动 L2 实时录制模块 (Symbol: {args.symbol})...")
    from data.l2_recoder import BinanceL2Recorder
    recorder = BinanceL2Recorder(symbol=args.symbol)
    try:
        asyncio.run(recorder.start())
    except KeyboardInterrupt:
        print("\n录制已停止")

def run_backtest(args):
    print(">>> 启动命令行回测模式...")
    from backtest.backtest import BacktestEngine
    from backtest.strategy import MyTrendStrategy
    
    if not os.path.exists(args.data):
        print(f"错误: 数据文件 {args.data} 不存在")
        return

    print(f"策略: TrendStrategy (Window={args.window})")
    print(f"数据: {args.data}")
    
    strategy = MyTrendStrategy(window_size=args.window)
    engine = BacktestEngine(args.data, strategy, initial_cash=args.cash)
    engine.run()

def run_gui(args):
    print(">>> 启动可视化终端...")
    # Streamlit 需要通过 shell 命令启动
    cmd = f"streamlit run gui/dashboard.py"
    os.system(cmd)

def main():
    parser = argparse.ArgumentParser(description="Narci Quantitative Trading System")
    subparsers = parser.add_subparsers(dest='command', help='可用命令')

    # 1. Download 命令
    parser_dl = subparsers.add_parser('download', help='下载历史数据')
    
    # 2. Record 命令
    parser_rec = subparsers.add_parser('l2record', help='录制实时 L2 数据')
    parser_rec.add_argument('--symbol', type=str, default='ETHUSDT', help='交易对名称 (例如 ETHUSDT)')

    # 3. GUI 命令
    parser_gui = subparsers.add_parser('gui', help='启动可视化 Dashboard')

    # 4. Backtest 命令
    parser_bt = subparsers.add_parser('backtest', help='运行策略回测')
    parser_bt.add_argument('--data', type=str, required=True, help='回测数据文件路径 (.parquet)')
    parser_bt.add_argument('--cash', type=float, default=10000.0, help='初始资金')
    parser_bt.add_argument('--window', type=int, default=100, help='策略参数: 均线窗口')

    args = parser.parse_args()

    if args.command == 'download':
        run_downloader(args)
    elif args.command == 'l2record':
        run_l2_recorder(args)
    elif args.command == 'gui':
        run_gui(args)
    elif args.command == 'backtest':
        run_backtest(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()