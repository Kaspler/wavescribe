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
- Words in double quotes are a phrase: found in that order, one right after the other, with
  only spaces or punctuation between them. Its first word starts a word, as any word does, and
  its last may go on: "corte de águ" finds "corte de águas", but not "corte de luz e água". In
  scripts written without spaces, its words may also follow with nothing between. A quote
  left open runs to the end of the query.
- Every character is itself: ``%`` and ``_`` are no wildcards. An apostrophe matches any other
  (U+0027, U+2019, U+2018, U+02BC), so "it's" finds the typographic "it\u2019s" and the
  other way round.

The store's trigram index finds the candidates (every word as a substring, in each of its
``spellings``; of a phrase, its words of three characters or more, which the index can look up,
see ``lookups``); ``match`` keeps the ones whose words start where they should and says where,
for highlighting.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from wavescribe.rules.text import NO_SPACES
from wavescribe.rules.text import _folded as _rules_folded  # one folding for rules and search

FRAGMENT = "*"
SNIPPET_CHARS = 80  # about what FTS5's snippet() gave over trigrams, cut at spaces
# An apostrophe is typed one way and written another: a keyboard's ', typography's U+2019, U+2018
# by mistake, and U+02BC (a letter in Ukrainian, among others). Each finds the others.
APOSTROPHES = "'\u2019\u2018\u02bc"
# Double quotes, as a keyboard types them and as typography writes them in several languages.
QUOTES = '"\u201c\u201d\u201e\u00ab\u00bb'
_QUOTE = re.compile(f"[{QUOTES}]")


@dataclass(frozen=True)
class Term:
    text: str  # as typed, without ``*``: what the index is asked for
    folded: str
    # May start inside a word. For a phrase's later words: may follow the word before with
    # nothing between (a script written without spaces).
    anywhere: bool
    then: tuple[Term, ...] = ()  # a phrase's later words, in order


def parse(query: str) -> list[Term]:
    """The words and phrases of ``query``, in order."""
    terms: list[Term] = []
    for i, part in enumerate(_QUOTE.split(query)):
        words = [t for t in (_term(w) for w in part.split()) if t is not None]
        if i % 2 == 0:  # outside quotes
            terms += words
            continue
        # Punctuation alone between a phrase's words is skipped like a space.
        words = [t for t in words if any(_is_word(c) for c in t.folded)]
        if words:
            later = tuple(Term(t.text, t.folded, bool(NO_SPACES.search(t.text))) for t in words[1:])
            terms.append(Term(words[0].text, words[0].folded, words[0].anywhere, later))
    return terms


def lookups(terms: list[Term]) -> list[str]:
    """What the store's index is asked for: every word, and of a phrase the words of three
    characters or more (all of them when it has none), since the index can't look up shorter
    ones and ``match`` checks the whole phrase anyway."""
    out: list[str] = []
    for term in terms:
        words = [term.text, *(t.text for t in term.then)]
        out += [w for w in words if len(w) >= 3] or words
    return out


def _term(word: str) -> Term | None:
    text = word.strip(FRAGMENT)
    folded = _folded(text)[0]
    if not folded:
        return None
    anywhere = word.startswith(FRAGMENT) or bool(NO_SPACES.search(text)) or not _is_word(folded[0])
    return Term(text, folded, anywhere)


def match(terms: list[Term], text: str) -> list[tuple[int, int]] | None:
    """Character spans in ``text`` where the terms are found, in order and merged, or None
    when a word that must start a word doesn't, or a phrase isn't there in its order. The index
    has already found every word as a substring, so one that may match anywhere never rules a
    text out, even where Python's folding differs from FTS5's."""
    folded, origin = _folded(text)
    found: list[tuple[int, int]] = []
    for term in terms:
        hits = []
        at = folded.find(term.folded)
        while at != -1:
            if term.anywhere or at == 0 or not _is_word(folded[at - 1]):
                end = _phrase_end(term.then, folded, at + len(term.folded))
                if end is not None:
                    hits.append((at, end))
            at = folded.find(term.folded, at + 1)
        if not hits and (term.then or not term.anywhere):  # the index never saw the order
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


def _phrase_end(words: tuple[Term, ...], folded: str, at: int) -> int | None:
    """Where a phrase ends when its later ``words`` follow from ``at`` in ``folded``, each
    after spaces or punctuation (or right after the one before, where that doesn't cut into a
    word); None when they don't."""
    for word in words:
        gap = at
        while gap < len(folded) and not _is_word(folded[gap]):
            gap += 1
        # A word that begins with punctuation of its own may begin inside the gap.
        start = next((p for p in range(at, gap + 1) if folded.startswith(word.folded, p)), None)
        if start is None:
            return None
        inside = 0 < start < len(folded) and _is_word(folded[start - 1]) and _is_word(
            folded[start])  # fmt: skip
        if inside and not word.anywhere:
            return None
        at = start + len(word.folded)
    return at


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
