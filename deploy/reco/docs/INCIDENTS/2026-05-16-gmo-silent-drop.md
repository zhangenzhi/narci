# 2026-05-16  gmo recorder 静默丢消息

| 字段 | 值 |
|---|---|
| 发现时间 | 2026-05-16 10:50 UTC(JP 上线 tokyo-extra 后约 50 min) |
| 影响范围 | `narci-recorder-gmo-spot` (BTC/ETH/XRP)、`narci-recorder-gmo-leverage` (BTC/ETH/XRP/SOL_JPY) |
| 数据损失 | 上线到修复期间所有 GMO orderbooks + trades 推送全丢 |
| Severity | 中(只影响新上线 venue;老 recorder 无影响) |

## 症状

`./aws/health_probe.sh jp` 报全 7 个 recorder healthy,但:

```
volume per-venue 体积:
  gmo/spot/l2     0 B
  gmo/leverage/l2 0 B
gdrive 顶层:
  无 gmo/ 目录
```

gmo container 进程活着、WS 连上、订阅成功、orderbook 初始 snapshot 收到了 —— 然后就再没 save cycle log。从 10:03:22 startup 到 10:50 共 47 分钟,recorder 一行 `📥 ... 原始数据固化` 都没打。

## 检测手法 (反推后用作 pattern)

只看 `/health` endpoint **抓不到** —— recorder 自报 healthy(它的 healthcheck 是看 host 全局最新 shard 年龄,不区分自己有没有写)。

实际定位三步:
1. `du -sh /data/realtime/*/*/l2` 在 cloud-sync 容器里 — 看到 gmo/* 都是 0B
2. `rclone lsd gdrive:/narci_raw/realtime/` — 看到 gdrive 上压根没 gmo 顶层目录
3. 平行开 **独立 WS 客户端**(`/tmp/gmo_probe.py`,不动 recorder)接 GMO 拉 8 条 raw msg,看 `symbol` 字段实际值

这三步现在已经默认包含在 `reco/aws/health_probe.sh` 里(① ② 默认开,③ 是 ad-hoc 诊断)。

## 根因

`data/exchange/gmo.py:238-239`:

```python
rec_sym = sym_native.lower()                            # WS 收 "BTC" → "btc"
if not ch or rec_sym not in recorder.symbols:           # recorder.symbols=["BTC","ETH","XRP"]
    continue                                            # "btc" not in list → 丢
```

实测 GMO 推回的 `symbol` 字段:

| Subscribe | 收到 `msg["symbol"]` |
|---|---|
| `BTC` (spot) | `'BTC'` |
| `BTC_JPY` (leverage) | `'BTC_JPY'` |

GMO 不会大小写规范化,subscribe 啥它推啥(全大写,跟 `to_native` 的 `.upper()` 输出一致)。所以 `.lower()` 之后跟 `recorder.symbols`(从 yaml 读入保持大写)永远 mismatch。

对比同一 codebase 里其他 adapter 为什么不踩这个坑:bitflyer/coincheck/binance 各自 `recorder.symbols` 跟 WS 推 `symbol` 大小写匹配(各自约定不同,但 adapter 跟 config 自洽)。gmo 是唯一一处 adapter 内规范化方向跟 config 不一致。

## 修复

`data/exchange/gmo.py:238`:
```python
- rec_sym = sym_native.lower()
+ rec_sym = self.to_std(sym_native)   # to_std() 已经 .upper(),跟 recorder.symbols (yaml 大写) 对齐
```

commit: `<TBD by next commit>`

## 验证手段

修复 push 后(narci-reco 角色):

```bash
./aws/recorder_restart.sh jp --pull --service recorder-gmo-spot
./aws/recorder_restart.sh jp --pull --service recorder-gmo-leverage
sleep 600    # 等第一个 save cycle (gmo save_interval ≈ 600s)
./aws/health_probe.sh jp --rclone
```

预期:
- `gmo/spot/l2` `gmo/leverage/l2` 体积从 0 升到 MB 级
- `rclone lsd gdrive:/narci_raw/realtime/` 列表里出现 `gmo/`

## Lessons

1. **`/health` endpoint 不可信** — 它看 host 全局,不看自己。后续给 narci 角色提:per-recorder 自检 (这个 PID 是否真在 buffer 写入) 比 global file age 更可靠
2. **`STALE-VENUE-ALERT` 早就在叫** — cloud-sync 自带的 watchdog 第一个 sync 周期就报 `total_shards=0`,我之前误以为是「数据还没流到」,实际是终态信号
3. **GMO 是 case-sensitive subscribe/echo** — 其他 adapter 不能照搬这个假设,新 adapter 上线前应 ad-hoc WS probe 验一遍 case 行为

## 时间线

| UTC | 事件 |
|---|---|
| 10:03:22 | JP 升 t4g.medium 后 4 个新 recorder 启动,gmo WS 连上 + subscribe |
| 10:05 | cloud-sync 第一个周期,STALE-VENUE-ALERT gmo total_shards=0(被忽视) |
| 10:13:22 | bitflyer 第一个 save cycle 落盘,gmo 仍无 save 活动 |
| ~10:50 | 用户问「gdrive 同步成功了吗」,触发深度诊断 |
| 10:55 | 定位到 gmo.py:238 lower/upper 不匹配 |
| 10:58 | 平行 WS probe 实测确认 GMO 推 `'BTC'` 大写,bug 证实 |
| (TBD) | narci 角色 1-line fix push,narci-reco --pull 重启 |
