# SPDX-License-Identifier: Apache-2.0
"""Language ``auto``: each utterance transcribed in the language it is spoken in.

A watch hears whoever is on the air, in whatever language they speak, so its language can be
``auto``. Whisper tells the language from the first 30 s of an utterance; the station's
``languages`` (if any) narrow the choice to the likely ones, which keeps a short or noisy
utterance from being taken for a language nobody nearby speaks. A language clearly none of
them is still taken as heard: told that speech in one language is another, Whisper translates
instead of transcribing, and the text would look like what was said when it isn't. The rest of the
pipeline then sees the utterance as one of a station in that language: its checks,
translation, alert rules and the store.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from wavescribe.asr import Transcriber, Transcript
from wavescribe.registry import ModelError

if TYPE_CHECKING:
    from wavescribe.config import Station
    from wavescribe.registry import ModelEntry, ModelRegistry

AUTO = "auto"
OUTSIDE = 0.1  # the station's languages together less likely than this: it's none of them

NEEDS = (
    "language 'auto' needs a speech model that tells languages apart: "
    "wavescribe models fetch whisper-large-v3-turbo; or give the one language spoken, e.g. "
    '--language pt-PT (language = "pt-PT" in the stations file)'
)


class CannotDetect(ModelError):
    """The model chosen for a station in language ``auto`` can't tell languages apart; the
    page reads it as code ``cannot_detect`` (wavescribe/refusal.py)."""

    code = "cannot_detect"


def can_detect(entry: ModelEntry, path: Path) -> bool:
    """Whether a model can say which language is spoken (``identify_language``), as its wrapper
    tells from the model on disk: an English-only Whisper can't."""
    from wavescribe.asr.mlx_audio import MlxAudioTranscriber

    return entry.backend == "mlx-audio" and MlxAudioTranscriber.tells_languages(path)


def detects_languages(entry: ModelEntry, path: Path | None) -> bool:
    """Whether a speech model, at ``path`` on disk (None: not downloaded), can be the model of a
    station in language ``auto``: the test ``plan_detecting`` makes of each model. What a model
    not downloaded can tell isn't known, so it can't."""
    return entry.capability == "asr" and path is not None and can_detect(entry, path)


def plan_detecting(
    registry: ModelRegistry, station: Station, asr_model: str | None = None
) -> tuple[ModelEntry, Path]:
    """The ASR model for a station in language ``auto``: the one named on the command line or
    chosen on the page, else the first installed model of the chain that can tell languages
    apart and knows every one of the station's ``languages``. A chosen model that can't tell
    them is refused with ``CannotDetect``."""
    chosen = asr_model or station.asr_model
    entries = [registry.get(chosen)] if chosen else registry.chain("asr")
    for entry in entries:
        unknown = [tag for tag in station.languages if not entry.supports(tag)]
        path = registry.local_path(entry) if entry.backend == "mlx-audio" else None
        if path is None and entry.backend == "mlx-audio":
            if chosen:
                raise ModelError(
                    f"{entry.id} is not installed; run: wavescribe models fetch {entry.id}"
                )
            continue
        detects = detects_languages(entry, path)
        if not detects or unknown:
            if chosen and not detects:
                raise CannotDetect(f"{entry.id} can't tell languages; {NEEDS}")
            if chosen:
                raise ModelError(f"{entry.id} doesn't know {', '.join(unknown)}; {NEEDS}")
            continue
        try:
            registry.check_license(entry)
        except ModelError:
            if chosen:
                raise
            continue
        return entry, path
    raise ModelError(NEEDS)


def likeliest(probs: Mapping[str, float], candidates: Sequence[str] = ()) -> str:
    """The candidate (as given, e.g. "pt-PT") whose language is the likeliest, by its primary
    subtag; the first one listed on a tie. With no candidates, or none of them plausible, the
    likeliest of all."""
    chance = {tag: probs.get(tag.split("-")[0].lower(), 0.0) for tag in candidates}
    primaries = {tag.split("-")[0].lower() for tag in candidates}
    if candidates and sum(probs.get(p, 0.0) for p in primaries) >= OUTSIDE:
        return max(candidates, key=chance.__getitem__)
    return max(probs, key=probs.__getitem__)


def transcribe_any(
    transcriber: Transcriber, samples: np.ndarray, candidates: Sequence[str] = ()
) -> Transcript:
    """Transcribe ``samples`` in the language spoken in them; the transcript's language is the
    one chosen."""
    probs_of = getattr(transcriber, "identify_language", None)
    probs = probs_of(samples) if probs_of is not None else None
    if probs:
        language = likeliest(probs, candidates)
    elif candidates:
        language = candidates[0]  # the model can't tell: the likeliest the station listed
    else:
        raise ValueError(f"{transcriber.model_id} can't tell which language is spoken")
    return transcriber.transcribe(samples, language)
