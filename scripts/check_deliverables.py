#!/usr/bin/env python3
"""Check public submission material against policy and build evidence.

``check_deliverables`` is the public seam.  It returns structured findings so
tests, CI, and the command-line renderer see the same evidence. The CLI is a
thin adapter: only a BLOCKING finding produces a non-zero exit status.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gates.registry import SEVERITIES
from policy.public_projection import project_public_claims


FINDING_CODES = (
    "VALUE_CONFLICT",
    "HARDCODED_GATE_COUNT",
    "QUOTED_EXTERNAL_AUTHORITY",
    "BINARY_PROVENANCE_MISSING",
    "BINARY_PROVENANCE_MISMATCH",
    "BINARY_PROVENANCE_UNTRACKED_INPUT",
    "POLICY_PROVENANCE_MISMATCH",
    "HISTORICAL_EXEMPTION",
)

HISTORICAL_FILES = (
    "docs/CLAUDE_CODE_PLAN.md",
    "docs/PLAN_VS_ACTUAL.md",
)
PROVENANCE_PATH = "docs/deliverable-provenance.json"
CANONICAL_BINARIES = (
    "media/sentinel_demo.mp4",
    "media/slides.pdf",
    "media/cover.png",
)


class Finding(NamedTuple):
    """One checkable discrepancy or review prompt in public material.

    ``manifest_key``, ``expected``, and ``actual`` are intentionally nullable:
    an external quotation has no manifest value to invent. ``code`` and
    ``severity`` are closed vocabularies.
    """

    code: str
    severity: str
    location: str
    manifest_key: str | None
    expected: str | None
    actual: str | None


def _assert_finding(finding: Finding) -> Finding:
    if finding.code not in FINDING_CODES:
        raise ValueError(f"unknown deliverable finding code: {finding.code}")
    if finding.severity not in SEVERITIES:
        raise ValueError(f"unknown deliverable finding severity: {finding.severity}")
    return finding


def _finding(*args, **kwargs) -> Finding:
    return _assert_finding(Finding(*args, **kwargs))


def _money(value: float) -> str:
    return f"${value:,.0f}"


def canonical(manifest) -> tuple[dict[str, float], set[int]]:
    """Return manifest-backed public values; never derive them from prose."""
    claims = project_public_claims(manifest)
    return claims.risk_values, set(claims.legal_dollar_values)


def discover_deliverables(root: Path) -> tuple[Path, ...]:
    """Discover public text, instead of maintaining a hand-edited file list."""
    paths: set[Path] = set()
    if (root / "README.md").is_file():
        paths.add(root / "README.md")
    for pattern in (
        "docs/*.md",
        "media/build/*.html",
        "media/build/script.json",
        "sentinel-video/index.html",
        "sentinel-video/compositions/*.html",
        "sentinel-video/assets/align/*.json",
        "sentinel-video/narration.json",
    ):
        paths.update(path for path in root.glob(pattern) if path.is_file())
    return tuple(sorted(paths, key=lambda path: path.relative_to(root).as_posix()))


def _text_lines(path: Path) -> tuple[str, ...]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix != ".json":
        return tuple(raw.splitlines())
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return tuple(raw.splitlines())
    if isinstance(decoded, list):
        return tuple(str(item.get("text", "")) for item in decoded if isinstance(item, Mapping))
    if isinstance(decoded, Mapping) and isinstance(decoded.get("characters"), list):
        return ("".join(map(str, decoded["characters"])),)
    return tuple(raw.splitlines())


DOLLARS = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{1,6})(?:\s*([kK]))?")
RISK_WORDS = re.compile(
    r"hard cap|per[- ]trade|max(?:imum)? loss|at[- ]risk|kill switch|"
    r"equity floor|entry maintenance|daily exposure|exposure cap|book[- ]wide",
    re.I,
)
SLOT_PATTERNS = (
    (re.compile(r"hard cap[^.]{0,50}single trade|single trade[^.]{0,50}hard cap|hard per-trade", re.I),
     "per_trade_hard_cap"),
    (re.compile(r"at[- ]risk cap|across the whole book|book[- ]wide cap|portfolio at[- ]risk", re.I),
     "at_risk_cap"),
    (re.compile(r"kill switch", re.I), "daily_kill"),
    (re.compile(r"entry maintenance|equity floor", re.I), "equity_floor"),
)
GATE_COUNT = re.compile(
    r"\b(?:sixteen|seventeen|eighteen|nineteen|\d{1,2})\s+"
    r"(?:deterministic\s+)?gates?\b", re.I)
EXTERNAL_QUOTE = re.compile(r'(?:^|\s)(?:\*\*)?"[^"\n]{20,}"(?:\*\*)?')


def _location(root: Path, path: Path, line: int) -> str:
    return f"{path.relative_to(root).as_posix()}:{line}"


def _dollar_values(line: str) -> list[int]:
    values = []
    for match in DOLLARS.finditer(line):
        value = int(match.group(1).replace(",", ""))
        values.append(value * 1000 if match.group(2) else value)
    return values


def _text_findings(root: Path, manifest) -> list[Finding]:
    slots, legal = canonical(manifest)
    findings: list[Finding] = []
    for path in discover_deliverables(root):
        relative = path.relative_to(root).as_posix()
        if relative in HISTORICAL_FILES:
            findings.append(_finding(
                "HISTORICAL_EXEMPTION", "ATTENTION", relative, None,
                "current-deliverable scan", "historical record",
            ))
            continue
        in_historical_section = False
        try:
            lines = _text_lines(path)
        except OSError as exc:
            findings.append(_finding(
                "VALUE_CONFLICT", "BLOCKING", relative, None,
                "readable deliverable", str(exc),
            ))
            continue
        for line_number, line in enumerate(lines, 1):
            location = _location(root, path, line_number)
            if "<!-- deliverable-check: historical -->" in line:
                in_historical_section = True
                findings.append(_finding(
                    "HISTORICAL_EXEMPTION", "ATTENTION", location, None,
                    "current-deliverable scan", "historical section",
                ))
                continue
            if line.startswith("## "):
                in_historical_section = False
            if in_historical_section:
                continue
            count = GATE_COUNT.search(line)
            if count:
                findings.append(_finding(
                    "HARDCODED_GATE_COUNT", "ATTENTION", location, None,
                    "deterministic gates", count.group(0),
                ))
            if path.name == "COMPLIANCE.md" and EXTERNAL_QUOTE.search(line):
                findings.append(_finding(
                    "QUOTED_EXTERNAL_AUTHORITY", "ATTENTION", location,
                    None, "recheck event source before submission", line.strip(),
                ))
            values = _dollar_values(line)
            if not values or not RISK_WORDS.search(line):
                continue
            for pattern, manifest_key in SLOT_PATTERNS:
                if not pattern.search(line):
                    continue
                wanted = round(slots[manifest_key])
                if wanted not in values:
                    findings.append(_finding(
                        "VALUE_CONFLICT", "BLOCKING", location, manifest_key,
                        _money(wanted), ", ".join(_money(value) for value in values),
                    ))
                break
            else:
                for value in values:
                    if value not in legal:
                        findings.append(_finding(
                            "VALUE_CONFLICT", "BLOCKING", location, None,
                            "a value derivable from policy/manifest.json", _money(value),
                        ))
    return findings


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_binary_inputs(root: Path, artifact: str) -> tuple[str, ...]:
    """Discover inputs from the build topology rather than recording a list."""
    if artifact == "media/sentinel_demo.mp4":
        paths = {
            root / "sentinel-video/index.html",
            root / "sentinel-video/narration.json",
            root / "sentinel-video/hyperframes.json",
            root / "sentinel-video/package.json",
            root / "sentinel-video/tools/timeline.py",
        }
        for pattern in (
            "sentinel-video/compositions/**/*.html",
            "sentinel-video/assets/audio/*",
            "sentinel-video/assets/align/*.json",
            "sentinel-video/assets/code_screenshot.*",
            "sentinel-video/assets/dash_*",
            "sentinel-video/assets/fonts/*",
        ):
            paths.update(root.glob(pattern))
    elif artifact == "media/slides.pdf":
        paths = {root / "media/build/slides.html", root / "media/build/datauris.json"}
    elif artifact == "media/cover.png":
        paths = {root / "media/build/cover.html", root / "media/build/datauris.json"}
    else:
        return ()
    policy_manifest = root / "policy" / "manifest.json"
    if policy_manifest.is_file():
        paths.add(policy_manifest)
    return tuple(sorted(
        path.relative_to(root).as_posix() for path in paths if path.is_file()
    ))


def _provenance_findings(root: Path, manifest) -> list[Finding]:
    outputs = tuple(relative for relative in CANONICAL_BINARIES if (root / relative).is_file())
    if not outputs:
        return []
    location = root / PROVENANCE_PATH
    if not location.is_file():
        return [_finding(
            "BINARY_PROVENANCE_MISSING", "BLOCKING", output, None,
            PROVENANCE_PATH, None,
        ) for output in outputs]
    try:
        document = json.loads(location.read_text(encoding="utf-8"))
        records = document["artifacts"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return [_finding(
            "BINARY_PROVENANCE_MISSING", "BLOCKING", output, None,
            "valid " + PROVENANCE_PATH, str(exc),
        ) for output in outputs]
    findings: list[Finding] = []
    if (root / "policy" / "manifest.json").is_file():
        claims = project_public_claims(manifest)
        expected = {
            "identity": getattr(manifest, "identity", None),
            "manifest_sha": getattr(manifest, "sha", None),
            "risk_values": claims.risk_values,
        }
        if document.get("policy") != expected:
            findings.append(_finding(
                "POLICY_PROVENANCE_MISMATCH", "BLOCKING", PROVENANCE_PATH,
                None, json.dumps(expected, sort_keys=True),
                json.dumps(document.get("policy"), sort_keys=True),
            ))
    for output in outputs:
        record = records.get(output)
        if not isinstance(record, Mapping):
            findings.append(_finding(
                "BINARY_PROVENANCE_MISSING", "BLOCKING", output, None,
                PROVENANCE_PATH, None,
            ))
            continue
        actual_hash = sha256(root / output)
        if record.get("sha256") != actual_hash:
            findings.append(_finding(
                "BINARY_PROVENANCE_MISMATCH", "BLOCKING", output, None,
                str(record.get("sha256")), actual_hash,
            ))
        recorded_inputs = record.get("inputs", {})
        if not isinstance(recorded_inputs, Mapping):
            findings.append(_finding(
                "BINARY_PROVENANCE_MISMATCH", "BLOCKING", output, None,
                "input SHA-256 map", type(recorded_inputs).__name__,
            ))
            continue
        for input_path in discover_binary_inputs(root, output):
            if input_path not in recorded_inputs:
                findings.append(_finding(
                    "BINARY_PROVENANCE_UNTRACKED_INPUT", "BLOCKING",
                    output, None, input_path, None,
                ))
                continue
            current = sha256(root / input_path)
            if recorded_inputs[input_path] != current:
                findings.append(_finding(
                    "BINARY_PROVENANCE_MISMATCH", "BLOCKING", output,
                    None, str(recorded_inputs[input_path]), current,
                ))
    return findings


def check_deliverables(*, root: Path = ROOT, manifest=None) -> tuple[Finding, ...]:
    """Return all public-material findings in a stable order."""
    if manifest is None:
        from policy.loader import load as load_manifest
        manifest = load_manifest()
    findings = _text_findings(root, manifest) + _provenance_findings(root, manifest)
    return tuple(sorted(findings, key=lambda finding: (
        finding.severity, finding.location, finding.code,
        finding.manifest_key or "", finding.actual or "",
    )))


def exit_code(findings: Iterable[Finding]) -> int:
    """ATTENTION is visible, but only BLOCKING fails automation."""
    return 1 if any(finding.severity == "BLOCKING" for finding in findings) else 0


def _render(finding: Finding) -> str:
    detail = "; ".join(
        item for item in (
            f"expected={finding.expected}" if finding.expected is not None else None,
            f"actual={finding.actual}" if finding.actual is not None else None,
            f"manifest_key={finding.manifest_key}" if finding.manifest_key else None,
        ) if item
    )
    return f"[{finding.severity:<9}] {finding.code:<35} {finding.location}" + (
        f" — {detail}" if detail else "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    args = parser.parse_args()
    findings = check_deliverables()
    if args.json:
        print(json.dumps([finding._asdict() for finding in findings], indent=2))
    elif findings:
        for finding in findings:
            print(_render(finding))
    else:
        print("No deliverable findings.")
    return exit_code(findings)


if __name__ == "__main__":
    raise SystemExit(main())
