# 部署 runbook — 录制端重构落地(2026-05-27)

> 前提:narci PR #1(`refactor/data-module` → `main`)**已合并**。部署主机
> `git pull --ff-only` 拉的是 main,不合就拉不到新代码(WAL/gap/exclude 全缺)。
> 所有 `aws/*.sh` 走 SSM Run Command,无需 SSH key;先 `cd deploy/reco`(脚本读 `.env`)。
> 交接背景见 `docs/INTERFACE_NARCI_RECO.md`。

图例:🟢 只读/dry-run（随便跑） ｜ 🟡 改动产线（确认后跑） ｜ ↩️ 回滚

---

## 第 1 步:apply IAM（aws-sg 实例角色加 PutMetricData）🟡

新增 statement 只放 `cloudwatch:PutMetricData` 且 namespace 收窄到 `Narci/Donor`
（见 `aws/iam/narci-reco-policy.json`，已有守卫测试 `tests/deploy/test_config.py`）。

```bash
cd deploy/reco

# 🟢 先看现行策略 diff（POLICY_NAME / 角色名按你们实际命名替换）
aws iam get-role-policy \
  --role-name <narci-reco-sg-role> \
  --policy-name narci-reco-policy \
  --query 'PolicyDocument' --output json | diff - aws/iam/narci-reco-policy.json || true

# 🟡 apply（inline policy 覆盖;若用 managed policy 则 create-policy-version）
aws iam put-role-policy \
  --role-name <narci-reco-sg-role> \
  --policy-name narci-reco-policy \
  --policy-document file://aws/iam/narci-reco-policy.json
```

验证（🟢，IAM 生效有秒级延迟）：
```bash
aws iam get-role-policy --role-name <narci-reco-sg-role> \
  --policy-name narci-reco-policy \
  --query "PolicyDocument.Statement[?Sid=='CloudWatchPutDonorMetric']"
```
↩️ 回滚：`put-role-policy` 重新挂上一版 JSON（或删除该 statement 后重 apply）。

---

## 第 2 步:重部录制器（build → restart）+ 确认 gdrive 无 wal/ 🟡

`recorder_build.sh` 第 1 步在主机 `git pull --ff-only`，build 出新 `:latest` 并把旧版
存成 `:prev`（**不 recreate**）；`recorder_restart.sh` 才真正切上线。

```bash
cd deploy/reco

# 🟢 先确认主机当前 commit / 容器状态(只读)
aws/recorder_restart.sh sg --ps-only
aws/recorder_restart.sh jp --ps-only

# 🟡 sg：pull + build + recreate(--pull 隐含 --build)
aws/recorder_restart.sh sg --pull
# 🟡 jp 同理
aws/recorder_restart.sh jp --pull
```

> 单 service 灰度可用 `aws/recorder_restart.sh sg --service recorder-coincheck`。
> cloud-sync sidecar 也要重建才会带上新的 `--exclude 'wal/**'`（它在同 compose 里，
> `--pull` 全量 recreate 会一并更新；若只 `--service` 某录制器，记得也 recreate cloud-sync）。

验证（🟢）：
```bash
# a) 健康端口活着
aws/health_probe.sh sg ; aws/health_probe.sh jp

# b) WAL 段在产出(主机上,经 SSM):replay_buffer/wal/ 下应有 .segwal,
#    且每个 save_interval 后被合并消失、realtime/ 下出现新 RAW_*.parquet
#    （human check：段数不该单调累积）

# c) ★关键★ gdrive 远端不该出现 wal/ 目录(exclude 生效)
rclone lsd gdrive:narci_raw                       # 顶层应只有 realtime/ cold/ 等,无 wal/
rclone lsf gdrive:narci_raw --include 'wal/**' | head   # 应为空
```
↩️ 回滚（< 30s，用 build 留的 `:prev`）：
```bash
aws/recorder_rollback.sh sg recorder-coincheck    # 按需逐个 service
aws/recorder_rollback.sh jp recorder-coincheck
```

---

## 第 3 步:donor 上 systemd-timer + CloudWatch Alarm 🟡

donor 跑在 **aws-sg**（能上 Binance + 自带 rclone→gdrive）。`donor_loop.sh` 的 tmux
可换 systemd（`check_health.sh` 无 tmux 时自动跳过 session 检查，靠日志新鲜度判活）。

### 3a. systemd（替代 tmux 常驻）🟡
```ini
# /etc/systemd/system/narci-donor.service
[Unit]
Description=Narci donor — Binance Vision daily push
After=network-online.target
[Service]
User=ec2-user
WorkingDirectory=/home/ec2-user/narci
Environment=DRIVE_REMOTE=gdrive:narci_official
ExecStart=/usr/bin/env bash deploy/reco/donor/donor_loop.sh
Restart=always
RestartSec=300
[Install]
WantedBy=multi-user.target
```
```ini
# /etc/systemd/system/narci-donor-health.service  (oneshot)
[Unit]
Description=Narci donor health check
[Service]
Type=oneshot
User=ec2-user
WorkingDirectory=/home/ec2-user/narci
Environment=AWS_REGION=ap-southeast-1
ExecStart=/usr/bin/env bash deploy/reco/donor/check_health.sh
```
```ini
# /etc/systemd/system/narci-donor-health.timer  (每天一次)
[Unit]
Description=Run donor health check daily
[Timer]
OnCalendar=daily
Persistent=true
[Install]
WantedBy=timers.target
```
```bash
# 🟡 启用
sudo systemctl daemon-reload
sudo systemctl enable --now narci-donor.service
sudo systemctl enable --now narci-donor-health.timer
# 🟢 验证
systemctl status narci-donor.service --no-pager
systemctl list-timers narci-donor-health.timer --no-pager
sudo systemctl start narci-donor-health.service   # 手动跑一次,催出首个指标
```

### 3b. CloudWatch Alarm → SNS 🟡
```bash
# 🟡 SNS topic（已有则跳过）+ 订阅
aws sns create-topic --name narci-donor-alerts --region ap-southeast-1
aws sns subscribe --region ap-southeast-1 \
  --topic-arn arn:aws:sns:ap-southeast-1:<acct>:narci-donor-alerts \
  --protocol email --notification-endpoint <you@example.com>

# 🟡 Alarm:DonorHealthy < 1 触发;★treatMissingData=breaching★
#    （donor 整个挂掉发不出指标时也要响,否则静默死亡）
aws cloudwatch put-metric-alarm --region ap-southeast-1 \
  --alarm-name narci-donor-unhealthy \
  --namespace Narci/Donor --metric-name DonorHealthy \
  --statistic Minimum --period 86400 --evaluation-periods 1 \
  --threshold 1 --comparison-operator LessThanThreshold \
  --treat-missing-data breaching \
  --alarm-actions arn:aws:sns:ap-southeast-1:<acct>:narci-donor-alerts \
  --ok-actions    arn:aws:sns:ap-southeast-1:<acct>:narci-donor-alerts
```
> period=86400 因 health check 每天一发、loop sleep 86400s（容忍 30h 静默）。若改成更频
> 的 timer，按比例缩 period。
验证（🟢）：`aws cloudwatch describe-alarms --alarm-names narci-donor-unhealthy --region ap-southeast-1`

---

## 第 4 步:WAL 长跑 + SIGKILL 恢复验证 🟢→🟡

narci 沙箱封 WS 验不了真崩溃恢复，只能在真主机做。

```bash
# 🟢 长跑数日,确认无静默丢数据:盯 gap sidecar
#    replay_buffer/cold/.../{SYMBOL}_GAPS_{date}.json → coverage_pct 高 / n_gaps 少
#    （Coincheck/bitbank/bitFlyer/GMO 唯一丢数据信号,见交接 #4）

# 🟡 模拟硬崩溃(挑一个 venue 容器,确认重启后 recover_orphans 合并残留段):
#    1. 记下 replay_buffer/wal/.../*.segwal 现有段
#    2. docker kill -s SIGKILL narci-recorder-coincheck   # 硬杀,不给 graceful
#    3. 容器重启(supervisord/compose 自动拉起)后看日志:recover_orphans
#       应把残留 .segwal 合并成 RAW_*.parquet,wal/ 下不再残留旧段
#    4. 确认那段时间的数据在 realtime/ 的 RAW 里没丢

# 🟢 opt-in 集成测试(真连网/只读 aws,可在 sg 主机跑):
NARCI_NET_TESTS=1 pytest tests/recorder/test_integration.py    # 真连 Coincheck WS / Vision
NARCI_AWS_TESTS=1 pytest tests/deploy/test_aws_smoke.py        # 只读 aws sts
```

---

## 一页纸顺序

1. 🟡 IAM apply（PutMetricData）→ 🟢 verify
2. 🟡 `recorder_restart.sh {sg,jp} --pull` → 🟢 health + ★`rclone lsf ... --include 'wal/**'` 为空★ → ↩️ 不对就 `recorder_rollback.sh`
3. 🟡 donor systemd（loop + health timer）+ SNS + Alarm（`treat-missing-data breaching`）
4. 🟢 长跑盯 gap sidecar + 🟡 一次 SIGKILL 验 `recover_orphans`
