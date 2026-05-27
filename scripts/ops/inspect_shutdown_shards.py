"""Inspect a UM recorder's SHUTDOWN-flush shards (one big parquet per symbol).

Run inside container:
    docker cp tools/inspect_shutdown_shards.py narci-recorder-binance-umfut:/tmp/i.py
    docker exec narci-recorder-binance-umfut python /tmp/i.py
"""
from __future__ import annotations

import datetime as dt
import glob

import numpy as np
import pyarrow.parquet as pq


PATTERN = "/app/replay_buffer/realtime/binance/um_futures/l2/*_RAW_20260508_0847*.parquet"


def main() -> None:
    files = sorted(glob.glob(PATTERN))
    if not files:
        print(f"no files matched {PATTERN}")
        return
    for f in files:
        t = pq.read_table(f, columns=["side", "timestamp"])
        s = t["side"].to_numpy()
        ts = t["timestamp"].to_numpy()
        uniq, cnt = np.unique(s, return_counts=True)
        sd = dict(zip(uniq.tolist(), cnt.tolist()))
        t0 = dt.datetime.fromtimestamp(ts.min() / 1000, tz=dt.timezone.utc)
        t1 = dt.datetime.fromtimestamp(ts.max() / 1000, tz=dt.timezone.utc)
        span_h = (ts.max() - ts.min()) / 3600000
        name = f.rsplit("/", 1)[-1]
        print(f"{name:50}  rows={len(ts):>8}  side={sd}  span={span_h:.1f}h  [{t0} -> {t1}]")


if __name__ == "__main__":
    main()
