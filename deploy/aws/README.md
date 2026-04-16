# AWS 部署手册（Tokyo 区）

目标：在 `ap-northeast-1` 跑 24/7 录制 + 冷数据推 S3。预算 ¥4,050/月，可支撑约 24 个月。

## 前置一次性准备

### 1. 创建 S3 bucket（冷数据）

```bash
aws s3api create-bucket \
  --bucket narci-cold-data-tokyo \
  --region ap-northeast-1 \
  --create-bucket-configuration LocationConstraint=ap-northeast-1

# 生命周期：90 天后自动转 Glacier Deep Archive
aws s3api put-bucket-lifecycle-configuration \
  --bucket narci-cold-data-tokyo \
  --lifecycle-configuration '{"Rules":[{"ID":"to-glacier","Status":"Enabled","Filter":{"Prefix":""},"Transitions":[{"Days":90,"StorageClass":"DEEP_ARCHIVE"}]}]}'
```

### 2. 创建 IAM Role（EC2 用）

Trust policy: `ec2.amazonaws.com`
Permission policy：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject", "s3:GetObject", "s3:DeleteObject",
        "s3:ListBucket", "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::narci-cold-data-tokyo",
        "arn:aws:s3:::narci-cold-data-tokyo/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["ssm:GetParameter"],
      "Resource": "arn:aws:ssm:ap-northeast-1:*:parameter/narci/*"
    }
  ]
}
```

Role 名字建议 `NarciRecorderRole`。

### 3. SSM 参数（保存 bucket 名）

```bash
aws ssm put-parameter \
  --region ap-northeast-1 \
  --name /narci/s3_bucket \
  --value s3://narci-cold-data-tokyo/narci_raw \
  --type String
```

### 4. Security Group

| 端口 | 协议 | 来源 | 用途 |
|---|---|---|---|
| 22 | TCP | 你的 IP | SSH |
| 8079-8081 | TCP | 你的 IP（可选） | 健康检查页面 |

## 启动 EC2

**建议规格**：

| 选项 | 规格 | 月成本 | 备注 |
|---|---|---|---|
| **推荐** | `t4g.small`（2 vCPU ARM, 2GB） | ~$15 | 录 3 个 recorder 刚够 |
| 备选 | `t4g.medium`（2 vCPU, 4GB） | ~$30 | 更宽裕 |

**配置**：
- AMI: Amazon Linux 2023 (ARM64)
- 存储: gp3 50GB（`retain_days=7` × 3 recorder × ~1GB/日 ≈ 21GB 峰值）
- IAM Role: `NarciRecorderRole`
- **User data**: 贴 `deploy/aws/user-data.sh` 全文

启动后约 3 分钟完成 bootstrap。SSH 进去验证：

```bash
ssh ec2-user@<EC2-IP>
source narci/deploy/server-aliases.sh
export NARCI_HOME=$HOME/narci
nstatus          # 应该看到 4 个容器 UP
nlog             # 看录制输出
```

## 日常运维

```bash
# 更新代码
npull

# 查看 S3 同步情况
aws s3 ls s3://narci-cold-data-tokyo/narci_raw/ --recursive --human-readable --summarize | tail

# 查看 S3 花费
aws ce get-cost-and-usage \
  --time-period Start=$(date -u -d '30 days ago' +%Y-%m-%d),End=$(date -u +%Y-%m-%d) \
  --granularity MONTHLY \
  --metrics UnblendedCost \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon Simple Storage Service"]}}'
```

## 从 GCP 迁移数据（可选）

如果 GCP 上已有录制数据想保留：

```bash
# 在 GCP 机器上
rclone copy /root/narci/replay_buffer s3://narci-cold-data-tokyo/narci_raw \
  --transfers 8 --s3-region ap-northeast-1 \
  --s3-access-key-id ... --s3-secret-access-key ...
```

## 成本护栏

设预算告警：

```bash
aws budgets create-budget \
  --account-id $(aws sts get-caller-identity --query Account --output text) \
  --budget '{"BudgetName":"narci-monthly","BudgetLimit":{"Amount":"50","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST"}' \
  --notifications-with-subscribers '[{"Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":80},"Subscribers":[{"SubscriptionType":"EMAIL","Address":"你的邮箱"}]}]'
```

## 故障排查

**cloud-sync 一直失败**：
```bash
docker compose logs cloud-sync | tail -30
# 如果看到 "NoCredentialProviders"，说明 IAM Role 没关联到实例
curl http://169.254.169.254/latest/meta-data/iam/security-credentials/
# 应该返回 NarciRecorderRole
```

**录制器 OOM**：
- 升级到 `t4g.medium`，或降低并发交易对数
- 检查 `retain_days` 是否过长导致磁盘占用

**EBS 磁盘打满**：
- `retain_days=7` 对合约高频对可能不够，改 `3`
- 或切更大的 gp3 volume（在线扩容，无需重启）
