# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Narci is a quantitative data capture, L2 orderbook reconstruction, and backtesting platform targeting multiple crypto exchanges. Primary markets:

- **Coincheck** (Japan, spot only, zero fees on main JPY pairs) — intended as the main live-trading venue
- **Binance Spot** (including JPY pairs) — secondary market
- **Binance U-margined Futures** — reference / hedging / signal source (no JPY pairs available)

Chinese comments throughout. Core capabilities:

- **Real-time L2 recording**: WebSocket-based multi-symbol depth + trades capture via exchange-adapter abstraction
- **Orderbook reconstruction**: Rebuild millisecond-level L2 orderbook from incremental events, compute microstructure factors
- **Data validation**: Cross-validate local recordings against official historical archives (Binance Vision) when supported
- **Historical data**: Pluggable sources — Binance Vision (free zip archives) and Tardis.dev (paid, for L2 depth including Coincheck)
- **Backtesting**: Event-driven L2-depth backtesting with spot (no leverage) and futures (leveraged, bidirectional) brokers

## Commands

```bash
pip install -r requirements.txt
python main.py gui                           # Streamlit dashboard

# Recording — one process per exchange/market
python main.py record --config configs/coincheck_recorder.yaml
python main.py record --config configs/spot_recorder.yaml
python main.py record --config configs/um_future_recorder.yaml
python main.py record --symbol DOGEUSDT      # single-symbol override

# Historical data
python main.py download                      # Binance Vision batch
python main.py tardis --symbol ETHUSDT --start 2025-09-01 --end 2026-03-01
python main.py merge --symbol ETHUSDT --start 2025-09-01 --end 2026-03-01

# Compact + (optional) cross-validate
python main.py compact --symbol ETHUSDT
python main.py compact --symbol ALL

# Offline feature cache
python main.py build-cache --market um_futures --symbol ETHUSDT

# Docker (runs all 3 recorders + rclone sidecar)
docker compose up -d
```

## Architecture

### Entry Point

`main.py` — CLI dispatcher with subcommands: `gui`, `record`, `compact`, `cloud-sync`, `download`, `tardis`, `merge`, `build-cache`.

### Exchange Adapter Layer (`data/exchange/`)

Abstracts exchange-specific details so the recorder, downloader, and validator are exchange-neutral.

- `base.py` — `ExchangeAdapter` ABC. Defines contract for `ws_url()`, `fetch_snapshot()`, `parse_message()`, `standardize_event()`, alignment hooks, and symbol-format conversion. **Side encoding (enforced by ABC):** `0`=bid update, `1`=ask update, `2`=aggTrade (negative qty = seller maker), `3`=bid snapshot, `4`=ask snapshot. Timestamps in milliseconds.
- `binance.py` — `BinanceAdapter` / `BinanceSpotAdapter` / `BinanceUmFuturesAdapter`. Implements U/u sequence alignment.
- `coincheck.py` — `CoincheckAdapter`. Spot only. No update-id alignment; uses REST `/api/order_books` for initial snapshot then consumes diff via WS channels `{pair}-orderbook` and `{pair}-trades`. **Trade messages are list-of-lists** (`[[ts_s, id, pair, rate, amount, type, ...], ...]`) — a single WS frame can carry multiple trades.
- `__init__.py` — `get_adapter(name, market_type=..., **kwargs)` factory.

### Historical Source Layer (`data/historical/`)

Unified interface for offline historical data downloading.

- `base.py` — `HistoricalSource` ABC: `download_day()`, `verify()`, `supports()`.
- `binance_vision.py` — `BinanceVisionSource`. Downloads ZIP from `data.binance.vision`, converts to parquet, validates via official `.CHECKSUM` (MD5).
- `tardis.py` — `TardisSource`. Supports L2 depth (snapshot + diff) and aggTrades; works for both Binance and Coincheck. Direct output in Narci 4-column format.

### Data Pipeline (`data/`)

- `l2_recorder.py` — `L2Recorder` (alias `BinanceL2Recorder` kept for back-compat). Exchange-neutral; delegates all exchange specifics to its `ExchangeAdapter`. Maintains in-memory orderbook state machine, on every save interval atomically drains buffer and injects a full snapshot (side=3/4) into the next buffer so every file is self-contained. Graceful shutdown via `asyncio.Event`.
- `l2_reconstruct.py` — `L2Reconstructor`. Rebuilds L2 orderbook from raw parquet files.
- `daily_compactor.py` — `DailyCompactor`. Exchange-neutral: accepts an optional `HistoricalSource` for cross-validation. Coincheck / exchanges without official archives skip validation gracefully.
- `feature_builder.py` — `FeatureBuilder`. Offline + online alpha feature modes.
- `cloud_sync.py` — Independent `CloudSyncDaemon` for rclone-based Google Drive push.
- `download.py` — `HistoricalDownloader` (alias `BinanceDownloader`). Delegates to any registered `HistoricalSource`.
- `validator.py` — `DataValidator` (alias `BinanceDataValidator`). Exchange-neutral DataFrame business-rule checks.
- `tardis_downloader.py` / `format_converter.py` — Tardis.dev + Binance Vision merging pipeline for historical L2 reconstruction.

### Backtesting (`backtest/`)

- `backtest.py` — `BacktestEngine` and `JitBacktestEngine`. JIT variant performs on-the-fly L2 reconstruction from raw files with MD5-based caching.
- `broker.py` — Three-level class hierarchy:
  - `BaseBroker` — shared L1/L2 update, limit-order maker matching, market sweep, record/history queries.
  - `SpotBroker` — no leverage, no short selling, holds positive base-asset position; default fee 0 (Coincheck JPY zero-fee).
  - `SimulatedBroker` — bidirectional U-margined futures with leverage, margin-aware order sizing, realized PnL on offsetting fills.
- `strategy.py` — `BaseStrategy`: inherit and implement `on_tick(tick)` and `on_finish()`.

### GUI (`gui/`)

Streamlit dashboard. Panels auto-discover `realtime/{exchange}/{market}/l2/` directories (falls back to legacy `realtime/{market}/l2/`). Entry point: `gui/dashboard.py`.

### Deployment (`deploy/`)

- `entrypoint.sh` — Container entrypoint. rclone is no longer installed here (moved to separate sidecar).
- `supervisord.conf` — Runs recorder + healthcheck.
- `healthcheck.py` — HTTP `/health` on port 8079.
- `server-aliases.sh` — SSH aliases: `nstart`, `nstop`, `nlog{cc,spot,umfut}`, `nrestart{cc,spot,umfut}`, `nhealth{cc,spot,umfut}`, `npull`, `nsynclog`, etc.

### Docker Topology

Four containers orchestrated by `docker-compose.yaml`:

| Container | Config | Host Port |
|---|---|---|
| `narci-recorder-coincheck` | `configs/coincheck_recorder.yaml` | 8081 |
| `narci-recorder-spot` (Binance) | `configs/spot_recorder.yaml` | 8079 |
| `narci-recorder-umfut` (Binance) | `configs/um_future_recorder.yaml` | 8080 |
| `narci-cloud-sync` | rclone sidecar | — |

All recorders share the `narci-data` named volume; cloud-sync mounts it read-only and pushes to Google Drive every `SYNC_INTERVAL` seconds.

### Config (`configs/`)

```
configs/
├── exchanges/                          # Canonical recorder configs
│   ├── binance_spot.yaml
│   ├── binance_um_futures.yaml
│   └── coincheck_spot.yaml
├── spot_recorder.yaml         -> exchanges/binance_spot.yaml          (symlink, legacy name)
├── um_future_recorder.yaml    -> exchanges/binance_um_futures.yaml    (symlink)
├── coincheck_recorder.yaml    -> exchanges/coincheck_spot.yaml        (symlink)
├── broker.yaml                         # Broker parameters
├── backtest.yaml                       # Backtest + feature builder
├── downloader.yaml                     # Historical downloader (source, date range, symbols)
├── tardis.yaml                         # Tardis.dev usage notes
└── account.yaml                        # Sensitive credentials (gitignored)
```

Recorder config keys:
- `exchange`: `binance` | `coincheck` (default: binance)
- `market_type`: `spot` | `um_futures`
- `symbols`: list of native symbols (e.g. `BTCUSDT` for Binance, `btc_jpy` for Coincheck)
- `save_dir`, `save_interval_sec`, `retain_days`, `interval_ms`, `retry`, `endpoints`

### Data Format

4-column parquet: `timestamp` (ms), `side` (0-4), `price` (float), `quantity` (float, may be negative for seller-maker trades).

Directory layout: `replay_buffer/realtime/{exchange}/{market_type}/l2/{SYMBOL}_RAW_{YYYYMMDD}_{HHMMSS}.parquet`. Daily compacted: `*_DAILY.parquet`.

Reconstructed feature files (via nyx's `L2Reconstructor`) add: `mid_price`, `imbalance`, `spread`, `taker_buy_vol`, `taker_sell_vol`, plus depth levels `b_p_N`/`b_q_N`/`a_p_N`/`a_q_N`.

### Environment Variables (Docker)

- `NARCI_CONFIG` — Config path override
- `NARCI_SYMBOL` — Single symbol override
- `NARCI_RCLONE_REMOTE` — rclone destination (e.g., `gdrive:/narci_raw`)
- `RCLONE_GDRIVE_TOKEN` — Google Drive OAuth token JSON
- `RCLONE_GDRIVE_FOLDER_ID` — Google Drive folder ID
- `NARCI_RETAIN_DAYS` — Local retention days before auto-cleanup
- `SYNC_INTERVAL` — rclone sync period (sec, default 300)
- `TARDIS_API_KEY` — Tardis.dev API key for historical downloads
