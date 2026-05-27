# 2026-05-17 GMO ERR-5003 rate-limit + reconnect storm — silent

- **Severity**: 高(整 venue 数据丢失,被前一个 watchdog fix 部分掩盖)
- **First detect**: 2026-05-17 上一份 incident
  ([2026-05-17-gmo-watchdog-silent-death.md](./2026-05-17-gmo-watchdog-silent-death.md))
  fix 部署后,verify 阶段(t+11min)发现新 shard 仍只有 init snapshot 几行
  (12 KB,跟 fix 前一样),log 出现规律性 `⚠️ gmo depth 静默 180s > 180s,
  强制重连` 每 3 分钟一次
- **Affected venues**: `gmo/spot`、`gmo/leverage`(其他正常)
- **Resolution commit**: `8a0a14b fix(exchange/gmo): server error 解析 +
  subscribe 200ms 间隔 + 指数 backoff`
- **关联**: [2026-05-16-gmo-silent-drop.md](./2026-05-16-gmo-silent-drop.md)
  (case mismatch)、[2026-05-17-gmo-watchdog-silent-death.md](./2026-05-17-gmo-watchdog-silent-death.md)
  (watchdog AttributeError) — 3 天 3 个 gmo 静默 bug,同 file 不同根因

## Symptom

`9779ac4` (watchdog ws.closed fix) 部署后:
- gmo container `Up (healthy)`
- log 周期性 reconnect:`[t] 🔌 gmo WS 已连接` + 4-7 个 `📡 已订阅 ... for SYM`
  → 3 min 后 `⚠️ gmo depth 静默 180s` → close + reconnect。形成 3-min loop。
- 新 parquet 文件确实在 `save_interval=600s` 周期产生,**但每个 ~12 KB**,
  即只装 REST init snapshot 写入的 side=3/4 records,**0 个 WS 推送数据**。
- 11 hours 累积:gmo/spot 仍 ~400 K,leverage ~120 K(对比 bitbank 同期 ~130 M)。

## Root cause

独立 WS probe(`python /tmp/probe_gmo_raw.py`,直接 `websockets.connect`
+ 同样 subscribe payload)在 gmo-leverage container 内执行,实测 server 反应:

```
recv: {"error":"ERR-5003 Request too many."}
recv: {"channel":"orderbooks", ...}   ← probe 自己手发的 subscribe 部分成功
```

而 production recorder 启动时(`probe_gmo_raw.py` 跑前的 probe)收到的全是
`error` 字段消息(channel 字段为空 → `<no-ch>`),即 **所有 subscribe 都被
ERR-5003 reject**。

**故障链**:
1. 前一个 fix `9779ac4` 让 watchdog 真的工作,3-min idle threshold 触发 reconnect。
2. 每次 reconnect 立即 burst-send 6~8 个 subscribe(3-4 symbol × 2 channel)。
3. 累积下来 GMO server 端 detect 异常 burst → IP rate limit
   → 后续 subscribe 全 reply `ERR-5003 Request too many.`。
4. Pre-fix 的 gmo.py 不解析 `error` 字段,silent ignore →
   ws_state["last_depth_msg_t"] 不更新 → 又 3 min 后 watchdog 又 trigger
   → 再 burst subscribe → 再 ERR-5003 → **死循环**。
5. Production 持续 reconnect storm 让 GMO 端的 ban 不会自然冷却。

## 为什么前一个 fix(9779ac4)反而让问题更明显

`9779ac4` 之前,watchdog 因 `ws.closed` AttributeError 立刻死,主 loop 永远
挂在 `async for raw in ws:` 上但 GMO server 在 subscribe 后什么都不推 →
**0 reconnect**(连不上 GMO 错误也看不到,但也不撞 rate limit)。

`9779ac4` 让 watchdog 正常工作 → 3 min 自动重连 → 每天发几百次 subscribe →
撞 rate limit。

两次都是 silent failure,但 `9779ac4` 后的失败模式更糟(IP 被 ban,即使
container 暂停重启也要等冷却)。

## Fix

`<TBD>`:

1. **server error 解析**(主 loop 内):
   ```python
   if msg.get("error"):
       self._subscribe_error_count += 1
       ws_state["server_error"] = err
       print(f"⚠️ gmo server error #N: {err},退出 session 进 backoff")
       await ws.close()
       break
   ```
2. **subscribe 间 200ms 间隔**:`await asyncio.sleep(0.2)` 在每个 `ws.send`
   之后 — 不再 burst 撞 rate limit,subscribe 节奏改成 1.5s/symbol 左右。
3. **指数 backoff**(while 循环末尾,基于累积 error 计数):
   ```python
   if ws_state.get("server_error"):
       backoff = min(600, 10 * (2 ** min(self._subscribe_error_count, 6)))
       print(f"⏸ backoff {backoff}s")
       await asyncio.sleep(backoff)
   ```
   10s → 20s → 40s → 80s → 160s → 320s → 600s 上限。
4. **error 计数 reset**:收到任何有效 channel 消息时 `self._subscribe_error_count = 0`。

## Verify

1. **先 stop**:`docker compose stop recorder-gmo-spot recorder-gmo-leverage`
   让 GMO 端 IP rate limit 自然冷却 (~10-15 min)。
2. 部署新 image,start。
3. t+5min 看 log,subscribe 成功 + 开始有 orderbooks 推送(file size 应该
   快速增长到 MB 级别)。
4. t+30min 看 du,gmo/spot 应该 ~5-10 MB,leverage 类似(比 bitbank 量级低
   但远高于现在的 KB 级)。
5. 如果再次撞 ERR-5003,看 backoff log,几 cycle 后应该自然恢复。

## 2026-05-19 update — 200ms 间隔仍超官方限制

2h 冷却后 verify 阶段(实际上 partial verify,在等 nohup auto-start),交叉
验证发现 200ms `asyncio.sleep` 仍然 5x 超 GMO 官方限制:

> https://api.coin.z.com/docs/#restrictions-public-ws-api
> Public WebSocket APIの制限
> 同一IPからのリクエスト(subscribe, unsubscribe)は、**1秒間1回を上限**とします。

`curl https://api.coin.z.com/docs/` 拉到的官方文档原文(2026-05-19 验证有效)。
pybotters Issue #50 (2021-06-11,closed) 也命中同一根因:
https://github.com/pybotters/pybotters/issues/50

我们 `8a0a14b` 的 `await asyncio.sleep(0.2)` 是基于「我们撞墙了所以加点 sleep」
直觉,不是基于官方 docs 数值,5x over。**后续 fix 调到 1.1s**(给 100ms
buffer 防 jitter)。

## Lessons / TODO

1. **subscribe 必须 throttle**。多 channel multi-symbol 时 burst 会触发各种
   exchange 的反 DDoS 机制。bitflyer、bitbank 都有类似但未触发(connection
   层处理),GMO 特别严。
2. **任何 server response 都要解析所有字段**,包括 `error`。silent ignore
   异常字段是常见的「连上 → 0 数据」根因模式。
3. **watchdog 触发 reconnect 不一定是好事**。在 server rate-limit 场景下,
   reconnect 反而加剧故障。需要区分:
   - 网络/连接故障(reconnect 有用)
   - server-side reject(reconnect 让事情更糟,要长 backoff)
4. **加 integration test 跑 5 min 真 WS**(沿用前一份 incident 的建议)就
   会立刻 catch 到这个 bug — 因为 ERR-5003 在 subscribe 后秒级就回。
5. **monitoring 仍然 blind**:healthcheck 报 healthy,数据其实 0。per-venue
   shards-in-window 监控更急了。
