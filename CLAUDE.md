# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Narci is a quantitative data capture, L2 orderbook reconstruction, and backtesting platform focused on Binance markets (spot and USDT-margined futures). The project is written in Chinese comments throughout. Core capabilities:

- **Real-time L2 recording**: WebSocket-based multi-symbol depth snapshot + aggTrade capture at 100ms granularity
- **Orderbook reconstruction**: Rebuild millisecond-level L2 orderbook from incremental events, compute microstructure factors (mid price, imbalance, spread)
- **Data validation**: Cross-validate local recordings against Binance official historical archives to detect packet loss
- **Backtesting**: Event-driven L2-depth backtesting with simulated broker supporting maker/taker orders and leverage

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Launch Streamlit GUI dashboard
python main.py gui

# Start L2 recorder (default: configs/um_future_recorder.yaml)
python main.py record
python main.py record --config configs/spot_recorder.yaml
python main.py record --symbol DOGEUSDT

# Compact daily data + cross-validate against Binance archives
python main.py compact --symbol ETHUSDT
python main.py compact --symbol ALL
python main.py compact --symbol ETHUSDT --date 2026-02-22

# Build offline feature cache for fast backtesting
python main.py build-cache --market um_futures --symbol ETHUSDT

# Docker deployment (cloud recording + rclone push to Google Drive)
docker compose up -d
```

## Architecture

### Entry Point

`main.py` — Central CLI dispatcher with subcommands: `gui`, `record`, `compact`, `build-cache`.

### Data Pipeline (`data/`)

- `l2_recorder.py` — `BinanceL2Recorder`: Async WebSocket engine that maintains an in-memory orderbook state machine per symbol. On each save interval, it atomically drains the buffer and injects a full snapshot into the next buffer (self-contained files). Supports rclone cloud sync and auto-cleanup of old files via `retain_days`.
- `l2_reconstruct.py` — `L2Reconstructor`: Rebuilds L2 orderbook from raw parquet files. Core method `process_dataframe()` uses NumPy arrays (not itertuples) for 5-10x speedup. Supports two modes: sampled 100ms snapshots (for backtesting) and per-trade L3 alignment.
- `daily_compactor.py` — `DailyCompactor`: Merges 1-minute fragment parquet files into daily archives using PyArrow Dataset, downloads Binance official aggTrades for cross-validation, archives to cold storage, and cleans old fragments.
- `feature_builder.py` — `FeatureBuilder`: Generates derived alpha features (EMA imbalance, volatility, momentum flags). Has both offline vectorized (`build_offline`) and online streaming (`build_online`) modes.
- `download.py` / `validator.py` / `third_party.py` — Data download and validation utilities.

### Backtesting (`backtest/`)

- `backtest.py` — `BacktestEngine` and `JitBacktestEngine`. JIT variant performs on-the-fly L2 reconstruction from raw files with MD5-based caching to avoid reprocessing the same file set.
- `broker.py` — `SimulatedBroker`: U-margined futures simulator with leverage, maker/taker fee differentiation, limit order matching against L2 depth, and market order sweep across multiple price levels.
- `strategy.py` — `BaseStrategy`: Inherit and implement `on_tick(tick)` and `on_finish()`.

### GUI (`gui/`)

Streamlit-based dashboard with panels for L2 depth visualization, cold data browsing, backtesting, and settings. Entry point: `gui/dashboard.py`.

### Deployment (`deploy/`)

- `entrypoint.sh` — Docker entrypoint that auto-generates rclone config from env vars, creates remote Google Drive folders, and launches supervisord.
- `supervisord.conf` — Runs recorder + healthcheck processes.
- `healthcheck.py` — HTTP health endpoint on port 8079.

### Config (`configs/`)

YAML-driven configuration. Key files:
- `um_future_recorder.yaml` / `spot_recorder.yaml` — Recorder settings (symbols, market_type, save_interval_sec, endpoints)
- `broker.yaml` — Broker parameters (initial_cash, fees)
- `backtest.yaml` — Backtest + feature builder settings
- `account.yaml` — Sensitive credentials (gitignored)

### Data Format

Raw parquet files have 4 columns: `timestamp`, `side`, `price`, `quantity`.
- side 0/1 = bid/ask depth update, 2 = aggTrade (negative qty = seller maker), 3/4 = snapshot bid/ask injection.
- Files are named `{SYMBOL}_RAW_{YYYYMMDD}_{HHMMSS}.parquet`, daily compacted files end with `_DAILY.parquet`.
- Reconstructed feature files add columns: `mid_price`, `imbalance`, `spread`, `taker_buy_vol`, `taker_sell_vol`, plus depth levels `b_p_N`/`b_q_N`/`a_p_N`/`a_q_N`.

### Environment Variables (Docker)

- `NARCI_CONFIG` — Config file path override
- `NARCI_SYMBOL` — Single symbol override
- `NARCI_RCLONE_REMOTE` — rclone destination (e.g., `gdrive:/narci_raw`)
- `RCLONE_GDRIVE_TOKEN` — Google Drive OAuth token JSON
- `RCLONE_GDRIVE_FOLDER_ID` — Google Drive folder ID
- `NARCI_RETAIN_DAYS` — Days to retain local parquet files before cleanup
