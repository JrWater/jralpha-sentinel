# 计划 vs 实际 — Claude Code 参赛计划的执行对照（复查用）

> 本文是复查辅助文档：把 `docs/CLAUDE_CODE_PLAN.md`（Claude Code 拟定的计划原文，2026-08-25 归档）与仓库实际实现逐条对照，记录完成度、偏差及原因。**不是原文的修改**。最后更新：2026-08-25。

## 0. 工作时间线（谁、何时、做了什么）

| 时间 (PDT) | 提交 / 事件 | 作者 |
|---|---|---|
| 8/25 14:26 | `9963e6c` Sentinel：闸门架构、policy manifest、账户验证器（计划 §五 的骨架） | Claude Code |
| 8/25 14:58 | `9907586` 绑定竞赛账户；机械守卫至开赛（competition_window 闸门） | Claude Code |
| 8/25 15:0x–16:2x | `00c0058` **Quadrant v1**：四引擎策略、LLM 提议层、限价执行器、一页写作、社媒帖草稿 | DeepSeek 代理（本轮会话） |
| 8/25 16:4x | `7e5aa8d` **v2.1**：结构级退出、日闸落地、LULU 到期日修复、0-DTE 链、防重复 | DeepSeek 代理 |
| 8/25 17:0x | `60b261b` **v2.2**：独立对抗评审（18 项）修复——趋势引擎 NameError、报价新鲜度、信用价差符号、NFP 缺口例外等 | DeepSeek 代理 |
| 8/25 17:1x | `968e767` gitignore 运行时决策日志 | DeepSeek 代理 |
| 8/25 18:48 | `3eec988` 删除 legacy `smoke_spread.py`（评审发现跨到期日对角线缺陷、--submit 可达竞赛账户、--close-all 市价平仓） | Claude Code |
| 8/25 19:01 | `e8f325c` **修复三个静默缺陷**：daily_bars 取旧不取新（正是多次 dry-run "insufficient history" 的根因）；`rsi()` 取前 14 根而非后 14 根（v1 引入的缺陷）；全涨序列 RSI 返回 50；窗口 60→120 日 | Claude Code |
| 8/25 19:0x | 未提交：`dashboard/app.py`（Streamlit 闸门矩阵面板，读取 `docs/snapshot.json`，无凭据）+ `agent/snapshot.py` | Claude Code |

当前基线：**82/82 测试通过**；账户 `PA3K3A9ZBCBI` 验证 READY（$100,000.00、Level 3、未交易）；manifest `SENTINEL-OPTIONS-V2@2.2.0`。

## 1. 赛事事实核对（计划 vs 本仓库复核）

| 计划说法 | 复核结论 |
|---|---|
| 窗口 8/28 08:00–9/4 08:00 PDT = 15:00 UTC | ✅ 一致（lablab 官方页面） |
| 奖池 $6,000（$2,500/$1,500/$1,000 + 2×社媒奖 $500） | ✅ 一致。⚠️「+每人 1 个月 Algo Trader Plus」在官网未核到，未确认 |
| 评审「5 项含社媒互动」 | ⚠️ 官网主评审列为 **4 项**（P&L / 技术实现 / 创意原创 / 演示执行）；社媒是**独立的 Extra challenge**（提交时附 ≤5 条社媒链接）。语义近似，表述不同 |
| 「one per email」 | ⚠️ 官网无此原文；官网只要求全新专用账户。开赛 Discord Q&A 确认（计划自己已注明） |
| 提交物清单 | ✅ 一致（含 Application URL、account ID、≤5 条社媒链接、16:9 封面、MP4≤5min、PDF slides） |
| 期权 Level 3 自动批准 | ✅ 实测通过（账户 options level 3） |
| 免费档期权延迟 15 分钟 | ✅ 实测：indicative feed，快照最新报价在收盘后即停更；本策略据此设计 |

## 2. 赛前时间表 — 完成度

| 计划项 | 状态 | 证据/说明 |
|---|---|---|
| 8/25 用户手动：lablab 报名 + 建队、建 Alpaca 新 paper 账户、生成新 key | ✅ | 账户 `PA3K3A9ZBCBI`（用户建，名 "AI Trading"）；lablab 已报名（用户确认） |
| 8/25 建独立目录、`gh repo create JrWater/jralpha-sentinel --public`、MIT | ✅ | `git remote -v` 指向 `github.com:JrWater/jralpha-sentinel.git`；LICENSE MIT |
| 8/25 装 alpaca-py + CLI + MCP，连通性 | ✅ | alpaca-py 0.44.0 在 `.venv`；`.mcp.json` 配置 MCP server；README 记录 CLI（`brew install alpacahq/tap/cli`） |
| 8/26 用**新账户**下真实期权单（链路验证） | ⚠️ **未执行，且不应执行** | 计划自相矛盾：8/26 下真实单会破坏"竞赛账户起始必须恰好 $100,000"。实现以 `competition_window` 闸门机械裁决——开发/验证一律走 legacy 账户（`verify_account.py --compare-legacy`）。`smoke_spread.py`（原链路验证工具）已被删除（见时间线）。**若需链路验证，应在 legacy 账户上做** |
| 8/26 manifest 落地 | ✅ | `policy/manifest.json` v2.2.0，order shape 白名单 + 拒绝未声明形状（`check_order_shape_declared`） |
| 8/26 骨架跑通 cron→决策→闸门→执行→落账 | ✅ | `scripts/run_cycle.py`（--dry-run 实测通过：闸门矩阵正确、拒绝逻辑正确）；README 有 crontab |
| 8/27 五维闸门 + Entry Maintenance | ✅ | `gates/`：16 闸门、5 维度、3 档位、六字段必填（registry `validate()` 保证）；Entry Maintenance = `equity_floor` 闸门（<92% 禁止新敞口、离场照常） |
| 8/27 Streamlit 面板 + 部署 URL | ✅ **已部署** | 公开 URL：https://jralpha-sentinel.streamlit.app 。GitHub OAuth 登录 + 部署表单本人操作/确认；构建通过，渲染结果与本地实测一致（Entry permit BLOCKED / market_session，账户 PA3K3A9ZBCBI $100,000.00）。托管端无 Alpaca 凭据 |
| 8/28 开赛日三问（Discord Q&A） | ⏳ **待办（用户）** | 9/4 平仓口径、起算点、one-per-email 三问需用户在开赛日 Q&A 确认 |
| 8/29–9/2 一页写作/测试/社媒 | ✅ 大部分 | `docs/ONE_PAGE_WRITEUP.md`（v2.2 版）、`docs/SOCIAL_POSTS.md`（5 帖草稿）、测试 82 项（含 fail-closed 覆盖） |
| 9/2–9/3 视频、slides、长短描述、封面 | ✅ **提前完成（8/25）** | 封面图 `media/cover.png`（1920×1080）、9 页 PDF `media/slides.pdf`、5 分钟旁白视频 `media/sentinel_demo.mp4`（4:36，182wpm TTS + headless Chrome 帧 + ffmpeg 合成，全部脚本化可复现见 `media/build/`）。Results 页/段刻意占位——真实 P&L 只能赛后填，提前编数字等于造假。长短描述仍待办 |
| 9/3 收盘前全平仓 | 🔶 **已修改（见 §4 偏差表）** | 平仓改到 9/4 10:45 ET + NFP 早盘例外；原因见偏差表 |
| 9/4 提交缓冲 | ⏳ 待办 | 提交物清单逐项打勾（尤其 account ID） |

## 3. 账户边界（计划 §二）

- 用户手动步骤全部完成；`verify_account.py --compare-legacy` 实测：ACTIVE / $100,000.00 / cash 未动 / Level 3 / 与 legacy 开发账户（号码不公开）不同 → **READY**。
- `.env` 独立于 `.openclaw/.env` ✅；变量名 `ALPACA_SECRET_KEY` 兼容处理（verify_account 同时读 `ALPACA_API_SECRET`）✅。

## 4. 赛中策略 — 计划 vs 实际（最大的偏差区）

| 维度 | 计划（Claude Code） | 实际（v2.2 Quadrant） | 偏差原因 |
|---|---|---|---|
| 总体结构 | 杠铃：核心 80% 卖方 credit spread + 卫星 20% 长单腿 | 四引擎：Trend 45% / Catalyst 25% / Event 15% / Vol 15% | 计划版在 5 天窗口 P&L 期望过低（theta 低个位数）；事件日历研究（NVDA/CRM/CRWD 8/26 财报、LULU 9/3、NFP 9/4）表明**事件驱动**才是窗口内真正的 P&L 来源 |
| 核心结构 | 1–3 DTE credit spread，delta 0.10–0.15 | 0–2 DTE debit vertical（delta 0.45）+ 信用价差（IV 偏高时，delta 0.20） | 计划版 delta 0.10–0.15 太保守，赢不了 P&L 榜；实测回测支撑回落过滤的趋势信号（d2 相对 SPY +1.17%） |
| 卫星 | 2–4 周 long single-leg 动量 | Catalyst（财报跨式/PEAD）+ Event（NFP） | 2–4 周卫星在 5 天窗口衰减太慢；换成窗口内**已确认事件** |
| 单笔风险 | ≤0.5%（$500） | 引擎级 $800–$2,000，硬顶 $2,000（2%） | $500/笔 × 12 笔 = $6,000 在险，P&L 上限 ~$3–5k 但事件腿只有 $500 火力；v2.2 提高催化剂火力至 $2,000/$1,500 |
| 净值线 | $97,000（-3%） | $92,000（-8%）+ 日熔断 -$3k + 次日减半 | 计划版 3% 就停，五天里一次错误事件就缴械；v2.2 用日熔断替代净值线作为第一道防线 |
| 平仓 | **9/3 收盘前全平** | **9/4 10:45 ET 平仓 + NFP 早盘 0-DTE 例外** | 计划版放弃 9/4 上午——但 9/4 08:30 非农是**窗口内最大宏观事件**；v2.2 保留 NFP 双枪（9/3 勒式 + 9/4 缺口单腿），10:45 前全部限价平掉，既吃到事件又不留未结算仓位 |
| 平仓机制 | —（未定义） | 结构级退出（`strategy/exits.py`）+ 末日强平即使门禁红灯也执行 | 评审发现逐腿平仓可制造裸空腿；已改为结构整体平仓 |
| 新增（计划未提） | — | 日闸（新敞口上限 $6k/日、kill switch、减半）、fire-once 防重复、SPY 20 日新高突破覆盖、部分成交修复 | 评审（v2.2）与自查（v2.1）产出 |
| 保留（与计划一致） | ✅ LLM 只能提议不能下单 | ✅ 16 闸门 + 提议层 + 限价执行器 | 计划的核心杀手锏原样实现，且是提交写作的主论点 |

**为什么改策略**（复查者须知）：计划的杠铃是**胜率优先、架构分优先**的设计——诚实、可讲、不爆仓，但 P&L 期望 ≈ +1~3%，在"P&L 是评奖第一项"的榜单里几乎没有领奖概率。v2 系列转向"四向量 + 事件驱动 + 定义风险"，是在**保持全部风险纪律**（无裸空、无市价单、全定义风险、门禁可拒）的前提下把期望 P&L 拉高：赛程内 6 个已确认事件（8/26 三巨头财报 PEAD、8/28 PCE、9/1 ISM、9/2 ADP、9/3 ISM+LULU、9/4 NFP）全部有引擎覆盖。**这不是推翻计划的纪律，是替换计划的收益结构。**

## 5. 技术实现 — 计划 vs 实际

| 计划 | 实际 |
|---|---|
| `agent/brain.py`（Claude 决策层） | → `agent/proposer.py`（LLM 提议层，无凭据、无下单工具，失败降级引擎排序） |
| `agent/executor.py` | ✅ 同名实现（限价、mleg 形状校验、credit 负限价、close intents） |
| `broker/alpaca_client.py`（双路径） | → `strategy/data.py`（AlpacaData：Trading/Stock/Option 三客户端）+ README CLI 路径 |
| `dashboard/app.py`（Streamlit） | ✅ 已建未提交未部署（见 §2） |
| `docs/ONE_PAGER.md` | → `docs/ONE_PAGE_WRITEUP.md` |
| `docs/adr/`（3–4 篇 ADR） | 🔶 目录为空，未写（可补） |
| MCP + CLI 双用 | ✅ `.mcp.json` + README CLI 命令；MCP 用于研究/只读，CLI 用于 ops/status |
| `tests/` fail-closed 覆盖 | ✅ 82 项（test_gates 36+ / test_strategy 46+） |

## 6. 已知未决项（提交前必须处理，8/26 晚更新）

1. ~~Streamlit 面板未部署~~ ✅ 已部署且已验证（本节 8/25 首次写的时候还没部署，早已过时）：https://jralpha-sentinel.streamlit.app 。8/26 还发现并修了它的免费档"睡死"问题（约 4 分钟冷启动）——crontab 里已加每 3 小时保活 ping，见 §8。
2. **8/28 Discord Q&A 三问**（起算点 / one-per-email / 9/4 平仓口径）——用户到场，无法代劳。
3. ~~长短描述~~ ✅ 已起草，见 `docs/SUBMISSION_COPY.md`——标题/短描述/长描述三项字数都程序验证过，在赛事页规定的上限/下限内。视频 v2（真实录屏证据 + 真实词级字幕，3:53.6）已经用户确认可接受。PDF slides + 封面图仍是 8/25 那版。
4. **W-8BEN + 证件 + 银行信息**：提前备好（加拿大居民默认 30% 预扣）——用户专属动作。
5. **社媒帖发布**：`docs/SOCIAL_POSTS.md` 有 5 帖草稿，计划排期从开赛日 8/28 才开始发，现在还没到时候。
6. **9/4 平仓口径确认**：10:45 ET 前强平逻辑已实现；若 Q&A 确认"截止时未平仓按市值计入"则现行逻辑无风险；若要求全现金，逻辑不变。
7. **链路验证（原计划里说"可选"，实际是整套"绕开延迟链"架构唯一没跑过真实数据的一环）**：`scripts/validate_delay_chain.py` 已写好、硬编码只连 legacy 账户（`PA31GLG5O9HU`，与竞赛账户隔离）、dry-run 过一次（收盘边缘数据，数字不算数）。真正的 `--live` 验证要等下一次开盘——8/27 06:35 PT（已按 §8 那次时区 bug 修正过的真实首次触发时刻）。

## 7. 复查指引

- 想看"Claude Code 打算做什么" → `docs/CLAUDE_CODE_PLAN.md`（原文）。
- 想看"实际做成什么样、为什么偏离" → 本文 + git log（9 个提交，见 §0 时间线）。
- 想看策略细节 → `docs/STRATEGY.md`（v2.2，含回测证据）。
- 提交材料 → `docs/ONE_PAGE_WRITEUP.md`、`docs/SOCIAL_POSTS.md`。
- 已知缺陷修复轨迹（评审驱动）→ commit `60b261b`（v2.2 修复 7 项）、`e8f325c`（Claude Code 修 3 个静默缺陷——其中 RSI 两个缺陷源于 v1 `strategy/indicators.py`）。

## 8. 8/26 追加复查（视频重做 + 两项运维缺口）

用户否定了 8/25 的第一版视频（ElevenLabs 配音 + 静态 slide 幻灯片，"更像 PPT"）。用真实 Chrome 直接打开 lablab.ai 逐页核实规则（`WebFetch` 对 lablab.ai 全域返回 403，像反爬，改走 `claude-in-chrome`），原文抓到：

> Video Presentation: A maximum 5-minute video in MP4 format. **Begin with an introduction, discuss your PDF presentation, then showcase your project's functionalities.**
> Pro Tips: **Showcase User Interaction: A screen recording demonstrating user interaction is impactful.**
> （均来自 `lablab.ai/delivering-your-hackathon-solution`，8/26 现查现摘）

结论：5 分钟是**上限**不是目标；规则没有"录屏 vs 制作视频"的硬性二选一，但 lablab 自己的取向很清楚——要看真东西在跑。修正后的版本：`sentinel-video/index.html`（HyperFrames），10 个场景全部真 GSAP 动效（不再是静态图片配旁白），第 7 幕（dashboard）用的是**真实**账户截图（`sentinel-video/assets/dash_f*.png`，来自已部署的公开面板），第 8 幕是真实源码截图，第 9 幕（results）保持诚实占位（`— pending —`，不编数字）。旁白沿用 8/25 的 ElevenLabs 十条音轨（内容和时长没问题，问题只在画面）。`npx hyperframes check` 全过（0 error，38/38 对比度），已提交渲染。产出：`sentinel-video/out/sentinel_demo.mp4`。

同一轮核对里，另外查到两个和视频无关、但更要紧的运维缺口——两个都已解决，过程记在这里因为都不是一次搞对的：

| 缺口 | 发现 | 状态 |
|---|---|---|
| **agent 从未自动跑过** | `crontab -l` 为空——README §"Schedule the live cycle with cron" 那段调度只写在文档里，从未真正装到系统上。 | ✅ 已装。第一次尝试 `Operation not permitted`（`crontab` 写自己的临时文件被 TCC 拦，追到根是 `/Applications/Claude.app` 没有"完全磁盘访问权限"——这是系统级授权对话框，我这边跨不过去）；用户在系统设置里手动给 Claude 加了权限、Cmd+Q 重启后，`crontab` 装上生效。 |
| **面板会真的睡死** | 面板今天从"Zzzz, wake it up?"到能打开，实测约 4 分钟。 | ✅ crontab 里加了一条每 3 小时 `curl` 保活。 |
| **crontab 装上了，但时区算错了** | 上面那条装完之后，实测第一次真实触发（`logs/cycle.log` mtime 15:05）暴露：crontab 里 `TZ=America/New_York` 这行**只是设置任务自己的环境变量，不会改调度器本身用的时区**——`man 5 crontab` 通篇没提 `TZ=` 对调度有任何特殊含义，macOS 的 cron 就是按系统本地时区（这台机器是 America/Vancouver = PDT）字面读那几个数字。所以"15:05"那条其实是 15:05 **PDT** 触发，也就是 18:05 ET——收盘两小时后。整张表全部按 ET 字面数字写、实际按 PDT 触发，等于早了三小时的窗口全部落空、原本该落在盘中的触发点全部滑到了收盘后。`run_cycle.py` 本身没事（`now_et` 是拿真实 UTC 经 `zoneinfo` 转的，从不读 `$TZ`），所以没有任何一次触发会因为这个 bug 而误判"现在是盘中"——`market_session` 闸门该拦的还是拦了；真正的损失是本该发生在盘中的触发点根本没落在盘中，等于这周大部分时间 agent 压根没有机会评估任何候选。已改成把 ET 数字手动转成 PDT 字面量重装（README 和 crontab 文件里都留了换算表和这段踩坑记录，换机器/换时区必须重算，不能直接照抄）。 |

装完之后随便挑一条手动跑一次 `run_cycle.py --dry-run` 确认路径没写错，比空等下一个整点踏实。开赛前（8/28）竞赛账户会被 `competition_window` 闸门挡住，装早了不会误下单。
