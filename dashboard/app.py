#!/usr/bin/env python3
"""Sentinel — the live gate matrix.

Renders the credential-free snapshot the agent writes at the end of every
cycle (docs/snapshot.json). This page holds no API keys and makes no broker
calls; see agent/snapshot.py for why.

    .venv/bin/streamlit run dashboard/app.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs" / "snapshot.json"

DIMENSION_ORDER = ("Entry Authority", "Data Readiness", "Process Health",
                   "Release Integrity", "Delivery Health")

DIMENSION_ASKS = {
    "Entry Authority": "Is *this* account, in *this* mode, allowed to trade?",
    "Data Readiness": "Is every input the decision needs actually present?",
    "Process Health": "Did the machinery obey its operational contract?",
    "Release Integrity": "Is the running code the code that was verified?",
    "Delivery Health": "If something breaks, will anyone find out?",
}

st.set_page_config(page_title="Sentinel — Gate Matrix", page_icon="🛡",
                   layout="wide")


@st.cache_data(ttl=30)
def load() -> dict | None:
    try:
        return json.loads(SNAPSHOT.read_text())
    except (OSError, ValueError):
        return None


def age_str(iso: str) -> str:
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        secs = (datetime.now(timezone.utc) - then).total_seconds()
    except (ValueError, AttributeError):
        return "unknown"
    if secs < 90:
        return f"{secs:.0f}s ago"
    if secs < 5400:
        return f"{secs / 60:.0f} min ago"
    return f"{secs / 3600:.1f} h ago"


snap = load()

st.title("Sentinel")
st.markdown(
    "**An autonomous options trading agent whose language model cannot place "
    "an order.** It can only propose one — these gates decide."
)

if snap is None:
    st.warning(
        "No snapshot yet. The agent writes `docs/snapshot.json` at the end of "
        "each cycle; run `.venv/bin/python scripts/run_cycle.py --dry-run` to "
        "produce one."
    )
    st.stop()

acct = snap.get("account", {})
permit = snap.get("permit", {})
gates = snap.get("gates", [])

# ── headline ────────────────────────────────────────────────────────────────
c1, c2, c3, c4 = st.columns(4)
pnl = acct.get("pnl_dollars", 0.0)
c1.metric("Equity", f"${acct.get('equity', 0):,.2f}",
          f"{pnl:+,.2f} ({acct.get('pnl_percent', 0):+.2f}%)")
c2.metric("Account", acct.get("account_number") or "—",
          f"{acct.get('mode', 'PAPER')} · options L{acct.get('options_level', '?')}")
c3.metric("Market", "OPEN" if snap.get("market", {}).get("is_open") else "CLOSED")

status = permit.get("status", "UNKNOWN")
c4.metric("Entry permit", status,
          "new exposure allowed" if status == "READY" else "ENTRY MAINTENANCE",
          delta_color="normal" if status == "READY" else "inverse")

if status != "READY":
    st.info(
        "**Entry Maintenance.** The permit gates *new exposure only*. "
        "Reconciliation, protective exits and risk-reducing orders keep "
        "running — an agent that dumps its book the moment a feed hiccups is "
        "the less obvious hazard, and panic-liquidating at a threshold is "
        "itself a strategy this policy does not authorize."
        + (f"\n\nBlocking: `{'`, `'.join(permit.get('blocking_gates', []))}`"
           if permit.get("blocking_gates") else "")
    )

st.caption(f"Snapshot {age_str(snap.get('generated_at_utc', ''))} · "
           f"policy `{snap.get('policy', {}).get('identity', '?')}`")

# ── the gate matrix ─────────────────────────────────────────────────────────
st.subheader("Gate matrix")
st.caption(
    "A gate belongs to exactly one dimension, and its severity answers exactly "
    "one question: *it is red, so why should the agent not open new exposure "
    "right now?* A check that cannot answer that does not get to be BLOCKING."
)

by_dim: dict = {}
for g in gates:
    by_dim.setdefault(g.get("dimension", "unknown"), []).append(g)

ordered = [d for d in DIMENSION_ORDER if d in by_dim]
ordered += [d for d in sorted(by_dim) if d not in DIMENSION_ORDER]

for dim in ordered:
    rows = sorted(by_dim[dim], key=lambda r: r["name"])
    reds = [r for r in rows if not r["ok"] and r["severity"] == "BLOCKING"]
    ambers = [r for r in rows if not r["ok"] and r["severity"] != "BLOCKING"]
    mark = "🔴" if reds else ("🟠" if ambers else "🟢")

    with st.expander(f"{mark}  **{dim}** — {len(rows) - len(reds) - len(ambers)}"
                     f"/{len(rows)} clear", expanded=bool(reds)):
        st.caption(DIMENSION_ASKS.get(dim, ""))
        for r in rows:
            icon = "🟢" if r["ok"] else ("🔴" if r["severity"] == "BLOCKING"
                                        else "🟠")
            st.markdown(
                f"{icon} **`{r['name']}`** · {r['severity']} · {r['phase']}  \n"
                f"&nbsp;&nbsp;&nbsp;&nbsp;{r['detail'] or '—'}"
            )
            if r.get("rationale"):
                st.caption(f"&nbsp;&nbsp;&nbsp;&nbsp;_{r['rationale']}_")

# ── decisions: the claim, made checkable ────────────────────────────────────
st.subheader("What the model proposed, and what the gates did about it")
st.caption(
    "The model holds no broker credentials and has no submission tool. Every "
    "row below started as a structured proposal; the verdict column is the "
    "gates', not the model's."
)

decisions = list(reversed(snap.get("decisions", [])))
if not decisions:
    st.write("_No proposals recorded yet._")
else:
    refused = sum(1 for d in decisions if not d.get("accepted"))
    st.caption(f"{len(decisions)} recorded · **{refused} refused** by a gate")
    for d in decisions[:40]:
        ok = d.get("accepted")
        icon = "✅" if ok else "⛔"
        head = (f"{icon} `{d.get('engine', '?')}` "
                f"**{d.get('underlying', '?')}** {d.get('structure', '')}")
        detail = d.get("reason") or d.get("detail") or ""
        risk = d.get("max_loss_dollars")
        line = head
        if risk:
            line += f" · max loss ${float(risk):,.0f}"
        st.markdown(line)
        if detail:
            st.caption(f"&nbsp;&nbsp;&nbsp;&nbsp;{detail}")

# ── book and equity ─────────────────────────────────────────────────────────
left, right = st.columns([1, 1])

with left:
    st.subheader("Open contracts")
    positions = snap.get("positions", [])
    if not positions:
        st.write("_Flat._")
    else:
        st.caption(f"{len(positions)} contracts. A vertical is two contracts "
                   f"but one structure, one risk number and one exit.")
        st.dataframe(positions, use_container_width=True, hide_index=True)

with right:
    st.subheader("Equity")
    history = snap.get("equity_history", [])
    if len(history) < 2:
        st.write("_Not enough points yet._")
    else:
        st.line_chart({"equity": [h["equity"] for h in history]})

regime = snap.get("regime")
if regime and regime.get("mode"):
    st.caption(f"Regime: **{regime['mode']}** "
               f"({regime.get('confidence', 0):.2f}) — {regime.get('reason', '')}")

code = snap.get("code", {})
head = (code.get("git_head") or "unknown")[:12]
dirty = " · worktree dirty" if code.get("worktree_dirty") else ""
st.caption(
    f"Running `{head}`{dirty} · policy sha `{snap.get('policy', {}).get('sha', '')[:12]}`. "
    f"Every decision record carries both, so *which parameters was the agent "
    f"running when it made that trade* is answerable from the record alone. "
    f"Paper trading only — nothing here is investment advice."
)
