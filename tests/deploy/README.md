# tests/deploy — 部署配置/脚本的静态验证入口

deploy/ 是脚本(aws/docker/rclone)+ 配置 + healthcheck.py。脚本**会真动 AWS**,
故测试**默认绝不执行脚本**,只做静态校验 + 只读冒烟:

| 测试文件 | 保证什么 |
|---|---|
| `test_config.py` | ECS task def(family/FARGATE/recorder 容器/端口 8079 与 Dockerfile 一致/EFS 挂载)、**IAM 最小权限**(无 Action:*、EC2 生命周期操作 tag 限定、secrets 限 narci/*、PutMetricData 限 Narci/Donor namespace)、**cloud-sync 排除 wal/\*\***(段式 WAL 不上远端)、supervisord(跑 recorder+healthcheck、autorestart) |
| `test_shell_syntax.py` | 所有 deploy `*.sh` 过 `bash -n`(语法,不执行);reco/aws 脚本真调 aws cli;entrypoint 分发 record/shell 模式 |
| `test_aws_smoke.py` | **opt-in 只读**:`NARCI_AWS_TESTS=1` 时 `aws sts get-caller-identity` 验凭证;**SG 实例 IAM 角色可解析**(donor PutMetricData 的授予目标 = 实例 role,非控制面策略);**rclone 远端配置为 remote:path**(wal/ 排除验证的目标 remote)。env 缺 / 无 cli/凭证 → skip。绝不跑会动 AWS 的脚本 |
| `test_donor_prune.py` | donor `binance_vision_push.sh` 推送后按 `DONOR_RETAIN_DAYS` 清本地暂存(2026-06-09 堆盘事故修复 42c4217 的回归守卫):旧暂存删/近的留、空目录清、retain env 生效、**prune 必在 `rclone copy` 之后**。 |

> **`test_donor_prune.py` 是「绝不执行脚本」的唯一受控例外**:它**确实跑真脚本文件**
> (而非复制 find 命令,否则脚本漂移测试照过),但把两个外部副作用全 stub 成 no-op
> —— `PYTHON=true`(跳过 `main.py download`)+ PATH 注入假 `rclone`(`exit 0`),
> `NARCI_DIR`/`DRIVE_REMOTE` 全指 tmp。脚本除了在 tmp 里跑 `find -delete` 外不碰
> 任何网络/AWS/真实数据。需要行为测试是因为静态「含 -delete」断言挡不住
> `-mtime +1` 写成 `-mtime -1`(删反方向)这类回归。

healthcheck.py 的 per-symbol staleness 逻辑在 `tests/recorder/test_healthcheck_per_symbol.py`。
