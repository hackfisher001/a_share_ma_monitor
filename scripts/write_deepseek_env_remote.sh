#!/usr/bin/env bash
# 只把 DeepSeek 配置写入阿里云 .env，不覆盖飞书等其它项，不走 git。
set -euo pipefail
HOST="${DEPLOY_HOST:-root@47.95.122.231}"
KEY="${DEPLOY_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE="${DEPLOY_PATH:-/root/a_share_ma_monitor}"
SSH_OPTS=(-4 -o ConnectTimeout=25 -o ServerAliveInterval=10 -i "$KEY")

# 从本机 .env 读（勿把 key 写进仓库脚本）
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
set -a
source "$ROOT/.env"
set +a

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "本地 .env 缺少 DEEPSEEK_API_KEY" >&2
  exit 1
fi

BASE_URL="${DEEPSEEK_BASE_URL:-https://api.deepseek.com}"
MODEL="${DEEPSEEK_MODEL:-deepseek-flash}"

echo "==> 写入 $HOST:$REMOTE/.env （仅 DEEPSEEK_*）"
ssh "${SSH_OPTS[@]}" "$HOST" \
  "REMOTE='$REMOTE' KEY='$DEEPSEEK_API_KEY' BASE='$BASE_URL' MODEL='$MODEL' python3 -" <<'PY'
import os
from pathlib import Path
p = Path(os.environ["REMOTE"]) / ".env"
text = p.read_text(encoding="utf-8") if p.exists() else ""
lines = [
    ln for ln in text.splitlines()
    if ln.strip() and not ln.strip().startswith("DEEPSEEK_")
]
lines += [
    f"DEEPSEEK_API_KEY={os.environ['KEY']}",
    f"DEEPSEEK_BASE_URL={os.environ['BASE']}",
    f"DEEPSEEK_MODEL={os.environ['MODEL']}",
]
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("ok", p)
for ln in p.read_text(encoding="utf-8").splitlines():
    if ln.startswith("DEEPSEEK_"):
        k, _, v = ln.partition("=")
        print(k, "=", (v[:7] + "…") if "KEY" in k else v)
PY

echo "完成。未推送 git，未覆盖飞书 webhook。"
