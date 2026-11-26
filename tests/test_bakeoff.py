# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import numpy as np
import pytest

from wavescribe.bakeoff import (
    ModelRun,
    Segment,
    edit_distance,
    normalise,
    read_ref,
    read_wav,
    ref_line,
    report,
    wer,
    write_wav,
)


def test_normalise_keeps_accents_drops_punctuation_and_case() -> None:
    assert normalise("Água, em FARO!  Será reposta.") == ["água", "em", "faro", "será", "reposta"]
    assert normalise("água") != normalise("agua")


def test_edit_distance_and_wer() -> None:
    assert edit_distance(["a", "b", "c"], ["a", "x", "c", "d"]) == 2
    assert wer(["a b c d"], ["a b c d"]) == 0.0
    assert wer(["a b c d", "e f"], ["a c d", "e f g"]) == pytest.approx(2 / 6)


def test_reference_round_trip(tmp_path: Path) -> None:
    segs = [Segment(0, 0.5, 4.25), Segment(1, 70.0, 75.5)]
    path = tmp_path / "clip.ref.txt"
    path.write_text("\n".join(ref_line(s, f"texto {s.index}") for s in segs) + "\n")
    assert path.read_text().splitlines()[1].startswith("[0001 01:10.0-01:15.5]")
    assert read_ref(path, 2) == ["texto 0", "texto 1"]
    with pytest.raises(ValueError, match="2 lines for 3"):
        read_ref(path, 3)
    assert read_ref(tmp_path / "missing.txt", 2) is None


def test_wav_round_trip(tmp_path: Path) -> None:
    audio = (0.5 * np.sin(np.linspace(0, 100, 16_000))).astype(np.float32)
    write_wav(tmp_path / "a.wav", audio)
    assert np.allclose(read_wav(tmp_path / "a.wav"), audio, atol=1e-4)


def test_report_with_and_without_reference() -> None:
    runs = [ModelRun("m1", ["a b", "c d"], 1.0, 2.0), ModelRun("m2", ["a x", "c d"], 2.0, 1.0)]
    with_ref = report(runs, ["a b", "c d"], speech_s=100, clip_s=120, n_utts=2)
    assert "| m1 | 0.0% | 0.010 | 100x real time | — | 2.0 |" in with_ref
    assert "| m2 | 25.0% |" in with_ref
    assert "Agreement" in with_ref
    assert "WER not computed" in report(runs, None, speech_s=100, clip_s=120, n_utts=2)


def test_saved_runs_are_reused_only_for_the_same_utterances(tmp_path: Path) -> None:
    run = ModelRun("m", ["a", "b"], 1.5, 0.2, [1.0, 0.5])
    run.save(tmp_path / "m.json", "sig1")
    assert ModelRun.load(tmp_path / "m.json", "sig1") == run
    assert ModelRun.load(tmp_path / "m.json", "sig2") is None
    assert ModelRun.load(tmp_path / "missing.json", "sig1") is None
