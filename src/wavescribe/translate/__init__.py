# SPDX-License-Identifier: Apache-2.0
"""Translation: source-language text -> a target language. Never on the alert path."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from wavescribe.registry import ModelEntry

# Sentence ends across common scripts. Latin, Cyrillic, Greek, Devanagari (danda) and Arabic put
# a space after the mark; CJK full stops and full-width marks (U+3002, U+FF01, U+FF1F) don't.
_SENTENCE_END = re.compile(r"(?<=[.!?\u2026\u0964\u061f])\s+|(?<=[\u3002\uff01\uff1f])\s*")


class Translator(Protocol):
    model_id: str

    def translate(self, text: str, source: str, target: str) -> str:
        """Translate ``text`` from BCP-47 ``source`` to ``target``."""
        ...


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text.strip()) if s]


def unsupported_targets(entry: ModelEntry, path: Path, targets: Iterable[str]) -> list[str]:
    """The targets this translator can't translate into, found without loading the model, so a
    wrong ``translate_to`` is reported when stations are planned rather than at the first
    utterance. An LLM takes any language by name; MADLAD has a fixed list."""
    if entry.backend == "ctranslate2-madlad":
        from wavescribe.translate.madlad import unknown_targets

        return unknown_targets(path, targets)
    return []


def load_translator(entry: ModelEntry, path: Path, *, threads: int = 4) -> Translator:
    if entry.backend == "ctranslate2-madlad":
        from wavescribe.translate.madlad import MadladTranslator

        return MadladTranslator(entry.id, path, threads=threads)
    if entry.backend == "mlx-lm":
        from wavescribe.translate.llm import LlmTranslator

        return LlmTranslator(entry.id, path)
    raise ValueError(f"{entry.id}: no translator backend {entry.backend!r}")
