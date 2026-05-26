"""Check side distribution of the most recent UM recorder shards.

Used to verify whether `/market/stream` endpoint is delivering both
aggTrade (side=2) AND depth incremental (sides 0/1), or only trades.

Run inside container:
    docker cp tools/check_new_shards.py narci-recorder-binance-umfut:/tmp/c.py
    docker exec narci-recorder-binance-umfut python /tmp/c.py
"""
from __future__ import annotations

import glob

import numpy as np
import pyarrow.parquet as pq


L2_DIR = "/app/replay_buffer/realtime/binance/um_futures/l2"

# Today (UTC) — adjust if running across midnight
PATTERN = f"{L2_DIR}/BTCUSDT_RAW_20260508_*.parquet"


def main() -> None:
    files = sorted(glob.glob(PATTERN))
    if not files:
        print(f"no files matched {PATTERN}")
        return
    print(f"total files today: {len(files)}")
    print("--- last 5 ---")
    for f in files[-5:]:
        t = pq.read_table(f, columns=["side"])
        s = t["side"].to_numpy()
        uniq, cnt = np.unique(s, return_counts=True)
        sd = dict(zip(uniq.tolist(), cnt.tolist()))
        name = f.rsplit("/", 1)[-1]
        print(f"  {name}: rows={len(s):>8}  side={sd}")


if __name__ == "__main__":
    main()
