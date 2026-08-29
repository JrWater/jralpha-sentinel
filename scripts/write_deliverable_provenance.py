#!/usr/bin/env python3
"""Write reproducible SHA-256 provenance for final submission binaries."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.check_deliverables import (
    CANONICAL_BINARIES,
    PROVENANCE_PATH,
    discover_binary_inputs,
    sha256,
)

def write_provenance(*, root: Path = ROOT,
                     artifacts: tuple[str, ...] = CANONICAL_BINARIES) -> dict:
    """Hash present canonical outputs and their build-discovered input graph."""
    records = {}
    for artifact in artifacts:
        output = root / artifact
        if not output.is_file():
            raise FileNotFoundError(f"canonical deliverable is missing: {artifact}")
        inputs = {
            input_path: sha256(root / input_path)
            for input_path in discover_binary_inputs(root, artifact)
        }
        records[artifact] = {
            "sha256": sha256(output),
            "inputs": inputs,
        }
    document = {
        "schema_version": 1,
        "artifacts": records,
    }
    target = root / PROVENANCE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifacts", nargs="*")
    args = parser.parse_args()
    artifacts = tuple(args.artifacts) or CANONICAL_BINARIES
    invalid = sorted(set(artifacts) - set(CANONICAL_BINARIES))
    if invalid:
        parser.error("unknown canonical artifact(s): " + ", ".join(invalid))
    document = write_provenance(artifacts=artifacts)
    print(f"Wrote {PROVENANCE_PATH} for {len(document['artifacts'])} artifact(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
