# Narci Reco Ops 控制台 — 设计

> reco(Mac Studio)运维域专用的只读 fleet 监控控制台。与 quant 终端
> (`analytics/gui/`)完全分开。2026-05-27 与 reco 共同设计。

## 决策(locked)

| 维度 | 决定 |
|---|---|
| 应用边界 | 独立 app,入口 `main.py reco-gui` → `streamlit run ops/dashboard.py` |
| 代码位置 | 顶层 `ops/`,**不进** core<recorder<analytics 链(leaf,只 import boto3+stdlib) |
| Fleet 数据源 | **SSM → EC2**,每 host 一发批量探针 |
| Cold 数据源 | **ssh/scp → lustre1**(可配置 target) |
| Cold v1 | 昨日逐 venue 落地检测(DAILY+GAPS 在否) |
| 权限 | 纯只读,无动作(restart/rollback 仍走 CLI) |
| 刷新 | `st.cache_data(ttl=60)` + 手动按钮,无常驻 autorefresh |
| Cold future | 数值一致性 = cold ↔ 官方归档对账(留 `probe_hpc` 读内容扩展点) |

## 数据通道

```
EC2(jp/sg) ──SSM──▶ ┌ Mac Studio = reco ┐ ──ssh──▶ lustre1(cold tier)
  recorder 状态        ops 控制台              {SYM}_DAILY/_GAPS.json
  /health · freshness  (唯一持 AWS 凭证)
  wal 积压 · 部署版本
```
gdrive 不在 mac-studio 这条路里(raw 由 lustre 拉)。

## 模块

```
ops/
├── config.py     # 读 deploy/reco/.env:实例ID/region/deploy_path/health 端口/lustre target
├── probe_aws.py  # SSM:每 host 一发批量探针(base64 传脚本绕引号;容器内复用 scan_freshness/wal)
├── probe_hpc.py  # ssh lustre1:cold-tier 昨日落地(留读内容扩展点)  [phase 3]
├── fleet.py      # 纯解析器:探针 stdout → 结构化 dict(可单测,fixture=真实输出)
├── dashboard.py  # streamlit 入口(薄)
└── panels/{fleet_overview,venue_freshness,wal_backlog,health_detail,cold_landing}.py
```

## 探针实现要点

- **base64 传脚本**:SSM commands = `echo <b64> | base64 -d | sudo -u ec2-user bash`。
  远端脚本任意引号/`$` 自由写,零转义地狱(本会话踩够了内联引号坑)。
- **内嵌自包含 scanner**:recorder 镜像 `.dockerignore` **剥掉了 analytics/**(recorder 不需 gui/torch),
  容器内 import 不到 recorder_health。故探针把扫描逻辑**内嵌**(`probe_aws._REMOTE_SCANNER`,
  heredoc 喂容器 python),逻辑复刻 `recorder_health.scan_freshness/scan_wal_backlog`,parity 由
  `tests/ops/test_remote_scanner.py` 钉死。任一 recorder 容器都看得到全量(共享 narci-data 卷)。
- **每 host 一发**:ec2 state / ssm ping 走 `aws ec2/ssm` 只读 API;host commit / ps /
  freshness / wal / health 全塞进一条 SSM send-command,降 API 成本+延迟。

## 关键协同

「对每个**正在录**的 venue(AWS up 且非 PARKED),昨日 cold-tier **落地了吗**(lustre)?」
—— 两通道交叉才 catch "在录没归档" 和 "归档了但其实早停"。PARKED 沿用 healthcheck.py
的 7 天无新 shard 豁免(GMO 靠此,见记忆 project_gmo_recorders_parked)。

## 分阶段

1. **P1 数据层**:config + probe_aws + fleet 解析器 + tests(fixture=真实 stdout)
2. **P2 看板**:dashboard + fleet_overview + venue_freshness + wal_backlog + health_detail
3. **P3 HPC**:probe_hpc + cold_landing 面板
4. **(future)** cold↔官方对账

## 不进 v1

动作按钮 · CloudWatch/gdrive 通道 · 历史趋势 · cold↔官方对账(只留扩展点)
