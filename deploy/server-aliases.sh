#!/bin/bash
# ============================================================
# Narci 服务器常用命令别名
# 安装: source ~/narci/deploy/server-aliases.sh
# 永久生效: echo 'source ~/narci/deploy/server-aliases.sh' >> ~/.bashrc
#
# 自动识别当前部署模式（tokyo / global），只显示相关命令
# ============================================================

export NARCI_HOME="$HOME/narci"

# 检测当前 profile
_NARCI_PROFILE=""
if [ -f "$NARCI_HOME/.env" ]; then
    _NARCI_PROFILE=$(grep -oP 'COMPOSE_PROFILES=\K\S+' "$NARCI_HOME/.env" 2>/dev/null || echo "")
fi

# --- 服务管理（通用） ---
alias nstart='cd $NARCI_HOME && docker compose up -d'
alias nstop='cd $NARCI_HOME && docker compose down'
alias nrestart='cd $NARCI_HOME && docker compose restart'
alias nrebuild='cd $NARCI_HOME && docker compose up -d --build'

# --- 日志（通用） ---
alias nlog='cd $NARCI_HOME && docker compose logs -f'
alias nlog100='cd $NARCI_HOME && docker compose logs --tail=100'
alias nsynclog='cd $NARCI_HOME && docker compose logs -f cloud-sync'
alias nsynclog100='cd $NARCI_HOME && docker compose logs --tail=100 cloud-sync'

# --- Tokyo profile: Coincheck + Binance JP ---
alias nrestartcc='cd $NARCI_HOME && docker compose restart recorder-coincheck'
alias nrestartbnjp='cd $NARCI_HOME && docker compose restart recorder-binance-jp'
alias nlogcc='cd $NARCI_HOME && docker compose logs -f recorder-coincheck'
alias nlogbnjp='cd $NARCI_HOME && docker compose logs -f recorder-binance-jp'
alias nlogcc100='cd $NARCI_HOME && docker compose logs --tail=100 recorder-coincheck'
alias nlogbnjp100='cd $NARCI_HOME && docker compose logs --tail=100 recorder-binance-jp'
alias nshellcc='cd $NARCI_HOME && docker compose exec recorder-coincheck sh'
alias nshellbnjp='cd $NARCI_HOME && docker compose exec recorder-binance-jp sh'

# --- Global profile: Binance 国际 spot + futures ---
alias nrestartbnspot='cd $NARCI_HOME && docker compose restart recorder-binance-spot'
alias nrestartbnfut='cd $NARCI_HOME && docker compose restart recorder-binance-umfut'
alias nrestartbn='cd $NARCI_HOME && docker compose restart recorder-binance-spot recorder-binance-umfut'
alias nlogbnspot='cd $NARCI_HOME && docker compose logs -f recorder-binance-spot'
alias nlogbnfut='cd $NARCI_HOME && docker compose logs -f recorder-binance-umfut'
alias nlogbn='cd $NARCI_HOME && docker compose logs -f recorder-binance-spot recorder-binance-umfut'
alias nshellbnspot='cd $NARCI_HOME && docker compose exec recorder-binance-spot sh'
alias nshellbnfut='cd $NARCI_HOME && docker compose exec recorder-binance-umfut sh'

# --- 状态检查 ---
alias nstatus='docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"'
alias nhealth='for p in 8079 8080; do echo "[:$p]"; curl -s http://localhost:$p/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "  not running"; done'

# --- 数据（Docker volume 内） ---
ncount() {
    for c in $(docker ps --format '{{.Names}}' | grep recorder); do
        cnt=$(docker exec "$c" find /app/replay_buffer -name '*.parquet' 2>/dev/null | wc -l)
        echo "  $c: $cnt files"
    done
}
alias ndisk='df -h / && echo "" && docker system df'

# --- 云同步管理 ---
alias nsyncrestart='cd $NARCI_HOME && docker compose restart cloud-sync'
alias nsyncstop='cd $NARCI_HOME && docker compose stop cloud-sync'
alias nsyncstart='cd $NARCI_HOME && docker compose start cloud-sync'

# --- 代码更新 ---
alias npull='cd $NARCI_HOME && git pull && docker compose up -d --build --no-cache && source $NARCI_HOME/deploy/server-aliases.sh'

# --- 启动日志 ---
alias nbootlog='cat /var/log/narci-bootstrap.log'

echo "Narci aliases loaded (profile: ${_NARCI_PROFILE:-unknown}). Type 'nhelp' for commands."

nhelp() {
    echo "============================================"
    echo "  Narci 常用命令 (profile: ${_NARCI_PROFILE:-unknown})"
    echo "============================================"
    echo ""
    echo "  服务管理:"
    echo "    nstart             启动当前 profile 的所有服务"
    echo "    nstop              停止所有服务"
    echo "    nrestart           重启所有运行中的服务"
    echo "    nrebuild           重新构建并启动"
    echo "    npull              拉代码 + 重建 + 重载别名"
    echo ""
    if [ "$_NARCI_PROFILE" = "tokyo" ] || [ -z "$_NARCI_PROFILE" ]; then
    echo "  Tokyo 录制器:"
    echo "    nlogcc / nlogcc100     Coincheck 日志"
    echo "    nlogbnjp / nlogbnjp100 Binance JP 日志"
    echo "    nrestartcc             重启 Coincheck"
    echo "    nrestartbnjp           重启 Binance JP"
    echo "    nshellcc / nshellbnjp  进入容器"
    echo ""
    fi
    if [ "$_NARCI_PROFILE" = "global" ] || [ -z "$_NARCI_PROFILE" ]; then
    echo "  Global 录制器:"
    echo "    nlogbnspot / nlogbnfut Binance 现货/合约日志"
    echo "    nlogbn                 Binance 两个录制器"
    echo "    nrestartbn             重启 Binance 两个"
    echo "    nrestartbnspot/bnfut   单独重启"
    echo "    nshellbnspot/bnfut     进入容器"
    echo ""
    fi
    echo "  状态 & 数据:"
    echo "    nstatus            容器运行状态"
    echo "    nhealth            健康检查"
    echo "    ncount             各容器 parquet 文件数"
    echo "    ndisk              磁盘 + Docker 空间"
    echo ""
    echo "  云同步:"
    echo "    nsynclog           cloud-sync 日志"
    echo "    nsyncrestart/stop/start"
    echo ""
    echo "  其他:"
    echo "    nlog / nlog100     所有服务合并日志"
    echo "    nbootlog           EC2 启动脚本日志"
    echo "    nhelp              显示本帮助"
    echo "============================================"
}
