# SPDX-License-Identifier: Apache-2.0
"""Requests to other machines (webhooks, pushes, remote stations, ``wavescribe follow``) never
follow a redirect. urllib follows 301, 302 and 303 for a POST as a GET, and every redirect with
the same headers, so a token, an Authorization header or a signature would go wherever the
answer points. A 3xx is reported instead, with where it pointed (``Redirected``)."""

from __future__ import annotations

import contextlib
import urllib.request
from typing import Any


class Redirected(OSError):
    """The other side answered with a redirect, which isn't followed."""

    def __init__(self, status: int, location: str | None) -> None:
        self.status, self.location = status, location
        where = (location or "nowhere (no Location)")[:200]
        super().__init__(f"HTTP {status}: redirected to {where}; not followed")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any,
                         newurl: str) -> None:  # fmt: skip
        with contextlib.suppress(Exception):
            fp.close()
        raise Redirected(code, headers.get("Location") or newurl)


def opener(*handlers: Any) -> urllib.request.OpenerDirector:
    """An opener like urllib's own (with ``handlers`` added), except that it raises
    ``Redirected`` on 301, 302, 303, 307 and 308 instead of following them."""
    return urllib.request.build_opener(*handlers, _NoRedirects)


def redirected(e: Any) -> Redirected | None:
    """A 3xx that urllib raised as an HTTPError (300, 304, 305…), as ``Redirected``."""
    code = getattr(e, "code", None)
    if isinstance(code, int) and 300 <= code < 400:
        headers = getattr(e, "headers", None)
        return Redirected(code, headers.get("Location") if headers is not None else None)
    return None
