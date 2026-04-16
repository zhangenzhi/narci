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
alias nrestart='cd $NARCI_HOME && docker compose restart'
alias nrebuild='cd $NARCI_HOME && docker compose up -d --build'

# --- 按交易所重启 ---
alias nrestartcc='cd $NARCI_HOME && docker compose restart recorder-coincheck'
alias nrestartbn='cd $NARCI_HOME && docker compose restart recorder-binance-spot recorder-binance-umfut'
alias nrestartbnspot='cd $NARCI_HOME && docker compose restart recorder-binance-spot'
alias nrestartbnfut='cd $NARCI_HOME && docker compose restart recorder-binance-umfut'

# --- 日志 ---
alias nlog='cd $NARCI_HOME && docker compose logs -f'
alias nlogcc='cd $NARCI_HOME && docker compose logs -f recorder-coincheck'
alias nlogbn='cd $NARCI_HOME && docker compose logs -f recorder-binance-spot recorder-binance-umfut'
alias nlogbnspot='cd $NARCI_HOME && docker compose logs -f recorder-binance-spot'
alias nlogbnfut='cd $NARCI_HOME && docker compose logs -f recorder-binance-umfut'
alias nlog100='cd $NARCI_HOME && docker compose logs --tail=100'
alias nsynclog='cd $NARCI_HOME && docker compose logs -f cloud-sync'
alias nsynclog100='cd $NARCI_HOME && docker compose logs --tail=100 cloud-sync'

# --- 状态检查 ---
alias nstatus='docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"'
alias nhealth='for p in 8079 8080 8081; do echo "[:$p]"; curl -s http://localhost:$p/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "  not running"; done'
alias ndisk='df -h / && echo "" && du -sh $NARCI_HOME/replay_buffer/*/ 2>/dev/null'

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
alias nshellbnspot='cd $NARCI_HOME && docker compose exec recorder-binance-spot sh'
alias nshellbnfut='cd $NARCI_HOME && docker compose exec recorder-binance-umfut sh'

echo "Narci aliases loaded. Type 'nhelp' for available commands."

nhelp() {
    echo "============================================"
    echo "  Narci 常用命令"
    echo "============================================"
    echo ""
    echo "  服务管理:"
    echo "    nstart             启动当前 profile 的所有服务"
    echo "    nstop              停止所有服务"
    echo "    nrestart           重启所有运行中的服务"
    echo "    nrebuild           重新构建并启动"
    echo ""
    echo "  按交易所操作:"
    echo "    nrestartcc         重启 Coincheck 录制器"
    echo "    nrestartbn         重启 Binance 两个录制器"
    echo "    nrestartbnspot     只重启 Binance 现货"
    echo "    nrestartbnfut      只重启 Binance 合约"
    echo ""
    echo "  日志:"
    echo "    nlog               所有运行中服务的合并日志"
    echo "    nlogcc             Coincheck 录制器日志"
    echo "    nlogbn             Binance 两个录制器日志"
    echo "    nlogbnspot/bnfut   单独查看"
    echo "    nsynclog           云同步日志"
    echo ""
    echo "  状态:"
    echo "    nstatus            容器运行状态"
    echo "    nhealth            所有端口健康检查"
    echo "    ndisk              磁盘使用"
    echo ""
    echo "  云同步:"
    echo "    nsyncrestart/stop/start"
    echo ""
    echo "  其他:"
    echo "    npull              拉代码 + 重建 + 重载别名"
    echo "    nshellcc/bnspot/bnfut  进入容器 shell"
    echo "    nhelp              显示本帮助"
    echo "============================================"
}
