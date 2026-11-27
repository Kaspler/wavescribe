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
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from wavescribe import __version__, httpclient
from wavescribe.agent import DEFAULT_URL
from wavescribe.digest import TIMEOUT_S as DIGEST_WAIT_S
from wavescribe.events import Seen, resume_point, to_json
from wavescribe.remote import feed_lines, sse_events
from wavescribe.web.server import CONTROL_HEADER

REQUEST_TIMEOUT_S = 30.0  # also between two pieces of a streamed answer: the server sends
# a comment every 10 s while the model works
STREAM_TIMEOUT_S = 45.0  # silence this long on a live stream means it is gone
DIGEST_TIMEOUT_S = DIGEST_WAIT_S + 30  # the server gives up first, and says why
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
        from wavescribe.control import headers_for

        self.url = url.rstrip("/")
        self.token = token
        # This machine's control key, for a Wavescribe on this machine only: behind a proxy
        # it is what tells this machine's requests from another's (wavescribe.control).
        self._local = headers_for(self.url)
        # No redirects: the token would go wherever one points.
        self._opener = httpclient.opener(urllib.request.ProxyHandler({}))

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
        sent = {"User-Agent": f"wavescribe/{__version__}", **self._local, **(headers or {})}
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
        except httpclient.Redirected as e:
            raise Refused(e.status, str(e)) from None
        except urllib.error.HTTPError as e:
            moved = httpclient.redirected(e)
            raise Refused(e.code, str(moved) if moved else _reason(e)) from None
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

    def digest(self, body: dict[str, Any]) -> Any:
        """A digest (``POST /api/digest``), once the server's model has made it."""
        try:
            resp = self.open(
                "POST", "/api/digest", body=body, control=True, timeout=DIGEST_TIMEOUT_S
            )
        except Refused as e:
            if e.status != 404:  # the route never answers 404: a Wavescribe from before it
                raise
            raise Refused(
                404, f"the Wavescribe at {self.url} makes no digests; update it and restart it"
            ) from None
        with resp:
            try:
                return json.loads(resp.read())
            except ValueError:
                raise Refused(
                    resp.status,
                    f"what answers at {self.url} isn't Wavescribe; give its address with --url",
                ) from None


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


class ResumeFile:
    """``follow --resume FILE``: the feed's resume point after the last utterance printed, kept
    between runs (docs/AGENT_TOOLS.md, *Stopping and going on*). One line of text, replaced
    whole after each line printed (a new file synced and renamed over it), so a run stopped at
    any moment, even killed, leaves the point after the last line or the one before it, never
    part of one: a line may come twice after a restart, none is lost."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> str | None:
        """The point kept; None when there is none yet (no file, or an empty one).
        ``ValueError`` when the file can't be read or holds something else."""
        try:
            text = self.path.read_text("ascii").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as e:
            raise ValueError(f"can't read {self.path}: {e}") from None
        if text and resume_point(text) is None:
            raise ValueError(
                f"{self.path} doesn't hold a resume point (like 195,197); remove it to start afresh"
            )
        return text or None

    def start(self, since_id: str | None) -> str | None:
        """Where a run starts: the point kept, else ``since_id``. It is written back at once,
        so a FILE that can't be written fails now rather than at the first line printed."""
        point = self.read() or since_id
        if point is not None:
            try:
                self.write(point)
            except OSError as e:
                raise ValueError(f"can't write {self.path}: {e.strerror or e}") from None
        elif not os.access(self.path.parent, os.W_OK):
            raise ValueError(
                f"can't write {self.path}: the folder {self.path.parent} isn't there or can't "
                "be written"
            )
        return point

    def write(self, point: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}-")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(point + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


class _Unkept(Exception):
    """The resume point couldn't be kept (``follow``'s ``keep`` failed): not an ``OSError``,
    which would read as a lost connection."""


def follow(
    client: Client,
    query: dict[str, str],
    out: IO[str],
    *,
    since_id: str | None = None,
    keep: Callable[[str], Any] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    err: IO[str] | None = None,
) -> int:
    """Write each utterance and alert of the live feed (``/feed/v1/events``) to ``out`` as one
    line of JSON, in the feed's own shape, until interrupted. Each utterance has ``resume``
    added: the feed's event id for it (``events.resume_point``), from which a new run goes on
    after that line without losing one that finished later.

    It starts after ``since_id`` (a resume point or a segment id), and a lost connection is
    tried again, waiting 1 s and then twice as long each time up to 30 s. It resumes from the
    last resume point (``Last-Event-ID``): the feed replays what was stored since, and
    utterances still being translated come when done. One the feed sends again is written once
    (by its ``id``). Alerts raised meanwhile aren't replayed; the store keeps them.

    ``keep`` is given the resume point after each utterance written, once it is written
    (``ResumeFile.write``); when it fails, the run stops. Stopped by Ctrl-C or SIGTERM, or by a
    refusal once connected, it says on ``err`` (stderr by default) how to go on.

    Returns 1 when no server answers the first time, or when the server refuses (a missing
    token, a bad parameter): trying again wouldn't help. ``BrokenPipeError`` from ``out`` (the
    program reading it has gone) is left to the caller.
    """
    err = err or sys.stderr
    last_id = since_id  # the resume point after the last utterance written
    seen = Seen()
    backoff, connected = 1.0, False
    query = {**query, "events": live(query.get("events") or "utterance,alert")}

    def stopped(code: int) -> int:
        if connected and last_id:
            print(
                f"wavescribe: run follow again with --since-id {last_id} to go on after the "
                "last line printed",
                file=err,
            )
        return code

    try:
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
                    return stopped(1)
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
                            if name != "utterance" or not isinstance(ev, dict):
                                out.write(to_json(ev) + "\n")
                                out.flush()
                                backoff = 1.0
                                continue
                            # Only a resume point goes back as Last-Event-ID: a header the
                            # HTTP client refuses would fail every reconnect from then on.
                            point = event_id if resume_point(event_id) is not None else None
                            if seen.first(ev):  # else sent again on a resume
                                out.write(to_json({**ev, "resume": point}) + "\n")
                                out.flush()
                                backoff = 1.0
                            # Moved on only once the line is out: stopped in between, the
                            # point kept and said is the one before, and the line comes twice.
                            if point is not None and point != last_id:
                                last_id = point
                                if keep is not None:
                                    try:
                                        keep(point)
                                    except OSError as e:
                                        raise _Unkept(f"can't keep the resume point: {e}") from e
                except BrokenPipeError:
                    raise  # only writing to ``out`` raises it here: the reader went away
                except _Unkept as e:
                    print(f"wavescribe: {e}; stopped", file=err)
                    return stopped(1)
                except (OSError, ValueError, http.client.HTTPException) as e:
                    why = str(e) or type(e).__name__
            print(f"wavescribe: lost the feed ({why}); trying again in {backoff:g} s", file=err)
            sleep(backoff)
            backoff = min(MAX_BACKOFF_S, backoff * 2)
    except (KeyboardInterrupt, SystemExit):  # Ctrl-C; SIGTERM exits through SystemExit
        stopped(0)
        raise
