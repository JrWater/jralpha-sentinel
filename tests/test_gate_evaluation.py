from datetime import datetime, timezone
import subprocess
from types import SimpleNamespace

from gates.registry import Gate, GateResult


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    )


def test_code_identity_ignores_generated_snapshot_but_not_source(tmp_path):
    """A cycle's public record is not an unreviewed code change."""
    from gates.evaluation import code_identity

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Sentinel test")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "snapshot.json").write_text('{"version": 1}\n')
    (tmp_path / "agent.py").write_text("VERSION = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline")

    (tmp_path / "docs" / "snapshot.json").write_text('{"version": 2}\n')
    _head, dirty = code_identity(tmp_path)
    assert dirty is False

    (tmp_path / "agent.py").write_text("VERSION = 2\n")
    _head, dirty = code_identity(tmp_path)
    assert dirty is True


def _subject_parts():
    return dict(
        manifest=SimpleNamespace(),
        state=SimpleNamespace(
            now_utc=datetime(2026, 8, 28, 18, tzinfo=timezone.utc),
            account=SimpleNamespace(), clock=SimpleNamespace(is_open=True),
            positions=[], chain_ages={}, latest={},
        ),
        ledger_positions=[],
        unresolved_dispatch_count=0,
    )


def test_evaluator_selects_only_the_exact_subject_type():
    from gates.evaluation import CycleSubject, GateEvaluator, ProposalSubject

    seen = []

    def cycle_gate(ctx):
        seen.append(("cycle", ctx.proposal))
        return GateResult(True, "cycle")

    def proposal_gate(ctx):
        seen.append(("proposal", ctx.proposal))
        return GateResult(True, "proposal")

    gates = (
        Gate("cycle", cycle_gate, CycleSubject, "BLOCKING", "Process Health", "r"),
        Gate("proposal", proposal_gate, ProposalSubject, "BLOCKING", "Entry Authority", "r"),
    )
    cycle = CycleSubject(**_subject_parts())
    proposal = SimpleNamespace(engine="trend_directional")
    evaluator = GateEvaluator(gates=gates)

    assert tuple(evaluator.evaluate(cycle)) == ("cycle",)
    assert tuple(evaluator.evaluate(ProposalSubject.from_cycle(cycle, proposal))) == (
        "proposal",
    )
    assert seen == [("cycle", None), ("proposal", proposal)]


def test_market_open_is_cycle_readiness_and_entry_window_is_proposal_authorization():
    from gates import checks
    from gates.evaluation import CycleSubject, ProposalSubject

    names_by_subject = {
        subject: {gate.name for gate in checks.GATES if gate.accepts is subject}
        for subject in (CycleSubject, ProposalSubject)
    }

    assert "market_open" in names_by_subject[CycleSubject]
    assert "entry_window" not in names_by_subject[CycleSubject]
    assert "entry_window" in names_by_subject[ProposalSubject]
    assert "market_open" not in names_by_subject[ProposalSubject]


def test_event_window_exception_needs_a_proposal_but_market_open_does_not():
    from gates import checks
    from policy.loader import EntryWindow

    class Manifest:
        def entry_window_for(self, engine):
            return EntryWindow(
                timezone="America/New_York",
                opens_at="09:30" if engine == "event_macro" else "10:00",
                closes_at="15:30",
            )

    def context(engine):
        return checks.EvalContext(
            manifest=Manifest(),
            now_utc=datetime(2026, 9, 4, 13, 35, tzinfo=timezone.utc),
            clock=SimpleNamespace(is_open=True),
            proposal=SimpleNamespace(engine=engine),
        )

    assert checks.check_market_open(context("trend_directional")).ok
    assert checks.check_entry_window(context("event_macro")).ok
    assert not checks.check_entry_window(context("trend_directional")).ok
