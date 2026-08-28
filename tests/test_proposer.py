from datetime import date

from agent.proposer import SelectionResult, select
from policy.loader import load
from strategy.engine import Candidate
from strategy.proposal import OptionLeg, Proposal


class Manifest:
    def __init__(self, agent):
        self.agent = agent

    def get(self, *path, default=...):
        if path[0] != "agent":
            raise KeyError(path)
        if path[1] in self.agent:
            return self.agent[path[1]]
        if default is not ...:
            return default
        raise KeyError(path)


def _candidate(symbol="SPY"):
    expiry = date(2026, 9, 4)
    proposal = Proposal(
        engine="trend_income", underlying=symbol, direction="long",
        structure="single_long",
        legs=[OptionLeg(f"{symbol}260904C00700000", "buy", 1, 700,
                        "call", expiry)],
        expiry=expiry, dte=1, limit_price=1.25,
        max_loss_dollars=125, conviction=0.7,
    )
    return Candidate(proposal, 0.7, "trend")


def test_deepseek_selection_is_returned_with_auditable_model_evidence():
    calls = []

    def model_call(**request):
        calls.append(request)
        return '{"selections":[{"candidate_index":1,"rank":1,"thesis":"best"}]}'

    result = select(
        [_candidate("SPY"), _candidate("QQQ")],
        regime="risk_on", portfolio={"equity": 100_000},
        manifest=Manifest({"provider": "deepseek",
                           "model": "deepseek-v4-flash",
                           "max_proposals_per_cycle": 1}),
        api_key="secret-for-test", model_call=model_call,
    )

    assert result == SelectionResult(
        indices=(1,), decision_mode="llm", provider="deepseek",
        model="deepseek-v4-flash", fallback_reason=None)
    assert calls[0]["provider"] == "deepseek"
    assert calls[0]["response_format"] == {"type": "json_object"}


def test_deepseek_failure_falls_back_and_says_why():
    def unavailable(**_request):
        raise TimeoutError("broker-independent model outage")

    result = select(
        [_candidate("SPY"), _candidate("QQQ")],
        manifest=Manifest({"provider": "deepseek",
                           "model": "deepseek-v4-flash",
                           "max_proposals_per_cycle": 1}),
        api_key="secret-for-test", model_call=unavailable,
    )

    assert result.indices == (0,)
    assert result.decision_mode == "deterministic_fallback"
    assert result.fallback_reason == "model_error"


def test_deepseek_key_can_be_loaded_from_the_ignored_local_env(
        monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=local-secret\n")
    seen = []

    def model_call(**request):
        seen.append(request["api_key"])
        return '{"selections":[{"candidate_index":0}]}'

    result = select(
        [_candidate()],
        manifest=Manifest({"provider": "deepseek",
                           "model": "deepseek-v4-flash",
                           "max_proposals_per_cycle": 1}),
        model_call=model_call,
    )

    assert result.decision_mode == "llm"
    assert seen == ["local-secret"]


def test_policy_declares_the_deepseek_model_used_by_the_agent():
    manifest = load()

    assert manifest.get("agent", "provider") == "deepseek"
    assert manifest.get("agent", "model") == "deepseek-v4-flash"
