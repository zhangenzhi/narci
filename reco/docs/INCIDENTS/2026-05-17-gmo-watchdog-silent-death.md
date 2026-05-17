# 2026-05-17 GMO custom_stream watchdog 静默死亡 — 11h 无数据无重连

- **Severity**: 高(整 venue 数据丢失,且无任何报警 — healthcheck 报 healthy)
- **First detect**: 2026-05-17 早间例行抽查 aws-jp 数据完整性
- **Affected venues**: `gmo/spot`、`gmo/leverage`(其他 venue 全正常)
- **Time window**: 2026-05-16 12:58 UTC ~ 2026-05-17 06:28 UTC,**约 17.5h**
- **Resolution commit**: `9779ac4 fix(exchange/gmo): watchdog ws.closed ...`
- **关联事故**: [2026-05-16-gmo-silent-drop.md](./2026-05-16-gmo-silent-drop.md)
  — 同 venue,同样「无数据无重连」形态,但**不同根因**(上次是 case mismatch,
  这次是 watchdog 静默崩)

## Symptom

aws-jp 7 个 recorder 全部 docker compose status = `Up 11 hours (healthy)`,
healthcheck endpoint 全 200 healthy。**但**:

| Venue | 11h 应有体积 | 实测体积 | shard 数 |
|---|---|---|---|
| binance_jp/spot | ~600M | 613.7M | 持续累积 |
| coincheck/spot | ~200M | 213.0M | 持续累积 |
| bitbank/spot | ~130M | 130.9M | 持续累积 |
| bitflyer/spot | ~13M | 13.1M | 持续累积 |
| bitflyer/fx | ~10M | 10.0M | 持续累积 |
| **gmo/spot** | ≈100M | **344K** | **仅 6 个**(BTC/ETH/XRP × 2 timestamps)|
| **gmo/leverage** | ≈100M | **80K** | **仅 8 个**(4 symbols × 2 timestamps)|

`docker compose logs recorder-gmo-{spot,leverage}` 完整输出:**只有 startup
头部 ~30 行**,从 `[12:58:48] 📡 已订阅 ...` 之后 11h **完全没有任何日志输出**
(既没有重连警告也没有保存日志)。

## Root cause

`data/exchange/gmo.py:custom_stream._watchdog_loop`(2026-05-16 18:00 UTC
前的版本)循环条件用 `while recorder.running and not ws.closed:`。

production 实测 (`pip show websockets` → `Version: 16.0`):

```
> ws = await websockets.connect("wss://api.coin.z.com/ws/public/v1")
> type(ws).__name__ → 'ClientConnection'
> hasattr(ws, 'closed') → False
> hasattr(ws, 'state')  → True
```

`websockets` v14+ 把 `connect()` 切换到新的 sans-IO 实现,返回
`ClientConnection`(没有 `.closed`,改用 `.state` 枚举:`CONNECTING`/`OPEN`/
`CLOSING`/`CLOSED`)。

因此 watchdog `while ... not ws.closed:` 一执行就抛 `AttributeError`,
asyncio task 异常被吞(unhandled task exception),watchdog 静默退出。
主消息 loop `async for raw in ws:` 永远挂着等消息 — 但 GMO server 在
subscribe 之后**不主动断也不推任何消息**(这是 GMO 的具体行为,不是协议
通用),所以 ws 不会自然终止,recorder 既不能收数据也不能重连。

## 为什么 healthcheck 没报

`deploy/healthcheck.py` 扫的是整个 `/app/replay_buffer/realtime/**/l2/*.parquet`
**聚合数**(`shards_in_window=118` 在所有 7 个 endpoint 返回完全一样)。即使
gmo 单 venue 完全静默,binance_jp 在产 shard 就让 healthcheck 全 healthy。

**Monitoring blind spot 待办**:healthcheck 应该按 venue 分别检测,而不是
聚合数。但需要 healthcheck.py 能拿到 recorder 实例的 `symbols` 配置 —
deploy 改动,有些复杂,先记 TODO。

## Fix

`9779ac4 fix(exchange/gmo): watchdog ws.closed ...`(narci 路径,
narci-reco 角色按用户授权代修,已知会 inform narci 角色):

1. watchdog 循环条件 `while recorder.running and not ws.closed:` →
   `while recorder.running:`。close 由 idle threshold 自己驱动,主 loop
   在 ws 关闭后自然退出。
2. `finally` 块的 `if ws is not None and not ws.closed:` → `if ws is not None:`,
   close 在 `ClientConnection` 上是幂等的。
3. watchdog 整段 body 再包一层 `try/except`,**任何未预期异常至少 print
   出来**而不是静默死。

代码改动 32 ins / 19 del。

## Verify

1. 部署:`9779ac4` push → aws-jp `git pull` + `docker compose up -d --build
   --force-recreate recorder-gmo-spot recorder-gmo-leverage`
2. recreate 完成 2026-05-17 06:28:13 UTC
3. **t+10min** 等首个 `save_interval_sec=600` 周期产 shard,看 7+8 个新 shard
   是否落盘(BTC/ETH/XRP + BTC_JPY/ETH_JPY/XRP_JPY/SOL_JPY)
4. 若有 idle 期间,watchdog 应该 print `⚠️ gmo trade 静默 ... 强制重连`
   并自动重连

## Lessons / TODO

1. **依赖库版本敏感的写法不要靠 attribute 名字**。`ws.closed` 这种历史 API
   名字在 14.x 没了。检 ws 状态应该 try/except 或者用 sentinel 而不是 hasattr
   做 best-effort。
2. **任何长时间 async loop 都要包外层 try/except**。如果 watchdog 在
   2026-05-16 fix 时就有 except print,这次 17.5h 就不会静默 — 会立刻看到
   `❌ gmo watchdog 异常: AttributeError: 'ClientConnection' object has no
   attribute 'closed'`。
3. **`docker compose ps healthy` ≠ 数据 healthy**。当前 healthcheck 是聚合
   buffer 维度,有 blind spot — 同一 host 单 venue 失败检测不到。需要
   per-venue 维度的 monitoring。短期靠 ops 例行查 du,长期需重做 healthcheck.py。
4. **跟 narci 协同同一 venue 重复修**。gmo 已经是 2 天内第 2 次 silent 故障
   (5/16 .lower mismatch,5/17 watchdog 崩),都属于同 file 的不同 bug。
   建议 narci 角色给 gmo 加一份独立集成测试,至少跑 5 min 真 WS 看消息能
   入 buffer,而不是只跑 unit test。
