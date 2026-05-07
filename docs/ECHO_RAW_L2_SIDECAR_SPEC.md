# Echo In-Process Raw L2 Sidecar — Minimal Spec

> 由 narci 2026-05-06 起草，回应 echo session 反馈 §3.1。
> 选项 B：echo 自己写 ~80 行 sidecar，narci 提供 schema + 行为契约。
> 不引入 narci.data.l2_recorder（避开 yaml/retain/cleanup 等无关依赖）。

---

## 1. 目的

Echo 在交易进程内同时录制 raw L2 stream，写到 `{session_dir}/raw_l2/`，calibrate_session 读这份做 sub-ms 精度的回测对账。

**为什么不复用 narci recorder**：narci 跑在 host A，echo 跑在 host B → 5-15 ms WS jitter。同进程录制 → 0 jitter（共享 WS 帧）。

---

## 2. Schema（narci 强制）

```python
from narci.calibration.schema import RAW_L2_SCHEMA, RAW_L2_FILENAME_TEMPLATE

# RAW_L2_SCHEMA：
# pa.field("timestamp", pa.int64(),  nullable=False),  # ms since epoch
# pa.field("side",      pa.int8(),   nullable=False),  # 0..4
# pa.field("price",     pa.float64(),nullable=False),
# pa.field("quantity",  pa.float64(),nullable=False),
```

Side 编码（必须严格匹配 narci recorder）：

| value | meaning | qty |
|---|---|---|
| 0 | bid update | `0` = delete level |
| 1 | ask update | `0` = delete level |
| 2 | aggTrade | signed: > 0 buyer maker, < 0 seller maker |
| 3 | bid snapshot | always positive |
| 4 | ask snapshot | always positive |

Timestamps in **milliseconds since epoch** (not ns) — match narci recorder convention.

---

## 3. 行为契约

### 3.1 文件位置 & 命名

```
{echo_session_dir}/raw_l2/{SYMBOL}_RAW_{YYYYMMDD}_{HHMMSS}.parquet
```

`{SYMBOL}` 用 echo native 大写（`BTC_JPY` 不是 `btc_jpy`）。

### 3.2 Snapshot 注入（每个 shard 自包含）

每个 parquet shard 必须以 **完整 book snapshot** 开头：

```
shard 内容顺序：
  [bid snapshot rows side=3]    ← 全部 bid levels at shard start
  [ask snapshot rows side=4]
  [incremental updates side=0/1/2 直到 flush]
```

注入时机：
- 每个 shard 第一行（flush_loop 唤醒时，主动从 reconstructor 状态拍照）
- 或：跟着真实 snapshot WS 推送（如果交易所发）

**关键**：`L2Reconstructor.replay_dataframe(shard)` 必须能从单个 shard 重建出当前 book — 测试方法见 §6。

### 3.3 Flush 节奏

- 每 60 秒 flush 一次（与 narci recorder 一致）
- 也可以每 N 个 events flush（防长尾积压）
- Atomic 写：`.tmp` → `os.replace`（与 EchoLogWriter 一致）

### 3.4 Multi-symbol

每个 symbol 独立 shard 文件。Echo 为多个 symbol 录时：
```
raw_l2/BTC_JPY_RAW_20260506_120000.parquet
raw_l2/BTCUSDT_RAW_20260506_120000.parquet
raw_l2/BTCJPY_RAW_20260506_120000.parquet
```

---

## 4. 参考实现（约 80 行）

```python
# echo/recording/raw_l2_sidecar.py
"""In-process raw L2 recorder. Subscribes to the same WS bus as the strategy
+ engine, batches events into parquet shards. Schema-locked to
narci.calibration.schema.RAW_L2_SCHEMA."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from narci.calibration.schema import RAW_L2_SCHEMA

log = logging.getLogger(__name__)


class RawL2Sidecar:
    def __init__(self, session_dir: Path, flush_interval_sec: float = 60.0):
        self.dir = Path(session_dir) / "raw_l2"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.flush_interval_sec = flush_interval_sec
        # buffer: symbol → list[(ts_ms, side, price, qty)]
        self._buf: dict[str, list[tuple]] = {}
        self._closed = False

    # -- producer side --------------------------------------------------- #
    def on_event(self, symbol: str, ts_ms: int, side: int,
                 price: float, qty: float) -> None:
        """Called by the WS bus on every L2/trade event for symbol."""
        if self._closed:
            return
        self._buf.setdefault(symbol, []).append(
            (int(ts_ms), int(side), float(price), float(qty))
        )

    def inject_snapshot(self, symbol: str, ts_ms: int,
                        bids: list[tuple[float, float]],
                        asks: list[tuple[float, float]]) -> None:
        """Inject a full-book snapshot. Must be called at shard start so
        every flushed shard is self-contained.
        bids / asks: list of (price, qty)."""
        if self._closed:
            return
        rows = self._buf.setdefault(symbol, [])
        for p, q in bids:
            rows.append((int(ts_ms), 3, float(p), float(q)))
        for p, q in asks:
            rows.append((int(ts_ms), 4, float(p), float(q)))

    # -- background flusher --------------------------------------------- #
    async def run_flush_loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.sleep(self.flush_interval_sec)
            except asyncio.CancelledError:
                break
            try:
                self.flush()
            except Exception:
                log.exception("raw_l2 flush error (continuing)")

    def flush(self) -> None:
        if not self._buf:
            return
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        for symbol, rows in list(self._buf.items()):
            if not rows:
                continue
            tbl = pa.Table.from_pydict({
                "timestamp": [r[0] for r in rows],
                "side":      [r[1] for r in rows],
                "price":     [r[2] for r in rows],
                "quantity":  [r[3] for r in rows],
            }, schema=RAW_L2_SCHEMA)
            fname = f"{symbol}_RAW_{ts_str}.parquet"
            path = self.dir / fname
            tmp = path.with_suffix(".parquet.tmp")
            try:
                pq.write_table(tbl, tmp)
                os.replace(tmp, path)
                self._buf[symbol].clear()
            except Exception:
                log.exception("write %s failed; keeping %d rows", path, len(rows))
                if tmp.exists():
                    try: tmp.unlink()
                    except OSError: pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.flush()
        except Exception:
            log.exception("final flush error")
```

---

## 5. 集成到 echo

### 5.1 启动

```python
# echo/main.py 启动时
sidecar = RawL2Sidecar(session_dir=Path(sid))
asyncio.create_task(sidecar.run_flush_loop())

# 把 sidecar.on_event 注册到你的 WS bus / engine 的 raw event hook
# 同一帧 → strategy.on_event + sidecar.on_event 双 consumer

# REST snapshot bootstrap 时（启动 / 重连后）：
sidecar.inject_snapshot(
    symbol="BTC_JPY",
    ts_ms=int(time.time() * 1000),
    bids=[(p, q) for p, q in snapshot["bids"]],
    asks=[(p, q) for p, q in snapshot["asks"]],
)
```

### 5.2 每 N 秒主动 inject snapshot（推荐）

为保证 shard 自包含，每个 flush 间隔开始时 inject 一份从 reconstructor 拍的 snapshot：

```python
# 自定义 RawL2Sidecar 之外的 task：
async def periodic_snapshot_injection(reconstructor, sidecar, interval=60):
    while True:
        await asyncio.sleep(interval)
        for sym, book in reconstructor.books.items():
            bids = sorted(book.bids.items(), reverse=True)
            asks = sorted(book.asks.items())
            sidecar.inject_snapshot(sym, int(time.time()*1000), bids, asks)
```

### 5.3 关闭

```python
await sidecar.close()  # final flush
```

---

## 6. 验收测试（echo 这边写）

每个 shard 必须能被 `L2Reconstructor.replay_dataframe()` 单独重建：

```python
import pyarrow.parquet as pq
from narci.data.l2_reconstruct import L2Reconstructor

shard = pq.read_table(some_shard_path).to_pandas()
shard = shard.sort_values(["timestamp", "side"], ascending=[True, False])
rec, state = L2Reconstructor.replay_dataframe(shard, depth_limit=5)
assert state is not None, f"shard {some_shard_path} not self-contained"
```

如果 state 为 None，说明 shard 没有 snapshot 或 snapshot 不完整。

---

## 7. 一些边界情况

| 情况 | 处理 |
|---|---|
| WS reconnect | 重连后立即拉 REST snapshot，inject_snapshot 给 sidecar |
| 进程崩溃 | sidecar buffer 丢失（最多 60s 数据丢失，可接受） |
| Flush 时磁盘满 | 错误日志，buffer 保留，下次重试 |
| Symbol 列表变更 | 每 symbol 独立 buffer/shard，新 symbol 自动 lazy 创建 |
| 多 session 并行 | 每 session 独立 sidecar 实例，独立目录 |

---

## 8. 不强制 / 可选

下面这些 echo 自由决定：

- 是否 retain 旧 shard（建议 retain ≥ 7 天供 calibration replay）
- 是否压缩 parquet（pyarrow 默认 SNAPPY 即可）
- 是否上传到 S3（建议但不强制 — narci calibrate_session 直接读本地）
- 是否双向写（同时给 narci recorder 一份）—— 不需要，narci recorder 自己跑

---

## 9. 跟 EchoLogWriter 的关系

`EchoLogWriter` 写的 4 套（decisions/fills/cancels/meta）是 echo 决策侧的产出。
`RawL2Sidecar` 写的 raw_l2/ 是市场数据侧的产出。

两者独立但都属于同一 session，`session_dir/` 同根目录下：

```
echo/logs/{session_id}/
├── meta.json                   # EchoLogWriter
├── decisions/                  # EchoLogWriter
├── fills/                      # EchoLogWriter
├── cancels/                    # EchoLogWriter
└── raw_l2/                     # RawL2Sidecar (本文档)
```

calibrate_session 默认从 `{session_dir}/raw_l2/` 读 raw L2，找不到才 fallback 到 narci recorder。

---

## 10. 失败模式 & 反向求救

如果 echo 实现遇到这份 spec 解释不清的边界：在 narci 这边的 `CALIBRATION_PROTOCOL.md` 末尾加 `## 反向需求` 一节，narci session 下次会读取并 review。
