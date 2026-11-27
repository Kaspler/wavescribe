# SPDX-License-Identifier: Apache-2.0
"""Checks on a translator's reply, before it is kept as a translation.

A translation model given text it can't make sense of (poor reception, a recognition gone
wrong) sometimes answers about the text instead of translating it: "The meaning of the text is
unclear due to recognition errors. A coherent translation cannot be provided." Stored as the
translation, that reads as if the station had said it. So:

- the prompt gives the model one fixed reply for "this can't be translated", ``UNTRANSLATABLE``
  (translate/llm.py), and a reply that carries it is no translation (``declined``);
- a line of speech is one line, so a reply of several lines holds more than its translation:
  a heading before it ("Here is the translation:") and notes after it are cut, and when more
  than one line is left nobody can say which is the translation (``remark``);
- a reply that is nothing but a bracketed note, to a line that has no brackets, is no
  translation (``remark``);
- a reply far longer than the original can justify is something else than its translation
  (``too_long``): a remark about a few garbled words, or a model that went on writing;
- a reply that says what translators say about a text (``remarks.toml``) is a remark, not a
  translation (``remark``), unless the original says the same kind of thing in its own
  language; and a bracketed part worded as a note ("[missing word]", "(possibly a
  recognition error)") is cut out, the rest kept: it still translates the line, which is what
  the reader came for. Other brackets stay. In real translations most were the speaker's own
  aside, an abbreviation or a word the translator filled in, and cutting those loses words
  that were said.

The first four work in any language; the last only in the languages of ``remarks.toml``, so
in another language a remark as long as a translation, on one line, is kept. A reply that
fails is never repaired, re-asked or replaced: the line keeps its original words alone, with a
flag saying which translation is missing and why (``left_out_flag``). When in doubt the reply
is kept, as the ASR checks keep a doubtful line.
"""

from __future__ import annotations

import functools
import re
import tomllib
from dataclasses import dataclass
from importlib import resources

from wavescribe.asr.checks import normalise
from wavescribe.rules.text import NO_SPACES

# The model's whole reply when it can't translate the text. Not a word of any language, so it
# is never the translation of something.
UNTRANSLATABLE = "[[untranslatable]]"
# What marks a line whose translation into a language was left out: "untranslated:en:remark".
UNTRANSLATED = "untranslated"
# Why: the model said it can't; the reply was a remark about the text; the reply was far longer
# than the original; the reply was empty; the model raised.
DECLINED, REMARK, TOO_LONG, EMPTY, FAILED = "declined", "remark", "too_long", "empty", "failed"

# A translation is at most this many times its original, plus some characters for a line of a
# word or two. Measured in letters of a spaced script: a character of a script written without
# spaces counts as DENSE of them. Of about 25,000 translations kept by test runs (from seven
# languages, Hebrew's unwritten vowels the widest gap), none went over this; past twice the
# original (plus the slack) were only a reply with a heading and a note and a prayer the model
# finished from memory.
MAX_GROWTH = 3.0
GROWTH_SLACK = 30
DENSE = 3

_TOKEN = re.compile(r"\[\s*\[?\s*untranslatable\s*\]?\s*\]", re.IGNORECASE)
_OPEN, _CLOSE = "([\uff08\uff3b\u3010", ")]\uff09\uff3d\u3011"  # with full-width ones
_BRACKETED = re.compile(
    rf"\s*[{re.escape(_OPEN)}][^{re.escape(_OPEN + _CLOSE)}]*[{re.escape(_CLOSE)}]"
)
_SPACE_BEFORE = re.compile(r"\s+([,.;:!?、。])")
_QUOTES = {'"': '"', "“": "”", "«": "»", "「": "」"}


@dataclass(frozen=True)
class Reply:
    text: str | None  # the translation to keep; None when there is none
    why: str | None = None  # why there is none: DECLINED, REMARK, TOO_LONG or EMPTY
    notes: int = 0  # the translator's notes cut out of the kept text


def check_reply(reply: str, original: str) -> Reply:
    """What to keep of a translator's ``reply`` to ``original``: the translation, without the
    translator's notes, or nothing and why."""
    text, original = reply.strip(), original.strip()
    if not text:
        return Reply(None, EMPTY)
    if _TOKEN.search(text):
        return Reply(None, DECLINED)
    said = is_remark(original)  # the station itself talks about translating or recognition
    notes = 0
    if "\n" in text and "\n" not in original:
        lines, notes = _the_translation(text, original, said)
        if len(lines) != 1:
            return Reply(None, REMARK)
        text = lines[0]
    if not any(c in original for c in _OPEN) and not _has_words(_unbracketed(text)):
        return Reply(None, REMARK)
    if not said:
        text, cut = _without_notes(text)
        notes += cut
        if not _has_words(text) or is_remark(text):
            return Reply(None, REMARK)
    if _measure(text) > MAX_GROWTH * _measure(original) + GROWTH_SLACK:
        return Reply(None, TOO_LONG)
    return Reply(text, notes=notes)


def left_out_flag(target: str, why: str) -> str:
    """The flag on a line whose translation into ``target`` was left out, e.g.
    ``untranslated:en:remark``."""
    return f"{UNTRANSLATED}:{target}:{why}"


def without_flag(flags: tuple[str, ...], target: str) -> tuple[str, ...]:
    """``flags`` without the one saying the translation into ``target`` was left out."""
    return tuple(f for f in flags if not f.startswith(f"{UNTRANSLATED}:{target}:"))


def is_left_out(flags: tuple[str, ...] | list[str]) -> bool:
    """Whether any translation of the line was left out."""
    return any(f.startswith(f"{UNTRANSLATED}:") for f in flags)


def is_remark(text: str) -> bool:
    """Whether ``text`` says what a translator says about a text (``remarks.toml``)."""
    return bool(_remarks()[0].search(normalise(text)))


def remark_languages() -> tuple[str, ...]:
    """The languages ``remarks.toml`` has phrases for."""
    return tuple(sorted(_data()))


def _has_words(text: str) -> bool:
    return bool(re.search(r"\w", text))


def _unbracketed(text: str) -> str:
    """``text`` without its bracketed parts, the inner ones first ("[[…]]")."""
    for _ in range(3):
        text, n = _BRACKETED.subn(" ", text)
        if not n:
            break
    return text


def _is_note(bracketed: str) -> bool:
    """Whether a bracketed part is worded as the translator's note."""
    normal = normalise(bracketed)
    return any(p.search(normal) for p in _remarks())


def _the_translation(text: str, original: str, said: bool) -> tuple[list[str], int]:
    """The lines of a reply of several lines that can be the translation of a one-line
    ``original``, and how many were cut: a first line that ends in a colon is a heading, and a
    line that is all brackets, or worded as a remark, is a note."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    kept = lines[1:] if lines[0].endswith((":", "\uff1a")) else list(lines)
    kept = [
        line for line in kept if _has_words(_unbracketed(line)) and (said or not is_remark(line))
    ]
    if len(kept) == 1 and len(kept[0]) > 2:  # in quotation marks of the model's own
        first, last = kept[0][0], kept[0][-1]
        if _QUOTES.get(first) == last and original[:1] != first:
            kept[0] = kept[0][1:-1].strip()
    return kept, len(lines) - len(kept)


def _without_notes(text: str) -> tuple[str, int]:
    """``text`` without the bracketed parts worded as the translator's notes, and how many
    there were."""
    notes = 0

    def cut(m: re.Match[str]) -> str:
        nonlocal notes
        if not _is_note(m.group(0)):
            return m.group(0)
        notes += 1
        return ""

    out = _BRACKETED.sub(cut, text)
    if not notes:
        return text, 0
    return _SPACE_BEFORE.sub(r"\1", " ".join(out.split())), notes


def _measure(text: str) -> int:
    return sum(DENSE if NO_SPACES.match(c) else 1 for c in text)


@functools.cache
def _remarks() -> tuple[re.Pattern[str], re.Pattern[str]]:
    """``remarks.toml`` as two patterns of whole words, matched as ``asr/junk.toml`` is: what
    is a remark anywhere (``phrases``), and what makes a note of a bracketed part
    (``notes``)."""

    def pattern(key: str) -> re.Pattern[str]:
        found = {normalise(p) for section in _data().values() for p in section.get(key, ())}
        alternatives = [re.escape(p) for p in sorted(found - {""}, key=len, reverse=True)]
        return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)")

    return pattern("phrases"), pattern("notes")


@functools.cache
def _data() -> dict[str, dict[str, list[str]]]:
    source = resources.files("wavescribe.translate").joinpath("remarks.toml")
    return tomllib.loads(source.read_text(encoding="utf-8"))
