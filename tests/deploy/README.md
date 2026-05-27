# tests/deploy — 部署配置/脚本的静态验证入口

deploy/ 是脚本(aws/docker/rclone)+ 配置 + healthcheck.py。脚本**会真动 AWS**,
故测试**绝不执行脚本**,只做静态校验 + 只读冒烟:

| 测试文件 | 保证什么 |
|---|---|
| `test_config.py` | ECS task def(family/FARGATE/recorder 容器/端口 8079 与 Dockerfile 一致/EFS 挂载)、**IAM 最小权限**(无 Action:*、EC2 生命周期操作 tag 限定、secrets 限 narci/*、PutMetricData 限 Narci/Donor namespace)、**cloud-sync 排除 wal/\*\***(段式 WAL 不上远端)、supervisord(跑 recorder+healthcheck、autorestart) |
| `test_shell_syntax.py` | 所有 deploy `*.sh` 过 `bash -n`(语法,不执行);reco/aws 脚本真调 aws cli;entrypoint 分发 record/shell 模式 |
| `test_aws_smoke.py` | **opt-in 只读**:`NARCI_AWS_TESTS=1` 时 `aws sts get-caller-identity` 验凭证;无 cli/凭证 → skip。绝不跑会动 AWS 的脚本 |

healthcheck.py 的 per-symbol staleness 逻辑在 `tests/recorder/test_healthcheck_per_symbol.py`。
