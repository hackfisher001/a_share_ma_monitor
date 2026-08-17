#!/usr/bin/env bash
# 在阿里云轻量 / ECS 上一键安装依赖并写入 crontab（交易日每 30 分钟）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ ! -d .venv ]]; then
  "$PYTHON_BIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

chmod +x scripts/run_once.sh

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "已生成 .env，请编辑填入 FEISHU_WEBHOOK_URL 后再跑监控。"
fi

CRON_LINE="*/30 9-14 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1"
# 15:00 再跑一次收盘附近
CRON_CLOSE="5 15 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1"

mkdir -p data
TMP="$(mktemp)"
crontab -l 2>/dev/null | grep -v "a_share_ma_monitor\|${ROOT}/scripts/run_once.sh" >"$TMP" || true
echo "$CRON_LINE" >>"$TMP"
echo "$CRON_CLOSE" >>"$TMP"
crontab "$TMP"
rm -f "$TMP"

echo "安装完成。当前 crontab："
crontab -l | grep run_once || true
echo
echo "飞书连通测试： ./scripts/run_once.sh --notify-test"
echo "手动试跑（不发消息）： ./scripts/run_once.sh --dry-run"
echo "正式试跑：             ./scripts/run_once.sh"
