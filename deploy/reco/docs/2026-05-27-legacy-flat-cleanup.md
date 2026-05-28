# 2026-05-27 旧扁平残留清理(gdrive + lustre cold)

P5 之前的布局没有 exchange 嵌套(`realtime/spot/l2/`、cold 根散落 DAILY),迁到
`{exchange}/{market}/` 后留下大量历史扁平残留。本次把残留**归档进 `old/`**(可逆、不删),
让 `realtime/` 和 `cold/` 根只剩结构化目录。reco-gui cold 面板扫 `cold/{exchange}/{market}/`,
本就扫不到这些根级残留,故纯属仓库卫生。

## 做了什么(均为 move,非 delete)

| 位置 | 残留 | 移到 | 备注 |
|---|---|---|---|
| gdrive `narci_raw/realtime/spot/` | 旧扁平 raw(~4月前) | `old/spot/` | 整目录 reparent,**秒级** |
| lustre `cold/` 根 258 个 `*_RAW_*_DAILY.parquet`(17G,3月起) | 旧扁平 cold DAILY | `cold/old/` | 本地 FS `mv`,秒级;3-4月数据唯一 compact 形态,不可删 |
| gdrive `narci_raw/` 根 3684 个 loose(678 MiB,3月) | 早期未嵌套 raw | `old/root_flat/` | per-file 服务端 move,**37min** |

清理后:gdrive `realtime/` = {binance, binance_jp, bitbank, bitflyer, coincheck, gmo};
lustre `cold/` 根 = 同 6 个 exchange + `old/`。

## 安全性
- `old/` 是 `realtime/` 的平级兄弟,**在 lustre 拉取的 `realtime/` 树之外** → 不扰 lustre pull。
- cloud-sync 是 `rclone copy /data`(EC2 卷),EC2 卷本就没这些旧残留 → **copy 不会重建**。
- exchange 从扁平名(`SOLJPY`/`BTCUSDT`)无法可靠还原 → 选择 stash 不重排。

## 关键事实(以后参考)
- **gdrive 目录级 move 即时**(folder reparent,一次 API);**文件级 move/list 慢**
  (per-file + gdrive list 分页),3684 文件移了 37min。
- 文件级 move 进同 remote 子目录会撞 rclone "overlapping remotes" 保护 →
  需 `--filter "- /old/**"` 排除目标子树。
- **lustre narci checkout 仍在 `87c46a5`(pre-P5)** → 其 daily compact 跑旧代码、
  无 gap_detect → 这就是 cold 里 `*_GAPS_*.json` 一直为 0 的原因。升 P5 需协调
  (动 compact/backtest 产线 + echo/nyx import 路径,见 docs/design/MIGRATION_P5_IMPORTS)。

## 未决(根因,待定方案)
- **gdrive prune 例程**:cloud-sync `rclone copy` 只增不删,gdrive 累积海量 raw(列举/同步慢)。
  治本 = 删已进 cold 的旧 raw(删前校验 cold 齐全)。gdrive 无 lifecycle → 手动;S3 可自动。
- **lustre 升 P5**:解 GAPS=0 + 布局统一。
