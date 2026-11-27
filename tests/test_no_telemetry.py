"""No library Wavescribe loads keeps or sends telemetry. onnxruntime (the speech detector)
starts a telemetry store under ~/Library/Application Support/Microsoft the first time it makes a
session, unless ORT_DISABLE_TELEMETRY is set before it loads; importing Wavescribe sets it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SESSION = """
import onnxruntime as ort
try:
    ort.InferenceSession(b"not a model", providers=["CPUExecutionProvider"])
except Exception:
    pass  # refused, but onnxruntime has started by now
"""


def _files_after(home: Path, code: str) -> list[Path]:
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    subprocess.run([sys.executable, "-I", "-c", code], env=env, cwd=home, check=True, timeout=120)
    return [p for p in home.rglob("*") if p.is_file()]


def test_importing_wavescribe_keeps_onnxruntime_from_starting_its_telemetry(
    tmp_path: Path,
) -> None:
    pytest.importorskip("onnxruntime")
    if not _files_after(tmp_path / "plain", SESSION):
        pytest.skip("this onnxruntime starts no telemetry store, so there is nothing to stop")
    assert _files_after(tmp_path / "wavescribe", "import wavescribe\n" + SESSION) == []


def test_the_switches_are_set_on_import() -> None:
    import os

    import wavescribe  # noqa: F401

    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
