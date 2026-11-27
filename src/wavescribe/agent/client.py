# SPDX-License-Identifier: Apache-2.0
"""A running Wavescribe from another program's side: requests to the routes the page and the
feed use (docs/FEED.md), and ``wavescribe follow``, the live feed as JSON lines.

Requests never go through a proxy. The server takes a request that came through one for
another machine's (``Handler._is_local``), so a proxy set for the whole Mac would make this
machine's own requests lose what only this machine may do.
"""

from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import IO, Any

from wavescribe import __version__
from wavescribe.agent import DEFAULT_URL
from wavescribe.events import Seen, resume_point, to_json
from wavescribe.remote import feed_lines, sse_events
from wavescribe.web.server import CONTROL_HEADER

REQUEST_TIMEOUT_S = 30.0  # also between two pieces of a streamed answer: the server sends
# a comment every 10 s while the model works
STREAM_TIMEOUT_S = 45.0  # silence this long on a live stream means it is gone
MAX_BACKOFF_S = 30.0


def live(events: str) -> str:
    """The ``events`` parameter for a live stream of these events (comma-separated), with
    ``status`` added: the server writes a status event every 10 s whatever else it leaves out,
    while its keepalive waits for 15 s in which nothing at all was published, which a busy
    server never has. Without it, a stream of alerts only goes quiet for minutes and looks
    dead. Readers skip the status events."""
    return f"{events},status"


class Unreachable(Exception):
    """No Wavescribe answered at the URL."""


class Refused(Exception):
    """The server answered with an error status; ``message`` is its own reason."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{message} (HTTP {status})")
        self.status = status
        self.message = message


class Client:
    """Requests to one Wavescribe: by default the one on this machine, which needs no token."""

    def __init__(self, url: str = DEFAULT_URL, token: str | None = None) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def open(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: Any = None,
        *,
        control: bool = False,
        headers: dict[str, str] | None = None,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> http.client.HTTPResponse:
        """The response to one request, for the caller to read and close. ``control`` adds the
        header the page sends with every change and every question, which another website's
        form can't set. Raises ``Unreachable`` or ``Refused``."""
        url = self.url + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
        sent = {"User-Agent": f"wavescribe/{__version__}", **(headers or {})}
        if self.token:
            sent["Authorization"] = f"Bearer {self.token}"
        if control:
            sent[CONTROL_HEADER] = "1"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=sent, method=method)
        try:
            return self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise Refused(e.code, _reason(e)) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            why = getattr(e, "reason", None) or e
            why = getattr(why, "strerror", None) or str(why) or type(why).__name__
            raise Unreachable(
                f"no Wavescribe answers at {self.url} ({why}); start it with `wavescribe serve`"
            ) from None

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        with self.open("GET", path, query) as resp:
            return json.loads(resp.read())

    def post(self, path: str, body: Any) -> Any:
        """A change through the control centre (``/api/manage``), as the page makes it."""
        with self.open("POST", path, body=body, control=True) as resp:
            return json.loads(resp.read())


def _reason(e: urllib.error.HTTPError) -> str:
    """The server's own words for an error: the ``error`` of its JSON, else its text."""
    raw = e.read(4096)
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return data["error"]
    return raw.decode("utf-8", errors="replace").strip()[:300] or str(e.reason)


def follow(
    client: Client,
    query: dict[str, str],
    out: IO[str],
    *,
    sleep: Callable[[float], Any] = time.sleep,
    err: IO[str] | None = None,
) -> int:
    """Write each utterance and alert of the live feed (``/feed/v1/events``) to ``out`` as one
    line of JSON, in the feed's own shape, until interrupted.

    A lost connection is tried again, waiting 1 s and then twice as long each time up to 30 s.
    It resumes from the last event id (``Last-Event-ID``): the feed replays what was stored
    since, and utterances still being translated come when done. One the feed sends again is
    written once (by its ``id``). Alerts raised meanwhile aren't replayed; the store keeps them.

    Returns 1 when no server answers the first time, or when the server refuses (a missing
    token, a bad parameter): trying again wouldn't help. ``BrokenPipeError`` from ``out`` (the
    program reading it has gone) is left to the caller. Notes go to ``err``, stderr by default.
    """
    err = err or sys.stderr
    last_id: str | None = None
    seen = Seen()
    backoff, connected = 1.0, False
    query = {**query, "events": live(query.get("events") or "utterance,alert")}
    while True:
        headers = {"Last-Event-ID": last_id} if last_id else None
        try:
            resp = client.open(
                "GET", "/feed/v1/events", query, headers=headers, timeout=STREAM_TIMEOUT_S
            )
        except Unreachable as e:
            if not connected:
                print(f"wavescribe: {e}", file=err)
                return 1
            why = str(e)
        except Refused as e:
            if e.status < 500:
                print(f"wavescribe: {e}", file=err)
                return 1
            why = str(e)
        else:
            connected, why = True, "the server ended the stream"
            try:
                with resp:
                    for name, data, event_id in sse_events(feed_lines(resp)):
                        if name not in ("utterance", "alert"):
                            continue
                        try:
                            ev = json.loads(data)
                        except ValueError:
                            continue
                        # Only a resume point goes back as Last-Event-ID: a header the HTTP
                        # client refuses would fail every reconnect from then on.
                        if event_id and resume_point(event_id) is not None:
                            last_id = event_id
                        if name == "utterance" and isinstance(ev, dict) and not seen.first(ev):
                            continue  # sent again on a resume
                        out.write(to_json(ev) + "\n")
                        out.flush()
                        backoff = 1.0
            except BrokenPipeError:
                raise  # only writing to ``out`` raises it here: the reader went away
            except (OSError, ValueError, http.client.HTTPException) as e:
                why = str(e) or type(e).__name__
        print(f"wavescribe: lost the feed ({why}); trying again in {backoff:g} s", file=err)
        sleep(backoff)
        backoff = min(MAX_BACKOFF_S, backoff * 2)
