#!/usr/bin/env bash
# 供 cron / systemd 调用：交易时段巡检一次
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
elif [[ -f venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

export PYTHONUNBUFFERED=1
exec python -m src.main "$@"
