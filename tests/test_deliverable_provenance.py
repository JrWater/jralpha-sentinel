import json

from scripts.check_deliverables import sha256
from scripts.write_deliverable_provenance import write_provenance


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
