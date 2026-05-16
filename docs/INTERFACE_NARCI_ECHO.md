# INTERFACE_NARCI_ECHO — narci → echo contract

> 这份是 **narci** 团队回给 **echo** 团队的对应文档。回应 echo 端
> `docs/INTERFACE_ECHO_NARCI.md` (echo SHA `82622ed` / `11957d4`,
> 之前文件名 `INTERFACE_NARCI.md`) 提出的契约,声明 narci 这边的对齐确认、
> 承诺、回向请求,以及不在 narci scope 内的事项。
>
> 命名约定:`INTERFACE_<author>_<audience>.md` (author 在前,跟 echo
> `3cad76f` refactor 对齐)。本文 narci 维护,与 echo `INTERFACE_ECHO_NARCI.md`
> 对偶。本文 2026-05-16 从 `INTERFACE_ECHO.md` 重命名为 `INTERFACE_NARCI_ECHO.md`。
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
3. **拓扑确认** (写进 spec):**echo-air 的 CC pair target IS narci-t4g**
   (= narci-tokyo, `i-0d73599f6798cfa1e`, ap-northeast-1a, t4g.small),
   跑 `recorder-coincheck` + `recorder-binance-jp` 两个容器,**和 echo-air
   同 AZ**,< 2 ms hop。Binance global spot + UM 在另一台
   **narci-sg** (= AWS-SG, ap-southeast-1, t4g.small),和 echo-air 跨
   region,跟 CC pair 无关。Source of truth:`docker-compose.yaml`
   (profile=tokyo / global) + `deploy/aws/README.md` (已在
   commit `<本次>` 修正 region 标错的问题)。
   *(2026-05-15 修正,纠正本文档前一版本错把 narci-t4g 标成 UM recorder、
   把 CC 标成"aws-sg cross-AZ"的反向错误;详见 §7.1。)*

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

如果资金到位时间未定,**请在 echo `INTERFACE_ECHO_NARCI.md` §4 加一个粗
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

### 4.3 narci CC recorder 拓扑 (本节 2026-05-15 重写)
正确拓扑:

| Host | Region / AZ | Records | echo-air → narci latency |
|---|---|---|---|
| **narci-t4g** (= narci-tokyo, `i-0d73599f...`) | ap-northeast-1a, t4g.small | Coincheck + Binance JP spot | **< 2 ms (同 AZ)** |
| **narci-sg** (= AWS-SG) | ap-southeast-1, t4g.small | Binance global spot + UM futures | ~70 ms (跨 region) — 跟 CC pair 无关 |

**echo-air 的 CC L2 pair target = narci-t4g** (同 AZ)。narci-sg 上的
Binance global / UM 数据走 gdrive 冷链交付,不参与 echo 实时 pair。

### 4.4 narci recorder 健康状态可见
echo 这边如果想知道 narci-t4g 上的 CC recorder 是不是 alive (避免 pair
到一个掉线的 narci window):

| Container | Host port | 用途 |
|---|---|---|
| `narci-recorder-coincheck` | `8079` on narci-t4g | CC L2 health |
| `narci-recorder-binance-jp` | `8080` on narci-t4g | Binance JP spot health |

(注:`docker-compose.yaml` 里 recorder-coincheck 映射宿主机 8079,
recorder-binance-jp 映射 8080;narci 内部容器端口都是 8079。)

narci-sg 上还有 binance-spot:8079 / binance-umfut:8080,但和 echo 的 CC
pair 无关,跨 region 走 gdrive。

跨 host 私网访问需要 security group 放行 —— echo-air 和 narci-t4g 在同一
个 `launch-wizard-3` SG (echo 已 confirm 共享 SG),narci 这边只需在 SG
inbound 上为 echo-air private IP 放行 8079/8080。完成后写进 §2.1 的
pairing spec。

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
- echo 端对应的 `docs/INTERFACE_ECHO_NARCI.md` 在 echo 这边维护(2026-05-16
  从 `INTERFACE_NARCI.md` 重命名)。两份文档**互
  相不引用对方源码路径,只引用对方 git SHA + 文档段落**,避免 cross-repo
  symlink rot。
- 协议级争议:narci 端 owner = `zhangenzhi@narci`,通过 git PR comment 或
  直接邮件 `zhangsuiyu657@gmail.com` 联系。

### Changelog
- **2026-05-15** (`84354aa`) — initial cut,响应 echo `INTERFACE_NARCI.md`
  authoritative 2026-05-14 (`03bb63b`)。(echo 端这份后来在 `3cad76f`
  重命名为 `INTERFACE_ECHO_NARCI.md`)
- **2026-05-15** (`289d61c`) — §2.1.3 / §4.3 / §4.4 拓扑重写;
  追加 §7 appendix 回应 echo `INTERFACE_NARCI.md §12` (echo SHA `82622ed`)。
  同时修 `deploy/aws/README.md` 的 region 错标 (narci-us → narci-sg)。
- **2026-05-15** (`6bb7663`) — §7.4 标 A17 "scaffold ready (`18e68b3`),
  deployment gated by ops capacity";§7.5 状态表更新 A15/A16/A17/cloud-sync 修复进度。
- **2026-05-16** (本次 commit) — 文件从 `INTERFACE_ECHO.md` 重命名为
  `INTERFACE_NARCI_ECHO.md`,统一 narci/echo/nyx 三边的
  `INTERFACE_<author>_<audience>.md` 命名约定 (跟 echo `3cad76f` 对齐)。
  内容无变化。

---

## 7. Appendix — 回应 echo `INTERFACE_NARCI.md §12` (2026-05-15, echo SHA `82622ed`)

### 7.1 Topology — narci §2.1.3 / §4.3 / §4.4 已重写,谁对谁错

echo §12.1 指出本文档前一版本 (narci SHA `429c3eb`) 拓扑错认,**主要责任
在 narci**。具体:

| 错认点 | 我之前的版本 | 实际正确 | echo 提出的版本 |
|---|---|---|---|
| narci-t4g 录什么 | "binance-global UM recorder" ❌ | **CC + Binance JP** ✓ | "CC + Binance JP" ✓ |
| echo-air CC pair target | "aws-sg, cross-AZ ~1ms" ❌ | **narci-t4g, same AZ < 2ms** ✓ | "narci-t4g, same AZ < 2ms" ✓ |
| binance global / UM 在哪 | (我隐含说在 narci-t4g) ❌ | **AWS-SG (Singapore, ap-southeast-1)** | "narci-us in us-west-2" ❌ |

**echo 在 CC 拓扑上 100% 对**(narci-t4g = narci-tokyo,同 AZ,< 2ms);
narci 之前的 §2.1.3 把 CC 和 UM 整个调换了,这是 narci 的错。**已在本次
commit 修正**。

**但 echo 在 UM 拓扑上 source 取错了** — `deploy/aws/README.md` 写
"narci-us / us-west-2 / t4g.micro" 是早期 plan,**没跟实际部署同步**。
narci 仓库代码里 4 处独立引用 `AWS-SG` 才是 source of truth:

- `features/realtime.py:334` — "BS recorder lives on AWS-SG, recording since..."
- `tools/probe_um_endpoints.py:3,10` — "Run from AWS-SG host" / "AWS-SG egress blocked"
- `calibration/tests/test_l2_recorder_refresh.py:202` — "out of recording 5h+ on AWS-SG 2026-05-09"
- (narci-side memory `project_aws_sg_oom`,基于 0507 journalctl 实证)

**已在本次 commit 同步修正 `deploy/aws/README.md`**,把 narci-us /
us-west-2 / t4g.micro 改成 **narci-sg / ap-southeast-1 / t4g.small**,
并加 explanatory warning 顶部。echo 后续如果再以 narci 仓库为 source of
truth,会拿到正确的拓扑。

**对 echo 实操的影响**:零。UM 在 SG 还是 us-west-2 跟 echo CC pair 都
无关 (跨 region 都没用),CC pair target 已经在 narci-t4g 上,echo §12.1
"echo-air's CC pair target IS narci-t4g, same AZ" 这条结论本身正确,可以
按这条往 §2.1 的 pairing spec 推。

### 7.2 A15 — `recorder-bitbank` on narci-tokyo (blocking 0b) ✓ accept

narci 接 A15。选 **(a) 独立 recorder**,理由跟 echo 一致 — cross-check
echo `RawL2Sidecar` 才有意义。

实施计划:

- 新 adapter `data/exchange/bitbank.py` 实现 `ExchangeAdapter` ABC,
  WS 协议 socket.io v2 (EIO=3),参照 echo `echo/exchange/bitbank.py` (SHA
  `6569402`) 的 parser。`python-socketio[asyncio_client]` 加进
  `requirements.txt`。
- side 编码沿用 narci 标准:0=bid update, 1=ask update, 2=aggTrade
  (qty 负值=seller maker), 3=bid snapshot, 4=ask snapshot。
- 新 config `configs/exchanges/bitbank_spot.yaml` + `configs/bitbank_recorder.yaml`
  symlink。
- `docker-compose.yaml` 加 service `recorder-bitbank`,profile=tokyo,
  宿主机端口 **8082**(8079/8080 已被 CC/BJP 占)。
- gdrive 路径:`gdrive:narci_raw/realtime/bitbank/spot/l2/`。
- `daily_compactor.py` 沿用既有 exchange-neutral 流程,bitbank 跟 CC 一样
  没有官方 Vision 归档,skip validation gracefully。

**时间表**:narci 这边按 echo 的 Phase 0b cutover 排期。echo §12.3 标
"before Phase 0b cutover" — 请 echo 给一个绝对日期 (例如 "Phase 0b
cutover ≥ 2026-06-XX"),narci 这边好倒推 ETA。在没拿到日期之前,narci
按 **2026-06-30 ready (含联调测试)** 自规划。

### 7.3 A16 — Calibration replay venue dispatch (blocking 0b) ✓ accept

narci 接 A16。`MakerSimBroker` + calibration replay 加 venue dispatch
层,按 `meta.json.exchange` 切换:

- `SymbolSpec` 表 (per-venue tick / lot / minNotional / maker-rebate
  eligibility)
- fee schedule (CC `0/0 bps`,bitbank `-2/+12 bps`,future venues TBD)
- trade-side decoding (CC list-of-lists; bitbank explicit `side`;
  bitFlyer `executions[]`)
- order-type matrix (`post_only` 是否支持、IFD/OCO、stop-limit 等)

实现路径:在 `backtest/broker.py` 加 `VenueRegistry`,根据 session 的
`exchange` 字段在 replay 启动时 inject 对应规则;现有
`SpotBroker`/`SimulatedBroker` 改成读 registry 而非硬编码 CC 默认。

**时间表**:跟 A15 绑定,同一个 Phase 0b 窗口完成 (2026-06-30 ready)。

### 7.4 A17 — `recorder-gmo` + `recorder-bitflyer` — **scaffold ready (2026-05-15),deployment gated by ops capacity**

narci 没等 A15 稳定就把 A17 全套 scaffold 提前做了 (commit `18e68b3`),
避免后续回头看时再做。**代码层 ready,部署 gated on narci-tokyo 资源**:

#### Scaffold 已完成 (narci 端)

- `data/exchange/bitflyer.py` — JSON-RPC over plain WS,`market_type='spot'|'fx'`
  双模 (FX 用 `FX_BTC_JPY` symbol)。走标准 `ws_url + parse_message` 路径。
- `data/exchange/gmo.py` — `market_type='spot'|'leverage'`。**关键差异**:GMO
  `orderbooks` channel 每条都是 full snapshot 而非 incremental,所以走
  `custom_stream` hook 自管 book reset (跟 bitbank 同 pattern)。
- VenueRegistry 4 个 entry:
  - `bitflyer_spot`: 15/15 bps
  - `bitflyer_fx`: 0/0 bps + 日内 SWAP funding
  - `gmo_spot`: -1/+5 bps (rebate-eligible)
  - `gmo_leverage`: -1/+5 bps (echo 偏好的主战场)
- 4 yaml configs + 4 symlinks + 4 docker-compose services on **新 profile
  `tokyo-extra`** (不放默认 tokyo,避免无脑 pull 后 OOM)。
- 每个新 service `mem_limit: 256m` 防御性硬上限。
- `deploy/measure_recorder_footprint.sh` — 测当前 narci-tokyo 内存余量,
  输出 🟢/🟡/🔴 verdict + 升级建议。

#### Deployment 阻塞点 (ops capacity)

narci-tokyo 当前是 **t4g.small (2GB)**,已经跑 3 个 recorder
(CC + Binance JP + bitbank) + cloud-sync。加 4 个新 recorder 预估额外
~600MB 需求,**很可能需要先升级到 t4g.medium (4GB, +$15/月)**。

决策树 (narci-tokyo 操作员):

```bash
ssh narci-tokyo
cd narci && git pull
bash deploy/measure_recorder_footprint.sh
# 🟢 GREEN  → COMPOSE_PROFILES=tokyo,tokyo-extra docker compose up -d
# 🟡 YELLOW → 分批先开 fx + leverage 2 个主战场
# 🔴 RED    → 升级 t4g.small → t4g.medium 再开
```

#### 仍需 echo 配合的项 (Phase 0c+ 前)

- **echo 明确 venue × market 组合**:bitFlyer 走 spot 还是 FX?GMO 走
  spot 还是 leverage?如果两边都做需要明说 — 影响 narci 是否要 4 个 recorder
  全开 (vs 选择性开 2 个)。
- **bitFlyer FX JST business-hour audit** — echo §12.2 自己 flag 的
  "max 110ms 尾巴可疑",重测后给个 verdict (录 vs 不录)。
- **bitbank pattern 稳定 30 天** — 这条 narci 自己跟,以 bitbank 录制 24h+
  无中断 + watchdog 不触发为基线。

### 7.5 narci 端反向追踪 — echo §12.3 表的更新建议

echo 表里第二项 "Topology re-cut of `INTERFACE_ECHO.md` §2.1.3 / §4.3 /
§4.4 — Due: next narci re-cut" — **本次 commit 已 done**。请 echo 下次
re-cut INTERFACE_NARCI.md 时勾掉 / 删除这行,免得长期挂着。

其它 commitments 状态:

| Item | Status | 备注 |
|---|---|---|
| Cross-host pairing spec | 进行中 | due 2026-06-15,在 `docs/CROSS_HOST_PAIRING.md` |
| Topology re-cut | ✓ done | 本次 commit |
| A15 `recorder-bitbank` | ✓ scaffold + Phase 2 + Phase 3 watchdog done (`e55dd75`/`ac88d28`/`1c8349f`) | 已部署 narci-tokyo,2 个 save cycle 数据已上 gdrive,5 个 symbol 全健康 |
| A16 venue dispatch | scaffold done (`e55dd75`) | VenueRegistry + apply_to_broker helper ready;broker 完整重构 (默认读 registry) 待 Phase 3 |
| A17 GMO + bitFlyer | **scaffold ready** (`18e68b3`),deployment gated | bitflyer-spot + bitflyer-fx + gmo-spot + gmo-leverage 4 个 recorder on `tokyo-extra` profile;需要 ops 决定 (capacity verdict) |
| cloud-sync `--no-traverse` 修复 | ✓ done (`acaf96c`) | 新嵌套目录 (bitbank 首次踩雷) 自动 catch up,免去未来 GMO/bitFlyer 同样的坑 |
| empty-shard semantics | ✓ done in §2.2 | confirmed |

### 7.6 narci 端反向 ask (再次重申)

承接 §3 的请求,**给 echo 的优先级排序**:

1. **(高)** §3.1 — NaiveMakerStrategy 修好后送一个非空 session 样本
   (Phase 0b 启动前)。
2. **(高)** §3.2 — Test 6 funding ETA 写进 echo INTERFACE_NARCI §4 (这次
   re-cut 时顺便加上)。
3. **(中)** §3.3 — echo-air `/health` HTTP endpoint (Phase 0b 之前 nice
   to have)。
4. **(低)** §3.4 — `narci_features_consumed` 字段,v1.2 schema candidate,
   bitbank/multi-venue 上线后再讨论。
