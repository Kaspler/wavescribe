# SPDX-License-Identifier: Apache-2.0
"""How a search query matches text: the same for the CLI, the page and the agent tools.

- Every word of the query must be found, ignoring case and accents.
- A word matches at the start of a word: "agua" finds "Águas", "eau" finds "l'eau" but not
  "niveau". Inflections that only add letters at the end are found; compounds are not.
- A word written with a leading ``*`` matches anywhere, inside words too: "*eau" finds "niveau".
  A trailing ``*`` changes nothing (every word is a prefix already).
- In scripts written without spaces between words (Chinese, Japanese, Thai, ...) there are no
  word starts to look for, so every word matches anywhere.

The store's trigram index finds the candidates (every word as a substring); ``match`` keeps the
ones whose words start where they should and says where, for highlighting.
"""

from __future__ import annotations

import functools
import unicodedata
from dataclasses import dataclass

from wavescribe.rules.text import NO_SPACES

FRAGMENT = "*"
SNIPPET_CHARS = 80  # about what FTS5's snippet() gave over trigrams, cut at spaces


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


def _folded(text: str) -> tuple[str, list[int]]:
    """``text`` in lower case without accents, and the index in ``text`` that each folded
    character comes from. Folded one character at a time, so query and text fold alike."""
    out, origin = [], []
    for i, c in enumerate(text):
        f = _fold_char(c)
        out.append(f)
        origin += [i] * len(f)
    return "".join(out), origin


@functools.lru_cache(maxsize=8192)
def _fold_char(c: str) -> str:
    decomposed = unicodedata.normalize("NFKD", c.casefold())
    bare = "".join(d for d in decomposed if not unicodedata.combining(d))
    return unicodedata.normalize("NFC", bare)


def _is_word(c: str) -> bool:
    return c.isalnum() or c == "_" or unicodedata.category(c).startswith("M")
