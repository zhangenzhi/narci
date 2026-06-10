# 2026-06-09 · Cold-tier 落地自 05-28 冻结(lustre compact 盲于分区 RAW)

**状态:RESOLVED**
**影响面:全 fleet 7 个 active venue 的 cold-tier DAILY 自 2026-05-29 起停产 ~11 天**
**数据损失:无**(RAW 完好且新鲜同步到 lustre,只是没被 compact;回填后 0 缺)

---

## 1. 现象

reco dashboard「❄️ Cold-tier 落地」面板:**所有 active venue 一律红、整齐缺 10 天**
(binance/spot/um、binance_jp/spot、bitbank、bitflyer×2、coincheck 全 `缺10 · lag2`)。
全员同一天断 = 共享链断,不是各家 gap。另:进度条没对齐。

## 2. 时间线(UTC)

| 时间 | 事件 |
|---|---|
| ~2026-05-29 | gdrive→lustre 的 RAW 同步切到 symbol/day 分区布局(`l2/{SYMBOL}/{YYYYMMDD}/`) |
| 2026-05-29 起 | lustre daily compact cron(`0 12 * * *`)每天空转:`数据库中未找到日期 …的原始碎片` |
| → 2026-06-08 | cold DAILY 冻结在 **20260528**,缺口随天数累积 |
| 2026-06-09 | reco 巡检 dashboard 发现全红;逐层 ssh 定位 + 修复 + 回填 |

## 3. 根因(逐层坐实)

| 层 | 实测 | 结论 |
|---|---|---|
| dashboard cold 探针 | cold 树平铺、`os.listdir` 能读到、最新 0528 | 探针无 bug,显示是对的 |
| RAW 输入(lustre) | `realtime/.../l2/{SYMBOL}/{YYYYMMDD}/`,18 万文件、最新 0609 | **RAW 新鲜、分区** ✅ |
| compact cron | 每天跑,log 全是 `未找到…原始碎片` | **空转不产 DAILY** |
| **lustre narci checkout** | **`87c46a5`(pre-P5),缺 `7435cfb`** | **真因** |

**机理**:RAW 5/29 切 symbol/day 分区;lustre 上的旧 compact(87c46a5)按**旧平铺**扫,
看不见分区 RAW → 每天空转。narci 的 `7435cfb fix(compact): 分区布局递归发现` 正是修复,
但 lustre checkout 一直**故意停在 pre-P5**(见 `../2026-05-27-legacy-flat-cleanup.md`),没拿到。

## 4. 处置

1. **进度条对齐**(reco):`ops/panels.py` 的 `.coldh-label` 从 `min-width` 改**定宽 200px**,
   变长徽标(缺/lag/上线前)移到 bar 之后,bar 起点不再被长 label 推歪。
2. **升 lustre compact 代码**(reco HPC ops,代码是 narci 的):
   - 回滚锚点 `87c46a5` 记下;pre-flight 确认 ff-only 可行、无 lustre-local commit、`?? .claude/` 未跟踪无碍。
   - `git pull --ff-only` → lustre 到 **`42c4217`**(含 `7435cfb`)。
   - **单日 smoke** `compact --symbol BTCUSDT --date 20260529`:分区 RAW 正确发现(218/144、141/141 切片),
     DAILY 落 cold,rc=0 —— 验证 P5 跳变后 CLI/miniconda py3.13/import 全兼容。
3. **全量回填**(reco):`compact --symbol ALL --date <d>` 逐日跑 **05-29→06-08(11 天)**,
   nohup 后台 + 轮询。**11 天全 rc=0,每天 72 个 DAILY**。
   - 注:首次 launch 因 nohup 子 shell 未继承 cwd(落在 narci 上一级)全 rc=2 瞬挂、未产垃圾;
     把 `cd` 显式放进 nohup 内层后重跑成功。

## 5. 验证

- per-venue 最新 cold DAILY:7 个 active venue **全部 0608**;gmo(parked)留 0527(正确,显「停」)。
- dashboard 总览:RED → **AMBER**(仅今天 0609 lag,明天 12:00 cron 自动补)。
- daily cron 同 checkout,**0609 起自动产 DAILY**,链路自愈。

## 6. 回归守卫(test)

- **reco 侧检测**:`tests/ops/test_cold.py::test_classify_cold_history_established_venue_freeze_is_red`
  —— 锁「老 venue 近端整段冻结必须显连续红('0'),不被 '-'/lag/parked 掩盖」,即当时靠人眼发现的信号。
- **narci 侧根因**:`tests/recorder/test_compact_partitioned_discovery.py`(随 `7435cfb`)—— 锁 compact 分区递归发现。

## 7. Follow-ups

- [x] **告知 narci**:lustre 已从 pre-P5 升到 `42c4217`,`2026-05-27-legacy-flat-cleanup.md`
      里「lustre 故意停 87c46a5 / 跑旧 compact」的状态**已变更**,该 doc 需更新。
- [ ] **缺自动告警**:cold-tier 冻结只靠人眼看 dashboard 发现(hot tier 有 `venue_stale_monitor`,
      cold tier 没有)。考虑给 cold-tier 落地加 staleness 告警(monitor 或 CloudWatch),否则下次
      仍是「攒满一窗才被发现」。
- [ ] **布局耦合风险**:`ops/probe_hpc.py` 的 cold 探针用平铺 `os.listdir`;cold 目前确实平铺,
      但若哪天 cold 也分区,探针会重蹈 compact 的覆辙 —— 留意。
