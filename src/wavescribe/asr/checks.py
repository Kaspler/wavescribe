# SPDX-License-Identifier: Apache-2.0
"""Sanity checks on ASR output, before anything downstream (store, rules, translation) sees it.

Two kinds of verdict:
- **drop**: text that can't be real speech (repetition loops, more text than the audio could
  hold), or nothing but the phrases models write over silence and music: subtitle credits and
  video sign-offs, listed in ``junk.toml`` for every language at once. Music that slipped past
  the gate is the usual source.
- **flag**: real-looking text in another language than the station's: a song's lyrics, a clip
  in another language, or the ASR translating instead of transcribing. Flagged text is kept and
  shown dimmed, because Portuguese radio does carry real English interviews. A repeated phrase
  inside a message is flagged too (``repetition``).

Repetition is how a decoder fails ("thank you, thank you, thank you" for a minute of music) and
also how an emergency is announced ("Atenção, atenção, atenção, atenção. Evacuem a zona
ribeirinha."), so a repeated phrase alone doesn't condemn a text. A stuck decoder produces its
loop and next to nothing else, and keeps going far longer than a speaker would. A phrase
repeated ``MIN_REPEATS`` times or more is therefore dropped only when

- fewer than ``MIN_OTHER_WORDS`` different words stand beside it
  ("Segundo, saco, frago, frago, frago, frago, frago."), or
- the whole text compresses better than ``MAX_COMPRESSION``, which a short phrase reaches
  after about eight repeats whatever surrounds them, or
- there is more text than the audio could hold (the first check).

Otherwise it is kept whole and flagged: "Evacuate now. Evacuate now. Evacuate now. Evacuate
now. Go to higher ground." reaches the store and the alert rules as it was said. The flag is a
note for readers and doesn't hold an alert back. When in doubt the text is kept: a line of
nonsense in the transcript costs less than a warning nobody saw. For the same reason a dropped
loop is still handed to the keyword rules, which keep any hit as a low alert that is never
announced (``Alerter.on_filtered``).

Language-agnostic: thresholds are per character and per second, the repetition check also works
on scripts written without spaces, and the language check uses the station's BCP-47 primary
subtag against a 97-language identifier (skipped for languages it doesn't know).
"""

from __future__ import annotations

import functools
import re
import tomllib
import unicodedata
import zlib
from dataclasses import dataclass
from importlib import resources

from wavescribe.languages import primary_subtag
from wavescribe.rules.text import NO_SPACES

MAX_CHARS_PER_S = 35.0  # broadcast speech measured at 6-19 chars/s
MAX_COMPRESSION = 2.4  # zlib ratio per character; real text is ~0.9-1.5 in every script tried
MIN_COMPRESSION_CHARS = 50  # shorter texts compress too little to tell
MIN_REPEATS = 4  # an n-gram repeated this many times in a row...
MIN_REPEAT_SHARE = 0.4  # ...covering this share of the words is a repeated phrase
MIN_OTHER_WORDS = 3  # a repeated phrase beside fewer different words than this is only a loop
MIN_LANG_CHARS = 20  # shorter texts are too short to identify ("Foi preso." reads as Romanian)
MAX_LANG_P = 0.05  # flag when the station's language is less likely than this


@dataclass(frozen=True)
class Check:
    drop: str | None = None  # why the text should be dropped
    flags: tuple[str, ...] = ()  # e.g. ("lang:es",)


def check_transcript(text: str, duration_s: float, language: str) -> Check:
    text = text.strip()
    if not text:
        return Check()
    if duration_s >= 1 and len(text) / duration_s > MAX_CHARS_PER_S:
        return Check(drop="too_long")
    repeated = _repetition(text)
    if repeated == "loop":
        return Check(drop="repetition")
    if is_junk(text):
        return Check(drop="junk")
    other = _other_language(text, language)
    flags = (("repetition",) if repeated else ()) + ((f"lang:{other}",) if other else ())
    return Check(flags=flags)


def is_junk(text: str) -> bool:
    """Whether ``text`` is nothing but subtitle credits, "thanks for watching" and the like
    (``junk.toml``), in any language: what a model writes when it hears no speech. The same
    words inside a longer line don't count."""
    normal = normalise(text)
    return bool(normal) and not re.search(r"\w", _junk().sub(" ", normal))


def normalise(text: str) -> str:
    """Lower case, without accents or other marks, and every run of punctuation and spaces as
    one space: how ``junk.toml`` is matched."""
    # Turkish dotless i (U+0131) is written "I" in capitals, which folds back to a dotted "i".
    decomposed = unicodedata.normalize("NFKD", text.casefold().replace("\u0131", "i"))
    bare = "".join(c for c in decomposed if not unicodedata.category(c).startswith("M"))
    return " ".join(re.sub(r"[\W_]+", " ", bare).split())


@functools.cache
def _junk() -> re.Pattern[str]:
    """``junk.toml`` as one pattern of whole words: phrases as written, then the patterns."""
    source = resources.files("wavescribe.asr").joinpath("junk.toml")
    raw = tomllib.loads(source.read_text(encoding="utf-8"))
    phrases = {normalise(p) for section in raw.values() for p in section.get("phrases", ())}
    patterns = [p for section in raw.values() for p in section.get("patterns", ())]
    # Longest first, so a phrase that contains another is taken whole.
    alternatives = [re.escape(p) for p in sorted(phrases - {""}, key=len, reverse=True)]
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives + patterns) + r")(?!\w)")


def _repetition(text: str) -> str | None:
    """``"loop"`` for a decoder's loop, ``"phrase"`` for a phrase repeated inside a message."""
    if len(text) >= MIN_COMPRESSION_CHARS and _compression(text) > MAX_COMPRESSION:
        return "loop"
    words = re.findall(r"\w+", text.lower())
    found = None
    for n in range(1, 5):
        run, phrase = _longest_run(words, n)
        if run >= MIN_REPEATS and run * n >= MIN_REPEAT_SHARE * len(words):
            if _other_words(words, phrase) < MIN_OTHER_WORDS:
                return "loop"
            found = "phrase"
    return found


def _compression(text: str) -> float:
    """How well ``text`` compresses, per character in any script. Each character becomes one
    code (two when the text uses more than 256 different ones): measured on UTF-8, a script
    whose letters take three bytes each (Thai, Devanagari) looks half as repetitive again as it
    is, and an ordinary news bulletin would be dropped as a loop."""
    alphabet: dict[str, int] = {}
    codes = [alphabet.setdefault(c, len(alphabet)) for c in text]
    width = max(1, ((len(alphabet) - 1).bit_length() + 7) // 8)
    raw = b"".join(c.to_bytes(width, "little") for c in codes)
    return len(raw) / len(zlib.compress(raw))


def _longest_run(words: list[str], n: int) -> tuple[int, tuple[str, ...]]:
    """The most consecutive repeats of any n-word sequence, and that sequence."""
    best: tuple[int, tuple[str, ...]] = (1, ())
    for start in range(n):
        run, prev = 1, None
        for i in range(start, len(words) - n + 1, n):
            gram = tuple(words[i : i + n])
            run = run + 1 if gram == prev else 1
            prev = gram
            if run > best[0]:
                best = (run, gram)
    return best


def _other_words(words: list[str], phrase: tuple[str, ...]) -> int:
    """How many different words the text has besides the repeated phrase. Scripts written
    without spaces give one "word" per clause, so there every character counts."""
    others = {w for w in words if w not in phrase}
    return sum(len(w) if NO_SPACES.search(w) else 1 for w in others)


@functools.cache
def _identifier():
    from py3langid.langid import MODEL_FILE, LanguageIdentifier

    return LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)


def _other_language(text: str, language: str) -> str | None:
    """The language the text looks like, if it clearly isn't ``language``."""
    if len(text) < MIN_LANG_CHARS:
        return None
    ident = _identifier()
    expected = primary_subtag(language)
    if expected not in ident.labels:
        return None  # a language the identifier doesn't know: no opinion
    ranked = dict(ident.rank(text))
    if ranked.get(expected, 0.0) >= MAX_LANG_P:
        return None
    return max(ranked, key=ranked.__getitem__)


def wrong_language(text: str, language: str) -> str | None:
    """For translations: the language ``text`` is clearly in instead of ``language``, else None.

    Translators sometimes answer in the wrong language (MADLAD did, and Qwen did when given a
    bare code), so alert translations are checked before anyone hears them.
    """
    return _other_language(text.strip(), language)
