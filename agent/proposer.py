#!/usr/bin/env python3
"""The LLM proposer: the model argues, the gates decide.

This is the piece that makes the project an *AI trading agent* in the sense
the hackathon requires, and the piece that makes it safe: the model receives a
candidate list, the market regime, and the portfolio state; it may select a
subset, reorder them, and write a free-text thesis for each. It can NOT build
an order, cannot see credentials, and its output is validated against the
candidate universe — a hallucinated ticker or structure is dropped, not
submitted.

Deterministic fallback: no API key, no connectivity, or parse failure => the
top-N candidates by engine conviction are selected unchanged. The agent
degrades to the quant engine rather than to nothing.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict

MAX_ATTEMPTS = 2


def _load_key() -> str | None:
    return (os.environ.get("ANTHROPIC_API_KEY")
            or _from_env_file())


def _from_env_file() -> str | None:
    from pathlib import Path
    for env_path in (Path.cwd() / ".env", Path.home() / ".openclaw" / ".env"):
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY="):
                    return line.partition("=")[2].strip().strip('"').strip("'")
        except OSError:
            continue
    return None


def _system_prompt() -> str:
    return (
        "You are the decision layer of Sentinel, a defined-risk options agent "
        "trading in a 5-day paper-trading competition. You will receive a "
        "ranked list of candidate trades produced by a deterministic quant "
        "engine, the market regime, and the portfolio state.\n\n"
        "Rules you cannot break:\n"
        "1. You may only select from the candidates given. You may not invent "
        "symbols, structures, or prices.\n"
        "2. Every selection must keep max_loss_dollars as reported. You may "
        "not modify positions/sizes.\n"
        "3. You may select at most N candidates and may rank them in order of "
        "conviction. Write one sentence of thesis per selection, in the voice "
        "of a disciplined options trader.\n"
        "4. All trades are defined-risk only. There are no naked shorts, no "
        "market orders, and this competition ends and flattens by 10:45 ET "
        "September 4.\n"
        "Respond with STRICT JSON: "
        '{"selections": [{"candidate_index": 0, "rank": 1, "thesis": "..."}, ...]}'
    )


def _user_prompt(regime, portfolio, candidates) -> str:
    rows = []
    for i, c in enumerate(candidates):
        p = c.proposal
        rows.append({
            "candidate_index": i,
            "engine": p.engine,
            "underlying": p.underlying,
            "structure": p.structure,
            "direction": p.direction,
            "dte": p.dte,
            "limit_price": p.limit_price,
            "max_loss_dollars": p.max_loss_dollars,
            "conviction": p.conviction,
            "reason": p.reason,
        })
    return json.dumps({
        "regime": regime,
        "portfolio": portfolio,
        "candidates": rows,
    }, indent=1)


def select(candidates, *, regime=None, portfolio=None, manifest=None,
           max_sel=None) -> list[int]:
    """Return candidate indices to trade, ranked best-first.

    Falls back to the engine ranking when the model is unavailable or its
    answer is unparseable — the fallback is a refusal to gamble on a bad
    answer, never an automatic submission of everything.
    """
    if not candidates:
        return []
    max_sel = max_sel or int(manifest.get("agent", "max_proposals_per_cycle")) \
        if manifest else 3
    max_sel = max_sel or 3

    key = _load_key()
    if not key:
        return list(range(min(max_sel, len(candidates))))

    try:
        import anthropic
    except ImportError:
        return list(range(min(max_sel, len(candidates))))

    model = (manifest.get("agent", "model") if manifest else "claude-sonnet-4-5")
    client = anthropic.Anthropic(api_key=key)

    for _ in range(MAX_ATTEMPTS):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=800,
                system=_system_prompt(),
                messages=[{"role": "user",
                           "content": _user_prompt(regime, portfolio, candidates)}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            parsed = _parse(text)
            return _validated(parsed, candidates, max_sel)
        except Exception:                                    # noqa: BLE001
            continue
    return list(range(min(max_sel, len(candidates))))


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in model response")
    return json.loads(m.group(0))


def _validated(parsed: dict, candidates, max_sel: int) -> list[int]:
    """Dedupe, bound, and intersect with the candidate universe."""
    out: list[int] = []
    for sel in parsed.get("selections", [])[: max_sel * 2]:
        try:
            idx = int(sel["candidate_index"])
        except (KeyError, ValueError, TypeError):
            continue
        if 0 <= idx < len(candidates) and idx not in out:
            out.append(idx)
    return out[:max_sel] or list(range(min(max_sel, len(candidates))))
