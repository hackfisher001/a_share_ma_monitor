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
# 间隔 5 分钟：15 分钟太疏，急跌穿档后要等下一轮才推，体感会「已经跌穿却没提醒」。
echo "35,40,45,50,55 9 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"
echo "*/5 10-11 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"
echo "*/5 13-14 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"
echo "5 15 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

# 美股盘中（北京时间 21:30-04:00）：腾讯美股行情可达，急跌当场就能收到
echo "*/30 22-23 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"
echo "*/30 0-3 * * 2-6 cd ${ROOT} && ./scripts/run_once.sh >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

# 心跳：每天固定说一句话，让「没消息」和「挂了」能区分开；同时备份状态文件
echo "0 21 * * * cd ${ROOT} && ./scripts/run_once.sh --heartbeat >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

# 日报按市场拆开：A股收盘后推 A 股；美股收盘后再推美股。
# 美股常规收盘 16:00 ET → 北京时间夏令约 04:00、冬令约 05:00，取 5:30 两边都盖住。
# 星期二到星期六对应美股周一到周五的交易日。
echo "10 16 * * 1-5 cd ${ROOT} && ./scripts/run_once.sh --report daily --market cn >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"
echo "30 5 * * 2-6 cd ${ROOT} && ./scripts/run_once.sh --report daily --market us >> ${ROOT}/data/cron.log 2>&1" >>"$TMP"

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
echo "A股日报： ./scripts/run_once.sh --report daily --market cn"
echo "美股日报： ./scripts/run_once.sh --report daily --market us"
echo "周报： ./scripts/run_once.sh --report weekly"
echo "月报： ./scripts/run_once.sh --report monthly"
