# Build-in-public posts (up to 5 links accepted with the submission)

Post on **X and LinkedIn**, tag **@lablab.ai / lablab.ai** and **@Alpaca (Alpaca Markets)**, link the repo `github.com/JrWater/jralpha-sentinel`. Drafts below are written to be posted over Aug 28 – Sep 4.

---

**Post 1 — Kickoff (Aug 28).**
Sentinel is live: an autonomous options agent for the @alpaca @lablab.ai AI Trading Agents Hackathon. The design claim: the LLM can *propose* a trade but can never place one — 16 deterministic gates decide, and nothing but a limit order ever reaches the API. $100k paper account, level-3 options, 5 days, 4 engines, 1 hard rule: defined risk only. Thread 🧵

**Post 2 — Architecture peek (Aug 29/30).**
How do you keep an LLM honest in an options ledger? You don't ask it to be good — you make it structurally incapable of being reckless. Sentinel: Claude proposes → 16 gates (5 dimensions, BLOCKING/ATTENTION) → executor. The model has no broker credentials and no order tool. A hallucinated ticker can't become a fill because the fill doesn't exist until the gates sign it. Code is on GitHub (MIT).

**Post 3 — The catalyst thesis (Sep 1).**
You can't beat a five-day paper window with theta alone — 0.13-delta spreads realize low single digits. You win it with scheduled surprises: LULU earnings Sep 3, the August Employment Situation Sep 4, and post-earnings drift off NVDA/CRM/CRWD from Aug 26. Four vectors, four risk budgets, $13k at-risk cap, no naked shorts, no market orders. The model argues the trade; the gates decide whether it exists.

**Post 4 — Mid-competition honesty (Sep 2/3).**
Day-by-day: the daily kill switch exists because it did its job. A −$3k day halts new entries and halves next-day sizing — every trade survives a wrong call because max loss is known before the first fill. This is what "risk gates" means in practice: they aren't in the write-up, they're in the order path. (Real numbers, real account, all paper.)

**Post 5 — Results + submission (Sep 4).**
Five days, one window, all paper. Final equity vs $100k start, realized-P&L split by engine (Trend/Catalyst/Event/Vol), and the one-page write-up: AI logic, 16 risk gates, Alpaca infrastructure (Trading API + MCP server + CLI). Thank you @alpaca and @lablab.ai — building for a competition like this is the best trading education available. Repo: github.com/JrWater/jralpha-sentinel (MIT).
