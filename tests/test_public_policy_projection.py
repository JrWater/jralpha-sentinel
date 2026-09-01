from pathlib import Path

from policy.loader import load
from policy.public_projection import project_public_claims
from scripts.check_deliverables import canonical


ROOT = Path(__file__).resolve().parents[1]


def test_public_deliverable_values_share_the_policy_projection():
    manifest = load(ROOT / "policy" / "manifest.json")

    claims = project_public_claims(manifest)
    slots, legal = canonical(manifest)

    assert slots == claims.risk_values
    assert legal == set(claims.legal_dollar_values)
    assert claims.risk_values["per_trade_hard_cap"] == 12_000.0
    assert claims.risk_values["at_risk_cap"] == 40_000.0
