# A股 30 日均线监控助手

监控 `watchlist.yaml` 里的 A 股。当**现价相对 30 日均线偏离 ≤ 0.5%**（可配置）时，通过 **飞书群机器人** 发送买入提醒卡片。同日同票只提醒一次。

可用 **GitHub Actions** 定时跑，也可部署到 **阿里云轻量/ECS** 用 crontab 跑（推荐长期方案）。

## 1. 配置飞书机器人（约 1 分钟）

1. 打开（或新建）一个飞书群  
2. 群设置 → **群机器人** → **添加机器人** → 选 **自定义机器人**  
3. 名称随意，例如 `MA30监控`；安全设置建议选「自定义关键词」，关键词填 `买入` 或 `MA30`（消息里已包含）  
4. 复制 Webhook 地址  

本地：

```bash
cp .env.example .env
# 把 FEISHU_WEBHOOK_URL= 后面换成你的地址
```

连通测试：

```bash
python -m src.main --notify-test
```

手机飞书应立刻收到「连通测试」卡片。

## 2. 快速开始

公司 VPN 下 PyPI / 行情源常不稳定，建议用国内镜像；正式长期跑推荐阿里云国内机。

```bash
cd a_share_ma_monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
cp .env.example .env   # 填入 FEISHU_WEBHOOK_URL
# 编辑 watchlist.yaml：改成你要盯的股票

python -m src.main --notify-test   # 测飞书
python -m src.main --dry-run       # 只算信号，不推送
python -m src.main                 # 正式推送
```

## 每日股价日报

交易日自动推送飞书（也可手动）：

```bash
python -m src.main --digest --dry-run          # 预览全部市场
python -m src.main --digest --market cn,hk     # 仅 A/H
python -m src.main --digest --market us        # 仅美股
python -m src.main --digest                    # 正式发送
```

每只股票包含：**现价、MA30、1日 / 5日 / 1周 / 1月 / 半年 / 1年** 涨跌幅。
A/H 默认 **16:10** 推送；美股默认北京时间 **次日 06:30** 推送。

## GitHub Actions

1. 推仓库到 GitHub  
2. Settings → Secrets → Actions，添加 `FEISHU_WEBHOOK_URL`  
3. 工作流 [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml) 工作日交易时段约每 30 分钟跑；也可手动 Run workflow  

海外 Actions 访问 A 股行情可能不稳，长期建议迁阿里云。

## 阿里云部署

```bash
sudo apt update && sudo apt install -y python3 python3-venv git   # Ubuntu
git clone <你的仓库地址> ~/a_share_ma_monitor
cd ~/a_share_ma_monitor
chmod +x scripts/*.sh
./scripts/install_aliyun.sh
nano .env                 # FEISHU_WEBHOOK_URL
nano watchlist.yaml
./scripts/run_once.sh --notify-test
./scripts/run_once.sh --dry-run
```

日志：`data/cron.log`；去重状态：`data/alert_state.json`。

## 目录结构

```
watchlist.yaml
.env.example                # 飞书 Webhook
src/notify.py               # 飞书卡片推送（主）
src/fetch_quotes.py
src/signals.py
src/state.py
src/main.py                 # --notify-test / --dry-run
scripts/run_once.sh
scripts/install_aliyun.sh
.github/workflows/monitor.yml
```

## 免责声明

本工具仅供学习与个人提醒，不构成投资建议。行情数据来自公开接口，可能延迟或中断。
