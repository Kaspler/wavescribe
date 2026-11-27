# SPDX-License-Identifier: Apache-2.0
"""``POST /api/digest``: what some stations said while nobody was listening, or what one is on
about now, as JSON (``wavescribe/digest`` makes it).

    {"stations": ["tsf", "rr"], "minutes": 60, "language": "en"}     what did I miss?
    {"stations": ["tsf"], "minutes": 60, "until": 1772614800}       the hour before then
    {"kind": "now", "stations": ["tsf"], "language": "en"}          what are they on about?

It spends the model as a question does, so it passes the gate a question does (server.py,
``do_POST``): the ``X-Wavescribe`` header, so that another website can't spend the model;
from another machine only with ``serve --allow-remote-ask``; through a proxy, a read token
unless ``--public-read``. Another machine gets a digest of only what may leave this one, over
a few hours at most. The answer comes once the model is done, which can take a few minutes
while live translation goes first. While two digests are being made, the next ones wait in
line for their turn. A digest that isn't made in time, or finds the line full, is refused 503
with code ``busy``: the sentence says what kept the model busy, and ``retry_after_s`` (also the
``Retry-After`` header) when asking again is worth it; with it come ``waited_s`` and
``busy_with``, when the digest waited (``digest.kept_busy``: ``[{"kind", "names",
"seconds"}]``, the most first; no names for another machine). While a digest waits, the
``status`` this machine's page gets (``/api/state`` and the ``status`` events) lists it under
``digests`` (``Digester.status``): by its id, how long it has waited, whether it is in line
behind other digests, waiting for the model behind how many other jobs of which kinds, or
being written, and which of its calls it is on.

Every answer carries the request's ``id``: the asker's (``"id"`` in the request, 8 to 64 of
``A-Z a-z 0-9 - _``, such as a random UUID), or one made for it. With its own id, the asker can
cancel the digest while waiting for it: ``POST /api/digest/<id>/cancel``, past the same gate.
A digest still queued for the model never reaches it; one being written stops at the next
token, and its model time goes back to live translation. The request it cancels is answered
409 with code ``cancelled``. Another machine can cancel only what other machines asked for.

``"speak": true`` also reads it aloud here, in the alerts' voice for its language; the answer
then carries ``speech``: ``{"started": true}``, or ``{"started": false, "error": why, "code":
…}``. That is a control, as starting a station is: this machine only, and never through a
proxy (the speakers are in the room of whoever runs Wavescribe). So are the two routes that
read aloud a digest already made, by its id (the last ``KEPT`` are kept in memory), and stop
reading:

    POST /api/digest/speak          {"id": "…"}  ->  {"id": "…", "speech": {"started": true}}
    POST /api/digest/speech/stop                 ->  {"stopped": true}

Refusals carry a ``code`` beside the sentence where a program needs one (wavescribe/refusal.py).
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from wavescribe import refusal
from wavescribe.digest import Busy, Cancelled, Failed, RequestError, Unavailable
from wavescribe.events import to_json

if TYPE_CHECKING:
    from wavescribe.web.server import Handler, WebHub

PATH = "/api/digest"
SPEAK_PATH = "/api/digest/speak"
STOP_PATH = "/api/digest/speech/stop"
CANCEL_PATH = re.compile(r"/api/digest/(?P<id>[A-Za-z0-9_-]{8,64})/cancel")  # REQUEST_ID
RETRY_AFTER_S = 60

NO_STORE = "a digest needs the store (this run was started with --no-store)"
NO_LLM = "a digest needs an LLM translator such as qwen3.5-9b (none is loaded)"
NOT_SET_UP = "digests aren't set up in this run"
# Why digests are off, as a code beside the sentence in /api/state (digest_code), so the page
# needn't read the words; "remote_off" for another machine without --allow-remote-ask, and
# "unavailable" for a reason this list doesn't know.
CODES = {NO_STORE: "no_store", NO_LLM: "no_llm", NOT_SET_UP: "not_set_up"}
NO_SPEECH = "reading aloud isn't set up in this run"
NOT_KEPT = "no digest with this id is kept here: make it again"
NOT_MADE = "no digest with this id is being made"


def unavailable(hub: WebHub) -> str | None:
    """Why digests can't be made in this run, or None when they can."""
    if hub.store is None:
        return NO_STORE
    if hub.plan.mt is None or hub.plan.mt[0].backend != "mlx-lm":
        return NO_LLM
    if hub.digester is None:
        return NOT_SET_UP
    return None


def digest_fields(reason: str | None) -> dict[str, str | None]:
    """``digest`` (why digests can't be had here, None when they can) and its ``digest_code``,
    as /api/state gives them."""
    from wavescribe.web.server import REMOTE_ASK_OFF

    codes = {**CODES, REMOTE_ASK_OFF: "remote_off"}
    return {
        "digest": reason,
        "digest_code": None if reason is None else codes.get(reason, "unavailable"),
    }


def cancel_id(path: str) -> str | None:
    """The id in ``POST /api/digest/<id>/cancel``, or None for any other path."""
    m = CANCEL_PATH.fullmatch(path)
    return m["id"] if m else None


def handle(h: Handler) -> None:
    """A request in, the digest out (past the gate a question passes)."""
    hub = h.server.hub
    reason = unavailable(hub)
    if reason:
        code = digest_fields(reason)["digest_code"]
        h._json({"error": reason, "code": code}, HTTPStatus.CONFLICT)
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
    except RequestError as e:
        h._json({"error": str(e), **refusal.fields(e)}, HTTPStatus.BAD_REQUEST)
        return
    rid = {"id": request.id}
    try:
        digest = hub.digester.run(request, exports)
    except RequestError as e:  # the id is in use
        h._json({"error": str(e), **refusal.fields(e), **rid}, HTTPStatus.BAD_REQUEST)
    except Unavailable as e:
        h._json({"error": str(e), "code": "no_llm", **rid}, HTTPStatus.CONFLICT)
    except Cancelled as e:
        h._json({"error": str(e), "code": "cancelled", **rid}, HTTPStatus.CONFLICT)
    except Busy as e:
        answer: dict[str, Any] = {"error": str(e), "code": "busy", **rid}
        answer["retry_after_s"] = max(1, round(e.retry_after_s))
        if e.waited_s is not None:
            answer.update(waited_s=round(e.waited_s, 1), busy_with=e.busy_with)
        _busy(h, answer)
    except Failed as e:
        h._json({"error": str(e), "code": "failed", **rid}, HTTPStatus.INTERNAL_SERVER_ERROR)
    else:
        out = digest.payload()
        if speak:
            out["speech"] = (
                hub.speaker.start(digest)
                if hub.speaker is not None
                else {"started": False, "error": NO_SPEECH, "code": "no_speech"}
            )
        h._json(out)


def cancel(h: Handler, request_id: str) -> None:
    """``POST /api/digest/<id>/cancel`` (past the gate a question passes): 200 ``{"id",
    "cancelled": true}``; 409 ``finished`` when it was made already, 404 ``not_found`` when no
    digest has that id (or, for another machine, when this machine asked for it)."""
    digester = h.server.hub.digester
    state = digester.cancel(request_id, here=h._is_local()) if digester is not None else None
    if state == "cancelled":
        h._json({"id": request_id, "cancelled": True})
    elif state == "finished":
        h._json(
            {"error": "the digest was made already", "code": "finished", "id": request_id},
            HTTPStatus.CONFLICT,
        )
    else:
        h._json({"error": NOT_MADE, "code": "not_found", "id": request_id}, HTTPStatus.NOT_FOUND)


def speak(h: Handler) -> None:
    """``POST /api/digest/speak`` ``{"id"}`` (a control: this machine only): read aloud a digest
    already made, without making it again. 200 ``{"id", "speech": {"started": true}}``; 404
    ``not_found`` when it isn't kept (unknown, or one of more than ``KEPT`` since); 409 with
    the reason's code (``digest/speech.py``, ``SPEECH_CODES``) when it can't be read now."""
    hub = h.server.hub
    try:
        body = h._read_json()
    except ValueError:
        body = None
    request_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(request_id, str) or not request_id:
        h._json(
            {"error": 'send {"id": …}: the id a digest\'s answer carries', "code": "bad_id"},
            HTTPStatus.BAD_REQUEST,
        )
        return
    digest = hub.digester.kept(request_id) if hub.digester is not None else None
    if digest is None:
        h._json({"error": NOT_KEPT, "code": "not_found", "id": request_id}, HTTPStatus.NOT_FOUND)
        return
    if hub.speaker is None:
        h._json({"error": NO_SPEECH, "code": "no_speech", "id": request_id}, HTTPStatus.CONFLICT)
        return
    speech: dict[str, Any] = hub.speaker.start(digest)
    if speech.get("started"):
        h._json({"id": request_id, "speech": speech})
    else:
        h._json(
            {"error": speech.get("error"), "code": speech.get("code"), "id": request_id},
            HTTPStatus.CONFLICT,
        )


def stop_speech(h: Handler) -> None:
    """``POST /api/digest/speech/stop`` (a control: this machine only): stop the digest being
    read aloud, at once. ``{"stopped": true}``, or false when none was being read."""
    speaker = h.server.hub.speaker
    h._json({"stopped": speaker.stop() if speaker is not None else False})


def _busy(h: Handler, answer: dict[str, Any]) -> None:
    body = to_json(answer).encode()
    h.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
    h.send_header("Retry-After", str(answer.get("retry_after_s", RETRY_AFTER_S)))
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(body)
