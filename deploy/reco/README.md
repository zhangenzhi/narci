# narci/reco — narci-reco 控制面

narci 仓库下的 ops 控制面子目录,跟 echo 仓库的多角色布局同套路(单 repo,
不同角色 checkout 后只跑各自子目录)。物理跑在 Mac Studio(家),通过
aws-cli 管 AWS 上的 recorder fleet,通过 tailscale 管 narci-donor (Mac mini
家)。

## 角色定位

narci 仓库下三角色:

| Role | Host | 职责 | Transport |
|---|---|---|---|
| **narci** (仓库根) | lustre1 PBS login + compute (GPU 节点 c30636g) | 研究 / 回测 / feature 缓存 / model train | 仅内网;无外网访问 |
| **narci-reco** (本目录 `reco/`) | Mac Studio @ home | 管 AWS recorder fleet + (future) 管 donor | aws-cli (公网) + tailscale (donor) + 物理直连 (lustre1) |
| **narci-donor** (代码将落入 `reco/donor/`) | Mac mini @ home | Binance Vision puller → gdrive:narci_official | tailscale (来自 narci-reco) + 公网 (Vision) + rclone (gdrive) |

```
                     ┌────────────────────────┐
                     │ narci-reco (Mac Studio)│
                     │ 控制面 / aws-cli hub    │ ──── aws-cli ────→ aws-jp / aws-sg
                     └─┬─────────┬──────────┬─┘
              物理直连 │         │ tailscale
                       │         │
            ┌──────────▼───┐ ┌───▼─────────────┐
            │ narci (root) │ │ narci-donor     │
            │ (lustre1)    │ │ (Mac mini)       │
            │ 研究 / 回测   │ │ Binance Vision   │
            └──────────────┘ └─────────────────┘
```

详见 [`docs/TOPOLOGY.md`](docs/TOPOLOGY.md)。

## 当前覆盖范围 (v0)

- [x] AWS recorder 实例 describe (jp + sg)
- [x] AWS recorder 重启 via SSM SendCommand
- [x] AWS recorder 健康探测 via SSM (curl localhost:807x/health on instance)
- [x] **reco-gui** 只读 fleet 监控控制台 (streamlit;SSM + ssh-lustre cold 落地)
- [ ] CloudWatch CPU/mem/disk 探测
- [ ] AMI / EBS snapshot 备份
- [ ] narci-donor 接管 (future,via tailscale)

## 快速开始 (Mac Studio)

```bash
# 0. clone narci 仓库 (跟 lustre1 上同一个 repo)
git clone git@github.com:zhangenzhi/narci.git ~/narci
cd ~/narci/reco

# 1. 凭证已经齐了 (跟 echo-air 同套路,共用 aws-cli profile + Secret Manager)
aws sts get-caller-identity                # 确认 IAM identity

# 2. 复制 .env.example → .env,填两台 EC2 instance ID
cp .env.example .env
$EDITOR .env

# 3. describe
./aws/ec2_describe.sh

# 4. health probe (走 SSM,不需要公网开 807x 端口)
./aws/health_probe.sh jp
./aws/health_probe.sh sg

# 5. recorder restart
./aws/recorder_restart.sh jp
./aws/recorder_restart.sh sg
```

## reco-gui — 只读 fleet 监控控制台

把上面那些 SSM 探测脚本的信号汇成一屏 GREEN/RED 的可视化看板。**纯只读**,不动产线
(重启/回滚仍走上面的 CLI)。代码在**仓库根 `ops/`**(不在本目录;它是顶层 leaf 模块,
设计见 [`docs/design/RECO_OPS_GUI.md`](../../docs/design/RECO_OPS_GUI.md))。

数据源:每 fleet 一发 **SSM 批量探针**(容器 / health / per-venue 新鲜度 / WAL 积压 /
部署 commit)+ **ssh lustre1** 查 cold-tier 昨日 DAILY 落地。不碰 gdrive。

### 启动(Mac Studio,narci conda env)

streamlit 在 narci conda env(`requirements-research.txt`)。`.env` 除 EC2 那几项,cold
面板还需 `NARCI_LUSTRE_SSH` / `NARCI_LUSTRE_SSH_KEY` / `NARCI_LUSTRE_COLD_PATH`。

```bash
conda activate narci                 # streamlit / pandas 在此 env
cd ~/narci
python main.py reco-gui              # = streamlit run ops/dashboard.py(默认 :8501)

# 常驻(tmux + 自定义端口):
tmux new -d -s narci-reco-gui -c ~/narci \
  'streamlit run ops/dashboard.py --server.port 8502 --server.headless true'
# 浏览器开 http://localhost:8502;停:tmux kill-session -t narci-reco-gui
```

### 看什么
- **顶部总览 strip**:jp / sg 各一块 GREEN/RED,2 秒判断
- **每 fleet 卡片**:模块色块(点击下钻该 venue 逐 symbol 新鲜度 + WAL),RED 自动展开陈旧 venue
- **❄️ Cold-tier 落地**:每 venue 昨日 DAILY 是否已落 lustre1

PARKED venue(容器 exited,如 GMO)即时豁免,不误报 RED。刷新 = 60s 缓存 + 手动按钮(省 SSM quota)。

## 角色边界(同 repo 不同角色的纪律)

| Concern | narci (仓库根) | narci-reco (本目录) |
|---|---|---|
| 编辑 recorder / model / feature 代码 | ✅ | ❌(只编辑 `reco/` 下) |
| AWS 凭证 | ❌ 严禁在 lustre1 放 | ✅ Mac Studio 唯一持有,本目录 `.env` (gitignored) |
| 跑 backtest / PBS submit | ✅ on lustre1 | ❌ |
| 跑 aws-cli / SSM 操作 production | ❌ | ✅ on Mac Studio |
| Daily health summary 生成 | ❌ 被动接收 | ✅ 主动生成 → rsync 回 lustre1 |

核心原则:**lustre1 不持有任何 production 凭证**(共享 box 风险大);
**narci-reco 不编辑 narci 研究代码**(只读 + 操作产物)。

## 文件布局

```
reco/
├── README.md              # 本文
├── .env.example           # AWS_PROFILE + instance ID 模板
├── .gitignore             # .env / *.log 等
├── docs/
│   └── TOPOLOGY.md        # 4-node 拓扑 source of truth
├── aws/
│   ├── iam/
│   │   └── narci-reco-policy.json    # IAM 最小权限 policy
│   ├── ec2_describe.sh
│   ├── recorder_restart.sh
│   └── health_probe.sh
└── donor/                 # future 接管 Mac mini puller 代码
    └── .gitkeep
```

> reco-gui 控制台代码不在本目录,在**仓库根 `ops/`**(`config/probe_aws/probe_hpc/fleet/
> panels/dashboard`)+ 测试 `tests/ops/`,入口 `main.py reco-gui`。设计见 `docs/design/RECO_OPS_GUI.md`。
