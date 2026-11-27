# SPDX-License-Identifier: Apache-2.0
"""The feed's live stream: every final line reaches a client once, live and on resume."""

import http.client
import json
import threading
import time
from pathlib import Path

from tests.test_pipeline import PLAN, station
from tests.test_store import result
from wavescribe.config import StationsConfig
from wavescribe.events import MAX_AHEAD, Seen, resume_id, resume_point
from wavescribe.store import Store
from wavescribe.web.feed import Cursor, Mark
from wavescribe.web.server import WebHub, WebServer


class FeedStream:
    """A client of /feed/v1/events, reading utterance events with their ids."""

    def __init__(self, server: WebServer, headers: dict[str, str] | None = None,
                 path: str = "/feed/v1/events?events=utterance") -> None:  # fmt: skip
        self.conn = http.client.HTTPConnection(*server.server_address, timeout=10)
        self.conn.request("GET", path, headers=headers or {})
        self.resp = self.conn.getresponse()
        self.status = self.resp.status

    def next(self) -> tuple[str | None, dict]:
        event_id, name = None, None
        while True:
            line = self.resp.readline().decode()
            assert line, "the stream ended"
            line = line.rstrip("\n")
            if line.startswith("id: "):
                event_id = line[4:]
            elif line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: ") and name == "utterance":
                return event_id, json.loads(line[6:])

    def ended(self) -> bool:
        """Whether the server ended the stream (reads past keepalives)."""
        while line := self.resp.readline():
            if not line.startswith((b":", b"\n", b"retry:")):
                return False
        return True

    def close(self) -> None:
        self.conn.close()


def feed_server(tmp_path: Path):
    store = Store(tmp_path / "w.db")
    config = StationsConfig((station("a"), station("b", translate_to=())), Path("t.toml"))
    hub = WebHub(config, PLAN, {}, store=store)
    server = WebServer(("127.0.0.1", 0), hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hub, store


def transcribed(hub: WebHub, sid: str, text: str):
    """Heard and transcribed as the app does it: stored, and in flight until ``finish``."""
    r = result(sid, text, time.time() - 30)
    hub.store_transcript(r)
    hub.on_transcript(r)
    return r


def finish(hub: WebHub, store: Store, r, **translations: str) -> None:
    r.translations.update(translations)
    store.add_translations(r)
    hub.on_result(r)


def wait_for(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_lines_that_finish_out_of_order_all_go_out_live_once(tmp_path: Path) -> None:
    server, hub, store = feed_server(tmp_path)
    try:
        client = FeedStream(server)
        um = transcribed(hub, "a", "um")  # station a waits for its translation
        dois = transcribed(hub, "b", "dois")
        finish(hub, store, dois)
        tres = transcribed(hub, "b", "três")
        finish(hub, store, tres)
        finish(hub, store, um, en="one")
        got = [client.next() for _ in range(3)]
        client.close()
        assert [d["text"] for _, d in got] == ["dois", "três", "um"]
        assert got[2][1]["translations"] == {"en": "one"}
        # The id stays below the line still in flight and lists those sent past it.
        low = um.segment_id - 1
        assert [i for i, _ in got] == [
            f"{low},{dois.segment_id}", f"{low},{dois.segment_id},{tres.segment_id}",
            str(tres.segment_id),
        ]  # fmt: skip
    finally:
        hub.close()
        server.shutdown()
        store.close()


def test_a_client_that_reconnects_gets_each_line_it_missed_once_and_finished(
    tmp_path: Path,
) -> None:
    server, hub, store = feed_server(tmp_path)
    try:
        first = FeedStream(server)
        um = transcribed(hub, "a", "um")
        dois = transcribed(hub, "b", "dois")
        finish(hub, store, dois)
        last_id, seen = first.next()
        assert seen["text"] == "dois"
        first.close()
        # While it is away: the older line finishes, a newer one comes and goes, and another
        # is transcribed but still being translated when the client is back.
        finish(hub, store, um, en="one")
        tres = transcribed(hub, "b", "três")
        finish(hub, store, tres)
        quatro = transcribed(hub, "a", "quatro")
        again = FeedStream(server, {"Last-Event-ID": last_id or ""})
        got = [again.next(), again.next()]
        finish(hub, store, quatro, en="four")
        got.append(again.next())
        again.close()
        assert [d["text"] for _, d in got] == ["um", "três", "quatro"]  # no "dois" again
        assert got[0][1]["translations"] == {"en": "one"}
        assert got[2][1]["translations"] == {"en": "four"}  # never sent unfinished
        assert got[-1][0] == str(quatro.segment_id)
        # ?since_id= takes the same id, for a client that opens a new stream itself.
        later = FeedStream(server, path=f"/feed/v1/events?events=utterance&since_id={last_id}")
        assert [later.next()[1]["text"] for _ in range(3)] == ["um", "três", "quatro"]
        later.close()
        bad = FeedStream(server, path="/feed/v1/events?since_id=yesterday")
        assert bad.status == 400
        bad.close()
    finally:
        hub.close()
        server.shutdown()
        store.close()


def open_stream(server: WebServer, path: str, headers: dict[str, str] | None = None):
    """The status of a streaming GET, and its connection (close it)."""
    conn = http.client.HTTPConnection(*server.server_address, timeout=10)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    return resp, conn


PAGE_READS = ["/api/state", "/api/history?station=a", "/api/search?q=x", "/api/alerts",
              "/api/alerts/1.cap", "/api/segments/1/audio.wav", "/api/segments/1/audio.pcm",
              "/audio/a.pcm"]  # fmt: skip


def test_through_a_proxy_the_pages_reads_need_a_token(tmp_path: Path, monkeypatch) -> None:
    from tests.test_app import Running, make_app
    from wavescribe.web.server import Handler

    node = Running(make_app(tmp_path))
    try:
        token = node.app.create_token("viewer", ["read"])["token"]
        pusher = node.app.create_token("pusher", ["push"])["token"]
        bearer = {"Authorization": f"Bearer {token}"}
        node.server.behind_proxy = True  # serve --behind-proxy
        for path in [*PAGE_READS, "/api/events"]:
            resp, conn = open_stream(node.server, path)
            body = resp.read() if resp.status == 401 else b""
            conn.close()
            assert resp.status == 401, path
            assert json.loads(body)["token_required"] is True
            assert resp.getheader("WWW-Authenticate") == 'Bearer realm="wavescribe"'
        for path in PAGE_READS:
            if path != "/audio/a.pcm":  # a live stream: it waits for audio
                assert node.call("GET", path, headers=bearer)[0] != 401, path
        push_only = {"Authorization": f"Bearer {pusher}"}
        assert node.call("GET", "/api/state", headers=push_only)[0] == 401
        # The page's EventSource can't send a header: a ticket made from the token.
        ticket = node.call("POST", "/feed/v1/ticket", headers=bearer)[1]["ticket"]
        resp, conn = open_stream(node.server, f"/api/events?ticket={ticket}")
        assert resp.status == 200
        conn.close()
        # The page itself, its health and its files need nothing.
        assert node.call("GET", "/")[0] == 200 and node.call("GET", "/healthz")[0] == 200

        node.server.public_read = True  # serve --behind-proxy --public-read
        assert node.call("GET", "/api/state")[0] == 200
        assert node.call("GET", "/feed/v1/node")[0] == 401  # the feed still needs a token

        # The LAN reads as before, tokens or not; a proxy's header makes it a proxy's request.
        node.server.behind_proxy = node.server.public_read = False
        monkeypatch.setattr(Handler, "_is_local", lambda self: False)
        assert node.call("GET", "/api/state")[0] == 200
        assert node.call("GET", "/api/state", headers={"X-Forwarded-For": "203.0.113.5"})[0] == 401
    finally:
        node.close()


def test_asking_from_another_machine_is_off_unless_turned_on(tmp_path: Path, monkeypatch) -> None:
    from tests.test_app import Running, make_app
    from wavescribe.web.server import REMOTE_ASK_OFF, Handler

    node = Running(make_app(tmp_path))
    question = {"question": "What was said about water?"}
    try:
        node.app.hub.ask_unavailable = lambda: None  # as if an LLM were loaded
        assert node.call("GET", "/api/state")[1]["ask"] is None
        monkeypatch.setattr(Handler, "_is_local", lambda self: False)
        status, answer = node.call("POST", "/api/ask", question)
        assert status == 403 and "--allow-remote-ask" in answer["error"]
        assert node.call("GET", "/api/state")[1]["ask"] == REMOTE_ASK_OFF  # the page says why
        assert node.call("GET", "/feed/v1/node")[1]["ask"] is False
        node.server.remote_ask = True  # serve --allow-remote-ask
        assert node.call("GET", "/api/state")[1]["ask"] is None
        node.app.hub.asks.acquire()  # every slot busy: past the gate, refused for that
        node.app.hub.asks.acquire()
        node.app.hub.asks.acquire()
        assert node.call("POST", "/api/ask", question)[0] == 429
        node.server.behind_proxy = True  # through a proxy, an answer needs a read token too
        assert node.call("POST", "/api/ask", question)[0] == 401
    finally:
        node.close()


def test_serve_says_at_start_who_can_read_and_ask() -> None:
    from wavescribe.web.server import access_line

    assert access_line("127.0.0.1") == "Access: only this machine can open the page."
    lan = access_line("0.0.0.0")
    assert "network read the page" in lan and "without a token" in lan
    assert "only this machine may ask" in lan
    proxied = access_line("127.0.0.1", behind_proxy=True, remote_ask=True)
    assert "needs a feed token" in proxied and "other machines may ask" in proxied
    public = access_line("127.0.0.1", behind_proxy=True, public_read=True)
    assert "anyone can read the page" in public and "--public-read" in public
    assert all(line.count("\n") == 0 for line in (lan, proxied, public))


def test_a_resume_point_lists_at_most_so_many_ids_and_a_client_skips_repeats() -> None:
    cursor = Cursor(None, lambda station: True)
    waiting = Mark(10 + MAX_AHEAD + 5, ((10, "a"),))  # line 10 still being translated
    ids = [cursor.sent(seg, waiting) for seg in range(11, 11 + MAX_AHEAD + 5)]
    low, have = resume_point(ids[-1]) or (None, frozenset())
    assert low == 9 and len(have) == MAX_AHEAD and min(have) == 16  # 11..15 would come again
    assert resume_point(resume_id(9, have)) == (9, have)
    # A line in flight on a station the client doesn't follow doesn't hold its id back.
    only_b = Cursor(None, lambda station: station == "b")
    assert only_b.sent(11, waiting) == str(waiting.highest)
    for bad in ("", "x", "1,", ",1", "1;2", "²", "9" * 19, "1" + ",2" * (MAX_AHEAD + 1)):
        assert resume_point(bad) is None, bad
    seen = Seen(size=2)
    assert seen.first({"id": 11, "text": "a"}) and not seen.first({"id": 11, "text": "a"})
    assert seen.first({"id": 11, "text": "b"})  # another line under a reused id
    assert seen.first({"id": None, "text": "a"}) and seen.first({"id": None, "text": "a"})
    seen.first({"id": 12, "text": "c"})
    assert seen.first({"id": 11, "text": "a"})  # forgotten: it remembers only so many
