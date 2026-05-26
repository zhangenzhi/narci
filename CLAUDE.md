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

# Docker (runs all 3 recorders + rclone sidecar)
docker compose up -d
```

## Architecture

### Entry Point

`main.py` — CLI dispatcher with subcommands: `gui`, `record`, `compact`, `cloud-sync`, `download`, `tardis`, `merge`.

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

- `l2_recorder.py` — `L2Recorder`. Exchange-neutral; delegates all exchange specifics to its `ExchangeAdapter`. **Crash-safe via segment WAL (`data/wal.py`)**: events stream into a per-symbol in-memory micro-buffer that `wal_loop` atomically flushes (`.tmp`→`os.replace`) to small `.segwal` segments every `wal_flush_interval_sec` (default 2s); `save_loop` every `save_interval_sec` merges a symbol's segments into one self-contained `{SYMBOL}_RAW_*.parquet` (with an injected side=3/4 snapshot) and deletes the segments. Hard-crash loss window is thus ~seconds, not one save interval; `recover_orphans()` on startup merges any leftover segments. WAL lives in a sibling `replay_buffer/wal/` tree with a `.segwal` extension so it's invisible to all `*.parquet`/`*_RAW_*` scanners (GUI, compactor, main). Circuit-breaker flushes the WAL before `os._exit`. Graceful shutdown via `asyncio.Event`.
- `l2_reconstruct.py` — `L2Reconstructor`. Rebuilds L2 orderbook from raw parquet files. The sampling decision ("when to emit a feature row") is delegated to a `Sampler` (see `sampling.py`); `process_dataframe(sample_interval_ms=…)` stays back-compatible.
- `sampling.py` — `Sampler` ABC + `FixedGridSampler` (global-aligned time grid; `1000ms`≡`1s_grid`) + `EventSampler` (event_at_* modes) + `make_sampler()`. The reproducibility-critical global grid; distinct from `features/realtime`'s relative perf-throttle (`metric_sample_interval_ms`), which is left as-is.
- `daily_compactor.py` — `DailyCompactor`. Exchange-neutral: accepts an optional `HistoricalSource` for cross-validation. Coincheck / exchanges without official archives skip validation gracefully.
- `cloud_sync.py` — Independent `CloudSyncDaemon` for rclone-based Google Drive push.
- `download.py` — `HistoricalDownloader`. Delegates to any registered `HistoricalSource`.
- `validator.py` — `DataValidator`. Exchange-neutral DataFrame business-rule checks.
- `_io.py` / `_config.py` — shared data-module utilities (single source for parquet read/write incl. atomic write, and YAML section loading).
- The online feature builder is `features/realtime.py` (`FeatureBuilder`, `FEATURES_VERSION`-pinned, 38-feature v6) — consumed by simulation/calibration/research.
- `tardis_downloader.py` / `format_converter.py` — Tardis.dev + Binance Vision merging pipeline for historical L2 reconstruction.

### Backtesting / Matching (`simulation/` + `calibration/`)

The single matching engine is `simulation/maker_broker.py` `MakerSimBroker` — a
maker-order simulator (FIFO queue-ahead, price-penetration fills, post-only,
spot inventory reservation, cancel latency) that doubles as the reference
implementation for the echo live broker. `simulation/backtest_alpha.py` replays
an `AlphaModel` over cold-tier days through it; `calibration/replay.py`
reconciles its output against echo's real fills. Behaviour is locked by
`tests/simulation/` (see its README).

`backtest/` is reduced to `symbol_spec.py` (`SymbolSpec` tick/lot/notional,
shared by simulation/calibration). The legacy `BacktestEngine`/`JitBacktestEngine`/
`EventBacktestEngine` + naive `broker.py` + `orderbook.py`/`venue_registry.py`/
`strategy.py` and the `build-cache` CLI were removed in P4 (see
`docs/REFACTOR_DESIGN.md`).

### GUI (`gui/`)

Streamlit dashboard with 4 tabs (L1 行情 / L2 盘口洞察 / 冷数据仓库 / 系统设置).
Panels auto-discover `realtime/{exchange}/{market}/l2/` directories (falls back to
legacy `realtime/{market}/l2/`). Entry point: `gui/dashboard.py`. (The legacy
backtest panel was removed in P4; backtesting now runs via simulation/calibration.)

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
- `SYNC_INTERVAL` — rclone sync period (sec, default 3600 = 1h)
- `TARDIS_API_KEY` — Tardis.dev API key for historical downloads
