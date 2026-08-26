> **归档说明（非原文）**：本文为 **Claude Code 拟定的参赛计划原文**，由 DeepSeek 代理于 2026-08-25 归档至本仓库 `docs/`，用途是日后复查 Claude Code 的工作（计划 → 实际 对照见 `docs/PLAN_VS_ACTUAL.md`）。自「# JrAlpha 参赛计划」起为逐字原文，未改动措辞；仅将 IBKR paper 账号号段脱敏为 `[已脱敏]`，避免公开仓库泄露券商账号标识。计划中标注的复选框状态是计划撰写时的状态，不代表执行状态。

# JrAlpha 参赛计划 — Alpaca AI Trading Agents Hackathon

## Context

Alpaca 与 lablab.ai 合办的线上黑客松，$6,000 奖池，2026-08-28 08:00 至 09-04 08:00（温哥华时间，赛事全程 PDT，**不需要换算**）。目标是拿奖。

**为什么需要一个计划而不是直接开干**：三个约束互相咬合，任何一个踩空整件事就归零。

1. **报名截止 = 开赛瞬间**（官网 live 页原文 `Registration closes the moment the event starts.`）。8/28 08:00 之后无法补报。截至 2026-08-25 14:00 只剩 **2 天 18 小时**。
2. **JrAlpha 现有资产不能直接用**。现仓只接 IBKR paper（`[已脱敏]`），**零 Alpaca 集成、零期权**；而比赛硬性要求「全新专用 Alpaca paper 账户」+「所有策略必须含期权」。同时现仓是私有备份仓、含真实券商账号，比赛却要求**公开** GitHub 仓——直接复用等于泄露 + 触发 `HEAD_MISMATCH` 锁死实盘下单许可。必须物理隔离。
3. **免费档期权行情延迟 15 分钟**（indicative feed；实时 OPRA 需 Algo Trader Plus $99/月）。这排除了一切日内抢单型策略，必须从一开始就设计成延迟容忍。

**预期产出**：一个自主期权交易 agent，跑在全新 $100k Alpaca paper 账户上，配套公开 MIT 仓库、Streamlit 面板、5 分钟视频、PDF slides、一页写作，以及 5 条社媒帖。

---

## 复核过的赛事事实（2026-08-25 亲验）

| 项 | 事实 | 来源 |
|---|---|---|
| 报名截止 | 8/28 08:00 PDT = 15:00 UTC，**与开赛同时** | live 页倒计时 |
| 提交截止 | 9/4 08:00 PDT = 15:00 UTC | live 页 |
| 奖池 | 1st $2,500 / 2nd $1,500 / 3rd $1,000 + **2 队社媒奖各 $500**（+每人 1 个月 Algo Trader Plus）= $6,000 | 赛事页 |
| 账户 | `Competition account starting balance must be set to $100,000.` | 赛事页原文 |
| 账户 | `create a brand-new Alpaca paper trading account dedicated to this hackathon`；复用旧账户 `will not be eligible for judging` | 赛事页原文 |
| 授权 | `Submissions must be original and MIT-compliant.` | 赛事页原文 |
| 技术 | 必须用 Trading API + （MCP server **或** CLI，二选一即可）+ 所有策略含期权 | 赛事页 |
| 期权权限 | paper 账户 **Level 3 自动批准**，多腿/价差/铁鹰直接可用，无需申请 | Alpaca changelog |
| 期权行情 | 免费档 = indicative feed，**延迟 15 分钟** | Alpaca 数据页 |
| 提交物 | 标题/短描述≤255字符/长描述≥100词/标签/16:9封面图/**MP4 视频≤5分钟**/**PDF slides**/**公开 GitHub 仓**/**Demo 平台 + Application URL**/**Alpaca paper account ID**/≤5 条社媒链接 | 赛事页 + lablab 提交指南 |
| 一页写作 | 必须覆盖 **AI logic、risk gates、Alpaca infrastructure implementation** | 赛事页 |
| 评审 | P&L / 技术实现 / 创意原创 / 演示执行 / 社媒互动，**5 项，权重未公开** | 赛事页 |
| 奖金 | 付给**个人**不付团队；需 **W-8BEN**（非美）+ 证件 + 银行信息；非美默认 **30% 预扣**，赛后 90 天内付款；获奖后 90 天不交材料作废 | 赛事页 Prize Terms |
| 队伍 | 1–6 人，**单人参赛也必须建队**；每位成员各自注册 | lablab FAQ |

注：邮件里的「one per email」官网无对应原文，官网只要求「全新专用账户」。kickoff 当天 Discord Q&A 确认。

---

## 一、赛前时间表（全部温哥华时间）

### 今晚 8/25（二）— 只有一件事：把不可逆的截止项做掉
**你手动做（约 15 分钟，我做不了，见下方边界说明）：**
- [ ] lablab.ai 注册账号 → 进赛事页点 Enroll → **建一个只有你自己的队伍**（单人也必须建队，否则提交无效）
- [ ] Alpaca dashboard → 左上角账号号码 → **Open New Paper Account** → 新账户余额默认即 $100,000（正好符合要求，别改）
- [ ] 为**新账户单独**生成 API key/secret（旧账户的 key 不能用）
- [ ] 把新账户的 **account ID + key + secret** 给我

**我随后做（今晚内）：**
- [ ] 建独立工作目录 `~/jralpha-sentinel/`（**不在 `.openclaw` 树内**，避免 commit 触发 JrAlpha 的 `HEAD_MISMATCH` 锁死实盘许可）
- [ ] `gh repo create JrWater/jralpha-sentinel --public`（`gh` 已以 JrWater 登录），MIT LICENSE
- [ ] 装 `alpaca-py` + Alpaca CLI + Alpaca MCP server，用新 key 跑通连通性

### 8/26（三）— 打通全链路，先跑出第一笔期权单
- [ ] 用新账户下**一笔真实的期权 paper 单**（如 SPY 单腿 + 一个 credit spread），确认多腿 Level 3 确实开放、确认 15 分钟延迟报价下的成交行为
- [ ] 落地 `policy/manifest.json`——移植 ADR-0007「Strategy Policy Manifest 是参数唯一权威」：universe、DTE、目标 delta、order shape、仓位上限全部声明在此，代码从 manifest 读，**未声明的 order shape 直接拒绝提交**
- [ ] 骨架跑通：cron → 决策 → 闸门 → 执行 → 落账

### 8/27（四）— 移植闸门 + 面板
- [ ] 五维闸门（Process Health / Data Readiness / Delivery Health / Release Integrity / **Entry Authority**）× 三档（BLOCKING / ATTENTION / INFO）重写为 Alpaca 版，六字段必填、缺一个 import 当场炸——这是 `gate_registry.py` 最值钱的设计，原样保留
- [ ] **Entry Maintenance** 状态机：证据过期时禁止新建敞口，但保留对账与降险离场
- [ ] Streamlit 面板（这就是提交要求里的 Application URL）：实时闸门矩阵 + 持仓 + 权益曲线 + agent 决策日志
- [ ] 部署到 Streamlit Community Cloud，拿到公开 URL

### 8/28（五）— 开赛日
- 08:00 开幕（Twitch）
- **09:00 Discord Q&A，务必到场问清 3 件事：**
  1. 竞赛账户可以提前开、赛前跑吗？还是 P&L 只从 8/28 起算？（官网只说 "over the course of the competition"，没说起算点）
  2. 「one per email」的准确含义——同一登录下的新 paper 子账户算不算合规？
  3. 9/4 08:00 提交时账户是否需要平仓？未平仓持仓如何计入 P&L？
- 美东 11:00（= 温哥华 08:00）盘中，agent 正式上线开始交易
- 发第 1 条社媒帖

### 8/29–8/30（周末，非交易日）— 内容与加固
- [ ] 写一页写作初稿 + slides 骨架
- [ ] 补测试（闸门的 fail-closed 行为必须有测试覆盖）
- [ ] 社媒帖 2

### 8/31–9/2（一二三）— agent 自动跑，你只监控
- [ ] 每天看面板 + 闸门是否误拦（**误拦要改闸门，不要加例外绕过**）
- [ ] 每天 1 条社媒帖（架构图、闸门拦截实例、权益曲线）
- [ ] 9/2 开始录视频素材

### 9/3（四）— **收盘前全部平仓**（关键战术，理由见下）
- [ ] 15:45 ET 前平掉所有期权持仓，账户回到干净的全现金状态、P&L 全部已实现
- [ ] 晚上录 5 分钟视频、定稿 slides、一页写作、长短描述、封面图
- [ ] **9/3 晚就提交**，不要卡 9/4 早上

### 9/4（五）08:00 前 — 缓冲
- [ ] 只留最后检查。技术故障可申请赛后 6 小时手动补交，但需**事先**获组织者/mentor 批准，别指望它

---

## 二、账户注册：我能做什么、不能做什么

**我不能做**（硬边界，不是权限设置问题）：
- 创建 lablab.ai 账号
- 创建 Alpaca paper trading 账户（券商账户创建）
- 输入任何密码进行认证

**你做完这三步，剩下全部交给我。** 精确路径：

1. **lablab**：`lablab.ai` → Sign up（Google 登录最快）→ 进 [赛事页](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon) → `Enroll` → `Create or join a team` → `Create a team` → 队名随意（建议 `JrAlpha Sentinel`）
2. **Alpaca 新 paper 账户**：dashboard 左上角**点账户号码** → `Open New Paper Account` → 确认余额显示 $100,000（默认值就是，别动）
3. **新账户 API key**：切换到新账户 → 生成 API Key → **key 和 secret 只显示一次，立刻复制给我**

**我拿到后立刻做的**：写入独立的 `~/jralpha-sentinel/.env`（不碰 `.openclaw/.env`），并处理变量名不一致——你现有的是 `ALPACA_API_SECRET`，而官方 SDK/MCP 期望 **`ALPACA_SECRET_KEY`**；用 API 反查并核对 account ID、余额是否 $100,000、期权 Level 是否为 3。

---

## 三、赛中策略

### 设计约束推导
- 只有 **约 4.9 个交易日**（8/28 从美东 11:00 起算 + 8/31–9/3 四整日 + 9/4 被平仓策略排除）
- 期权报价延迟 15 分钟 → **不能做日内抢单，必须延迟容忍**
- P&L 只是 5 项评审之一 → **不需要 +50%，需要的是可靠的正收益 + 讲得清的逻辑**。5 天内 +2%~+5%（$2,000–$5,000）在这个赛场大概率已属顶部，因为多数参赛者会是持平或亏损
- 因此：**不赌方差，赌胜率 + 架构分**

### 杠铃结构

**核心仓（约 80% 风险预算）：定义风险的期权卖方**
- 标的：SPY / QQQ（现已支持每个交易日到期）+ 2–3 只高流动性大盘股
- 结构：**1–3 DTE 的价外 credit spread**（put spread / call spread），目标 delta 0.10–0.15
- 逻辑：5 天窗口内最可靠的 P&L 来源是 theta 衰减；价差结构使单笔最大亏损天然封顶，**不可能爆仓**
- 为什么延迟容忍：决策依据是标的的日线/小时线（免费档 IEX 实时可用），期权链只用于选行权价——delta 0.10–0.15 的选择对 15 分钟延迟完全不敏感；下单一律限价单，不吃市价

**卫星仓（约 15–20% 风险预算）：方向性买方**
- 把 JrAlpha 的 V240 chandelier 动量语义移植过来，选出最强/最弱标的，买 2–4 周到期的近平值 call/put
- 作用：提供凸性，博一个 P&L 前三

**风险闸门（硬编码，LLM 无权覆盖）**
- 单笔最大亏损 ≤ 账户 0.5%（$500）
- 同时持仓 ≤ 12 笔
- 净值跌破 $97,000 → 自动进入 **Entry Maintenance**：禁止新建敞口，只允许降险离场
- 所有 order shape 必须在 `manifest.json` 声明，未声明者拒绝提交

### 架构上的杀手锏：**LLM 只能提议，不能下单**

这是与其他参赛者拉开差距的核心。绝大多数参赛作品会把 Alpaca MCP 的下单工具直接暴露给 LLM，让模型自己调用。JrAlpha 的做法相反：

```
Claude（决策层）→ 产出「提议」（结构化，非订单）
        ↓
五维闸门（确定性代码）→ 逐条判定 BLOCKING / ATTENTION / INFO
        ↓
Executor → 只接受通过闸门、且 order shape 在 manifest 中已声明的提议
        ↓
Alpaca Trading API
```

一页写作的核心论点就一句：**"The LLM cannot place an order. It can only propose one. The gate decides."**

视频里最有说服力的一幕：**当场掐掉数据源** → 面板上 Data Readiness 转红 → agent 自动进入 Entry Maintenance → 拒绝开新仓，但仍正常管理既有保护性离场。没有第二个参赛队会演这个。

### 技术栈（同时满足 MCP 与 CLI 要求，两个都用，最大化「技术实现」得分）
- **Alpaca MCP Server**：作为 agent 的工具面（赛事页称之为 "the core of the hackathon theme"），配合 Claude Agent SDK 驱动自主循环
- **Alpaca CLI**：ops 与对账路径，纯 JSON 输出，适配 cron —— 与 JrAlpha 现有 launchd 调度范式一致
- **alpaca-py**：确定性执行与结算核对

---

## 四、你没想到的（按重要度排序）

1. **报名截止就是开赛瞬间**——最大的单点失败风险，今晚必须做掉。
2. **社媒奖是第二个奖池，竞争者少一个数量级**。2 队 × $500，绝大多数 builder 根本不发帖。你的闸门架构本身就是好内容。每天 20 分钟，期望收益接近主奖。**这一项把总期望值几乎翻倍。**
3. **Application URL 是硬性提交项**。JrAlpha 是 headless cron 系统，没有可点的 URL——不补 Streamlit 面板，这一项直接失分，而它同时又是视频里最好的道具。
4. **绝对不要在 `.openclaw` 树内建比赛代码**。那里的 commit 会让 JrAlpha 的下单许可 `HEAD_MISMATCH`，交易日里等于自断实盘。物理隔离到 `~/jralpha-sentinel/`。
5. **9/3 收盘前全部平仓**。两个理由：(a) paper 环境的 NTA（到期/行权等非交易活动）**T+1 才同步**到 Activities，9/4 到期的仓位要 9/5 才结算完，评委在截止时看到的账户状态会是含糊的；(b) 9/4 08:00 截止时正在盘中，未平仓持仓让 P&L 口径不清。干净的全现金 + 全已实现 P&L，评委一眼看懂。
6. **W-8BEN 提前填好**。加拿大居民，默认 30% 美国预扣税，有税收协定可降。获奖后只有 90 天交齐材料，别到时候手忙脚乱。
7. **视频和 slides 从 9/2 开始做**，不是 9/3 晚上。5 分钟视频要剪，赶出来的和打磨过的在「演示执行」这项上差距极大。
8. **别买 $99 的 Algo Trader Plus**。策略已按延迟容忍设计，实时 OPRA 对 1–3 DTE、delta 0.10–0.15 的价差选择几乎无增量价值；而且它正好是社媒奖的奖品之一。
9. **闸门误拦要改闸门，不要加例外绕过**——这是 JrAlpha 一贯的纪律，比赛期间同样适用，且「我们修的是闸门不是绕过它」本身就是好的社媒内容。

---

## 五、要改动/新建的文件

全部在**新目录** `~/jralpha-sentinel/`（新建，与 `.openclaw` 完全隔离）：

```
policy/manifest.json          # Strategy Policy Manifest —— 参数唯一权威
                              # 移植自 workspace/docs/adr/0007-make-strategy-manifests-the-parameter-authority.md
gates/registry.py             # 五维闸门元数据，六字段必填
                              # 设计移植自 workspace/scripts/gate_registry.py
gates/entry_authority.py      # 移植自 workspace/scripts/entry_authority.py
gates/safety_gate.py          # 移植自 workspace/scripts/entry_safety_gate.py
agent/brain.py                # Claude 决策层 —— 只产出提议
agent/executor.py             # 只接受通过闸门 + manifest 已声明的 order shape
broker/alpaca_client.py       # alpaca-py + CLI 双路径
dashboard/app.py              # Streamlit 面板（= Application URL）
docs/ONE_PAGER.md             # AI logic / risk gates / Alpaca infrastructure
docs/adr/                     # 3–4 篇精简 ADR
tests/                        # 闸门 fail-closed 行为必须有覆盖
LICENSE                       # MIT（赛事要求 MIT-compliant）
.env                          # 新账户 key，本地，不入库
```

**明确不做的事**：不修改 `.openclaw/workspace/` 下任何文件；不复制 `policies/broker_accounts.json`（含真实 IBKR 账号 `[已脱敏]`）；不复制 V240 策略参数。公开的是**闸门设计**，不是你的策略参数和数据源——那才是真正的护城河。

---

## 六、验收方式

**注册阶段**（拿到 key 后立即验，5 分钟内出结论）：
```bash
# 账户身份、余额、期权等级三件套
~/jralpha-sentinel/.venv/bin/python -c "
from alpaca.trading.client import TradingClient
c = TradingClient(KEY, SECRET, paper=True)
a = c.get_account()
print(a.account_number, a.cash, a.options_approved_level, a.options_trading_level)
"
```
判定标准：account_number 是**新号**（非现有账户）、cash == 100000、options level == 3。任何一项不符立刻回到 dashboard 修，别往下走。

**链路阶段**（8/26 内必须出结果）：真实提交一笔 SPY credit spread paper 单，确认多腿被接受、能查到 fill、能平仓。**这一步不通过，整个计划要重排。**

**闸门阶段**（8/27）：
```bash
~/jralpha-sentinel/.venv/bin/python -m pytest tests/ -q
```
必须覆盖：闸门缺字段时 import 报 TypeError；BLOCKING 闸门红时 executor 拒绝下单；净值跌破阈值自动进入 Entry Maintenance。

**端到端**（8/28 开赛前）：面板公开 URL 可访问、显示实时闸门矩阵；手动掐掉数据源，确认 agent 转入 Entry Maintenance 并拒绝新开仓（这一幕同时也是视频素材）。

**提交前**（9/3 晚）：对照赛事页提交物清单逐项打勾，尤其确认 **Alpaca paper account ID 已填**——漏这一项评委无法评 P&L。
