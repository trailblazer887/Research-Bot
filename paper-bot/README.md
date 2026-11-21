# paper-bot：每日沙箱内存文献

每天 **09:30（周一到周六）** 从 arXiv 抓 `cs.OS / cs.DC / cs.PF / cs.CR` 的新论文，按“容器沙箱内存水位线”打分，取 5 篇，用一段话概括，推到 QQ。

## 它做什么

```
arXiv（72h 窗口） → 去重 → LLM 给全部候选打分（会议 +1.5）
→ Top 5 + 库存回填 → 结构化摘要 → 模板渲染
→ 写 sending 日志（分 chunk） → Qmsg 逐条推送并记录 msgId
→ 写 papers.jsonl + data/digests/YYYY-MM-DD.md → 精简 last_run
```

- **不做关键词过滤**：真实数据里 `watermark` 会命中视频水印、`swap` 会命中 SWaP / 量子纠缠、`reclaim` 会命中机器人项目，误报太多。每天约 130 篇候选全部交给 LLM 打分，成本仍然很低。
- **会议优先**：`comment` / `journal_ref` 里带 ASPLOS、OSDI、ATC 等的论文加 1.5 分，消息里标 `[ASPLOS'26]`。
- **不凑数**：不足 5 篇时从 14 天库存回填；仍不足就少发。
- **反幻觉**：摘要先输出 JSON，再用模板渲染；`evidence` 里的数字会回查 abstract，对不上就降级。
- **出站消毒**：发送前替换 URL、IPv4、5 位以上连续数字；错误通知只保留异常类型 / HTTP 状态 / host，不会带出 Qmsg key 或 URL。
- **发送幂等**：每个 chunk 成功后就记录 msgId；同一天重跑直接跳过，发送中断后只补未发送的 chunk，已提交的不重发。
- **无服务器**：GitHub Actions + JSONL，不需要数据库、VPS、Docker。

## 它不做什么（v1 边界）

- 不爬 ACM DL（Cloudflare），不覆盖“纯会议、无 arXiv 版本”的论文（v2 用 OpenAlex）。
- 不用向量库、不做全文 PDF 解析、不做双向交互。
- 不追求“每天必有 5 篇”：细分方向每天可能只有 2–3 篇。

## 快速开始

```bash
cd paper-bot

# 依赖只有 requests，系统 Python 通常已自带（有输出就不用装）
python3 -c "import requests; print('requests', requests.__version__)"

# 1) 不联网、不花钱，验证解析/渲染
python3 main.py --self-test

# 1b) 单元测试：消毒、异常脱敏、发送幂等/续发
python3 -m unittest discover -s tests -t . -v

# 2a) 联网抓取，只看抓到哪些（不配 LLM、不花钱、不发送）
python3 main.py --mode daily --dry-run

# 2b) 配上 LLM，看完整的打分 + 摘要（会花几分钱）
export LLM_API_KEY="sk-..."
python3 main.py --mode daily --dry-run --limit 3
```

`--dry-run` 不写状态，可以反复跑。

## 状态文件与仓库体积

`data/papers.jsonl` 会记录所有候选（约 120 条/天），但 `skipped` 行只保留：

```
id / title / title_key / score / final_score / status / first_seen
```

一天约 30–40 KB；`sent` 和 `pool` 保留完整摘要（回填时要用）。历史数据可以手动压缩：

```bash
python3 main.py --compact
```

`data/last_run.json` 在发送期间是**投递日志**：包含当天的 chunks、每个 chunk 的 msgId、待落盘的论文行。成功后会被压缩成 `status=sent` 的小文件；发送失败则保留 `status=failed`，下一轮只补未发送的 chunk。正常跑完后它只有几行，失败时才会临时变大。

## 关于依赖：不要直接 pip install 到系统 Python

`requirements.txt` 里只有 `requests`，而且通常随系统 Python 已装好。Ubuntu 23.04+ / Debian 12+ 的系统 Python 是 PEP 668 “externally managed”，直接 `pip install` 会被拒绝；加 `--break-system-packages` 才是真的污染系统，不推荐。

如果 `import requests` 失败，用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`.venv/` 已在 `.gitignore` 里。GitHub Actions 的 `pip install` 跑在隔离的 runner 上，不影响本机。

## Qmsg 的硬限制（重要）

| 限制 | 影响 | 已经怎么处理 |
|---|---|---|
| 单条 ≤ **1800 字符** | 5 篇会拆成 2 条 | `max_chars = 1700`，尽量在论文边界拆 |
| 同一 key **每 3 秒**只能推 1 次 | 连发第二条会被限流 | 两条之间自动 `sleep(3.5)` |
| **禁止 URL / IPv4 / 连续数字** | **消息里不能放 arXiv 链接**，否则可能违规封号 | 发送前 `sanitize_for_qmsg()` 替换；错误通知只保留类型/状态/host；默认 `include_url = false`，链接只保留在本地 `data/digests/*.md` |
| 内容检测是异步的 | 返回成功不代表最终送达 | 记录 msgId；失败时保留 chunk 供续发，不整轮重来 |

> 如果你一定要在 QQ 里点链接：换 NapCatQQ（自建，有封号风险）或 QQ 邮箱（SMTP，最稳），不要用编码/短链绕过 Qmsg 的检测。

## 你需要准备两样东西

| 名称 | 用途 | 获取 |
|---|---|---|
| `QMSG_KEY` | 推送到 QQ | 登录 <https://qmsg.zendee.cn/>，按提示加机器人好友，复制 key |
| `LLM_API_KEY` | 打分 + 摘要 | DeepSeek / Qwen / OpenAI 任一兼容接口 |

`config.toml` 里默认用 DeepSeek：

```toml
[llm]
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
```

换服务商只改这两行；key 永远放环境变量，不进仓库。

另外把 `config.toml` 里的 `user_agent` 邮箱改成你自己的（arXiv 礼貌池要求）：

```toml
[arxiv]
user_agent = "paper-bot/1.0 (mailto:你的邮箱@example.com)"
```

## 部署到 GitHub Actions

1. 新建一个仓库（建议 private），把 `paper-bot/` 和 `.github/workflows/daily-paper-digest.yml` 推上去。
2. 仓库 **Settings → Secrets and variables → Actions** 添加：
   - `QMSG_KEY`
   - `LLM_API_KEY`
3. **Actions → daily-paper-digest → Run workflow**，`mode=daily`，`limit` 填 `2` 先试跑。
4. 确认 QQ 收到后，定时任务自动生效（`30 1 * * 1-6` UTC）。

> 第一次务必手动触发。Actions 的 cron 在高峰期会延迟 5–30 分钟，这是正常的。

## 时间：09:30，正好在 arXiv 公告之后

- cron `30 1 * * 1-6`（UTC）= **09:30 CST，周一到周六**。
- arXiv 公告在美东 20:00（周日到周四），约等于北京时间 08:00–09:00。
- 09:30 在公告之后，所以当天就能看到前一晚公告的那批；周一也能看到周日晚的公告。
- 周日不发。
- 想更早（比如 07:30）：把 cron 改成 `30 23 * * 0-5`，代价是每天晚约 1 天、周一要靠库存回填。

## 调参

| 参数 | 位置 | 说明 |
|---|---|---|
| `top_n` | `[general]` | 每天推几篇，默认 5 |
| `min_final_score` | `[general]` | 进候选的最低分，默认 4.0 |
| `lookback_hours` | `[general]` | 抓取窗口，默认 72 |
| `boost` | `[venue]` | 会议加分，默认 1.5 |
| `max_chars` | `[qmsg]` | 单条消息上限，默认 1700（Qmsg 上限 1800） |
| `include_url` | `[qmsg]` | 是否在 QQ 消息里放链接，默认 `false` |

**前两周不要改参数。** 先记录“哪篇有用 / 哪篇误报”，两周后一次调，否则不知道是哪个变量起作用。

## 排障

| 现象 | 可能原因 | 处理 |
|---|---|---|
| QQ 没收到 | `QMSG_KEY` 错 / 没加机器人好友 | `curl -X POST "https://qmsg.zendee.cn/v3/send/$KEY" --data-urlencode "msg=test"` |
| Actions 失败 | 缺 secret / arXiv 超时 / LLM 余额 | 看 Actions 日志；失败会发邮件（Settings → Notifications） |
| LLM 400 | 兼容端点不支持 `response_format` | 把 `[llm] json_mode` 改成 `false` |
| 重复推送 | 同日手动重跑 / 上次的 state 没提交 | 现在同日 `status=sent` 会跳过；发送中断会续发未提交的 chunk。若仍重复，检查 `last_run.json` 是否被 `Persist state` 提交 |
| 只收到一半 | 第二条被限流或网络中断 | 看 `last_run.json` 的 `status=failed`；重新触发会只补未发送的 chunk |
| 定时不触发 | 不在默认分支 / 仓库 60 天无提交 | 手动 Run 一次；脚本每天写 `last_run.json`，正常情况下不会停 |
| 周一推送很少 | arXiv 周末不公告 | 正常，库存回填会补 |

## 成本

- 打分约 130 篇 × 1.2k 字符（约 4 万 tokens），摘要 5 篇 × 1.5k，合计约 5–6 万 tokens/天。
- DeepSeek 约 **¥0.1–0.4/天**；用本地 Ollama 可零成本，但 JSON 稳定性会下降。

## 合规

- QQ 只发摘要，不发 arXiv 链接（链接只写本地 `data/digests/*.md`）。
- 发送前会替换 URL / IPv4 / 长数字；错误通知不含 Qmsg key。
- Qmsg 是第三方服务，内容会经过它，不要推敏感信息。
- 遵守 arXiv API 条款：请求间隔 ≥ 3 秒、User-Agent 带联系方式。

## 目录

```
paper-bot/
├── main.py                 # 编排 + CLI + 投递日志
├── sources.py              # arXiv 抓取、会议识别、过滤
├── llm.py                  # 打分、摘要、数字校验
├── notify.py               # 渲染、拆分、Qmsg、出站消毒
├── safety.py               # 异常脱敏 + Qmsg 内容消毒
├── store.py                # JSONL 状态
├── config.toml
├── prompts/{score,summarize}.md
├── tests/                  # unittest：消毒 / 脱敏 / 幂等续发
└── data/                   # 运行后生成：papers.jsonl / last_run.json / digests/
```
