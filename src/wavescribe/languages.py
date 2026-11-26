# SPDX-License-Identifier: Apache-2.0
"""Language helpers that work for any BCP-47 tag, with no per-language code."""

from __future__ import annotations

import functools
from collections.abc import Iterable


@functools.lru_cache(maxsize=256)
def language_name(tag: str) -> str:
    """English name for a BCP-47 tag from CLDR (via Babel), e.g. "uk" -> "Ukrainian".

    LLM prompts need names: a bare code is ambiguous ("uk" reads as United Kingdom, so a model
    answered in English when asked for Ukrainian). Falls back to the tag itself.
    """
    try:
        from babel import Locale, UnknownLocaleError

        return Locale.parse(tag, sep="-").english_name or tag
    except (ImportError, ValueError, UnknownLocaleError):
        return tag


def primary_subtag(tag: str) -> str:
    """The primary language subtag of a BCP-47 tag, lowercased: "en-GB" -> "en"."""
    return tag.replace("_", "-").split("-", 1)[0].lower()


def same_language(a: str, b: str) -> bool:
    """True when two tags name the same language, ignoring script and region ("en-GB", "en")."""
    return primary_subtag(a) == primary_subtag(b)


def translation_targets(source: str, targets: Iterable[str]) -> tuple[str, ...]:
    """The targets that actually need a translation from ``source``, in order.

    A target in the source language is dropped: an English station needs no English
    translation, and a pt-PT one no "pt". Region variants are treated as the same language.
    """
    return tuple(t for t in targets if not same_language(source, t))
