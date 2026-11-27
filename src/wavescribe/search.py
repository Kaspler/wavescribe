# SPDX-License-Identifier: Apache-2.0
"""How a search query matches text: the same for the CLI, the page and the agent tools.

- Every word of the query must be found, ignoring case and accents, folded as the keyword
  rules fold them (``rules.text``): accents only where they are optional (Latin, Greek,
  Cyrillic, Hebrew, Arabic), so a Japanese voicing mark or a Thai tone mark still counts.
- A word matches at the start of a word: "agua" finds "Águas", "eau" finds "l'eau" but not
  "niveau". Inflections that only add letters at the end are found; compounds are not.
- A word written with a leading ``*`` matches anywhere, inside words too: "*eau" finds "niveau".
  A trailing ``*`` changes nothing (every word is a prefix already).
- In scripts written without spaces between words (Chinese, Japanese, Thai, ...) there are no
  word starts to look for, so every word matches anywhere.
- Every character is itself: ``%`` and ``_`` are no wildcards. An apostrophe matches any other
  (U+0027, U+2019, U+2018, U+02BC), so "it's" finds the typographic "it\u2019s" and the
  other way round.

The store's trigram index finds the candidates (every word as a substring, in each of its
``spellings``); ``match`` keeps the ones whose words start where they should and says where, for
highlighting.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from wavescribe.rules.text import NO_SPACES
from wavescribe.rules.text import _folded as _rules_folded  # one folding for rules and search

FRAGMENT = "*"
SNIPPET_CHARS = 80  # about what FTS5's snippet() gave over trigrams, cut at spaces
# An apostrophe is typed one way and written another: a keyboard's ', typography's U+2019, U+2018
# by mistake, and U+02BC (a letter in Ukrainian, among others). Each finds the others.
APOSTROPHES = "'\u2019\u2018\u02bc"


@dataclass(frozen=True)
class Term:
    text: str  # as typed, without ``*``: what the index is asked for
    folded: str
    anywhere: bool  # may start inside a word


def parse(query: str) -> list[Term]:
    """The words of ``query``, in order."""
    terms = []
    for word in query.split():
        text = word.strip(FRAGMENT)
        folded = _folded(text)[0]
        if not folded:
            continue
        anywhere = (
            word.startswith(FRAGMENT) or bool(NO_SPACES.search(text)) or not _is_word(folded[0])
        )
        terms.append(Term(text, folded, anywhere))
    return terms


def match(terms: list[Term], text: str) -> list[tuple[int, int]] | None:
    """Character spans in ``text`` where the terms are found, in order and merged, or None
    when a word that must start a word doesn't. The index has already found every term as a
    substring, so one that may match anywhere never rules a text out, even where Python's
    folding differs from FTS5's."""
    folded, origin = _folded(text)
    found: list[tuple[int, int]] = []
    for term in terms:
        hits = []
        at = folded.find(term.folded)
        while at != -1:
            if term.anywhere or at == 0 or not _is_word(folded[at - 1]):
                hits.append((at, at + len(term.folded)))
            at = folded.find(term.folded, at + 1)
        if not hits and not term.anywhere:
            return None
        found += hits
    spans: list[tuple[int, int]] = []
    for start, end in sorted(found):
        a, b = origin[start], origin[end - 1] + 1
        while b < len(text) and unicodedata.combining(text[b]):  # an accent written apart
            b += 1
        if spans and a <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(b, spans[-1][1]))
        else:
            spans.append((a, b))
    return spans


def snippet(
    text: str, spans: list[tuple[int, int]], marks: tuple[str, str], width: int = SNIPPET_CHARS
) -> str:
    """``text`` around its first match, cut to about ``width`` characters (at spaces where
    there are any), with ``marks`` around every match shown."""
    begin, end = 0, len(text)
    if len(text) > width:
        first = spans[0][0] if spans else 0
        begin = max(0, min(first - width // 4, len(text) - width))
        end = begin + width
        space = text.find(" ", begin, min(first, begin + 15))
        if begin and space != -1:
            begin = space + 1
        space = text.rfind(" ", end - 15, end)
        if end < len(text) and space > first:
            end = space
    out, at = [], begin
    for a, b in spans:
        if a < begin or b > end:
            continue
        out += [text[at:a], marks[0], text[a:b], marks[1]]
        at = b
    out.append(text[at:end])
    return ("…" if begin else "") + "".join(out).strip() + ("…" if end < len(text) else "")


def spellings(word: str) -> list[str]:
    """``word`` as a text may write it: with each kind of apostrophe in place of the one typed
    (for the index, which compares characters as they are). Just ``word`` when it has none."""
    if not any(c in APOSTROPHES for c in word):
        return [word]
    return ["".join(a if c in APOSTROPHES else c for c in word) for a in APOSTROPHES]


def _folded(text: str) -> tuple[str, list[int]]:
    """``text`` folded (decomposed), and the index in ``text`` that each folded character comes
    from. Query and text fold alike, one character at a time; every apostrophe becomes '."""
    pairs = list(_rules_folded(text))
    return "".join("'" if c in APOSTROPHES else c for c, _ in pairs), [i for _, i in pairs]


def _is_word(c: str) -> bool:
    """Part of a word, as the rules cut words: a letter, digit or ``_``, or a mark kept on one."""
    return c.isalnum() or c == "_" or unicodedata.category(c).startswith("M")
