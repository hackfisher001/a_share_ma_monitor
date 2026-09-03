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

# 中文字体：行情表 PNG 渲染需要
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fonts-wqy-microhei fonts-wqy-zenhei >/dev/null || true
fi

chmod +x scripts/run_once.sh

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "已生成 .env，请编辑填入 FEISHU_WEBHOOK_URL 后再跑监控。"
fi

mkdir -p data
TMP="$(mktemp)"
# 清掉本项目旧任务
crontab -l 2>/dev/null | grep -v "a_share_ma_monitor\|${ROOT}/scripts/run_once.sh" >"$TMP" || true

# A股盘中巡检。开盘前一刻跑没有意义（还是昨收），所以从 9:35 起；
# 14:50 那一跑是你当天还能下单的最后决策窗口。
echo "35 9 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"
echo "*/15 10-11 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"
echo "*/15 13-14 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"
echo "50 14 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"
echo "5 15 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

# 美股盘中（北京时间 21:30-04:00）：腾讯美股行情可达，急跌当场就能收到
echo "*/30 22-23 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"
echo "*/30 0-3 * * 2-6 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

# 心跳：每天固定说一句话，让「没消息」和「挂了」能区分开；同时备份状态文件
echo "0 21 * * * cd ${ROOT} && ./scripts/run_once.sh --heartbeat >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

# 发薪日提醒：每天判一次，只有到账日才会真的推
echo "30 9 * * * cd ${ROOT} && ./scripts/run_once.sh --payday >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

# A股日报：收盘后（含 DeepSeek 点评）
echo "10 16 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh --report daily --market cn >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

# 美股日报：美股收盘后（北京时间次日清晨，含周六看周五）
echo "30 6 * * 2-6 cd ${ROOT} && ./scripts/run_once.sh --report daily --market us >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

# 周报：每周日晚上
echo "0 20 * * 0 cd ${ROOT} && ./scripts/run_once.sh --report weekly >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

# 月报：每月 1 日早上
echo "0 9 1 * * cd ${ROOT} && ./scripts/run_once.sh --report monthly >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

crontab "$TMP"
rm -f "$TMP"

echo "安装完成。当前 crontab："
crontab -l | grep run_once || true
echo
echo "飞书连通测试： ./scripts/run_once.sh --notify-test"
echo "MA30 试跑：     ./scripts/run_once.sh --dry-run"
echo "心跳： ./scripts/run_once.sh --heartbeat --dry-run"
echo "发薪日：./scripts/run_once.sh --payday --dry-run"
echo "日报： ./scripts/run_once.sh --report daily"
echo "周报： ./scripts/run_once.sh --report weekly"
echo "月报： ./scripts/run_once.sh --report monthly"
