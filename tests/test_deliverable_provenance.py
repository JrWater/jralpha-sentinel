import json
import subprocess
import sys
from pathlib import Path

from scripts.check_deliverables import sha256
from scripts import write_deliverable_provenance as provenance_writer
from scripts.write_deliverable_provenance import write_provenance


ROOT = Path(__file__).resolve().parents[1]


def _write(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


def test_provenance_is_derived_from_the_real_output_and_discovered_inputs(tmp_path):
    _write(tmp_path, "media/cover.png", b"final-cover")
    _write(tmp_path, "media/build/cover.html", "<main>cover</main>")
    _write(tmp_path, "media/build/datauris.json", "{}")

    document = write_provenance(root=tmp_path, artifacts=("media/cover.png",))
    on_disk = json.loads((tmp_path / "docs/deliverable-provenance.json").read_text())
    record = document["artifacts"]["media/cover.png"]

    assert on_disk == document
    assert record["sha256"] == sha256(tmp_path / "media/cover.png")
    assert record["inputs"] == {
        "media/build/cover.html": sha256(tmp_path / "media/build/cover.html"),
        "media/build/datauris.json": sha256(tmp_path / "media/build/datauris.json"),
    }


def test_provenance_writer_runs_as_its_documented_direct_script(tmp_path):
    """The delivery command must not depend on an ambient PYTHONPATH."""
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/write_deliverable_provenance.py"),
         "--help"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_provenance_writer_defaults_to_all_canonical_artifacts(monkeypatch):
    """No positional argument is the documented "write all" invocation."""
    received = {}
    def record_artifacts(*, artifacts):
        received["artifacts"] = artifacts
        return {"artifacts": {}}

    monkeypatch.setattr(provenance_writer, "write_provenance", record_artifacts)
    monkeypatch.setattr(sys, "argv", ["write_deliverable_provenance.py"])

    assert provenance_writer.main() == 0
    assert received["artifacts"] == provenance_writer.CANONICAL_BINARIES
