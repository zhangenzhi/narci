# narci → echo-air — reply to INTERFACE_NARCI.md

> 这份是 **narci** 团队回给 **echo** 团队的对应文档。回应 echo 端
> `docs/INTERFACE_NARCI.md` (authoritative 2026-05-14, echo SHA `03bb63b`)
> 提出的契约,声明 narci 这边的对齐确认、承诺、回向请求,以及不在 narci
> scope 内的事项。
>
> Authoritative as of **2026-05-15**, narci git SHA `84354aa` on `main`.
> Re-cut whenever 这份的承诺或 schema 解读变化。

---

## 1. 确认 (echo 不用做事)

### 1.1 Schema v1.1 已对齐
narci 端 `calibration/schema.py:35` `SCHEMA_VERSION = "v1.1"`,与 echo bundle
中 pinned 的版本一致。echo 端的 `t.schema.equals(PARQUET_SCHEMA,
check_metadata=False)` assertion 走的就是 narci 这同一份代码路径,**不需要
echo 做任何 schema 兼容层**。

字段层面 narci 已确认存在 (default 值见括号):

| Field | Type | Default | 说明 |
|---|---|---|---|
| `trade_symbols` | `list[str]` | required | echo 实际下单的 subset,可以是 `symbols` 的真子集 |
| `echo_git_sha` | `str` | required | echo HEAD at session start |
| `narci_recorder_session_id` | `str` | `""` | cross-host pairing key,空字符串 = 故意不配对 (见 §2.1) |
| `narci_recorder_window_start_ns` | `int` | `0` | 同上 |
| `narci_recorder_window_end_ns` | `int` | `0` | 同上 |

### 1.2 narci 这边的 ingest 入口
`/lustre1/work/c30636/narci/calibration/inbox/` 已就绪,Phase 0a 的手动
`scp -r` tarball 直接落到这个目录即可,narci 的 calibration replay 会自动
识别 session 目录结构 (`meta.json` + 4 个 subdir)。**S3 sync 上线之前,
scp/rsync 不阻塞 narci 任何工作流。**

### 1.3 narci HEAD 比 bundle 领先 4 commits — 无 schema 破坏
echo bundle 的 narci SHA = `555bb3072ee0c7d29...`,narci 当前 HEAD =
`84354aa`。差异:

- 唯一语义变化是 `basis_um_bps_trade_proxy` fallback (cold tier 没有
  bookTicker 时用 last-trade-price 推算 mid),见 §4.1。
- **没有 schema / 字段名 / 字段类型变化**。
- 没有 strategy_class 接口变化。

echo 按自己的发布节奏升级即可,不急于追平。如果未来 narci 出 v1.2 schema
(目前没计划),narci 这边会提前通知并保持至少一个 release 的双版本兼容窗口。

---

## 2. narci 这边的承诺 / 待做

### 2.1 Cross-host pairing handshake 协议 (narci owns)

echo 当前 `meta.json` 里 `narci_recorder_session_id` /
`narci_recorder_window_start_ns` / `narci_recorder_window_end_ns` 三个字段
填空 (`""` / `0` / `0`)。**这是 narci 这边欠的协议设计**,echo 不用改任何
东西。

narci 这边的承诺:

1. **草案 deadline**:不晚于 2026-06-15 出 spec 第一版 (放在
   `docs/CROSS_HOST_PAIRING.md`),设计 echo↔narci-recorder 之间的握手机
   制 (轮询 recorder 健康 endpoint? UDP 时间戳广播? 还是简单从
   `replay_buffer/` 文件名反推 window?)。
2. **过渡期约定**:在 spec 落地前,**echo 把这三个字段保持空值即可**。
   narci 的 calibration replay 会把空字段当作 "intentionally unpaired"
   (而不是 "missing data") 处理 —— 这块逻辑在 narci 端,echo 不用关心。
3. **重要拓扑澄清** (写进 spec):**echo-air 后续要 pair 的 narci recorder
   不是 narci-t4g `i-0d73599f6798cfa1e`** (那个是 binance-global UM
   recorder),**而是 aws-sg 上的 binance_jp + coincheck 联合 recorder**。
   两台都在 ap-northeast-1 region,但 **不同 AZ**,跨 AZ hop ~1 ms。CC L2
   流来自 aws-sg recorder,这点会在 spec 里写死。

### 2.2 Calibration replay 对空 decisions/fills/cancels 的处理
echo §4 自报 `NaiveMakerStrategy` 没起来,跑成了默认 `EventStrategy`,导致
decisions/fills/cancels shard 全空。**narci 这边明确**:

- replay 把 "session_id 存在 + raw_l2 非空 + decisions/fills/cancels 全
  空" 这种 case **视作 "no strategy activity"**,**不是 data gap**,不会
  触发 alert。
- 等 echo 修好 `ln -sf → cp -f` 部署 bug 之后,replay 会自动识别非空 shard
  并启动 fill latency / decision lag 校准。echo 这边不用通知 narci 切换
  模式。

---

## 3. 反馈 / 请求 echo

### 3.1 NaiveMakerStrategy 修好后请发个完整 session 样本

`ln -sf → cp -f` 修好、`NaiveMakerStrategy` 真的跑起来之后,**请送一个
decisions/fills/cancels 都非空的 5-10 min session 过来** (scp 到
`calibration/inbox/` 即可)。narci 这边需要:

- 在真实数据上 round-trip 验证 `DecisionEvent` / `FillEvent` /
  `CancelEvent` 三个 schema (目前只验过 `RawL2Sidecar`)。
- 校准 fill latency 模型 —— 至少要看到 place→fill 或 place→cancel 的
  时间戳分布,才能给 backtest broker 设合理的 maker matching slack。

期望时间:**Phase 0b 启动前**。

### 3.2 Test 6 (`cc_test_order_lifecycle`) 资金/时间表的可见性
echo §4 把这个标成 "deferred — Coincheck account not funded"。narci 这边
没法替 echo 决定充钱节奏,但**希望进 Phase 0b 之前能跑一次完整 place→
cancel 闭环**,作为 fill latency 校准模型的 ground truth。

如果资金到位时间未定,**请在 echo 这份 INTERFACE_NARCI.md 的 §4 加一个粗
略 ETA 字段** (例如 "blocked on funding, expected by 2026-06-XX"),narci
这边好排自己的 Phase 0b 计划。

### 3.3 (Nice-to-have) echo-air HTTP health endpoint

目前 narci 监测 echo 健康的唯一手段是 scp poll `/var/lib/echo/logs/`
看最新 shard 的 mtime —— 延迟高、噪声大。

**如果 echo 能在 echo-air 上暴露一个 `/health` HTTP endpoint** (类比 narci
recorder 的 `:8079/health`,见 `deploy/healthcheck.py`),narci 这边可以接
一个 cron 监控:

- session 是否处于 "running" 状态
- 上一个 raw_l2 shard 距今多少秒
- WS 当前是否 connected
- 上次 reconnect 时间

这样 echo 的 `Restart=on-failure` + `StartLimitBurst=5` 一旦真的触发,
narci 这边能秒级感知,不用等 24h 后才发现 session 断了。

不是 hard requirement —— 但 echo 如果有时间,Phase 0b 之前加上会很顺。

### 3.4 (可选) `meta.json` 加一个 `narci_features_consumed` 字段

如果 echo 的策略读 narci feature cache (例如 `basis_um_bps`、`imbalance`
等),希望 echo 在 `meta.json` 里列出**这个 session 实际用过哪些 feature
名**。这样 narci 后续改 feature semantics (例如 §4.1 的 trade_proxy
fallback) 时,可以反查哪些历史 session 受影响。

字段建议:

```json
{
  "narci_features_consumed": ["basis_um_bps", "imbalance_5", "spread_bps"]
}
```

不在 v1.1 schema 必填范围,可以作为 v1.2 candidate。echo 这边觉得 OK 的话
narci 这边会发 schema 升级 PR。

---

## 4. Heads-up / 数据质量警示

### 4.1 冷数据 `basis_um_bps` 在 2026-04-17 → 05-09 有 NaN 段

narci v5 加了 `basis_um_bps = log(um_mid / bs_mid) * 1e4` 期现 basis
feature。**`bs.mid_price` 依赖 spot best_bid/ask,但 Binance Vision 上 spot
bookTicker 从未存在,UM bookTicker 停更在 2024-03-30** (详见
`deploy/donor/NARCI_DONOR_INTERFACE.md` "bookTicker 不可用" 段)。

影响:

- **冷 tier (04-17 → 05-09 这 23 天) 的 `basis_um_bps` 走 trade_proxy
  fallback** (commit `84354aa`),用 last-trade-price 当 mid,精度差。
- **从 narci spot recorder 启动 (05-10 之后) 开始**,`bs.mid_price` 是真实
  WS bookTicker,基础准确。

**对 echo 的影响**:如果 echo 的策略 (现在或将来) 读 narci feature cache
里的 `basis_um_bps`,**04-17 → 05-09 这段数值不可信**。建议策略加 sanity
check:

```python
if session.start_date < datetime(2026, 5, 10):
    assert features.get("basis_um_bps_source") != "trade_proxy"
```

narci 这边后续会在 feature cache 里加 `_source` 字段标注是 bookTicker
还是 trade_proxy,echo 可以据此 fail-fast。

### 4.2 narci CC recorder 的 venue quirks

如果 echo 的 calibration replay 想直接读 narci 这边的 CC L2 原始 parquet
(走 `replay_buffer/realtime/coincheck/spot/l2/` 路径),要注意:

- **side 编码**:`0`=bid update, `1`=ask update, `2`=aggTrade (qty 负值 =
  seller maker), `3`=bid snapshot, `4`=ask snapshot。每个 raw shard 文件开
  头都有一组 (side=3, side=4) snapshot,文件**自包含**,不需要外部 prev
  state。
- **CC 没有 update-id 对齐**:不像 Binance 用 U/u 字段,CC 是 REST snapshot
  + WS diff,只能按时间戳重放。
- **CC trade 帧是 list-of-lists**:一个 WS 帧可以带多个 trade,narci
  adapter 已展开,echo 不用关心,但如果 echo 直接读 CC 原始 WS (不通过
  narci),会踩这个坑。

### 4.3 narci CC recorder 不在 narci-t4g
重复 §2.1.3 提到的拓扑:**narci CC recording 在 aws-sg 那台联合 recorder
上,不在 echo-air 同 AZ 的 narci-t4g**。

跨 AZ 但同 region 的延迟特征:

- echo-air (ap-northeast-1a) ↔ narci-t4g UM recorder (1a): < 2 ms
- echo-air (ap-northeast-1a) ↔ aws-sg CC recorder (other AZ): ~1 ms (跨 AZ)

如果 echo 的策略对 narci CC 行情有 latency 敏感的依赖,**注意 1 ms 跨 AZ
hop** —— 但远小于 CC WS 本身的 jitter (CC 公网 WS,典型 RTT 几十 ms),
所以应该不是实际瓶颈。

### 4.4 narci recorder 健康状态可见
echo 这边如果想知道 narci recorder 是不是 alive (避免 pair 到一个掉线
的 narci window):

- narci-t4g (binance UM): `http://<narci-t4g-private-ip>:8080/health`
- aws-sg (binance_jp + CC 联合): `http://<aws-sg-private-ip>:8081/health`
- 端口都在 narci recorder 的 healthcheck (见 `deploy/healthcheck.py`)。

跨 host 私网访问需要 security group 放行 —— echo-air 和 narci-t4g 在同一
个 `launch-wizard-3` SG (echo 已 confirm),aws-sg 那边 narci 这边会去开
ingress。完成后写进 §2.1 的 pairing spec。

---

## 5. 不在 narci scope (留给 echo 自己)

为避免越界,以下事项 narci 不会主动跟进,echo 自己 own:

- `ECHO_AZ` / `ECHO_INSTANCE_TYPE` 环境变量没接 systemd unit —— echo deploy
  follow-up。
- `ln -sf → cp -f` 那个部署 bug (导致 `NaiveMakerStrategy` 没起来) —— echo
  deploy bug fix。
- Coincheck 账户充值与合规 —— echo / 运营。
- AWS Secrets Manager `echo/coincheck` 的 key rotation cadence —— echo
  ops。
- S3 sync (`s3://echo-logs-383941187690/`) 的 bucket 创建、IAM、生命周期
  —— Phase 0b echo ops。
- echo-paper 容器内的 strategy code (`narci.backtest.strategy.*`) ——
  虽然 import 自 narci 模块,但运行时挂载 / 加载 / 异常重启都是 echo
  ops。narci 只保证模块语义稳定。

---

## 6. Retrieval / 联系

### narci 这边的 ingest 路径

```bash
# echo-air → narci /lustre1 (Phase 0a)
scp -i ~/.ssh/aws-narci.pem -r \
    ec2-user@<echo-air-ip>:/var/lib/echo/logs/<session_id>/ \
    /lustre1/work/c30636/narci/calibration/inbox/

# 落到 inbox 之后 narci replay 会自动 pick up
```

### 文档维护
- 这份文档由 narci 在 `narci_git_sha` 变化引发对 echo 契约的影响时重新
  cut (例如 schema bump、recorder topology 变动、pairing spec 出台)。
- echo 端对应的 `docs/INTERFACE_NARCI.md` 在 echo 这边维护。两份文档**互
  相不引用对方源码路径,只引用对方 git SHA + 文档段落**,避免 cross-repo
  symlink rot。
- 协议级争议:narci 端 owner = `zhangenzhi@narci`,通过 git PR comment 或
  直接邮件 `zhangsuiyu657@gmail.com` 联系。

### Changelog
- **2026-05-15** (`84354aa`) — initial cut,响应 echo `INTERFACE_NARCI.md`
  authoritative 2026-05-14 (`03bb63b`)。
