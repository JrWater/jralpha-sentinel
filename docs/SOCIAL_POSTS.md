# 社媒帖终稿（8/31 更新 — 直接复制粘贴）

> **规则要求**（lablab.ai 赛事页原文）：在 **X 和 LinkedIn** 公开发帖，tag **lablab.ai 和 Alpaca**，
> 最多随提交附 **5 条帖子链接**（社交奖 2 队 × $500）。
> **代发说明**：无法代发——账号登录是你的身份。以下每条贴子 2 分钟就能发完。

## 通用素材

- 仓库：`github.com/JrWater/jralpha-sentinel`（MIT）
- 实时面板：`jralpha-sentinel.streamlit.app`（无凭据，评委可直接看闸门矩阵）
- X tag：`@lablabai @AlpacaHQ`
- LinkedIn：正文里 @ 提及 "lablab.ai" 与 "Alpaca" 公司主页
- 发完收集每条帖子的 **链接**（X：帖子 permalink；LinkedIn：帖子 URL），提交时附上（≤5 条）

---

## 帖 1（今晚 8/31 或明早 9/1 发）— 中途决策，诚实复盘

**X 版（<280 字符）：**
> Day 4 of @lablabai × @AlpacaHQ AI Trading Agents Hackathon. We cross-validated our trend engine on 4 independent backtesters — and disabled it mid-competition. Evidence over ego: its edge was noise. The remaining firepower goes to the events the data actually supports. Repo: github.com/JrWater/jralpha-sentinel

**LinkedIn 版（约 120 词）：**
> Day 4 of the lablab.ai × Alpaca AI Trading Agents Hackathon, and we made the hardest decision mid-flight: we turned our own trend engine off.
>
> The evidence forced it. Four independent backtesters over the same 250 sessions: our signal ported to spot lost −6.7% against SPY +23.9% (profit factor 0.92); our credit spreads survived but only at +2.1% after costs; the live account agreed after 3 days.
>
> So Sentinel's remaining window belongs to the two trades the data actually supports: the NFP gap continuation (9/10 in the historical study) and the LULU earnings straddle. The model still proposes, the gates still decide — they just get a better menu now.
>
> Live, credential-free dashboard: jralpha-sentinel.streamlit.app · Repo (MIT): github.com/JrWater/jralpha-sentinel
>
> #AI #Trading #Hackathon #Options

## 帖 2（9/2 发）— 架构杀招

**X 版：**
> Most agents this week hand an LLM a broker tool. Sentinel doesn't: the model proposes, 16 deterministic gates decide, and no market-order shape even exists in the manifest. A hallucinated ticker can't become a fill because the fill never exists until the gates sign it. @lablabai @AlpacaHQ

**LinkedIn 版（约 90 词）：**
> The failure mode of an LLM trader isn't being wrong — it's being confidently wrong at 3× size in a symbol it invented. Sentinel makes that structurally impossible: the model emits a structured proposal with no credentials and no order tool; 16 gates across 5 dimensions decide; the executor only accepts order shapes declared in the policy manifest, and no shape declares "market".
>
> Every gate is on screen, live, at jralpha-sentinel.streamlit.app — judges can read the refusals, not just trust the write-up.
>
> lablab.ai · Alpaca · #AI #TradingAgents #Options

## 帖 3（9/3 发）— 催化剂日（LULU 财报今晚 + NFP 明早）

**X 版：**
> Tonight LULU reports (16:30 ET), tomorrow 08:30 ET the jobs report. Sentinel is positioned: a straddle sized from a 10-sample move study (median 11.8%), plus the NFP gap play measured 9/10 across 33 first-Fridays. Defined risk, limit-only, gates live at jralpha-sentinel.streamlit.app. @lablabai @AlpacaHQ

**LinkedIn 版（约 90 词）：**
> Two scheduled catalysts, one agent, zero improvisation. Tonight: LULU Q2 earnings — Sentinel holds a straddle sized from a move study (median earnings move 11.8% vs ~6% breakeven). Tomorrow 08:30 ET: the August Employment Situation — the gap-continuation trade measured 9 wins in 10 across 33 first-Fridays.
>
> Every position defined-risk, every order limit-only, every decision gated and logged. Follow the live gate matrix: jralpha-sentinel.streamlit.app
>
> lablab.ai · Alpaca · #Options #Earnings #NFP

## 帖 4（9/4 早，收盘/提交后发）— 结果诚实版

**X 版：**
> Five days, one window, every number real. Final equity vs $100k start, win rate by vector, and what the gates refused — reported honestly whichever way it landed. Thank you @lablabai and @AlpacaHQ for the best trading education there is. github.com/JrWater/jralpha-sentinel

**LinkedIn 版（约 90 词）：**
> The window closed. Final equity, per-vector win rates, the event-study hits and misses — all from account PA3K3A9ZBCBI, nothing fabricated. The one-page write-up (AI logic / risk gates / Alpaca infrastructure), slides, and demo video are in the repo.
>
> Win or lose, the artifact stands: an options agent whose language model cannot place an order, cross-validated and honestly corrected mid-competition. That's the system we'd want running real money — slowly, with better data.
>
> lablab.ai · Alpaca · #Hackathon #AI #Options

## 提交时附的链接清单（≤5 条）

- [ ] 帖 1 URL（X 或 LinkedIn 任一）
- [ ] 帖 2 URL
- [ ] 帖 3 URL
- [ ] 帖 4 URL
- [ ] （可选第 5 条：面板/仓库帖）

## 注意事项

1. **别编数字**：帖 4 只写真实结果；9/3 帖里"9/10、11.8%"是赛前研究数据，表述时保留"measured/study"字样。
2. 每天 1 条即可；质量 > 数量，评委看的是**过程、推理、挫折**。
3. X 和 LinkedIn 内容别完全一样（上面已区分）。
4. 帖子里放仓库/面板链接，方便评委跳转；X 的链接占字符，用 LinkedIn 放完整链接。
