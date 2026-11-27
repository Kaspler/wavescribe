# SPDX-License-Identifier: Apache-2.0
"""What third-party libraries print while Wavescribe runs: on the terminal only what matters
(and all of it with -v); their routine notes go to the log."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

XNNPACK = "INFO: Created TensorFlow Lite XNNPACK delegate for CPU."


def test_a_native_librarys_routine_line_goes_to_the_log_and_the_rest_to_the_terminal(
    capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    from wavescribe.log import native_stderr

    log = logging.getLogger("wavescribe.test")
    with caplog.at_level("DEBUG", logger="wavescribe.test"), native_stderr(log, ("XNNPACK",)):
        os.write(2, f"{XNNPACK}\nERROR: something real\n".encode())
    err = capfd.readouterr().err
    assert XNNPACK not in err and "ERROR: something real" in err
    assert [r.getMessage() for r in caplog.records] == [f"native: {XNNPACK}"]
    assert caplog.records[0].levelno == logging.DEBUG
    os.write(2, b"after\n")  # file descriptor 2 is the terminal's again
    assert capfd.readouterr().err == "after\n"


def test_the_speech_and_music_gate_keeps_litert_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    import ai_edge_litert.interpreter as litert

    from wavescribe.gate.yamnet import YamnetGate

    class Interpreter:  # what LiteRT does as it opens a model: a line on fd 2, from C++
        def __init__(self, **_kw: object) -> None:
            os.write(2, f"{XNNPACK}\n".encode())

        def allocate_tensors(self) -> None:
            pass

        def get_input_details(self) -> list[dict]:
            return [{"index": 0}]

        get_output_details = get_input_details

    monkeypatch.setattr(litert, "Interpreter", Interpreter)
    model = tmp_path / "yamnet.tflite"
    with zipfile.ZipFile(model, "w") as z:
        z.writestr("yamnet_label_list.txt", "Speech\nMusic\n")
    YamnetGate(model)
    assert XNNPACK not in capfd.readouterr().err


@pytest.mark.parametrize(("verbose", "shown"), [(False, False), (True, True)])
def test_transformers_notes_show_on_the_terminal_only_with_verbose(
    verbose: bool, shown: bool
) -> None:
    """Loading Whisper's tokenizer printed a long note about a setting transformers ignores."""
    level = "INFO" if verbose else "WARNING"
    code = (
        "import logging; from wavescribe.log import setup_logging; "
        f"setup_logging(None, console_level=logging.{level}); "
        "from transformers.utils import logging as tl; "
        "tl.get_logger('transformers.tokenization_utils_base').warning('a note about a setting')"
    )
    env = {k: v for k, v in os.environ.items() if k != "TRANSFORMERS_VERBOSITY"}
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120
    )
    assert done.returncode == 0, done.stderr
    assert ("a note about a setting" in done.stderr) == shown
    assert ("PyTorch was not found" in done.stderr) == shown  # said as transformers loads
