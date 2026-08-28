# Alpaca AI Trading Agents Hackathon — official-rules check

Checked: 2026-08-27. Scope is deliberately limited to official `lablab.ai`
and `alpaca.markets` pages. Repository notes, email screenshots, social posts,
and third-party summaries are not used to prove a rule here.

## Result summary

| Item | Status | Officially supportable conclusion |
|---|---|---|
| Start | **Confirmed** | 2026-08-28 15:00 UTC |
| Submission deadline | **Confirmed** | 2026-09-04 15:00 UTC |
| Submission package | **Confirmed** | Basic project information; cover image, video and slides; public GitHub repository, demo platform and application URL |
| New dedicated paper account | **Not confirmed from the official pages retrieved** | No current official extract found that states the account must be brand-new or dedicated |
| Exact USD 100,000 competition balance | **Partially supported, not confirmed as an event rule** | Alpaca says paper mode uses simulated USD 100,000; the retrieved event page does not state the eligibility rule |
| LLM required | **Not confirmed** | Event requires an AI trading agent/app, but the retrieved official text does not specifically require an LLM |
| Trading API | **Confirmed as the event stack** | Event page says projects use Alpaca's Trading API |
| MCP / CLI | **Confirmed as named event technologies; mandatory cardinality unclear** | Event page names MCP server and CLI, but the retrieved text does not settle “both” versus “either one” |
| Options required | **Track confirmed; universal strategy rule not confirmed** | Official live page names the main track “Options Alpha Agents,” but no retrieved official extract says every strategy must use options |

## 1. Start and submission deadline

Source: [official live event page](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon/live)

Verifiable extracts:

> Upcoming · Registration open starts Aug 28, 2026, 15:00 UTC

> You have until Sep 4, 2026, 15:00 UTC to submit.

> Fri, Aug 28 15:00 UTC Kickoff · Registration closes

> Fri, Sep 4 15:00 UTC Submissions close

The same page says registration closes when the event starts. The official
boundary is therefore:

- kickoff and registration close: **2026-08-28 15:00 UTC**;
- final submission close: **2026-09-04 15:00 UTC**.

## 2. Required submission materials

Source: [official event page](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon)

The page's “What to submit” section lists:

> Basic information

> Project title; Short description; Long description; Technology & category
> tags

> Cover image and presentation

> Cover image; Video presentation; Slide presentation

> App hosting and repository

> Public GitHub repository; Demo application platform; Application URL

Thus the official page supports all of the following as final-submission
materials:

1. project title;
2. short description;
3. long description;
4. technology/category tags;
5. cover image;
6. video presentation;
7. slide presentation;
8. public GitHub repository;
9. demo application platform;
10. application URL.

The official live page sets the package deadline at 2026-09-04 15:00 UTC. No
official extract retrieved here requires the presentation package to be
submitted before kickoff.

The generic official page
[Delivering your hackathon solution](https://lablab.ai/delivering-your-hackathon-solution)
currently returned only a client-rendered/iframe shell to the web retriever.
Consequently this note does **not** independently claim a video duration,
format, slide count, or form-specific character limit from that page.

## 3. Event technology requirements

Source: [official event page](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon)
and [official live event page](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon/live)

Verifiable extract:

> Build AI trading agents on Alpaca — autonomous agents and trading apps using
> Alpaca's Trading API, MCP server and CLI.

This confirms that the official event theme/stack includes:

- an autonomous AI trading agent or trading application;
- Alpaca's Trading API;
- Alpaca MCP server;
- Alpaca CLI.

The retrieved wording does **not** resolve whether a qualifying project must
use both MCP and CLI or may use either one. It also says “AI trading agents” but
does not specifically mandate an LLM. Therefore these stronger statements are
not treated as confirmed rules in this review:

- “an LLM is mandatory”;
- “both MCP and CLI are mandatory”;
- “exactly one of MCP or CLI is sufficient.”

Additional Alpaca product evidence:

- [Alpaca MCP Server](https://alpaca.markets/mcp-server) describes the server as
  translating AI prompts into structured Trading API requests and supporting
  paper trading.
- [Building AI Trading Applications with Alpaca](https://alpaca.markets/blog/building-ai-trading-applications-with-alpaca/)
  identifies the Trading API, Trading MCP Server and Trading CLI as Alpaca's AI
  application tooling.

These pages verify the products' roles, not competition eligibility details.

## 4. Options requirement

Source: [official live event page](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon/live)

Verifiable extract:

> Options Alpha Agents

> Main track · open to all

This confirms that the named main track is options-focused. The general event
page also describes Alpaca as supporting stocks, options, ETFs and crypto.

However, no official extract retrieved in this check states:

> all strategies must incorporate options trading

Accordingly, “the main track is Options Alpha Agents” is confirmed, while “all
strategies must use options” remains **unconfirmed from the official pages
retrieved**.

## 5. New account and USD 100,000

Official Alpaca product source: [Alpaca MCP Server](https://alpaca.markets/mcp-server)

Verifiable extracts:

> Test in Paper Mode ... Try any workflow with $100K in simulated funds and
> real market data.

> Can I paper trade with Alpaca's MCP Server? Absolutely, try your trading
> workflow with simulated $100K fund.

This establishes Alpaca's normal simulated USD 100,000 paper environment. It
does **not** establish the following as event eligibility rules:

- the account must be brand-new;
- the account must be dedicated to this hackathon;
- a reused account is ineligible;
- the balance must be reset to exactly USD 100,000 at kickoff.

No current extract proving those event-specific statements was found on the
official lablab or Alpaca pages retrieved in this review. They must therefore
remain **unconfirmed here**, even if they may appear in an authenticated event
view, official email, or participant dashboard not accessible to this check.

## 6. Evidence boundary

### Confirmed by current official pages

- kickoff: 2026-08-28 15:00 UTC;
- registration closes at kickoff;
- submissions close: 2026-09-04 15:00 UTC;
- final-submission material categories listed in section 2;
- event is for autonomous AI trading agents/apps on Alpaca's Trading API, MCP
  server and CLI;
- main track is “Options Alpha Agents”;
- Alpaca's ordinary paper environment uses simulated USD 100,000.

### Not confirmed by the current official extracts

- a mandatory LLM;
- whether both MCP and CLI are required or either one is sufficient;
- a mandatory brand-new/dedicated hackathon account;
- exact USD 100,000 as an event eligibility condition rather than an Alpaca
  paper-environment default;
- a universal “all strategies must use options” rule;
- video format/duration and form character limits from the client-rendered
  generic submission guide.

