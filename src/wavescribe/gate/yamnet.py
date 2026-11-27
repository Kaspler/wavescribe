# SPDX-License-Identifier: Apache-2.0
"""YAMNet (Google, Apache-2.0) on LiteRT: an AudioSet classifier, about 1,000x real time on one
CPU core, with no TensorFlow."""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

import numpy as np

from wavescribe.gate import Verdict, decide

WINDOW = 15600  # samples per YAMNet window: 0.975 s at 16 kHz
MIN_TAIL = WINDOW // 4  # a shorter leftover isn't worth a window of its own

log = logging.getLogger(__name__)


class YamnetGate:
    def __init__(self, model_path: Path, *, model_id: str = "yamnet") -> None:
        from ai_edge_litert.interpreter import Interpreter

        from wavescribe.log import native_stderr

        self.model_id = model_id
        with native_stderr(log, ("XNNPACK delegate",)):  # LiteRT's note as it opens a model
            self._it = Interpreter(model_path=str(model_path), num_threads=1)
            self._it.allocate_tensors()
        self._in = self._it.get_input_details()[0]["index"]
        self._out = self._it.get_output_details()[0]["index"]
        # The label list travels inside the .tflite file (TFLite metadata is an appended zip).
        with zipfile.ZipFile(model_path) as z:
            labels = z.read("yamnet_label_list.txt").decode().splitlines()
        self._speech = labels.index("Speech")
        self._music = labels.index("Music")

    def classify(self, samples: np.ndarray) -> Verdict:
        samples = np.asarray(samples, dtype=np.float32)
        windows = [samples[i : i + WINDOW] for i in range(0, len(samples), WINDOW)]
        if len(windows) > 1 and len(windows[-1]) < MIN_TAIL:
            windows.pop()
        speech, music = [], []
        for w in windows:
            if len(w) < WINDOW:
                w = np.pad(w, (0, WINDOW - len(w)))
            self._it.set_tensor(self._in, w)
            self._it.invoke()
            scores = self._it.get_tensor(self._out)[0]
            speech.append(scores[self._speech])
            music.append(scores[self._music])
        return decide(np.array(speech), np.array(music))
