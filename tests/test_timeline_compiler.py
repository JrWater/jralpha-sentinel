"""The Sentinel timing compiler has one composition mode: split scenes."""
from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "sentinel-video" / "tools" / "timeline.py"


def _timeline_module():
    spec = importlib.util.spec_from_file_location("sentinel_timeline", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_every_declared_scene_requires_an_explicit_composition_file():
    timeline = _timeline_module()
    cfg, _script = timeline.load()

    paths = timeline.scene_files(cfg)

    assert len(paths) == len(cfg["scenes"])
    assert all(Path(path).exists() for path in paths.values())
