# narci ↔ donor host interface

Donor 端 = 可以访问 Binance Vision 的机器（Mac mini / linux box），
负责把 `data.binance.vision` 上的官方归档拉下来推到 gdrive，给
`/lustre1` 这边消费（[[feedback_no_crypto_network]]）。

## Topology

```
data.binance.vision
        │  https
        ▼
   Donor host  ────────► binance_vision_push.sh
   (Mac mini /              │
    linux box)              │  rclone copy
                            ▼
                  gdrive:narci_official
                            │
                            │  rclone copy (auto via cloud-sync)
                            ▼
                  /lustre1/work/c30636/narci/
                  replay_buffer/official_validation/
```

## 常驻

- `deploy/donor/donor_loop.sh` — tmux 后台跑，每 24h 一轮（`INTERVAL=86400`）
- `deploy/donor/binance_vision_push.sh` — 单次执行：`main.py download` + `rclone copy`
- 配置：`configs/downloader.yaml`（symbols / data_types / date_range）

## Manual trigger

```bash
ssh donor
cd ~/narci
git pull origin main          # 拿最新 downloader.yaml
bash deploy/donor/binance_vision_push.sh
# tail -f .donor/binance_vision_push.log
```

---

## 当前在拉的内容（截至 2026-05-14）

| Symbol | Market | Data type | 用途 |
|---|---|---|---|
| BTCUSDT / ETHUSDT / SOLUSDT / BNBUSDT / XRPUSDT / DOGEUSDT | spot + um_futures | aggTrades | trade backfill |
| BTCJPY / ETHJPY / SOLJPY / XRPJPY / DOGEJPY | spot only | aggTrades | Coincheck 主战场参考 |

---

## ⚠️ bookTicker 不可用（donor 端 2026-05-14 实测）

narci commit `c5eac4b` 和 `abdb9f5` 假设 Vision 有 spot bookTicker，donor
端 pull + 手动 trigger 一次实测后发现 **Vision 上根本拿不到我们要的
bookTicker**：

| 路径 | 实际情况 |
|---|---|
| `data/spot/daily/bookTicker/` | **从未存在** — spot daily 只有 aggTrades / klines / trades，S3 listing 直接没有 bookTicker prefix |
| `data/futures/um/daily/bookTicker/` | 存在，**但最后一天是 2024-03-30**，已停更 2 年多。S3 marker pagination 验证 2024-03-31 之后无任何 .zip |

直接验证命令：

```bash
curl -s "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/spot/daily/" | grep -oE "<Prefix>[^<]+</Prefix>"
# → aggTrades / klines / trades  (没有 bookTicker)

curl -s "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=data/futures/um/daily/bookTicker/BTCUSDT/&marker=data/futures/um/daily/bookTicker/BTCUSDT/BTCUSDT-bookTicker-2024-04-01.zip" | grep -c "<Key>"
# → 0 (2024-03-31 之后无任何 key)
```

因此 v5 `basis_um_bps` 用 Vision bookTicker 回补 2026-04-17 → 05-09 这条
路线走不通。

**donor 端处理（2026-05-14）**：从 `configs/downloader.yaml`
的 `data_types` 撤回 `bookTicker`（见同次 commit），避免每天 cron 跑 459 个
无用 404。继续只拉 aggTrades。

**narci 端需要重新评估**：

- 选 1：用 Tardis.dev（已经在 `data/historical/tardis.py` 接好，支持 L2
  depth + bookTicker，但是收费的）
- 选 2：让 recorder 实时录 spot bookTicker WS（`@bookTicker` channel），
  从今天开始有数据，2026-04-17 → 05-09 这段无法 backfill
- 选 3：用 aggTrades 推算 mid（last-trade-price proxy），精度差但能跑

`data/backfill_vision_bookticker.py` 这个脚本本身是对的（CSV schema、
narci side=3/4 转换都对），只是源数据没有，所以暂时跑不出结果。

---

## 2026-05-14: 请求 bookTicker 加速回补（已 BLOCKED，见上方"bookTicker 不可用"）

**背景**：narci v5 加了 `basis_um_bps = log(um_mid / bs_mid) * 1e4` 期现 basis
feature (commit `555bb30`)。BS = Binance global spot BTCUSDT/ETHUSDT。

`bs.mid_price` 需要 best_bid/best_ask 数据。当前 cold tier 04-17 → 05-09
只有 aggTrades（trade-only），没有 depth → basis_um_bps 全 NaN。

Vision **有 bookTicker 公开归档**（per-update best_bid/ask 流），刚加进
narci downloader.yaml（commit `c5eac4b`）。后续 cron 自动拉。

### 请donor 操作（按顺序）

1. **pull narci 拿新的 downloader.yaml**：
   ```bash
   ssh donor
   cd ~/narci
   git pull origin main
   ```
   确认 `configs/downloader.yaml` 的 `data_types` 列表里有 `bookTicker`。

2. **立刻手动跑一次**（不等明天 04:30 cron）：
   ```bash
   bash deploy/donor/binance_vision_push.sh
   ```

   日志在 `.donor/binance_vision_push.log`。bookTicker 比 aggTrades 大
   5-10x，所以这一轮会比平时慢，预计 ~30-60 分钟（取决于 donor 网速）。

3. **回补 nyx 训练池的 23 天 BTCUSDT/ETHUSDT spot bookTicker**

   `configs/downloader.yaml` 的 `start_date: "2025-09-01"`，所以这一轮会拉
   2025-09-01 至昨天的所有 bookTicker。对 v5 basis_um 真正关键的是
   2026-04-17 → 2026-05-09 这 23 天 spot/{BTCUSDT,ETHUSDT}/bookTicker。

   donor 拉完 + push gdrive 后，/lustre1 这边会 rclone copy 自动同步
   到 `replay_buffer/official_validation/spot/bookTicker/`。

4. **通知**：跑完用 `gh issue comment` 之类的方式 ping 一下 narci 这边？
   或者就把 push log 最后几行 ssh tail 出来：
   ```bash
   tail -20 ~/narci/.donor/binance_vision_push.log
   ```

### 我（narci 端）那一步做什么

donor 跑完 + /lustre1 rclone sync 完之后，narci 这边跑：

```bash
cd /lustre1/work/c30636/narci
DAYS=$(seq -f "202604%02g" 17 30 | tr '\n' ',')$(seq -f "202605%02g" 1 9 | tr '\n' ',')
for SYM in BTCUSDT ETHUSDT; do
  PYTHONPATH=. python3 -m data.backfill_vision_bookticker \
    --symbol $SYM --market spot --exchange binance \
    --days "${DAYS%,}"
done
```

把 bookTicker (side=3/4) merge 进 cold tier 已有的 trade-only daily 文件，
basis_um_bps 立即变 finite。

### 资源 / 风险

- **带宽**：bookTicker 每天 BTCUSDT spot ≈ 50-100MB compressed，每天 ETHUSDT
  spot ≈ 30-60MB。23 天 × 2 sym ≈ 2-3GB 一次性拉，之后每天 ~80-160MB。
- **存储**：gdrive 不限量；/lustre1 这边 official_validation 增量 ~3GB。
- **rate limit**：Vision 没有显式 rate limit。`max_workers: 3` 已经在 yaml
  里设了，conservative。
- **失败重试**：`retry.max_retries: 5`, `backoff_factor: 2`。单天偶尔 fail 不影响整体。

---

## 一般约定

- **不能 push 到 gdrive 的 path**：只用 `gdrive:narci_official`（在
  binance_vision_push.sh 的 `DRIVE_REMOTE` 默认值）。其它 path 是 narci
  recorder 的 raw shards（`gdrive:narci_raw`），donor 不要碰。
- **donor 端不跑 recorder**：donor 只负责 Vision 归档拉取，不录 WS。
- **配置同步**：所有 donor 配置改动都通过 `git pull` 同步，不在 donor 本地
  改 `configs/downloader.yaml`。

## 联系点

- 配置改动 / 加新 data_type：narci 这边改 `configs/downloader.yaml`，push 到
  origin/main。donor 下次 `git pull` 自动生效。
- donor 端 crash / 长时间没 push：narci 这边 `.donor/binance_vision_push.log`
  没新条目超过 48h 就该 ping donor host 操作员。

---
