# SPDX-License-Identifier: Apache-2.0
"""Refusals with a code: the sentence is for the person, the ``code`` (and any fields that go
with it) for a program such as the page, so it needn't judge from the words.

A refusal can travel as its sentence alone, as the page's config errors keep them
(``settings.effective_config``): ``said`` keeps the code beside the words in a ``str``, and
``Refusal.of`` makes a refusal of it again.
"""

from __future__ import annotations

from typing import Any, TypeVar

R = TypeVar("R", bound="Refusal")


class Refusal(ValueError):
    """Something that can't be done; ``str()`` says why. ``code`` names the refusal (None: it
    has no name a program needs), and ``extra`` holds the fields that go with it."""

    def __init__(self, message: str = "", code: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra

    @classmethod
    def of(cls: type[R], text: str) -> R:
        """A refusal with the sentence ``text``, and its code when ``said`` kept one."""
        return cls(str(text), getattr(text, "code", None), **getattr(text, "extra", {}))


class Said(str):
    """A refusal's sentence with its code kept beside it (``said``)."""

    code: str | None = None
    extra: dict[str, Any]


def said(e: BaseException) -> str:
    """``e``'s sentence; with its code and fields beside it when it has a code."""
    code = getattr(e, "code", None)
    if code is None:
        return str(e)
    text = Said(str(e))
    text.code, text.extra = code, dict(getattr(e, "extra", {}))
    return text


def fields(e: object) -> dict[str, Any]:
    """A refusal's ``code`` and fields, for the JSON that answers it; none without a code."""
    code = getattr(e, "code", None)
    return {"code": code, **getattr(e, "extra", {})} if code else {}
