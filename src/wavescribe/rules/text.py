# SPDX-License-Identifier: Apache-2.0
"""Language-aware text matching for keyword rules, with no language-specific code.

- Case and accents are folded ("Proteção" matches "protecao") in the scripts whose marks are
  optional (Latin, Greek, Cyrillic, Hebrew, Arabic). Elsewhere a mark is part of the word: a
  Thai tone mark, a Devanagari virama, a Japanese voicing mark ("ガス", gas, is not "カス").
- Words are stemmed with Snowball when it has an algorithm for the language, so a pack lists
  base forms and plurals match ("inundação" matches "inundações"). Other languages match whole
  folded words.
- Matches respect word boundaries ("REN" never matches inside "renda").
- Terms written in capitals (acronyms such as "REN", "ANEPC") match case-sensitively, unstemmed.
- Scripts written without spaces (Chinese, Japanese, Thai, ...) match by substring.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass

from wavescribe.languages import language_name, primary_subtag


def _mark_class() -> str:
    """A character class of the combining marks in the scripts that use them inside words
    (Indic vowel signs and viramas, Thai, Arabic and Hebrew points…): Python's ``\\w`` leaves
    them out, which cuts "चेतावनी" (warning) into three words, one of them "वन" (forest)."""
    spans = ((0x0300, 0x1E00), (0x20D0, 0x2100), (0x3099, 0x309B), (0xA670, 0xAB00),
             (0xFE20, 0xFE30))  # fmt: skip
    marks = [c for a, b in spans for c in range(a, b) if unicodedata.category(chr(c))[0] == "M"]
    ranges: list[list[int]] = []
    for c in marks:
        if ranges and ranges[-1][1] == c - 1:
            ranges[-1][1] = c
        else:
            ranges.append([c, c])
    return "".join(f"\\u{a:04x}-\\u{b:04x}" for a, b in ranges)


_WORD = re.compile(rf"\w[\w{_mark_class()}]*", re.UNICODE)
# Scripts that don't separate words with spaces: matching falls back to substrings.
NO_SPACES = re.compile(r"[฀-໿က-႟ក-៿぀-ヿ㐀-䶿一-鿿가-힯]")


def _marks_optional(base: str) -> bool:
    """Whether accents on ``base`` may be dropped: Latin, Greek and Cyrillic, and the vowel
    points of Hebrew and Arabic, which most text leaves out anyway."""
    o = ord(base)
    return o < 0x0530 or 0x0590 <= o < 0x0700 or 0x0750 <= o < 0x0780 or 0x08A0 <= o < 0x0900


def _folded(text: str) -> Iterator[tuple[str, int]]:
    """Each character of the folded text (decomposed) with the index it came from in ``text``."""
    base = " "
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKD", ch.casefold()):
            if not unicodedata.combining(c):
                base = c
            elif _marks_optional(base):
                continue
            yield c, i


def fold(text: str) -> str:
    """Lower case without accents: 'Proteção Civil' -> 'protecao civil'."""
    return unicodedata.normalize("NFC", "".join(c for c, _ in _folded(text)))


@functools.cache
def _stemmer(language: str):
    import snowballstemmer

    try:
        name = language_name(primary_subtag(language)).split(" (")[0].lower()
    except Exception:
        return None
    return snowballstemmer.stemmer(name) if name in snowballstemmer.algorithms() else None


@dataclass(frozen=True)
class Token:
    key: str  # what is compared: the folded stem (or the raw word for acronyms)
    raw: str
    start: int
    end: int


class Matcher:
    """Tokenises text and terms the same way for one language."""

    def __init__(self, language: str) -> None:
        self.language = language
        self._stem = _stemmer(language)

    def _key(self, word: str) -> str:
        # Stem before folding: Snowball's rules expect the accents.
        stem = self._stem.stemWord(word.lower()) if self._stem else word.lower()
        return fold(stem)

    def tokens(self, text: str) -> list[Token]:
        return [
            Token(self._key(m.group()), m.group(), m.start(), m.end()) for m in _WORD.finditer(text)
        ]

    def find(
        self, term: str, text: str, tokens: list[Token] | None = None
    ) -> list[tuple[int, int]]:
        """Character spans in ``text`` where ``term`` occurs."""
        if NO_SPACES.search(term):
            # Folding changes lengths (ำ decomposes into two, ﾞ joins its kana), so spans are
            # mapped back to the characters they came from.
            needle = "".join(c for c, _ in _folded(term))
            pairs = list(_folded(text))
            hay, where = "".join(c for c, _ in pairs), [i for _, i in pairs]
            return [(where[m.start()], where[m.end() - 1] + 1)
                    for m in re.finditer(re.escape(needle), hay)] if needle else []  # fmt: skip
        exact = term.startswith("=")
        words = _WORD.findall(term)
        if not words:
            return []
        tokens = self.tokens(text) if tokens is None else tokens
        acronym = len(words) == 1 and len(words[0]) >= 2 and words[0].isupper()
        if acronym and not exact:
            return [(t.start, t.end) for t in tokens if t.raw == words[0]]
        n = len(words)
        if exact:
            # "=word": as written (case and accents still don't matter), no stemming. For words
            # whose stem is shared by common ones: Snowball stems Portuguese "tornado" like
            # "torna" ("becomes"), and "água" like "águias" ("eagles").
            want = [fold(w) for w in words]
            have = [fold(t.raw) for t in tokens]
        else:
            want = [self._key(w) for w in words]
            have = [t.key for t in tokens]
        return [
            (tokens[i].start, tokens[i + n - 1].end)
            for i in range(len(tokens) - n + 1)
            if have[i : i + n] == want
        ]
