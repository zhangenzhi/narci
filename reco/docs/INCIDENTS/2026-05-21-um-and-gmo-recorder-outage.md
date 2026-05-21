# 2026-05-21 binance/um_futures + gmo (spot+leverage) 双 recorder 静默死亡 — narci → narci-reco handoff

- **Severity**: 高(UM 已 ~2.5 天全无数据;gmo 已 ~24h 全无数据)
- **Status**: 🟡 **OPEN / 待 narci-reco 接管**
- **First detect**: 2026-05-21 ~02:00 UTC(narci 端 cold-tier 例行核查发现)
- **Affected venues**:
  - `binance/um_futures`(aws-sg)— UM perpetual,6 symbols(BNB/BTC/DOGE/ETH/SOL/XRPUSDT)
  - `gmo/spot`(aws-jp)— BTC / ETH / XRP
  - `gmo/leverage`(aws-jp)— BTC_JPY / ETH_JPY / SOL_JPY / XRP_JPY
- **Time window**:
  - **UM**:**~2026-05-18 00:00 UTC** 起完全无数据;0520 短暂复活 ~0.5h(00:00-15:15 UTC,150 shards),然后又死;0521 至今无数据 → **覆盖约 2 天 12h**
  - **gmo**:**~2026-05-20 00:00 UTC** 起完全无数据,**至今 ~24h**
- **Resolution commit**: TBD(narci-reco 端 SSM 重启 + 必要时 narci 端代码 fix)
- **关联事故**:
  - [2026-05-17-gmo-watchdog-silent-death.md](./2026-05-17-gmo-watchdog-silent-death.md) — 同 venue gmo 第 3 次静默死亡(0516 case-mismatch、0517 watchdog `ws.closed`、本次根因 TBD)
  - [2026-05-17-gmo-rate-limit-reconnect-storm.md](./2026-05-17-gmo-rate-limit-reconnect-storm.md) — 同 fix 系列
  - [2026-05-16-gmo-silent-drop.md](./2026-05-16-gmo-silent-drop.md) — 同 venue 第 1 次

  UM 是首次进 INCIDENTS。**两 venue 不同 host 同时挂**(UM=aws-sg,gmo=aws-jp)— 非共因,纯巧合或各自独立 bug,不要混淆。

## Symptom — narci 侧实测

narci(`/lustre1/work/c30636/narci`)端 hot tier shard 状态(2026-05-21 01:58 UTC):

| Venue | 0518 shards | 0519 shards | 0520 shards | 0521 shards (UTC 进行中) | 0520 最后 shard 时间 |
|---|---|---|---|---|---|
| coincheck/spot | 884 ✅ | 874 ✅ | 996 ✅ | 56 | 0520 23:52 UTC |
| binance_jp/spot | 3480 ✅ | 3080 ✅ | 3455 ✅ | 192 | 0520 23:56 UTC |
| binance/spot | 3744 ✅ | 2334 ✅ | 3640 ✅ | 182 | 0520 23:53 UTC |
| bitbank/spot | (健康) | (健康) | 720 ✅ | 40 | 0520 23:52 UTC |
| bitflyer/spot+fx | (健康) | (健康) | 432+143 ✅ | 24+7 | 0520 23:50 UTC |
| **binance/um_futures** | **0** 🚨 | **0** 🚨 | **150** 🚨 (~0.5h coverage) | **0** 🚨 | **0520 15:15 UTC**(然后死) |
| **gmo/spot+leverage** | (健康) | (健康) | **0** 🚨 | **0** 🚨 | 0519 23:59 UTC 之后无 |

cold tier 推论(narci `replay_buffer/cold/`):
- `binance/um_futures` 最新归档 = **20260517 DAILY**(0518/0519 全空,无 shards 可 compact)
- `gmo/spot` + `gmo/leverage` 最新归档 = 20260519 DAILY(0520 cron 跑完会缺,因 0 shards)
- 健康 venue 最新归档 = 20260519 DAILY,等今天 12:00 JST cron 进 0520(预计正常)

narci 端 12:00 JST cron 行为预测:
- 健康 venue:0520 DAILY 落 cold ✅
- UM 0520:partial 150 shards → 会归档成 partial daily(后续 backfill 覆盖)
- gmo 0520:0 shards → cron `未找到 20260519 的 gmo/* 1min 原始碎片` 类警告,no archive
- 0521(今天 UTC 未结束):per `feedback_compact_utc_day_boundary` archive_to_cold 守护拒绝,**正常**

## 根因 — 待 narci-reco 查

narci 端不能 ssh aws-sg / aws-jp(crypto network 禁,per memory
`feedback_no_crypto_network`),所以下面是**症状推测**,不是根因。narci-reco
端跑 `./aws/recorder_restart.sh sg --ps-only` 和 `./aws/recorder_restart.sh
jp --ps-only` 看 docker compose ps,以及 `docker compose logs --since 36h
recorder-umfut` / `recorder-gmo-spot` / `recorder-gmo-leverage` 才能定位。

**UM 形态特征**(供 ops 参考):
- 0518/0519 完全 0 shards = WS 从未起来过 / connect 但 0 message
- 0520 头 15h 又写了 150 shards(~0.5h 实际,save_interval=60s → 150 shards = 150 个 1-min 切片 = 2.5h?需核 narci-reco)
- 0520 15:15 UTC 后又死
- → 像是 **WS 反复 connect-die-reconnect**,不是 container OOM(若 OOM 会立刻被 supervisord/docker restart 但起不来时间会更短) → 怀疑 **Binance UM endpoint 切换 / IP 封锁 / rate-limit**

  ⚠️ 见 memory `project_binance_um_market_endpoint` — 此前(0423)Binance UM
  split `/depth` 和 `/trade` 到 `/public` + `/market` endpoints,需 DUAL WS
  (real fix `4582d02`)。**核查 aws-sg recorder 用的 endpoint 是否被 Binance
  又调过 / 是否有 endpoint config 没追上**。也核查 aws-sg IP 是否被
  Binance 又封了(JP region IP 历史上有过被封)。

**gmo 形态特征**(供 ops 参考):
- 0520 00:00 UTC 起完全 0 shards(整 24h 干净的零)
- gmo 已 3 次同类事故:`5/16` case-mismatch、`5/17 早` watchdog `ws.closed`、`5/17 中` rate-limit reconnect storm。本次第 4 次。
- 不知道当前是哪个 bug 复发还是新 bug;narci-reco 看 `docker compose logs
  --since 36h recorder-gmo-spot recorder-gmo-leverage` 第一时间能判
- → 怀疑 watchdog/reconnect 系列又 regress(memory 提示 `5/17` 两次都是
  watchdog 静默死);也可能是 **GMO endpoint 切换 / API key 过期 / IP 封**

## Fix — narci-reco 端 ops recipe

### 1. 先 ps-only 看状态(2 命令并行)

```bash
cd ~/narci/reco
./aws/recorder_restart.sh sg --ps-only       # aws-sg (UM)
./aws/recorder_restart.sh jp --ps-only       # aws-jp (gmo + 其他健康 venue)
```

期望:gmo 两个 container 跟 UM container `docker compose ps` 都 `Up X hours`
但**healthy 状态可能误报**(precedent: `2026-05-17-gmo-watchdog-silent-death.md`
的核心教训之三 — healthy ≠ data healthy)。如果显示 Up,**healthcheck blind spot
又一次**;如果显示 Exited / restarting,直接看 logs。

### 2. 拉 36h logs 定位根因

```bash
# aws-sg UM
aws ssm send-command --instance-ids "$NARCI_SG_INSTANCE_ID" \
  --region "$AWS_REGION_SG" \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["cd /home/ubuntu/narci && docker compose logs --since 60h --tail 500 recorder-umfut 2>&1 | tail -300"]' \
  --output text --query 'Command.CommandId'
# 等几秒后 aws ssm get-command-invocation 取 stdout

# aws-jp gmo
aws ssm send-command --instance-ids "$NARCI_JP_INSTANCE_ID" \
  --region "$AWS_REGION_JP" \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["cd /home/ec2-user/narci && docker compose logs --since 36h --tail 300 recorder-gmo-spot recorder-gmo-leverage 2>&1 | tail -400"]' \
  --output text --query 'Command.CommandId'
```

或者直接走 `aws/health_probe.sh` 如果它会拉 logs(narci-reco 自己看)。

### 3. 重启(若 logs 无新洞 / 已有 patch 在路上)

```bash
# UM 单服务 restart(快)
./aws/recorder_restart.sh sg --service recorder-umfut

# 如果 narci 端有相应代码 fix（比如 endpoint config 改了 / watchdog 又改了），需 git pull + build
./aws/recorder_restart.sh sg --pull

# gmo
./aws/recorder_restart.sh jp --service recorder-gmo-spot
./aws/recorder_restart.sh jp --service recorder-gmo-leverage
# 或一次性
./aws/recorder_restart.sh jp --pull
```

### 4. 监测 t+10min(save_interval = 60s → 10 个 1-min shard 应到 hot tier)

narci 端(lustre1)看 hot tier 是否有新 shard:

```bash
# narci-reco 跑(在 Mac Studio 上 ssh narci lustre1 或者用任何能看到 hot tier 的方式)
ls -lt /lustre1/work/c30636/narci/replay_buffer/realtime/binance/um_futures/l2/ | head -5
ls -lt /lustre1/work/c30636/narci/replay_buffer/realtime/gmo/spot/l2/ | head -5
ls -lt /lustre1/work/c30636/narci/replay_buffer/realtime/gmo/leverage/l2/ | head -5
```

期望 t+15min(rclone sync 默认 300s 一轮,加上写盘耗时):看到 timestamp ≥
当前时间 - 15min 的新 shard。

### 5. backfill 决策

| Venue | 缺失日期 | 可 backfill? | 路径 |
|---|---|---|---|
| binance/um_futures | 0518 / 0519 / 0520(尾) | ✅ Yes | Binance Vision 有 `um_futures/aggTrades` + `bookTicker`。**narci-donor 跑 [[project_external_validation_via_drive]] 流程**:Vision 下载 → `gdrive:narci_official` → lustre1 pull → 跑 `compact --force --symbol BTCUSDT --date 20260518` 等重写 cold。注意 `BINANCE_VISION_OFFLINE=1`,**必须 donor 先 push 完**,lustre1 不会主动 fetch。 |
| binance/um_futures | 0521(进行中) | ⚠️ 看恢复时机 | 若 0521 12:00 UTC 之前 recorder 复活,0521 至少能保半天 hot tier;否则也走 donor backfill。 |
| gmo/spot | 0520 / 0521 | ❌ No | gmo **不在 Binance Vision 上**,无第三方 archive。**纯 data loss**。 |
| gmo/leverage | 0520 / 0521 | ❌ No | 同上。 |

gmo 数据丢失影响评估:
- narci 当前 binding 家族(v9_eth_midy / v9_bj_midy 等)**没有用 gmo features**,
  research 不阻塞
- 但 gmo 反复死(4 次)说明该 venue recorder 不稳,建议本次修完后 narci 角色
  考虑加 integration test(per `2026-05-17-gmo-watchdog-silent-death.md` 教训 4)

## Verify(narci 端)

修复后 narci-reco 通知 narci 角色(本 incident 文档加 close 注),narci 端跑:

```bash
cd /lustre1/work/c30636/narci
# 健康
python -c "
import pandas as pd
from pathlib import Path
import datetime as dt
COLD = Path('replay_buffer/cold')
for venue, sym, days in [
    ('binance/um_futures', 'BTCUSDT', ['20260518','20260519','20260520']),
    ('gmo/spot', 'BTC', ['20260520']),
    ('gmo/leverage', 'BTC_JPY', ['20260520']),
]:
    for d in days:
        p = COLD / venue / f'{sym}_RAW_{d}_DAILY.parquet'
        if p.exists():
            df = pd.read_parquet(p)
            print(f'{venue}/{sym} {d}: {len(df):,} rows | '
                  f'ts span {dt.datetime.utcfromtimestamp(df.timestamp.min()/1000)} → '
                  f'{dt.datetime.utcfromtimestamp(df.timestamp.max()/1000)}')
        else:
            print(f'{venue}/{sym} {d}: MISSING')
"
```

期望:UM 0518/0519/0520 全 24h 覆盖(若 donor backfill 完成);gmo 0520 标
data-loss(0 rows 或 file absent)。

跑 `data/sanity_gate.py`(若有 per-day per-venue audit)再 cross-check。

## Lessons / TODO(本次 incident 加进去)

1. **per-venue healthcheck per-recorder 化(narci task #85,长期 pending)**:
   `docker compose ps healthy` 跟 `/health` 200 都不能反映单 venue 数据流。
   GMO 已经 4 次 silent death 一次都没被 healthcheck 抓到。narci 角色这次
   要把 `deploy/healthcheck.py` 改成读 `replay_buffer/realtime/*/l2/`
   ts max 与 now 差 > 10min 就报 unhealthy。**优先级从 [非紧急] 升 [P1]**。
2. **UM endpoint 漂移**:Binance UM 历史上有过 `/public` vs `/market` split
   (0423,`4582d02` real fix)。本次 0518 起 UM 完全停 = 可能又是 endpoint
   漂移 / IP 封。建议 narci-reco 端跑 ad-hoc WS probe(`exchange/binance.py`
   起 1 个 connect 看 echo)在 ssh 上线后做 sanity。
3. **gmo 第 4 次同形态故障**:再修一次后,narci 角色应该排个独立
   integration test,跑 5 min 真 WS,确认 message 入 buffer。`reco/docs/
   INCIDENTS/` 已经有 3 篇 gmo 同形态,这是产品级的"反复 regress"。
4. **narci 缺主动 outage alert**:本次靠手动核查 cold-tier 发现,如果 narci
   end 没人例行查可能 UM 死 1 周才发现。建议 narci 端跑个 daily cron:
   per-venue per-day shard count check,过 threshold 写 STALE-VENUE-ALERT
   到 `replay_buffer/_alerts/` 并 echo 到 Mac Studio。

---

**handoff status**:narci 角色已写完本 doc + commit。**narci-reco 角色接手** —
跑 step 1-4 修 UM/gmo,donor 角色跑 backfill UM(独立工作流)。修完回此 doc
加 `## Resolution`/`## Verify` 节、改 status 为 ✅ CLOSED。

narci 端无主动 fix 动作(crypto network 禁,本次状态分析 + handoff 是
narci 域 boundary 的全部责任)。
