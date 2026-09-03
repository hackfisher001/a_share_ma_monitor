#!/usr/bin/env bash
# 从本地把代码同步到阿里云并重装 crontab。
#
# 只同步代码与配置，绝不覆盖服务器上的 .env 和 data/：前者存着飞书 webhook，
# 后者存着机动仓账本与成交流水，都是本地没有、且算不回来的东西。
set -euo pipefail

HOST="${DEPLOY_HOST:-root@47.95.122.231}"
KEY="${DEPLOY_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE="${DEPLOY_PATH:-/root/a_share_ma_monitor}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# 强制 IPv4（-4）：本机若走 IPv6 出口，服务器 sshd 会在密钥交换阶段直接断连，
# 表现为「TCP 22 能通、banner 能拿到、但 KEX 后即被关闭」。加 -4 立即恢复。
SSH_OPTS=(-4 -o ConnectTimeout=25 -o ServerAliveInterval=10 -i "$KEY")

cd "$ROOT"

echo "==> 检查连通性 $HOST"
if ! ssh -o BatchMode=yes "${SSH_OPTS[@]}" "$HOST" 'echo ok' >/dev/null 2>&1; then
  echo "SSH 连不上。本脚本已强制 IPv4；若仍失败且 TCP 22 能通但握手即断，" >&2
  echo "通常是 sshd 限流或机器资源耗尽，需去阿里云控制台看 VNC / 重启。" >&2
  exit 1
fi

echo "==> 本地测试"
if [ -x .venv/bin/python ]; then
  .venv/bin/python -m pytest tests/ -q
fi

echo "==> 同步代码到 $REMOTE"
ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p '$REMOTE'"
rsync -az --delete \
  -e "ssh ${SSH_OPTS[*]}" \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '.env' \
  --exclude 'data/' \
  ./ "$HOST:$REMOTE/"

echo "==> 远端安装依赖与 crontab"
ssh "${SSH_OPTS[@]}" "$HOST" "cd '$REMOTE' && chmod +x scripts/*.sh && ./scripts/install_aliyun.sh"

echo "==> 远端试跑（不发消息）"
ssh "${SSH_OPTS[@]}" "$HOST" "cd '$REMOTE' && ./scripts/run_once.sh --dry-run 2>&1 | tail -5"

echo "==> 远端心跳自检"
ssh "${SSH_OPTS[@]}" "$HOST" "cd '$REMOTE' && ./scripts/run_once.sh --heartbeat --dry-run 2>&1 | tail -8"

echo
echo "部署完成。远端 crontab："
ssh "${SSH_OPTS[@]}" "$HOST" 'crontab -l | grep run_once'
