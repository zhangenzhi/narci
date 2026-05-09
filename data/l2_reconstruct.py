import heapq
import operator

import pandas as pd
import numpy as np

_ITEMGETTER_0 = operator.itemgetter(0)

class L2Reconstructor:
    def __init__(self, depth_limit=20, book_staleness_seconds: float = 0.0,
                 prune_snapshot_dust: bool = False,
                 incremental_ready_threshold: int = 5):
        """
        :param depth_limit: 还原 L2 时保留的深度档位数量
        :param book_staleness_seconds: when get_top1/get_state can't produce
            a fresh non-crossing top, fall back to the most recent valid
            cached result if its age is within this tolerance. Default 0
            disables fallback (strict NaN). Used to mask the BJ "short-run
            NaN" pattern where cross-spread / dust transients briefly
            invalidate the book — the staleness window keeps features
            valid for ~10s of recovery time. Per-call override available.
        :param prune_snapshot_dust: P1-C fix 2026-05-08. The narci recorder
            writes snapshots from its in-memory book, which accumulates
            dust when WS delete events are dropped. When True, at the
            close of every snapshot batch, walk to a non-crossing top
            pair via _resolve_top_non_crossing and remove all bids at
            or above that ask (cross-side dust) and asks at or below
            that bid. Conservative: only removes definitely-crossing
            levels; legitimate deep liquidity is kept. Default False
            preserves old behavior. See docs for staleness fallback (P1)
            for how this composes with the cache-fallback path.
        :param incremental_ready_threshold: P3 fix 2026-05-09. Pre-fix
            `is_ready` flipped True only on side=3/4 (snapshot) events.
            BJ-style venues with long save-interval (600s) and segment
            replay with 300s warmup → 50% of segments never see a
            snapshot during warmup → is_ready=False permanently → all
            get_top1/get_state calls return None even though incrementals
            had filled bids+asks to thousands of levels. Now: when both
            sides have ≥ this many levels (via any combination of
            incrementals/snapshots), is_ready is set True. Default 5
            matches typical top_n=5 query depth. Set to 1 for fastest
            bootstrap or 0 to disable incremental-bootstrap entirely
            (keeps strict snapshot-only semantics).
        """
        self.depth_limit = depth_limit
        self.book_staleness_seconds = book_staleness_seconds
        self.prune_snapshot_dust = prune_snapshot_dust
        self.incremental_ready_threshold = max(0, int(incremental_ready_threshold))
        self.bids = {}
        self.asks = {}
        self.is_ready = False

        # 用于聚合采样期间内的 L1 逐笔主动买卖成交量
        self.period_taker_buy_vol = 0.0
        self.period_taker_sell_vol = 0.0

    def apply_diff(self, side, price, quantity):
        """保留外部接口以维持兼容性，但在 process_dataframe 中已被 inline 展开以提升性能"""
        target_map = self.bids if side in [0, 3] else self.asks
        if quantity == 0:
            target_map.pop(price, None)
        else:
            target_map[price] = quantity
            
        if side in [3, 4]:
            self.is_ready = True

    def get_snapshot(self):
        """获取当前排好序的 Top N 深度视图"""
        if not self.bids or not self.asks:
            return [], []

        sorted_bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:self.depth_limit]
        sorted_asks = sorted(self.asks.items(), key=lambda x: x[0], reverse=False)[:self.depth_limit]
        return sorted_bids, sorted_asks

    def process_dataframe(self, df, sample_interval_ms=None):
        """
        核心性能优化方法：直接处理传入的 DataFrame，避免反复读盘。
        """
        results = []
        next_sample_ts = 0
        
        # 重置状态
        self.bids, self.asks = {}, {}
        self.is_ready = False
        self.period_taker_buy_vol = 0.0
        self.period_taker_sell_vol = 0.0

        # 【核心优化 1】: 将 DataFrame 转换为 NumPy 数组遍历，跳过 df.itertuples() 的庞大开销，提速 5~10 倍
        columns = ['timestamp', 'side', 'price', 'quantity']
        for col in columns:
            if col not in df.columns:
                return pd.DataFrame()
        
        values = df[columns].to_numpy()
        
        for i in range(len(values)):
            ts = int(values[i, 0])
            side = int(values[i, 1])
            price = values[i, 2]
            qty = values[i, 3]

            # 盘口挂单更新
            if side in [0, 1, 3, 4]:
                # 【核心优化 2】: 展开 apply_diff 逻辑，避免每行产生一次 Python 函数调用栈开销
                target_map = self.bids if side in [0, 3] else self.asks
                if qty == 0:
                    target_map.pop(price, None)
                else:
                    target_map[price] = qty
                    
                if side in [3, 4]:
                    self.is_ready = True
                    # 【致命 Bug 修复】: 快照注入阶段 (side 3/4) 时间戳完全相同，
                    # 绝对不能在这里采样！否则会记录下只注入了“半边”的残缺盘口。跳过此次循环的采样检测。
                    continue
                    
            # L1 逐笔成交量统计
            elif side == 2:
                if qty > 0:
                    self.period_taker_buy_vol += qty
                else:
                    self.period_taker_sell_vol += abs(qty)

            # 采样逻辑
            if sample_interval_ms is not None:
                if self.is_ready and ts >= next_sample_ts:
                    bids_top = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:self.depth_limit]
                    asks_top = sorted(self.asks.items(), key=lambda x: x[0], reverse=False)[:self.depth_limit]
                    
                    if bids_top and asks_top:
                        b1_p, _ = bids_top[0]
                        a1_p, _ = asks_top[0]
                        
                        # 多档深度加权 (Top-5 Deep Imbalance)
                        b_q_sum = sum(q for p, q in bids_top[:5])
                        a_q_sum = sum(q for p, q in asks_top[:5])
                        imbalance = (b_q_sum - a_q_sum) / (b_q_sum + a_q_sum) if (b_q_sum + a_q_sum) > 0 else 0.0
                            
                        record = {
                            'timestamp': ts,
                            'mid_price': (b1_p + a1_p) / 2.0,
                            'imbalance': round(imbalance, 4),
                            'spread': round(a1_p - b1_p, 8),
                            'taker_buy_vol': round(self.period_taker_buy_vol, 4),
                            'taker_sell_vol': round(self.period_taker_sell_vol, 4)
                        }
                        
                        for idx in range(self.depth_limit):
                            if idx < len(bids_top):
                                record[f'b_p_{idx}'], record[f'b_q_{idx}'] = bids_top[idx]
                            if idx < len(asks_top):
                                record[f'a_p_{idx}'], record[f'a_q_{idx}'] = asks_top[idx]
                        
                        results.append(record)
                        
                        # 清空当前周期的成交统计，准备进入下一个周期
                        self.period_taker_buy_vol = 0.0
                        self.period_taker_sell_vol = 0.0
                        
                        # 【核心优化 3】: 时间漂移修复与全局绝对对齐
                        # 确保无论文件何时开始，Tick 切片永远在全球对齐的 100ms 边界上 (如 ...000, ...100, ...200)
                        # 下游 DatasetBuilder 做 GRU resample 时就不会出现坑洞
                        next_sample_ts = (ts // sample_interval_ms + 1) * sample_interval_ms
            
            # L3 逐笔成交对齐逻辑
            else:
                if side == 2 and self.is_ready:
                    bids_top = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:1]
                    asks_top = sorted(self.asks.items(), key=lambda x: x[0], reverse=False)[:1]
                    if bids_top and asks_top:
                        results.append({
                            'timestamp': ts,
                            'price': price,
                            'quantity': qty,
                            'bid1': bids_top[0][0],
                            'ask1': asks_top[0][0],
                            'spread': round(asks_top[0][0] - bids_top[0][0], 8)
                        })
                        
        return pd.DataFrame(results)

    def generate_l2_dataset(self, file_path, sample_interval_ms=1000):
        df = pd.read_parquet(file_path)
        # 快照注入后确保排序规则不变：时间戳升序，side 降序 (确保 4,3 的快照最先处理)
        df = df.sort_values(by=['timestamp', 'side'], ascending=[True, False])
        return self.process_dataframe(df, sample_interval_ms)

    # ------------------------------------------------------------------ #
    # Streaming API (added 2026-05) — for echo realtime + MakerSimBroker
    #
    # process_dataframe is the batch-optimized path; the methods below let
    # consumers feed events one-at-a-time and query derived state.
    #
    # Logic mirrors the inline body of process_dataframe; if you change
    # one, change the other.
    # ------------------------------------------------------------------ #

    def reset(self):
        """Wipe all state. Call before replaying a new session."""
        self.bids = {}
        self.asks = {}
        self.is_ready = False
        self.period_taker_buy_vol = 0.0
        self.period_taker_sell_vol = 0.0
        self._last_ts = 0
        self._snapshot_batch_ts = None  # if last write was a snapshot row, ts
        # incremental top-1 cache (populated by apply_event); None means stale/empty
        self._best_bid_p = None
        self._best_ask_p = None
        # Snapshot batch detection: narci recorder injects full-book snapshots
        # atomically (side=3/4 events at the same ts). On a NEW snapshot ts,
        # clear the corresponding side dict before applying — matches recorder
        # semantics ("drains buffer and injects a full snapshot"). Without
        # this, dust levels from prior snapshots/markets accumulate and break
        # cross-venue features like basis_um_bps via stuck top-1.
        self._last_snap_bid_ts = -1
        self._last_snap_ask_ts = -1
        # Last-valid caches for stale-fallback (P1 fix 2026-05-08).
        # Populated at end of successful get_top1 / get_state. Returned
        # (with `stale=True`) when a later call can't compute a fresh
        # value but the cached one is within book_staleness_seconds.
        self._last_valid_top1 = None
        self._last_valid_top1_ts = -1
        # get_state cache is keyed by top_n (different callers may ask
        # for different depths; the top-5 cache is unsuitable for top-10).
        self._last_valid_state_by_n: dict = {}
        self._last_valid_state_ts_by_n: dict = {}

    def apply_event(self, ts, side, price, qty):
        """Apply a single L2 event to the running orderbook state.

        Side encoding (same as narci recorder format):
          0 = bid update         (qty == 0 → delete level)
          1 = ask update
          2 = aggTrade           (signed qty: > 0 buyer maker, < 0 seller maker)
          3 = bid snapshot       (sets is_ready)
          4 = ask snapshot

        For trade events (side=2), accumulates rolling taker_buy_vol /
        taker_sell_vol. Caller resets these via reset_period_volumes()
        after consuming.

        No emit happens here — query state via get_state() / get_snapshot().
        """
        ts = int(ts)
        # P1-C fix: when configured, prune cross-side dust at the close
        # of a snapshot batch. The first non-snapshot event after a
        # batch (sides 0/1/2 while _snapshot_batch_ts is set) is the
        # natural close trigger; prune runs BEFORE the current event
        # modifies the book.
        if (self.prune_snapshot_dust
                and self._snapshot_batch_ts is not None
                and side in (0, 1, 2)):
            self.prune_dust()
        if side in (0, 3):
            if side == 3 and ts != self._last_snap_bid_ts:
                # New snapshot batch — atomically replace bid book. Old
                # levels (incl. stale dust from previous snapshots / past
                # incremental updates) are wiped.
                self.bids.clear()
                self._best_bid_p = None
                self._last_snap_bid_ts = ts
            if qty == 0:
                self.bids.pop(price, None)
                # If we deleted current best, recompute (rare path).
                if price == self._best_bid_p:
                    self._best_bid_p = max(self.bids) if self.bids else None
            else:
                self.bids[price] = qty
                # Cheap update: only promote if higher.
                if self._best_bid_p is None or price > self._best_bid_p:
                    self._best_bid_p = price
            if side == 3:
                self.is_ready = True
                self._snapshot_batch_ts = ts
            elif (not self.is_ready
                    and self.incremental_ready_threshold > 0
                    and len(self.bids) >= self.incremental_ready_threshold
                    and len(self.asks) >= self.incremental_ready_threshold):
                # P3 fix: bootstrap is_ready from incrementals so segments
                # that don't span a 600s snapshot batch in their warmup
                # window can still produce features. Sticky once set.
                self.is_ready = True
        elif side in (1, 4):
            if side == 4 and ts != self._last_snap_ask_ts:
                # New snapshot batch — atomically replace ask book.
                self.asks.clear()
                self._best_ask_p = None
                self._last_snap_ask_ts = ts
            if qty == 0:
                self.asks.pop(price, None)
                if price == self._best_ask_p:
                    self._best_ask_p = min(self.asks) if self.asks else None
            else:
                self.asks[price] = qty
                if self._best_ask_p is None or price < self._best_ask_p:
                    self._best_ask_p = price
            if side == 4:
                self.is_ready = True
                self._snapshot_batch_ts = ts
            elif (not self.is_ready
                    and self.incremental_ready_threshold > 0
                    and len(self.bids) >= self.incremental_ready_threshold
                    and len(self.asks) >= self.incremental_ready_threshold):
                # P3 fix: see mirror branch on side=0/3 above.
                self.is_ready = True
        elif side == 2:
            if qty > 0:
                self.period_taker_buy_vol += qty
            else:
                self.period_taker_sell_vol += abs(qty)
            # any non-snapshot event implicitly closes a snapshot batch
            self._snapshot_batch_ts = None

        if side in (0, 1):
            self._snapshot_batch_ts = None

        self._last_ts = ts

    def reset_period_volumes(self):
        """Zero out the taker_buy_vol / taker_sell_vol accumulators.
        Call after `get_state()` returns these values, to start a fresh window."""
        self.period_taker_buy_vol = 0.0
        self.period_taker_sell_vol = 0.0

    def get_top1(self, allow_stale_seconds: float | None = None):
        """Fast O(1) top-of-book accessor maintained incrementally inside
        apply_event. Returns dict with keys:
          ts, best_bid, best_ask, mid_price, spread, spread_bps,
          bid_qty_top1, ask_qty_top1, imbalance_top1, microprice
        Or None if book not ready / empty.

        Use this in the inner event loop instead of get_state(top_n=1)
        which costs O(N log N). For top-5+ metrics, use get_state().

        Crossing handling (B0 fix, 2026-05-06): if the incremental cache
        becomes crossed (bp >= ap) — typically because the recorder
        retains 'dust' levels above market that never receive a delete
        event — fall back to a sorted walk that skips dust until a
        non-crossing pair is found, and refresh the cache. O(N log N) on
        first call after crossing detected; O(1) afterwards until next
        crossing event.

        Stale fallback (P1 fix 2026-05-08): when the book can't produce
        a fresh non-crossing top (book empty or cross-walk fails), fall
        back to the last valid result if its age (in apply_event ts
        space) is within `allow_stale_seconds`. Returned dict has
        `stale=True` and `stale_age_ms=int` so callers can audit. If
        `allow_stale_seconds` is None, uses the constructor-level
        `book_staleness_seconds`. Pass 0 to force strict (no fallback).
        """
        if allow_stale_seconds is None:
            allow_stale_seconds = self.book_staleness_seconds

        bp = self._best_bid_p
        ap = self._best_ask_p
        fresh = None
        if self.is_ready and self.bids and self.asks:
            if bp is None or ap is None or bp >= ap:
                bp, ap = self._resolve_top_non_crossing()
                if bp is not None:
                    # cache the cleaned values; next event will validate again
                    self._best_bid_p = bp
                    self._best_ask_p = ap
            if bp is not None and ap is not None and bp < ap:
                bq = self.bids[bp]
                aq = self.asks[ap]
                mid = (bp + ap) / 2.0
                spread = ap - bp
                bq_aq = bq + aq
                microprice = (bp * aq + ap * bq) / bq_aq if bq_aq > 0 else mid
                fresh = {
                    "ts": self._last_ts,
                    "best_bid": bp,
                    "best_ask": ap,
                    "mid_price": mid,
                    "spread": spread,
                    "spread_bps": spread / mid * 10000.0,
                    "bid_qty_top1": bq,
                    "ask_qty_top1": aq,
                    "imbalance_top1": (bq - aq) / bq_aq if bq_aq > 0 else 0.0,
                    "microprice": microprice,
                }

        if fresh is not None:
            # cache for future stale-fallback. Store a copy so future mutation
            # by the caller doesn't poison the cache.
            self._last_valid_top1 = dict(fresh)
            self._last_valid_top1_ts = self._last_ts
            return fresh

        # No fresh value. Try stale fallback.
        if (allow_stale_seconds > 0
                and self._last_valid_top1 is not None
                and self._last_valid_top1_ts >= 0):
            age_ms = self._last_ts - self._last_valid_top1_ts
            if 0 <= age_ms <= allow_stale_seconds * 1000:
                stale = dict(self._last_valid_top1)
                stale["ts"] = self._last_ts
                stale["stale"] = True
                stale["stale_age_ms"] = int(age_ms)
                return stale
        return None

    def get_state(self, top_n=5, allow_stale_seconds: float | None = None):
        """Snapshot the current top-N book + derived metrics.

        Returns None if not ready (no snapshot ingested yet) or book empty.
        Returns dict with:
          ts, mid_price, microprice, spread, spread_bps,
          imbalance_top1, imbalance_top5,
          bid_qty_top1, ask_qty_top1, bid_qty_top5_sum, ask_qty_top5_sum,
          bids_top: list[(price, qty)], asks_top: list[(price, qty)],
          taker_buy_vol, taker_sell_vol,
          is_consistent: True unless we're mid-snapshot-batch

        Cost: O(N log N) sorts of bids/asks dicts each call. Don't call in
        the inner loop; once per quote refresh is fine.

        Stale fallback (P1 fix 2026-05-08): same semantics as
        get_top1(allow_stale_seconds=...). Cache is keyed by top_n —
        callers asking for a different depth get an independent cache.
        Stale dicts gain `stale=True` + `stale_age_ms`.
        """
        if allow_stale_seconds is None:
            allow_stale_seconds = self.book_staleness_seconds

        fresh = None
        if self.is_ready and self.bids and self.asks:
            # Use heapq partial-sort O(N log K) instead of full sort O(N log N).
            # For typical UM book (~20k levels) and top_n=5, this is ~30× faster.
            # operator.itemgetter beats lambda by ~2× in CPython for hot loops.
            bids_top = heapq.nlargest(top_n, self.bids.items(), key=_ITEMGETTER_0)
            asks_top = heapq.nsmallest(top_n, self.asks.items(), key=_ITEMGETTER_0)
            if bids_top and asks_top:
                b1_p, b1_q = bids_top[0]
                a1_p, a1_q = asks_top[0]
                if b1_p < a1_p:
                    mid = (b1_p + a1_p) / 2.0
                    microprice = (b1_p * a1_q + a1_p * b1_q) / (b1_q + a1_q) if (b1_q + a1_q) > 0 else mid
                    spread = a1_p - b1_p
                    spread_bps = spread / mid * 10000.0
                    imb1 = (b1_q - a1_q) / (b1_q + a1_q) if (b1_q + a1_q) > 0 else 0.0
                    b5_sum = sum(q for _, q in bids_top)
                    a5_sum = sum(q for _, q in asks_top)
                    imb5 = (b5_sum - a5_sum) / (b5_sum + a5_sum) if (b5_sum + a5_sum) > 0 else 0.0
                    is_consistent = self._snapshot_batch_ts is None or self._last_ts != self._snapshot_batch_ts
                    fresh = {
                        "ts": self._last_ts,
                        "mid_price": mid,
                        "microprice": microprice,
                        "spread": spread,
                        "spread_bps": spread_bps,
                        "imbalance_top1": imb1,
                        "imbalance_top5": imb5,
                        "bid_qty_top1": b1_q,
                        "ask_qty_top1": a1_q,
                        "bid_qty_top5_sum": b5_sum,
                        "ask_qty_top5_sum": a5_sum,
                        "best_bid": b1_p,
                        "best_ask": a1_p,
                        "bids_top": bids_top,
                        "asks_top": asks_top,
                        "taker_buy_vol": self.period_taker_buy_vol,
                        "taker_sell_vol": self.period_taker_sell_vol,
                        "is_consistent": is_consistent,
                    }

        if fresh is not None:
            # Cache an independent copy (callers may mutate). bids_top /
            # asks_top are already fresh lists from heapq, safe to share.
            cached = dict(fresh)
            self._last_valid_state_by_n[top_n] = cached
            self._last_valid_state_ts_by_n[top_n] = self._last_ts
            return fresh

        if allow_stale_seconds > 0:
            cached = self._last_valid_state_by_n.get(top_n)
            cached_ts = self._last_valid_state_ts_by_n.get(top_n, -1)
            if cached is not None and cached_ts >= 0:
                age_ms = self._last_ts - cached_ts
                if 0 <= age_ms <= allow_stale_seconds * 1000:
                    stale = dict(cached)
                    stale["ts"] = self._last_ts
                    stale["stale"] = True
                    stale["stale_age_ms"] = int(age_ms)
                    # taker volumes are accumulators — return current live
                    # values (not cached stale ones) so flow features stay
                    # accurate even when book is briefly invalid.
                    stale["taker_buy_vol"] = self.period_taker_buy_vol
                    stale["taker_sell_vol"] = self.period_taker_sell_vol
                    return stale
        return None

    def prune_dust(self) -> int:
        """Remove cross-side dust that the recorder's in-memory book
        accumulated and re-emitted in its snapshot batch.

        Walks to a non-crossing top pair via _resolve_top_non_crossing,
        then removes:
          - bids priced >= validated best_ask  (dust above market)
          - asks priced <= validated best_bid  (dust below market)
        Levels at validated bp/ap themselves are kept.

        No-op if book is empty or has no resolvable non-crossing pair.
        Returns the number of levels pruned (for telemetry).

        Conservative by design: deep liquidity (bids well below market,
        asks well above market) is left intact; only definitely-crossing
        levels are touched. False-positive pruning of a legitimate
        thin-qty level is bounded by _resolve_top_non_crossing's own
        skip-by-qty heuristic — a real top sometimes loses its top
        position when a bigger-qty stale level masks it, but the result
        is still a usable mid (slightly worse spread), not None."""
        if not self.bids or not self.asks:
            return 0
        bp, ap = self._resolve_top_non_crossing()
        if bp is None or ap is None:
            return 0
        bids_to_drop = [p for p in self.bids if p >= ap and p != bp]
        asks_to_drop = [p for p in self.asks if p <= bp and p != ap]
        for p in bids_to_drop:
            self.bids.pop(p, None)
        for p in asks_to_drop:
            self.asks.pop(p, None)
        # Refresh top-1 cache to validated values; subsequent get_top1
        # short-circuits the resolve walk.
        self._best_bid_p = bp
        self._best_ask_p = ap
        return len(bids_to_drop) + len(asks_to_drop)

    def _resolve_top_non_crossing(self):
        """Walk sorted bids (desc) and asks (asc) simultaneously, skipping
        whichever side has smaller qty until a non-crossing pair is found.
        Returns (bp, ap) or (None, None) if no non-crossing pair exists.
        Used when the incremental top-1 cache becomes crossed (dust). Does
        NOT modify self.bids / self.asks — leaves dust in place so that
        future updates can still reference those levels if needed."""
        bids_sorted = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)
        asks_sorted = sorted(self.asks.items(), key=lambda x: x[0])
        bi = ai = 0
        n_bids = len(bids_sorted)
        n_asks = len(asks_sorted)
        while bi < n_bids and ai < n_asks:
            bp, bq = bids_sorted[bi]
            ap, aq = asks_sorted[ai]
            if bp < ap:
                return bp, ap
            # crossed: skip dust on lower-qty side
            if bq <= aq:
                bi += 1
            else:
                ai += 1
        return None, None

    @classmethod
    def replay_dataframe(cls, df, depth_limit=20):
        """Stream-mode replay: feed every row of df via apply_event; useful
        for unit tests that compare streaming vs batch.

        Returns (reconstructor, final_state_dict_or_None)."""
        rec = cls(depth_limit=depth_limit)
        rec.reset()
        # column lookup once
        cols = ['timestamp', 'side', 'price', 'quantity']
        for c in cols:
            if c not in df.columns:
                raise ValueError(f"replay_dataframe needs column '{c}'")
        arr = df[cols].to_numpy()
        for i in range(len(arr)):
            rec.apply_event(int(arr[i, 0]), int(arr[i, 1]), arr[i, 2], arr[i, 3])
        return rec, rec.get_state(top_n=depth_limit)