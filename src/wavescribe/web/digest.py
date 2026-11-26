# SPDX-License-Identifier: Apache-2.0
"""``POST /api/digest``: what some stations said while nobody was listening, or what one is on
about now, as JSON (``wavescribe/digest`` makes it).

    {"stations": ["tsf", "rr"], "minutes": 60, "language": "en"}     what did I miss?
    {"kind": "now", "stations": ["tsf"], "language": "en"}          what are they on about?

It reads only, so other machines (``serve --lan``) may ask too, and get a digest of only what
may leave this machine; like asking, it needs the ``X-Wavescribe`` header, so that another
website can't spend the model. The answer comes once the model is done, which can take a
few minutes while live translation goes first.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from wavescribe.digest import Busy, Failed, RequestError, Unavailable

if TYPE_CHECKING:
    from wavescribe.web.server import Handler, WebHub

PATH = "/api/digest"


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
    """A request in, the digest out (the header has been checked)."""
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
    exports = h._exports()
    try:
        request = hub.digester.request(body, exports)
        digest = hub.digester.run(request, exports)
    except RequestError as e:
        h._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
    except Unavailable as e:
        h._json({"error": str(e)}, HTTPStatus.CONFLICT)
    except Busy as e:
        h._json({"error": str(e)}, HTTPStatus.SERVICE_UNAVAILABLE)
    except Failed as e:
        h._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
    else:
        h._json(digest.payload())
