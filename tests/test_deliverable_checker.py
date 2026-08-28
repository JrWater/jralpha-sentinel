from scripts.check_deliverables import (
    FINDING_CODES,
    Finding,
    check_deliverables,
    exit_code,
)


def _manifest():
    return {
        "environment": {"required_starting_equity": 100_000},
        "risk_caps": {
            "max_loss_per_position_fraction": 0.12,
            "at_risk_cap_fraction": 0.40,
            "daily_loss_kill_fraction": 0.12,
            "equity_floor_fraction": 0.70,
            "daily_new_exposure_cap_fraction": 0.30,
        },
        "strategies": {},
        "version": "3.1.1",
    }


def _write(root, relative, text=""):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_checker_discovers_a_new_scene_without_a_handmaintained_list(tmp_path):
    _write(tmp_path, "README.md", "Sentinel\n")
    _write(tmp_path, "sentinel-video/index.html", "<main>root</main>")
    _write(tmp_path, "sentinel-video/compositions/scene-99.html", "16 gates")

    findings = check_deliverables(root=tmp_path, manifest=_manifest())

    assert Finding(
        code="HARDCODED_GATE_COUNT", severity="ATTENTION",
        location="sentinel-video/compositions/scene-99.html:1",
        manifest_key=None, expected="deterministic gates", actual="16 gates",
    ) in findings


def test_checker_reports_external_authority_quotes_without_failing_the_build(tmp_path):
    _write(
        tmp_path, "docs/COMPLIANCE.md",
        '**"Competition account starting balance must be set to $100,000."**\n',
    )

    findings = check_deliverables(root=tmp_path, manifest=_manifest())

    assert any(
        finding.code == "QUOTED_EXTERNAL_AUTHORITY"
        and finding.severity == "ATTENTION"
        and finding.location == "docs/COMPLIANCE.md:1"
        for finding in findings
    )
    assert exit_code(findings) == 0


def test_checker_makes_a_wrong_manifest_value_blocking(tmp_path):
    _write(tmp_path, "README.md", "The hard cap on any single trade is $2,000.\n")

    findings = check_deliverables(root=tmp_path, manifest=_manifest())

    assert any(
        finding.code == "VALUE_CONFLICT"
        and finding.severity == "BLOCKING"
        and finding.location == "README.md:1"
        and finding.manifest_key == "per_trade_hard_cap"
        and finding.expected == "$12,000"
        and finding.actual == "$2,000"
        for finding in findings
    )
    assert exit_code(findings) == 1


def test_checker_normalizes_compact_dollar_values_before_comparing_policy(tmp_path):
    _write(tmp_path, "README.md", "The at-risk cap is $13k across the book.\n")

    findings = check_deliverables(root=tmp_path, manifest=_manifest())

    assert Finding(
        "VALUE_CONFLICT", "BLOCKING", "README.md:1", "at_risk_cap",
        "$40,000", "$13,000",
    ) in findings


def test_inline_historical_section_is_visible_but_does_not_fail_current_copy(tmp_path):
    _write(
        tmp_path, "docs/STRATEGY.md",
        "## v3.0 archive <!-- deliverable-check: historical -->\n"
        "The hard cap on any single trade was $2,000.\n"
        "## Current\n"
        "The hard cap on any single trade is $12,000.\n",
    )

    findings = check_deliverables(root=tmp_path, manifest=_manifest())

    assert Finding(
        "HISTORICAL_EXEMPTION", "ATTENTION", "docs/STRATEGY.md:1", None,
        "current-deliverable scan", "historical section",
    ) in findings
    assert not any(
        finding.code == "VALUE_CONFLICT" and finding.location == "docs/STRATEGY.md:2"
        for finding in findings
    )


def test_checker_blocks_a_canonical_binary_without_reproducible_provenance(tmp_path):
    _write(tmp_path, "media/sentinel_demo.mp4", "not really an mp4")

    findings = check_deliverables(root=tmp_path, manifest=_manifest())

    assert Finding(
        code="BINARY_PROVENANCE_MISSING", severity="BLOCKING",
        location="media/sentinel_demo.mp4", manifest_key=None,
        expected="docs/deliverable-provenance.json", actual=None,
    ) in findings


def test_finding_codes_are_closed_and_exit_only_depends_on_blocking():
    assert FINDING_CODES == (
        "VALUE_CONFLICT",
        "HARDCODED_GATE_COUNT",
        "QUOTED_EXTERNAL_AUTHORITY",
        "BINARY_PROVENANCE_MISSING",
        "BINARY_PROVENANCE_MISMATCH",
        "BINARY_PROVENANCE_UNTRACKED_INPUT",
        "HISTORICAL_EXEMPTION",
    )
    assert exit_code((
        Finding("HARDCODED_GATE_COUNT", "ATTENTION", "README.md:1",
                None, None, "16 gates"),
    )) == 0
