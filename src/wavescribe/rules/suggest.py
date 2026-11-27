# SPDX-License-Identifier: Apache-2.0
"""The user's alert words in each station language, proposed by the LLM for them to check.

The alert path itself never translates: the words are saved per language and matched on the
source text. This only fills the form, and what it fills in is a proposal: a model can offer a
word that means something else, and nothing here can tell.

One question per language, each its own job in the LLM's queue (``Pipeline.request_llm``), so
an alert's translation gets in between them, and a reply that is cut or unreadable costs one
language, not all. Each reply has a token budget sized to the number of words asked for, and
the whole request a time limit: a language the model didn't get to says so. Nothing is asked
twice.

What comes back says, per language, the words or why there are none (``Why``). A word is kept
only if it is written in its language's script, where that is known: the script of the
language's own name in CLDR ("עברית", "українська"), which needs no table of languages. It
catches a word left in the language it was typed in, or spelt out in Latin letters; it can't
catch a wrong word in the right script.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from typing import Any

from wavescribe.languages import language_name
from wavescribe.log import event

log = logging.getLogger(__name__)

MAX_TERMS = 30
MAX_LANGUAGES = 12
MAX_WORDS = 20  # suggestions kept per language
MAX_WORD_CHARS = 60  # longer than this is an explanation, not a word to listen for
PER_TERM = 3  # suggestions asked for per word
MIN_WORDS = 6
# A reply's budget: a suggestion is a few tokens in Latin script and up to about a dozen in
# others, with its quotation marks and comma.
TOKENS_PER_WORD = 16
TOKENS_BASE = 40
TOTAL_S = 120.0  # for the whole request, as the page waits for one answer

# Why a language has no words, or fewer than the model meant to give.
CUT = "cut"  # the reply ran out of its budget
UNREADABLE = "unreadable"  # the reply was no list
BUSY = "busy"  # the LLM's queue had no room
TIMEOUT = "timeout"  # the time for the whole request ran out first
NO_LLM = "no_llm"  # no LLM in this run
NONE = "none"  # the model answered with no words
SCRIPT = "script"  # every word was in another script than the language's

_SAYS = {
    CUT: "the model's reply was cut short",
    UNREADABLE: "the model's reply couldn't be read",
    BUSY: "the model was busy",
    TIMEOUT: "the time ran out before the model got to it",
    NO_LLM: "there is no LLM in this run",
    NONE: "the model had no suggestion",
    SCRIPT: "the model's words weren't in this language's script",
}
# A quoted word: as JSON writes it, or between the quotation marks other scripts use.
_QUOTED = re.compile(
    r'"((?:[^"\\]|\\.)*)"|\u201c([^\u201d]*)\u201d|\u00ab([^\u00bb]*)\u00bb'
    r"|\u300c([^\u300d]*)\u300d|\u300e([^\u300f]*)\u300f"
)
# Scripts that are written together with the one a language's own name is in.
_WITH = {
    "Jpan": {"CJK", "HIRAGANA", "KATAKANA"},
    "Kore": {"HANGUL", "CJK"},
}

Submit = Callable[[Callable[[Any], None]], bool]


def suggest(
    submit: Submit,
    terms: list[str],
    languages: list[str],
    *,
    total_s: float = TOTAL_S,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[threading.Event, float], bool] = lambda done, s: done.wait(s),
) -> dict[str, Any]:
    """Ask the LLM for ``terms`` in each of ``languages``, one queued job per language.
    ``submit(run)`` queues ``run(llm)`` and says whether it could.

    Returns ``terms`` ({language: words}), ``languages`` ({language: {words, why, dropped,
    message}}), ``count`` (words in all), ``empty`` (the languages with none) and ``message``
    (one sentence for the person)."""
    terms = [t.strip() for t in terms if t.strip()][:MAX_TERMS]
    langs = [x for x in dict.fromkeys(languages) if x][:MAX_LANGUAGES]
    started = clock()
    deadline = started + total_s
    per: dict[str, dict[str, Any]] = {}
    stop = None  # why no further language is asked
    for lang in langs:
        if stop is None and clock() >= deadline:
            stop = TIMEOUT
        if stop is not None:
            per[lang] = _entry([], stop)
            continue
        done, box, gave_up = threading.Event(), {}, threading.Event()

        def run(llm: Any, lang=lang, done=done, box=box, gave_up=gave_up) -> None:
            try:
                if not gave_up.is_set():  # nobody waits for it any more
                    box.update(_ask(llm, terms, lang))
            except Exception:
                log.exception("suggesting words in %s failed", lang)
                box.update(_entry([], UNREADABLE))
            finally:
                done.set()

        if not submit(run):
            per[lang] = _entry([], BUSY)
            stop = BUSY  # a full queue is full for the next language too
        elif wait(done, max(0.0, deadline - clock())) and box:
            per[lang] = dict(box)
            if box["why"] == NO_LLM:
                stop = NO_LLM
        else:
            gave_up.set()
            per[lang] = _entry([], TIMEOUT)
            stop = TIMEOUT
    count = sum(len(e["words"]) for e in per.values())
    event(log, "suggest", languages=len(per), count=count,
          why=",".join(sorted({e["why"] for e in per.values() if e["why"]})) or None,
          ms=round((clock() - started) * 1000))  # fmt: skip
    return {
        "terms": {lang: list(e["words"]) for lang, e in per.items()},
        "languages": per,
        "count": count,
        "empty": [lang for lang, e in per.items() if not e["words"]],
        "message": _message(per, count),
    }


def budget(n_terms: int) -> tuple[int, int]:
    """How many suggestions to ask for, and the reply's token budget for them."""
    words = min(MAX_WORDS, max(MIN_WORDS, PER_TERM * n_terms))
    return words, TOKENS_BASE + TOKENS_PER_WORD * words


def prompt(terms: list[str], lang: str) -> str:
    words, _ = budget(len(terms))
    name = language_name(lang)
    return (
        "These words should raise an alert when a radio station says them: "
        + "; ".join(terms)
        + f".\nGive the words and short phrases a broadcaster would use for the same things in "
        f"{name} ({lang}): base forms, written as {name} is written, at most {words} in all, "
        "no explanations. Reply with one JSON array of strings and nothing else."
    )


def read_words(reply: str, lang: str = "") -> tuple[list[str] | None, bool]:
    """The words in a reply, and whether the list is whole. A cut reply gives the words that
    were complete; one with no list in it gives None."""
    text = reply.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    start, end = text.find("["), text.rfind("]")
    for candidate in (text, text[start : end + 1] if 0 <= start < end else ""):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):  # {"de": [...]}: the list for this language, or the only one
            lists = [v for v in data.values() if isinstance(v, list)]
            data = data.get(lang) if isinstance(data.get(lang), list) else (
                lists[0] if len(lists) == 1 else None)  # fmt: skip
        if isinstance(data, list):
            return [w for w in data if isinstance(w, str)], True
    if start < 0:
        return None, False
    body = text[start + 1 :]
    closed = "]" in body
    inside = body.split("]", 1)[0]
    words = []
    for m in _QUOTED.finditer(inside):
        if m.group(1) is None:  # quotation marks of another script: 「…」, «…», “…”
            words.append(next(g for g in m.groups()[1:] if g is not None))
            continue
        try:
            words.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            continue
    if not words and re.search(r"\w", inside):  # a list, but not one of quoted words
        return None, closed
    return words, closed


def in_script(word: str, lang: str) -> bool:
    """Whether ``word`` is written in the script of ``lang``: most of its letters are. True
    when the script isn't known (a language CLDR doesn't have) or the word has no letters."""
    expected = _scripts(lang)
    letters = [_script(c) for c in word if c.isalpha()]
    if not expected or not letters:
        return True
    return 2 * sum(s in expected for s in letters) > len(letters)


def _ask(llm: Any, terms: list[str], lang: str) -> dict[str, Any]:
    if not hasattr(llm, "complete"):
        return _entry([], NO_LLM)
    _, tokens = budget(len(terms))
    reply = llm.complete([{"role": "user", "content": prompt(terms, lang)}], max_tokens=tokens)
    found, whole = read_words(reply, lang)
    cut = getattr(llm, "last_finish_reason", None) == "length" or (
        not whole and "[" in reply and "]" not in reply
    )
    if found is None:
        return _entry([], CUT if cut else UNREADABLE)
    clean = [" ".join(w.split()) for w in found]
    clean = [w for w in dict.fromkeys(clean) if w and len(w) <= MAX_WORD_CHARS]
    words = [w for w in clean if in_script(w, lang)][:MAX_WORDS]
    dropped = len(clean) - len([w for w in clean if in_script(w, lang)])
    if cut:
        why = CUT
    elif words:
        why = None
    elif dropped:
        why = SCRIPT
    else:
        why = NONE if whole else UNREADABLE
    return _entry(words, why, dropped)


def _entry(words: list[str], why: str | None, dropped: int = 0) -> dict[str, Any]:
    """One language's answer. ``why``: why there are no words, or (``cut``) why there may be
    fewer than the model meant; ``dropped``: words left out for their script."""
    if why == CUT and words:
        message = "the model's reply was cut short: there may have been more"
    else:
        message = _SAYS.get(why) if why else None
    return {"words": words, "why": why, "dropped": dropped, "message": message}


def _message(per: dict[str, dict[str, Any]], count: int) -> str:
    empty = [f"{language_name(lang)} ({e['message']})" for lang, e in per.items() if not e["words"]]
    none = f" None for {', '.join(empty)}." if empty else ""
    if not count:
        return "No suggestions came back." + none
    return (
        f"{count} suggested in {len(per) - len(empty)} of {len(per)} languages. They are the "
        "model's proposals and can be wrong: check each one before saving." + none
    )


def _script(char: str) -> str:
    """The script a letter belongs to, as the first word of its Unicode name ("HEBREW")."""
    return re.split(r"[ -]", unicodedata.name(char, "?"), maxsplit=1)[0]


@functools.lru_cache(maxsize=256)
def _scripts(lang: str) -> frozenset[str]:
    """The scripts ``lang`` is written in: that of its own name in CLDR, in each script CLDR
    has the language in ("sr" also as "sr_Latn"), unless the tag names one ("sr-Latn"); and
    the ones written with them. Empty when CLDR doesn't know the language."""
    try:
        from babel import Locale, UnknownLocaleError
        from babel.core import get_global
        from babel.localedata import locale_identifiers

        locale = Locale.parse(lang, sep="-")
        likely = get_global("likely_subtags")
        variants = [locale] if locale.script else [
            locale,
            *(Locale.parse(i) for i in locale_identifiers()
              if re.fullmatch(rf"{locale.language}_[A-Z][a-z]{{3}}", i)),
        ]  # fmt: skip
    except (ImportError, ValueError, UnknownLocaleError):
        return frozenset()
    found: set[str] = set()
    for v in variants:
        found |= {_script(c) for c in (v.get_display_name(v) or "") if c.isalpha()}
        code = v.script or [*likely.get(v.language, "").split("_"), ""][1]
        found |= _WITH.get(code, set())
    return frozenset(found)
