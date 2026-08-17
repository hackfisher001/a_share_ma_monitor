#!/usr/bin/env bash
# 在阿里云轻量 / ECS 上一键安装依赖并写入 crontab
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

mkdir -p data
TMP="$(mktemp)"
# 清掉本项目旧任务
crontab -l 2>/dev/null | grep -v "a_share_ma_monitor\|${ROOT}/scripts/run_once.sh" >"$TMP" || true

# MA30 触及：交易日白天巡检
echo "*/30 9-14 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"
echo "5 15 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

# A股/港股日报：收盘后
echo "10 16 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh --digest --market cn,hk >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

# 美股日报：美股收盘后（北京时间次日清晨，含周六看周五）
echo "30 6 * * 2-6 cd ${ROOT} && ./scripts/run_once.sh --digest --market us >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

crontab "$TMP"
rm -f "$TMP"

echo "安装完成。当前 crontab："
crontab -l | grep run_once || true
echo
echo "飞书连通测试： ./scripts/run_once.sh --notify-test"
echo "MA30 试跑：     ./scripts/run_once.sh --dry-run"
echo "日报试跑：     ./scripts/run_once.sh --digest --dry-run"
echo "正式日报：     ./scripts/run_once.sh --digest"
