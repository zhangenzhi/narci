# Topology

narci 4-node 部署拓扑 + transport / port / 凭证细节。narci 跟 narci-reco
共用一个 git 仓库(跟 echo 同套路),本文是 narci-reco 角色 source of
truth。研究端代码在仓库根,ops 端代码在 `reco/` 子目录,凭证只在 Mac
Studio 上的 `reco/.env`(gitignored)。

## Nodes

### narci (lustre1 PBS)
- **Host**: `/lustre1/work/c30636/narci/` on PBS login node + c30636g GPU 队列 compute 节点
- **OS / runtime**: Linux + miniconda3
- **职责**:
  - 代码 / 仓库唯一编辑入口
  - 拉 gdrive(`gdrive:narci_raw` + `gdrive:narci_official`)→ 本地 cold tier 做 backtest
  - PBS submit:feature cache rebuild / model train / backtest sweep
- **网络限制**: 无 crypto exchange / 外部 archive 网络访问。所有外部数据
  走 donor → gdrive → 本机 rclone pull。
- **不做的事**: 录制(没法连 WS);ops(没法连 AWS API,也不该放 IAM creds)

### narci-reco (Mac Studio @ home)
- **Host**: Mac Studio 在家用户私网
- **职责**:
  - 通过 aws-cli + Secret Manager 管 aws-jp / aws-sg(本仓库主功能)
  - Future: 通过 tailscale 管 narci-donor
- **凭证**: AWS IAM user (跟 echo-air 同一套路,共用 Secret Manager pattern)
- **物理连接**: 跟 lustre1 物理直连 → 代码同步零延迟(rsync / git remote)

### narci-donor (Mac mini @ home)
- **Host**: Mac mini 在家用户私网
- **职责**:
  - 下 Binance Vision daily ZIP → 转 parquet → 推 gdrive:narci_official
  - cron 任务,每日固定时间
- **网络**: 公网(Binance Vision)+ rclone(gdrive)+ tailscale(被 narci-reco 看到)
- **Future**: narci-reco 通过 tailscale 接管,跑健康检查 / 强制 backfill / 日志拉取

### aws-jp (AWS EC2, Tokyo region)
- **Instance type**: t4g (ARM)
- **录制范围**:
  - Coincheck 现货
  - Binance JP 现货
  - bitbank 现货(2026-05-15 上线)
  - Future: bitFlyer spot+FX / GMO spot+leverage(tokyo-extra profile,机器扩容后)
- **Docker compose profile**: `tokyo` (+ future `tokyo-extra`)
- **端口**(实测 2026-05-16): 8079 (coincheck) / 8080 (binance-jp) / 8082 (bitbank)
- **数据出口**: cloud-sync sidecar → gdrive:narci_raw/realtime/

### aws-sg (AWS EC2, Singapore region)
- **Instance type**: t4g(2026-05-07 之后从 t4g.small 升 t4g.medium,因为 UM OOM)
- **录制范围**: Binance Global 现货 + UM 期货 (两个 recorder 共驻)
- **Docker compose profile**: `global`
- **端口**(实测 2026-05-16): 8079 (binance-spot global) / 8080 (binance-umfut)
- **数据出口**: cloud-sync sidecar → gdrive:narci_raw/realtime/binance/um_futures/

## Links

| 起点 | 终点 | Transport | 用途 |
|---|---|---|---|
| narci-reco | aws-jp | aws-cli (HTTPS) | EC2 lifecycle + SSM SendCommand |
| narci-reco | aws-sg | aws-cli (HTTPS) | EC2 lifecycle + SSM SendCommand |
| narci-reco | narci-donor | tailscale SSH (future) | donor 接管 |
| narci-reco | narci (lustre1) | 物理直连 + ssh / rsync / git | 代码同步 + ops report 回流 |
| aws-jp | gdrive | rclone (cloud-sync sidecar) | raw L2 推送 |
| aws-sg | gdrive | rclone (cloud-sync sidecar) | raw L2 推送 |
| narci-donor | gdrive | rclone (cron) | Vision archive 推送 |
| narci (lustre1) | gdrive | rclone (本机) | 拉 raw + official 进 cold tier |

## 数据流(end-to-end)

```
Binance / Coincheck / bitbank WS  ──→  aws-jp / aws-sg recorder
                                              │
                                              ▼
                                     gdrive:narci_raw
                                              ▲
Binance Vision (官方 archive)  ──→  narci-donor
                                              │
                                              ▼
                                     gdrive:narci_official
                                              │
                                              ▼  (rclone pull on lustre1)
                                     narci cold tier ──→  feature cache ──→  backtest / train
```

## Ops 流程契约(narci-reco 提供给 narci 角色)

同 repo 不同角色,通过 commit / file 路径协作,无需跨 repo INTERFACE doc:

- **每日 health summary**:两台 EC2 实例状态 + 各 recorder /health 返回值 +
  最新 shard mtime。Mac Studio 跑 `reco/aws/health_probe.sh` 后 rsync 到
  lustre1 某路径(物理直连零延迟),或 commit 到本仓库 `reco/status/`
  子目录让 narci 端 git pull 看
- **Incident alert**:cloud-sync stale alert(narci 自己内部 docker log
  里会打 `[STALE-VENUE-ALERT]`)+ narci-reco 自己跑的 SSM 探测失败 → 告警
  (邮件 / 推送 / Slack,narci-reco 决定)
- **Code rollout**:narci 角色 push 到 main 后,narci-reco 角色在两台
  EC2 上 `git pull && docker compose up -d --force-recreate`(走
  `reco/aws/recorder_restart.sh --pull`)
- **机器升级**:t4g.small → t4g.medium 这种,narci-reco 通过 aws-cli 改
  instance type + reboot,narci 角色无需介入

## 未来扩展

- **narci-donor 接管**(在 tailscale link 上):
  - 拉 cron 日志、强制 backfill 某日 Vision、health check
  - 把 donor 的 puller 代码也 fold 进 `donor/` 子目录
- **CloudWatch dashboard**:CPU / mem / disk / network 写脚本拉指标
- **AMI 备份**:定期 create-image,机器爆了能从最近 AMI 起恢复 docker 配置
