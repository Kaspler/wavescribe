# SPDX-License-Identifier: Apache-2.0
"""``POST /api/digest``: what some stations said while nobody was listening, or what one is on
about now, as JSON (``wavescribe/digest`` makes it).

    {"stations": ["tsf", "rr"], "minutes": 60, "language": "en"}     what did I miss?
    {"kind": "now", "stations": ["tsf"], "language": "en"}          what are they on about?

It spends the model as a question does, so it passes the gate a question does (server.py,
``do_POST``): the ``X-Wavescribe`` header, so that another website can't spend the model;
from another machine only with ``serve --allow-remote-ask``; through a proxy, a read token
unless ``--public-read``. Another machine gets a digest of only what may leave this one, over
a few hours at most. The answer comes once the model is done, which can take a few minutes
while live translation goes first; when two digests are being made, it is refused at once
with ``Retry-After``.

``"speak": true`` also reads it aloud here, in the alerts' voice for its language; the answer
then carries ``speech``: ``{"started": true}``, or ``{"started": false, "error": why}``. That
is a control, as starting a station is: this machine only, and never through a proxy (the
speakers are in the room of whoever runs Wavescribe).
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from wavescribe.digest import Busy, Failed, RequestError, Unavailable
from wavescribe.events import to_json

if TYPE_CHECKING:
    from wavescribe.web.server import Handler, WebHub

PATH = "/api/digest"
RETRY_AFTER_S = 60


def unavailable(hub: WebHub) -> str | None:
    """Why digests can't be made in this run, or None when they can."""
    if hub.store is None:
        return "a digest needs the store (this run was started with --no-store)"
    if hub.plan.mt is None or hub.plan.mt[0].backend != "mlx-lm":
        return "a digest needs an LLM translator such as qwen3.5-9b (none is loaded)"
    if hub.digester is None:
        return "digests aren't set up in this run"
    return None


def handle(h: Handler) -> None:
    """A request in, the digest out (past the gate a question passes)."""
    hub = h.server.hub
    reason = unavailable(hub)
    if reason:
        h._json({"error": reason}, HTTPStatus.CONFLICT)
        return
    assert hub.digester is not None
    try:
        body = h._read_json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        h._json(
            {"error": 'send a JSON object, e.g. {"stations": ["tsf"], "minutes": 60}'},
            HTTPStatus.BAD_REQUEST,
        )
        return
    exports = h._exports()  # None: this machine, not through a proxy
    speak = body.get("speak", False)
    if not isinstance(speak, bool):
        h._json({"error": "speak is true or false"}, HTTPStatus.BAD_REQUEST)
        return
    if speak and not h._is_local():
        h._json(
            {"error": "only the Wavescribe machine itself can have a digest read aloud"},
            HTTPStatus.FORBIDDEN,
        )
        return
    try:
        request = hub.digester.request(body, exports, remote=exports is not None)
        digest = hub.digester.run(request, exports)
    except RequestError as e:
        h._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
    except Unavailable as e:
        h._json({"error": str(e)}, HTTPStatus.CONFLICT)
    except Busy as e:
        _busy(h, str(e))
    except Failed as e:
        h._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
    else:
        out = digest.payload()
        if speak:
            out["speech"] = (
                hub.speaker.start(digest)
                if hub.speaker is not None
                else {"started": False, "error": "reading aloud isn't set up in this run"}
            )
        h._json(out)


def _busy(h: Handler, why: str) -> None:
    body = to_json({"error": why}).encode()
    h.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
    h.send_header("Retry-After", str(RETRY_AFTER_S))
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(body)
