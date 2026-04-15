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
alias nrestart='cd $NARCI_HOME && docker compose restart recorder-coincheck recorder-spot recorder-umfut'
alias nrestartcc='cd $NARCI_HOME && docker compose restart recorder-coincheck'
alias nrestartspot='cd $NARCI_HOME && docker compose restart recorder-spot'
alias nrestartumfut='cd $NARCI_HOME && docker compose restart recorder-umfut'
alias nrebuild='cd $NARCI_HOME && docker compose up -d --build'

# --- 日志 ---
alias nlog='cd $NARCI_HOME && docker compose logs -f recorder-coincheck recorder-spot recorder-umfut'
alias nlogcc='cd $NARCI_HOME && docker compose logs -f recorder-coincheck'
alias nlogspot='cd $NARCI_HOME && docker compose logs -f recorder-spot'
alias nlogumfut='cd $NARCI_HOME && docker compose logs -f recorder-umfut'
alias nlog100='cd $NARCI_HOME && docker compose logs --tail=100 recorder-coincheck recorder-spot recorder-umfut'
alias nsynclog='cd $NARCI_HOME && docker compose logs -f cloud-sync'
alias nsynclog100='cd $NARCI_HOME && docker compose logs --tail=100 cloud-sync'

# --- 状态检查 ---
alias nstatus='docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"'
alias nhealthcc='curl -s http://localhost:8081/health | python3 -m json.tool'
alias nhealthspot='curl -s http://localhost:8079/health | python3 -m json.tool'
alias nhealthumfut='curl -s http://localhost:8080/health | python3 -m json.tool'
alias nhealth='echo "[COINCHECK]"; nhealthcc; echo "[BN-SPOT]"; nhealthspot; echo "[BN-UMFUT]"; nhealthumfut'
alias ndisk='df -h /home && echo "" && du -sh $NARCI_HOME/replay_buffer/*/ 2>/dev/null'

# --- 数据管理 ---
alias ncount='find $NARCI_HOME/replay_buffer -name "*.parquet" | wc -l'
alias ncold='ls -lhS $NARCI_HOME/replay_buffer/cold/ 2>/dev/null || echo "cold 目录为空"'

# --- 云同步管理 ---
alias nsyncrestart='cd $NARCI_HOME && docker compose restart cloud-sync'
alias nsyncstop='cd $NARCI_HOME && docker compose stop cloud-sync'
alias nsyncstart='cd $NARCI_HOME && docker compose start cloud-sync'

# --- 代码更新 ---
alias npull='cd $NARCI_HOME && git pull && docker compose up -d --build --no-cache && source $NARCI_HOME/deploy/server-aliases.sh'

# --- 快捷进入容器 ---
alias nshellcc='cd $NARCI_HOME && docker compose exec recorder-coincheck sh'
alias nshellspot='cd $NARCI_HOME && docker compose exec recorder-spot sh'
alias nshellumfut='cd $NARCI_HOME && docker compose exec recorder-umfut sh'

echo "Narci aliases loaded. Type 'nhelp' for available commands."

nhelp() {
    echo "============================================"
    echo "  Narci 常用命令"
    echo "============================================"
    echo ""
    echo "  服务管理:"
    echo "    nstart             启动所有服务 (spot + umfut + cloud-sync)"
    echo "    nstop              停止所有服务"
    echo "    nrestart           重启两个 recorder"
    echo "    nrestartspot       只重启现货录制器"
    echo "    nrestartumfut      只重启合约录制器"
    echo "    nrebuild           重新构建并启动"
    echo ""
    echo "  日志:"
    echo "    nlog               实时查看两个 recorder 的合并日志"
    echo "    nlogspot           实时查看现货 recorder 日志"
    echo "    nlogumfut          实时查看合约 recorder 日志"
    echo "    nlog100            两个 recorder 最近 100 行"
    echo "    nsynclog           实时查看云同步日志"
    echo "    nsynclog100        云同步最近 100 行"
    echo ""
    echo "  状态检查:"
    echo "    nstatus            查看容器运行状态"
    echo "    nhealth            两个 recorder 健康检查"
    echo "    nhealthspot/umfut  单独检查"
    echo "    ndisk              磁盘使用情况"
    echo ""
    echo "  数据管理:"
    echo "    ncount             统计 parquet 文件数量"
    echo "    ncold              查看冷数据归档"
    echo ""
    echo "  云同步管理:"
    echo "    nsyncrestart       重启 cloud-sync"
    echo "    nsyncstop          停止 cloud-sync"
    echo "    nsyncstart         启动 cloud-sync"
    echo ""
    echo "  其他:"
    echo "    npull              拉取最新代码并重启"
    echo "    nshellspot/umfut   进入对应容器 shell"
    echo "    nhelp              显示本帮助"
    echo "============================================"
}
