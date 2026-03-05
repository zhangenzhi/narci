#!/bin/bash
# ============================================================
# Narci 服务器常用命令别名
# 安装: source ~/narci/deploy/server-aliases.sh
# 永久生效: echo 'source ~/narci/deploy/server-aliases.sh' >> ~/.bashrc
# ============================================================

export NARCI_HOME="$HOME/narci"

# --- 服务管理 ---
alias nstart='cd $NARCI_HOME && docker compose up -d'
alias nstop='cd $NARCI_HOME && docker compose down'
alias nrestart='cd $NARCI_HOME && docker compose restart recorder'
alias nrebuild='cd $NARCI_HOME && docker compose up -d --build'

# --- 日志 ---
alias nlog='cd $NARCI_HOME && docker compose logs -f recorder'
alias nlog100='cd $NARCI_HOME && docker compose logs --tail=100 recorder'

# --- 状态检查 ---
alias nstatus='docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"'
alias nhealth='curl -s http://localhost:8079/health | python3 -m json.tool'
alias ndisk='df -h /home && echo "" && du -sh $NARCI_HOME/replay_buffer/*/ 2>/dev/null'

# --- 数据管理 ---
alias ncompact='cd $NARCI_HOME && docker compose run --rm compact'
alias nsync='cd $NARCI_HOME && docker compose run --rm sync'
alias ncount='find $NARCI_HOME/replay_buffer -name "*.parquet" | wc -l'
alias ncold='ls -lhS $NARCI_HOME/replay_buffer/cold/ 2>/dev/null || echo "cold 目录为空"'

# --- 代码更新 ---
alias npull='cd $NARCI_HOME && git pull && docker compose up -d --build'

# --- 快捷进入容器 ---
alias nshell='cd $NARCI_HOME && docker compose run --rm recorder shell'

echo "Narci aliases loaded. Type 'nhelp' for available commands."

nhelp() {
    echo "============================================"
    echo "  Narci 常用命令"
    echo "============================================"
    echo ""
    echo "  服务管理:"
    echo "    nstart     启动录制服务"
    echo "    nstop      停止所有服务"
    echo "    nrestart   重启 recorder"
    echo "    nrebuild   重新构建并启动"
    echo ""
    echo "  日志与状态:"
    echo "    nlog       实时查看日志"
    echo "    nlog100    查看最近 100 行日志"
    echo "    nstatus    查看容器运行状态"
    echo "    nhealth    健康检查"
    echo "    ndisk      磁盘使用情况"
    echo ""
    echo "  数据管理:"
    echo "    ncompact   手动执行 compact"
    echo "    nsync      手动同步到 Google Drive"
    echo "    ncount     统计 parquet 文件数量"
    echo "    ncold      查看冷数据归档"
    echo ""
    echo "  其他:"
    echo "    npull      拉取最新代码并重启"
    echo "    nshell     进入容器 shell"
    echo "    nhelp      显示本帮助"
    echo "============================================"
}
