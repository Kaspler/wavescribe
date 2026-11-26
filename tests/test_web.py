# SPDX-License-Identifier: Apache-2.0
import http.client
import json
import threading
import time
from pathlib import Path

import numpy as np

from tests.helpers import requires_ffmpeg
from tests.test_pipeline import (
    PLAN,
    EnergyVad,
    FakeTranscriber,
    FakeTranslator,
    ListSource,
    silence,
    station,
    tone,
)
from wavescribe.config import StationsConfig
from wavescribe.pipeline import Pipeline, fan_out
from wavescribe.store import Store
from wavescribe.web.server import Handler, WebHub, WebServer


def start_server(
    audio: np.ndarray, store: Store | None = None, transcriber=lambda _e, _p: FakeTranscriber()
):
    config = StationsConfig((station("a"), station("b")), Path("t.toml"))
    hub = WebHub(config, PLAN, {"b": "no asr model for 'pt-PT'"}, store=store)
    pipeline = Pipeline(
        [],
        PLAN,
        fan_out(store and store.add_translations, hub.on_result),
        on_transcript=fan_out(store and store.add_transcript, hub.on_transcript),
        on_audio=hub.on_audio,
        source_factory=lambda st: ListSource(st.id, audio),
        vad_factory=lambda _p: EnergyVad(),
        transcriber_factory=transcriber,
        translator_factory=lambda _e, _p: FakeTranslator(),
    )
    hub.attach(pipeline)
    server = WebServer(("127.0.0.1", 0), hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    runner = threading.Thread(target=lambda: pipeline.run(until_idle=False), daemon=True)
    runner.start()
    return server, hub, pipeline


def request(
    server: WebServer, method: str, path: str, headers: dict[str, str] | None = None
) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(*server.server_address, timeout=10)
    if headers is None:
        headers = {"X-Wavescribe": "1"} if method == "POST" else {}
    conn.request(method, path, headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.read()


def test_page_state_start_station_and_live_events() -> None:
    audio = np.concatenate([silence(0.5), tone(2), silence(1), tone(2), silence(1)])
    server, hub, pipeline = start_server(audio)
    try:
        status, body = request(server, "GET", "/")
        assert status == 200 and b"<title>Wavescribe</title>" in body

        status, body = request(server, "GET", "/api/state")
        state = json.loads(body)
        assert state["can_control"] is True
        by_id = {s["id"]: s for s in state["stations"]}
        assert by_id["b"]["available"] is False and "no asr model" in by_id["b"]["reason"]

        # Subscribe to live events, then start a station.
        conn = http.client.HTTPConnection(*server.server_address, timeout=10)
        conn.request("GET", "/api/events")
        events = conn.getresponse()
        assert events.status == 200

        # A cross-site form can't set the header, so a POST without it is refused.
        assert request(server, "POST", "/api/stations/a/start", headers={})[0] == 403
        assert request(server, "POST", "/api/stations/a/start") == (200, b'{"ok": true}')
        assert request(server, "POST", "/api/stations/b/start")[0] == 409

        seen: list[dict] = []
        event = None
        while len([u for u in seen if u["final"]]) < 2:
            line = events.readline().decode().strip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event == "utterance":
                seen.append(json.loads(line[5:]))
        partial = next(u for u in seen if not u["final"])
        assert partial["pending"] == ["en"] and partial["translations"] == {}
        finals = [u for u in seen if u["final"]]
        assert [u["translations"] for u in finals] == [{"en": "en:2s"}, {"en": "en:2s"}]
        assert finals[0]["key"] == "a:s1" and finals[0]["latency_ms"] is not None
        conn.close()

        recent = json.loads(request(server, "GET", "/api/state")[1])["recent"]
        assert [u["key"] for u in recent] == ["a:s1", "a:s2"]
        assert request(server, "GET", "/audio/b.pcm")[0] == 409
    finally:
        hub.close()
        pipeline.stop()
        server.shutdown()


def wait_for_finals(hub: WebHub, n: int, timeout: float = 15) -> list[dict]:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        finals = [u for u in hub.state()["recent"] if u["final"] and u["translations"]]
        if len(finals) >= n:
            return finals
        time.sleep(0.05)
    raise AssertionError(f"fewer than {n} finished utterances")


@requires_ffmpeg
def test_store_backs_history_search_and_replay(tmp_path: Path) -> None:
    from wavescribe.store import AudioArchive

    audio = np.concatenate([silence(0.5), tone(2), silence(1), tone(3), silence(1)])
    # The fake source's clock starts near 1970: keep "old" audio so retention doesn't prune it.
    archive = AudioArchive(tmp_path / "audio", max_age_days=10**6)
    store = Store(tmp_path / "w.db", archive=archive)
    server, hub, pipeline = start_server(audio, store)
    try:
        assert request(server, "POST", "/api/stations/a/start")[0] == 200
        finals = wait_for_finals(hub, 2)
        assert [u["text"] for u in finals] == ["2s", "3s"]
        assert all(u["key"] == f"a:{u['id']}" for u in finals)  # live keys match stored ones

        before = finals[1]["id"]
        status, body = request(server, "GET", f"/api/history?station=a&before_id={before}")
        assert status == 200 and [u["text"] for u in json.loads(body)] == ["2s"]

        hits = json.loads(request(server, "GET", "/api/search?q=en%3A3s")[1])
        assert [h["text"] for h in hits] == ["3s"] and hits[0]["field"] == "translation"

        import time

        deadline = time.time() + 15
        while store.counts()["audio_segments"] < 2 and time.time() < deadline:
            time.sleep(0.1)
        status, pcm = request(server, "GET", f"/api/segments/{finals[0]['id']}/audio.pcm")
        assert status == 200 and abs(len(pcm) / 2 / 16000 - 2.0) < 0.6
        assert request(server, "GET", "/api/segments/999/audio.pcm")[0] == 404
    finally:
        hub.close()
        pipeline.stop()
        server.shutdown()
        store.close()


def test_lan_viewers_are_read_only(monkeypatch) -> None:
    server, hub, pipeline = start_server(np.concatenate([tone(2), silence(1)]))
    monkeypatch.setattr(Handler, "_is_local", lambda _self: False)
    try:
        assert json.loads(request(server, "GET", "/api/state")[1])["can_control"] is False
        status, body = request(server, "POST", "/api/stations/a/start")
        assert status == 403 and b"read-only" in body
        assert hub.status()["stations"] == {}
    finally:
        hub.close()
        pipeline.stop()
        server.shutdown()


def test_check_port_refuses_a_port_another_program_holds_on_loopback() -> None:
    import socket

    import pytest

    from wavescribe.web.server import PortInUseError, check_port

    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = other.getsockname()[1]
        with pytest.raises(PortInUseError, match=str(port)):
            check_port("0.0.0.0", port)  # the wildcard bind alone would have succeeded on macOS
    check_port("127.0.0.1", port)  # free again


def test_history_is_kept_per_station() -> None:
    from wavescribe.web import server as web

    hub = WebHub(StationsConfig((station("a"), station("b")), Path("t.toml")), PLAN, {})
    for i in range(web.RECENT_PER_STATION + 5):
        hub._remember({"key": f"a:{i}", "station": "a", "t_start": 100.0 + i})
    hub._remember({"key": "b:0", "station": "b", "t_start": 50.0})
    recent = hub.state()["recent"]
    assert recent[0]["key"] == "b:0"  # the quiet station's line survives the chatty one
    assert len(recent) == web.RECENT_PER_STATION + 1


def test_audio_tap_fans_out_pcm_only_to_listeners() -> None:
    from wavescribe.web.audio import AudioTap

    tap = AudioTap("a")
    tap.feed(tone(0.5))  # nobody listening: nothing is converted or queued
    q1, q2 = tap.add_listener(), tap.add_listener()
    tap.feed(np.array([0.0, 0.5, -1.0, 2.0], dtype=np.float32))
    expected = np.array([0, 16383, -32767, 32767], dtype="<i2").tobytes()
    assert q1.get_nowait() == expected and q2.get_nowait() == expected
    tap.remove_listener(q1)
    tap.close()
    assert q2.get_nowait() is None and tap.listeners == 0


def test_alerts_feed_and_cap_export(tmp_path: Path) -> None:
    from tests.test_alerts import make, settle
    from tests.test_store import result

    store = Store(tmp_path / "w.db")
    alerter, *_ = make(tmp_path, {"en": "Water cut tomorrow in Vila Nova."}, store=store)
    r = result("a", "Corte de água em Vila Nova amanhã.", 1000.0)
    store.add_transcript(r)
    alerter.on_transcript(r)
    settle(alerter)
    server, hub, pipeline = start_server(np.zeros(1, dtype=np.float32), store)
    try:
        status, body = request(server, "GET", "/api/alerts")
        [a] = json.loads(body)
        assert status == 200 and a["rule"] == "water" and a["summaries"]["en"]
        state = json.loads(request(server, "GET", "/api/state")[1])
        assert [x["id"] for x in state["alerts"]] == [a["id"]]
        assert state["recent"][0]["hits"][0]["rule"] == "water"  # highlights survive a reload
        status, xml = request(server, "GET", f"/api/alerts/{a['id']}.cap")
        assert status == 200 and b"urn:oasis:names:tc:emergency:cap:1.2" in xml
        assert request(server, "GET", "/api/alerts/999.cap")[0] == 404
    finally:
        hub.close()
        pipeline.stop()
        server.shutdown()
        store.close()


def idle_server(store: Store | None = None, *, behind_proxy: bool = False):
    hub = WebHub(StationsConfig((station("a"),), Path("t.toml")), PLAN, {}, store=store)
    server = WebServer(("127.0.0.1", 0), hub, behind_proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hub


def test_history_opens_around_one_utterance_with_a_bounded_limit(tmp_path: Path) -> None:
    """Show in the transcript, from an alert or a search hit: the words around it."""
    from tests.test_store import result

    store = Store(tmp_path / "w.db")
    ids = [store.add_transcript(result("a", f"frase {i}", 1000.0 + i)) for i in range(400)]
    server, hub = idle_server(store)

    def history(query: str) -> list[int]:
        status, body = request(server, "GET", f"/api/history?station=a&{query}")
        assert status == 200
        return [u["id"] for u in json.loads(body)]

    try:
        assert history(f"around_id={ids[200]}") == ids[176:226]  # 50 by default
        assert history(f"around_id={ids[200]}&limit=5") == ids[198:203]
        assert history(f"around_id={ids[200]}&limit=5000") == ids[51:351]  # at most 300
        assert history(f"around_id={ids[399]}&limit=3") == ids[397:]
        assert history("limit=2") == ids[398:]  # the live end, also bounded
        assert history(f"before_id={ids[10]}&limit=3") == ids[7:10]
        assert len(history("around_id=x")) == 300  # not an id: the latest, as before
    finally:
        hub.close()
        server.shutdown()
        store.close()


def test_a_connection_that_sends_nothing_is_closed_and_streams_go_on(monkeypatch) -> None:
    import socket

    from wavescribe.web import server as web

    monkeypatch.setattr(web, "REQUEST_TIMEOUT_S", 0.3)
    server, hub = idle_server()
    try:
        conn = http.client.HTTPConnection(*server.server_address, timeout=10)
        conn.request("GET", "/api/events")
        events = conn.getresponse()
        with socket.create_connection(server.server_address, timeout=10) as stalled:
            stalled.sendall(b"GET /api/state HT")  # and then nothing
            assert stalled.recv(100) == b""  # hung up on, long before the client gives up
        time.sleep(0.4)  # the stream has been silent for longer than the timeout
        hub.publish("status", {"ready": True})
        lines = [events.readline().decode().strip() for _ in range(4)]
        assert "event: status" in lines
        conn.close()
    finally:
        hub.close()
        server.shutdown()


def test_a_client_that_trickles_its_request_is_cut_off(monkeypatch) -> None:
    import contextlib
    import socket

    from wavescribe.web import server as web

    monkeypatch.setattr(web, "REQUEST_DEADLINE_S", 0.5)  # each byte comes well within 30 s
    server, hub = idle_server()
    stop = threading.Event()
    sent = b"GET /api/state HTTP/1.1\r\nHost: localhost\r\nX-Pad: " + b"a" * 400

    def trickle(conn: socket.socket) -> None:
        for byte in sent:
            if stop.wait(0.02):
                return
            try:
                conn.send(bytes([byte]))
            except OSError:
                return  # hung up on

    try:
        with socket.create_connection(server.server_address, timeout=5) as slow:
            threading.Thread(target=trickle, args=(slow,), daemon=True).start()
            started = time.monotonic()
            with contextlib.suppress(ConnectionResetError):  # closed with bytes still unread
                assert slow.recv(100) == b""  # no answer: the connection is closed
            assert time.monotonic() - started < 3  # the whole request would take 9 s
        stop.set()
        assert request(server, "GET", "/api/state")[0] == 200  # one sent in one go still works
    finally:
        stop.set()
        hub.close()
        server.shutdown()


def test_other_machines_get_only_so_many_connections(monkeypatch) -> None:
    import socket

    import pytest

    from wavescribe.web import server as web

    monkeypatch.setattr(web, "MAX_REMOTE_CONNECTIONS", 2)
    monkeypatch.setattr(web, "MAX_REMOTE_REFUSALS", 1)
    server, hub = idle_server(behind_proxy=True)  # every connection is another machine's
    own, own_hub = idle_server()
    try:
        held = [socket.create_connection(server.server_address) for _ in range(2)]
        status, body = request(server, "GET", "/api/state")
        assert status == 503 and b"too many connections" in body
        turned_away = socket.create_connection(server.server_address)  # yet to be told
        with pytest.raises(ConnectionError):  # no thread, no answer
            request(server, "GET", "/api/state")
        for conn in (*held, turned_away):
            conn.close()
        deadline = time.time() + 5
        while (status := request(server, "GET", "/api/state")[0]) != 200:
            assert time.time() < deadline
            time.sleep(0.05)

        held = [socket.create_connection(own.server_address) for _ in range(4)]
        assert request(own, "GET", "/api/state")[0] == 200  # this machine is never counted
        for conn in held:
            conn.close()
    finally:
        for h, s in ((hub, server), (own_hub, own)):
            h.close()
            s.shutdown()


def test_requests_a_proxy_here_forwards_count_against_the_cap(monkeypatch) -> None:
    from wavescribe.web import server as web

    monkeypatch.setattr(web, "MAX_REMOTE_CONNECTIONS", 1)
    server, hub = idle_server()  # no --behind-proxy: the proxy's address is this machine's
    forwarded = {"X-Forwarded-For": "203.0.113.9"}
    try:
        conn = http.client.HTTPConnection(*server.server_address, timeout=10)
        conn.request("GET", "/api/events", headers=forwarded)  # holds its connection
        assert conn.getresponse().status == 200
        status, body = request(server, "GET", "/api/state", forwarded)
        assert status == 503 and b"too many connections" in body
        assert request(server, "GET", "/api/state")[0] == 200  # this machine is never counted
        conn.close()
    finally:
        hub.close()
        server.shutdown()


def test_other_machines_get_only_so_many_live_streams(monkeypatch) -> None:
    from wavescribe.web import server as web

    monkeypatch.setattr(web, "MAX_REMOTE_SUBSCRIBERS", 1)
    monkeypatch.setattr(Handler, "_is_local", lambda _self: False)
    server, hub = idle_server()
    try:
        conn = http.client.HTTPConnection(*server.server_address, timeout=10)
        conn.request("GET", "/api/events")
        assert conn.getresponse().status == 200
        status, body = request(server, "GET", "/api/events")
        assert status == 503 and b"too many pages" in body
        assert request(server, "GET", "/api/state")[0] == 200  # the rest of the page works
        hub.unsubscribe(hub.subscribe())  # and so does following along on this machine
        conn.close()
    finally:
        hub.close()
        server.shutdown()


def test_closing_the_hub_ends_every_live_stream() -> None:
    hub = WebHub(StationsConfig((station("a"),), Path("t.toml")), PLAN, {})
    here, there = hub.subscribe(), hub.subscribe(lan=True)
    hub.close()
    # None tells a stream's handler to end its response: on this machine and on the others.
    assert here.get_nowait() is None and there.get_nowait() is None


def test_other_machines_get_only_so_many_recordings_decoded_at_once(
    tmp_path: Path, monkeypatch
) -> None:
    from tests.test_store import result
    from wavescribe.store import AudioArchive

    store = Store(tmp_path / "w.db", archive=AudioArchive(tmp_path / "audio"))
    segment = store.add_transcript(result("a", "Bom dia", 1000.0))
    assert segment is not None and store.archive is not None
    store.archive.root.mkdir(parents=True, exist_ok=True)  # the archive's thread may not have yet
    (store.archive.root / "a.ogg").write_bytes(b"OggS")
    store._saved(segment, "a.ogg", 4)
    decoding, finish = threading.Event(), threading.Event()

    def slow_decode(_path: Path) -> bytes:
        decoding.set()
        assert finish.wait(10)
        return bytes(3200)

    monkeypatch.setattr(store.archive, "decode", slow_decode)
    server, hub = idle_server(store)
    hub.decodes = threading.BoundedSemaphore(1)
    answers: list[int] = []
    wav = f"/api/segments/{segment}/audio.wav"
    try:
        first = threading.Thread(target=lambda: answers.append(request(server, "GET", wav)[0]))
        monkeypatch.setattr(Handler, "_is_local", lambda _self: False)
        first.start()
        assert decoding.wait(10)
        status, body = request(server, "GET", wav)
        assert status == 503 and b"try again" in body
        monkeypatch.setattr(Handler, "_is_local", lambda _self: True)
        finish.set()
        assert request(server, "GET", wav)[0] == 200  # this machine never waits its turn
        first.join(10)
        assert answers == [200]
        monkeypatch.setattr(Handler, "_is_local", lambda _self: False)
        assert request(server, "GET", wav)[0] == 200  # the place is free again
    finally:
        finish.set()
        hub.close()
        server.shutdown()
        store.close()


def test_a_time_window_goes_on_by_since_id_and_misses_nothing(tmp_path: Path) -> None:
    from tests.test_store import result

    store = Store(tmp_path / "w.db")
    now = time.time()
    # Station b runs a few seconds behind: its utterance starts before a's second one, and is
    # stored after it.
    for sid, t in (("a", now - 30), ("a", now - 20), ("b", now - 25), ("a", now - 10)):
        store.add_transcript(result(sid, f"{sid} at {t:.0f}", t))
    config = StationsConfig((station("a"), station("b")), Path("t.toml"))
    hub = WebHub(config, PLAN, {}, store=store)
    server = WebServer(("127.0.0.1", 0), hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        path, seen = f"/feed/v1/utterances?since={now - 60:.0f}&limit=2", []
        while page := json.loads(request(server, "GET", path)[1]):
            seen += [e["id"] for e in page]
            path = f"/feed/v1/utterances?since_id={page[-1]['id']}&limit=2"
        assert seen == [1, 2, 3, 4]
    finally:
        hub.close()
        server.shutdown()
        store.close()


def test_ids_in_urls_take_plain_digits_only(tmp_path: Path) -> None:
    import socket

    store = Store(tmp_path / "w.db")
    server, hub = idle_server(store)

    def status_of(path: bytes) -> bytes:
        with socket.create_connection(server.server_address, timeout=10) as conn:
            conn.sendall(b"GET " + path + b" HTTP/1.0\r\n\r\n")
            return conn.makefile("rb").readline().split()[1]

    try:
        # "²" counts as a digit for str.isdigit(), and int() refuses it.
        assert status_of(b"/api/history?station=a&before_id=%C2%B2") == b"200"
        assert status_of(b"/api/segments/\xb2/audio.wav") == b"404"
        assert status_of(b"/api/segments/\xb2/audio.pcm") == b"404"
        assert status_of(b"/api/alerts/\xb2.cap") == b"404"
    finally:
        hub.close()
        server.shutdown()
        store.close()


def test_why_a_model_did_not_load_stays_on_this_machine(monkeypatch) -> None:
    def unloadable(_entry, _path):
        raise OSError("no such file: /home/owner/models/whisper/weights.npz")

    audio = np.concatenate([silence(0.5), tone(2), silence(1)])
    server, hub, pipeline = start_server(audio, transcriber=unloadable)
    try:
        assert request(server, "POST", "/api/stations/a/start")[0] == 200
        deadline = time.time() + 15
        while not hub.status()["models"]["errors"]:
            assert time.time() < deadline
            time.sleep(0.05)
        here = json.loads(request(server, "GET", "/api/state")[1])["status"]["models"]["errors"]
        [(model, why)] = here.items()
        assert "/home/owner" in why

        monkeypatch.setattr(Handler, "_is_local", lambda _self: False)
        errors = json.loads(request(server, "GET", "/api/state")[1])["status"]["models"]["errors"]
        assert list(errors) == [model] and "owner" not in errors[model]
        assert "log on the Wavescribe machine" in errors[model]
        live = hub.for_lan("status", hub.status())
        assert live is not None and "owner" not in json.dumps(live)
    finally:
        hub.close()
        pipeline.stop()
        server.shutdown()
