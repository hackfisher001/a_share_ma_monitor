# A股 30 日均线监控助手

监控 `watchlist.yaml` 里的 A 股、美股与 ETF。默认进入“行动模式”：只有做 T 低吸/高抛这类需要你处理的信号才即时推送；MA30、回撤和短期涨跌压缩进日报，避免让飞书变成 K 线连续剧。

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

## 推送的几类消息

| 类型 | 触发 | 定位 |
|---|---|---|
| 急跌提醒 · -N% | 实时价对比**昨收**跌破 `intraday_dip.levels` 某一档 | **即时**，盘中就推 |
| 做T · 低吸 / 高抛 | 见下 | 即时行动清单，只动机动仓 |
| 发薪日 · 按计划买入 | 到 `payday.day`（及年终奖月） | 即时，与价格无关 |
| 走势提示 · MA30 | 现价距 MA30 在 ±`touch_pct` 以内 | 默认收进日报 |
| 回撤观察 · N% | 距 252 日高点跨过 `drawdown_levels` 的某一档 | 默认收进日报 |
| 每日巡检心跳 | 每天固定一条 | 用来判断监控本身是否还活着 |

`watchlist.yaml` 中 `notifications.action_only: true` 是默认值，它会把 MA30、回撤、异常回撤收进日报。**急跌提醒不受它影响**——这类信号只在你还能下单时有价值，收进日报等于没有。

### 急跌提醒

用实时价对比昨收，每次巡检都判一次，所以开盘跳空或盘中跳水都能当场收到。每档每天最多推一次，次日重置。

它刻意不做「是否属于该标的历史极端」的分位过滤：原先的 3 日 / 20 日分位阈值是为慢跌设计的，实测中黄金 ETF 单日跌 2.37% 两个条件都不满足，正是这类跳空被漏掉。

A 股与美股都支持盘中（美股行情走腾讯美股板）。`GC=F`、`BTC-USD` 这类非普通代码取不到实时价，会回退到最近收盘价，消息里会标注。

### 走势提示与回撤观察

这两类**只回答「现在贵还是便宜」**，不代表该推迟买入。12.3 年回测（`docs/定投与做T回测.md`，含 2015、2018、2022 三轮下跌）显示「攒钱等回撤」在 13 个标的上 **0 胜**，等 -10% 平均每年少赚 1.71%，等 -20% 少赚 3.56%。工资到账就买是最优解。

## 发薪日提醒

回测里唯一被验证有效的动作，所以它按日历触发、完全不看价格：

```yaml
payday:
  enabled: true
  day: 10             # 工资到账日，短月自动收敛到月末
  bonus_months: [2]   # 年终奖月份，会额外提示一次性买入
  bonus_day: 10
```

推送内容按「距一年高点」由低到高排序，仅供分配参考，**不作为是否买入的依据**。

## 心跳与备份

监控的沉默是有歧义的：可能今天真没信号，也可能机器已经挂了。心跳每天固定说一句话来消除这个歧义——**收不到它，就说明监控本身出事了**。

```bash
python -m src.main --heartbeat --dry-run
```

同一条命令会把 `data/alert_state.json` 与 `data/trades.csv` 快照到 `data/backups/<日期>/`，保留 30 天。这两个文件是唯一无法重新算出来的记录。

巡检的退出码也已修正：**只要有标的抓取失败就返回非 0**，不再因为碰巧有别的提醒发出去就报成功。

## 做 T（低吸 / 高抛）

默认**只对招商银行、中国移动开启**——这两只是 13 个标的里唯一在 -8%/+8% 与 -5%/+5% 两组参数下都跑赢定投的。成长股（英伟达、AMD、美光、紫金）和所有 ETF 一律不开，回测中它们每年输给定投 3%~9%。

规则（`watchlist.yaml` 的 `swing_t`）：

- 现价距 252 日高点 ≤ `-buy_drawdown_pct`（5%）→ 发**低吸**，随后解除武装
- 回升到距高点 `-rearm_pct`（4%）以内 → 重新武装，可再低吸，最多 `max_adds`（4）笔
- 机动仓均价浮盈 ≥ `+sell_bounce_pct`（5%）→ 发**高抛**，清空机动仓

给某只股票开启，加一行即可：

```yaml
  - code: "600036"
    name: "招商银行"
    market: cn
    swing_t: true
```

机动仓状态（笔数、均价、是否武装）记在 `data/alert_state.json` 的 `t_sleeve` 里，跨次运行保留。它是账本而不是去重标记，所以 `--force` 不会重放做 T 提醒；`--dry-run` 也不会写入。

待确认的提醒**不会永久静音**。同一个信号默认不重复推，但满足任一条件会重提：

- 又朝同方向走了 `escalate_pct`（3%）——避免 -5% 提醒过之后，跌到 -15% 反而没声音
- 挂满 `pending_ttl_days`（5 天）仍未记录成交

没有这条规则时，漏记一次会让这只票的做 T 彻底哑掉：买入侧被「已有待确认」永久拦截，机动仓停在 0 笔，卖出侧也就永远不会触发。

### 记录实际行为

信号只是待办，不代表你已执行。每次成交后记录一笔，系统才会推进做 T 机动仓状态、关闭对应的“待你确认”事项，并在日报显示最近行为：

```bash
python -m src.main --record-trade BUY 600036 100 42.35 --market cn
python -m src.main --record-trade SELL TSLA 5 315.20 --market us
```

记录保存在本地 `data/trades.csv`，不会提交到 Git。数量和成交价必须填真实值；当前版本不会替你下单。

流水会回放成真实持仓：日报里的**持仓与成本**卡片显示每只的数量、平均成本、现价和浮盈。卖出按平均成本扣减，已实现盈亏与剩余成本分开记。

## 每日股价日报

交易日自动推送飞书（也可手动）：

```bash
python -m src.main --digest --dry-run          # 预览：行动清单 + 市场压缩摘要
python -m src.main --digest --market cn,hk     # 仅 A/H
python -m src.main --digest --market us        # 仅美股
python -m src.main --digest                    # 正式发送
python -m src.main --digest --full-report      # 发送完整标的表（需要时再看）
```

默认日报只有三张小卡：**待确认行动、市场最强两只、市场最弱两只**。全量表可加 `--full-report` 查看；周报、月报仍保留完整表与 LLM 点评。
A/H 默认 **16:10** 推送；美股默认北京时间 **次日 06:30** 推送。

## GitHub Actions

1. 推仓库到 GitHub  
2. Settings → Secrets → Actions，添加 `FEISHU_WEBHOOK_URL`  
3. 工作流 [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml) 工作日交易时段约每 30 分钟跑；也可手动 Run workflow  

海外 Actions 访问 A 股行情可能不稳，长期建议迁阿里云。

## 阿里云部署

首次安装（在服务器上）：

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

之后每次改完代码，在**本地**一条命令同步（跑测试 → rsync → 重装 crontab → 远端试跑 → 心跳自检）：

```bash
./scripts/deploy_aliyun.sh
```

它不会覆盖服务器上的 `.env` 和 `data/`——前者存着飞书 webhook，后者存着机动仓账本与成交流水，都是本地没有且算不回来的。

若报「TCP 22 能通但握手即断」（`kex_exchange_identification: Connection closed`），那是 sshd 限流或机器资源耗尽，**本地重试无用**，需要去阿里云控制台看 VNC 或重启。

日志：`data/cron.log`；去重状态：`data/alert_state.json`；状态快照：`data/backups/`。

定时安排（`scripts/install_aliyun.sh` 自动写入）：

| 时间（北京） | 任务 |
|---|---|
| 工作日 9:35、10-11 与 13-14 每 15 分钟、14:50、15:05 | A 股盘中巡检 |
| 工作日 22:00-03:30 每 30 分钟 | 美股盘中巡检 |
| 每天 9:30 | 发薪日判定（非到账日静默） |
| 每天 21:00 | 心跳 + 状态备份 |
| 工作日 16:10 / 次日 6:30 | A股 / 美股日报 |
| 周日 20:00、每月 1 日 9:00 | 周报 / 月报 |

开盘前那一跑已经去掉——9:00 拿到的还是昨收，纯属白跑。14:50 是新增的，那是你当天还能下单的最后决策窗口。

## 目录结构

```
watchlist.yaml
.env.example                # 飞书 Webhook
src/notify.py               # 飞书卡片推送（主）
src/fetch_quotes.py
src/signals.py              # MA30 / 回撤观察 / 盘中急跌
src/t_signals.py            # 做T 低吸/高抛 + 机动仓状态
src/payday.py               # 发薪日提醒
src/ops.py                  # 巡检心跳 + 状态备份
src/trades.py               # 成交流水与持仓回放
src/action_digest.py        # 行动清单 / 持仓卡片 / 重复提醒判定
src/state.py
src/main.py                 # --notify-test / --dry-run / --heartbeat / --payday
scripts/run_once.sh
scripts/install_aliyun.sh
.github/workflows/monitor.yml

# 研究（不参与线上运行）
src/dca_backtest.py         # 按每月工资+年终奖的现金流回测
src/history_quality.py      # 长周期行情清洗（拆股还原、异常剔除）
scripts/backtest_dca_vs_t.py
docs/定投与做T回测.md        # 结论与全部明细
```

## 免责声明

本工具仅供学习与个人提醒，不构成投资建议。行情数据来自公开接口，可能延迟或中断。
