# SPDX-License-Identifier: Apache-2.0
"""MADLAD-400 (Apache-2.0, 419 languages) through CTranslate2 on the CPU.

Running on the CPU leaves the GPU to speech recognition. The target language is chosen with a
``<2xx>`` token prepended to the source text.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from wavescribe.translate import split_sentences

if TYPE_CHECKING:
    from sentencepiece import SentencePieceProcessor

VOCABULARY = "spiece.model"


def _target_token(sp: SentencePieceProcessor, target: str) -> str | None:
    """The ``<2xx>`` token for a BCP-47 tag: the most specific form MADLAD knows."""
    parts = target.replace("_", "-").split("-")
    for n in range(len(parts), 0, -1):
        token = f"<2{'_'.join(parts[:n])}>"
        if sp.piece_to_id(token) != sp.unk_id():
            return token
    return None


@functools.lru_cache(maxsize=1)
def _vocabulary(path: Path) -> SentencePieceProcessor:
    import sentencepiece

    return sentencepiece.SentencePieceProcessor(model_file=str(path))


def unknown_targets(model_path: Path, targets: Iterable[str]) -> list[str]:
    """The targets MADLAD has no token for, read from its vocabulary alone (a few megabytes;
    the model itself stays unloaded). A folder without the vocabulary has no opinion: loading
    it fails with its own message."""
    vocabulary = model_path / VOCABULARY
    targets = list(targets)
    if not targets or not vocabulary.is_file():
        return []
    sp = _vocabulary(vocabulary)
    return [t for t in targets if _target_token(sp, t) is None]


class MadladTranslator:
    def __init__(
        self, model_id: str, model_path: Path, *, threads: int = 4, beam_size: int = 2
    ) -> None:
        import ctranslate2
        import sentencepiece

        self.model_id = model_id
        self._beam = beam_size
        self._translator = ctranslate2.Translator(
            str(model_path),
            device="cpu",
            compute_type="int8",
            inter_threads=1,
            intra_threads=threads,
        )
        self._sp = sentencepiece.SentencePieceProcessor(model_file=str(model_path / VOCABULARY))
        self._tags: dict[str, str] = {}

    def target_token(self, target: str) -> str:
        if target not in self._tags:
            token = _target_token(self._sp, target)
            if token is None:
                raise ValueError(f"MADLAD-400 has no target token for {target!r}")
            self._tags[target] = token
        return self._tags[target]

    def translate(self, text: str, source: str, target: str) -> str:
        sentences = split_sentences(text)
        if not sentences:
            return ""
        prefix = self.target_token(target)
        batch = [[*self._sp.encode(f"{prefix} {s}", out_type=str), "</s>"] for s in sentences]
        results = self._translator.translate_batch(
            batch,
            beam_size=self._beam,
            max_decoding_length=256,
            max_input_length=512,
        )
        return " ".join(self._sp.decode(r.hypotheses[0]) for r in results).strip()
