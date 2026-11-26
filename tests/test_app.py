# SPDX-License-Identifier: Apache-2.0
"""The control centre end to end: an App with fake models behind a real HTTP server."""

import http.client
import json
import threading
import time
import tomllib
from pathlib import Path

import numpy as np
import pytest

from tests.test_outputs import Recorder
from tests.test_pipeline import ENTRY, FakeTranscriber, FakeTranslator, ListSource
from tests.test_rules import pack
from tests.test_segmenter import EnergyVad, silence, tone
from wavescribe.app import App
from wavescribe.config import parse_stations
from wavescribe.registry import ModelRegistry
from wavescribe.settings import Settings, effective_config
from wavescribe.store import Store
from wavescribe.web.server import WebServer

BASE = {
    "defaults": {"language": "pt-PT", "translate_to": ["en"]},
    "station": [
        {"id": "a", "name": "Rádio A", "stream": "http://example.test/a"},
        {"id": "b", "name": "Rádio B", "stream": "http://example.test/b"},
    ],
}
ALERTS = """
[delivery]
notify_language = "en"
translation_wait_s = 1
[actions]
high = ["log"]
medium = ["log"]
low = ["log"]
[[rule]]
id = "water"
topics = ["water"]
severity = "high"
"""


class NowSource(ListSource):
    """Like ListSource, with real timestamps (feeds catch up on the last hour)."""

    def __iter__(self):
        from wavescribe.audio import SAMPLE_RATE, AudioChunk

        t0, step = time.time(), SAMPLE_RATE // 2
        for i in range(0, len(self._audio), step):
            yield AudioChunk(self._audio[i : i + step], t_end=t0 + i / SAMPLE_RATE,
                             discontinuity=i == 0)  # fmt: skip


class SpeakingTranscriber(FakeTranscriber):
    def transcribe(self, samples, language):
        from wavescribe.asr import Transcript

        return Transcript("Corte de água amanhã", language, self.model_id)


def registry(tmp_path: Path) -> ModelRegistry:
    reg = ModelRegistry([ENTRY], {"vad": ["m"], "asr": ["m"], "translate": ["m"]},
                        models_dir=tmp_path / "models")  # fmt: skip
    (tmp_path / "models" / "m").mkdir(parents=True)
    (tmp_path / "models" / "m" / "x").write_bytes(b"")
    return reg


def make_app(tmp_path: Path, name: str = "node", *, transcriber=SpeakingTranscriber,
             audio=None, transport=None, start=None) -> App:  # fmt: skip
    root = tmp_path / name
    root.mkdir()
    (root / "alerts.toml").write_text(ALERTS)
    audio = audio if audio is not None else np.concatenate([silence(0.5), tone(2), silence(1)])
    settings = Settings(root / "ui.toml")
    settings.update(lambda d: d.setdefault("node", {}).update(name=name))
    return App(
        parse_stations(BASE, root / "stations.toml"), settings, registry(root),
        store=Store(root / "w.db"), alerts_path=root / "alerts.toml",
        keywords_dir=pack(root), start=start if start is not None else [],
        pipeline_kwargs={
            "source_factory": lambda st: NowSource(st.id, audio),
            "vad_factory": lambda _p: EnergyVad(),
            "transcriber_factory": lambda _e, _p: transcriber(),
            "translator_factory": lambda _e, _p: FakeTranslator(),
        },
        outputs_transport=transport,
    )  # fmt: skip


class Running:
    def __init__(self, app: App) -> None:
        self.app = app
        self.server = WebServer(("127.0.0.1", 0), app.hub)
        app.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.runner = threading.Thread(target=app.run, daemon=True)
        self.runner.start()
        wait_for(lambda: app.pipeline.ready)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def call(self, method: str, path: str, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=15)
        h = {"X-Wavescribe": "1", **(headers or {})}
        data = json.dumps(body) if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        conn.request(method, path, data, h)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw)
        except json.JSONDecodeError:
            return resp.status, raw

    def close(self) -> None:
        self.app.pipeline.stop()
        self.server.shutdown()
        self.app.store.close()


def wait_for(cond, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


@pytest.fixture
def node(tmp_path: Path):
    r = Running(make_app(tmp_path))
    yield r
    r.close()


def test_stations_added_and_changed_from_the_page_persist(node: Running, tmp_path: Path) -> None:
    status, added = node.call("POST", "/api/manage/stations", {
        "name": "Rádio Nova", "stream": "http://example.test/nova", "language": "es-ES",
        "translate_to": ["en", "uk"], "start": True,
    })  # fmt: skip
    assert status == 200 and added["id"] == "radio-nova" and added["origin"] == "page"
    wait_for(lambda: node.app.store.recent("radio-nova"))
    [utt] = node.app.store.recent("radio-nova")
    assert utt["text"] == "Corte de água amanhã"
    wait_for(lambda: len(node.app.store.recent("radio-nova")[0]["translations"]) == 2)

    # A stations.toml station gets an override; the file itself is never touched.
    status, changed = node.call("POST", "/api/manage/stations/a", {"translate_to": ["uk"]})
    assert status == 200 and changed["translate_to"] == ["uk"] and changed["origin"] == "file"
    saved = tomllib.loads((tmp_path / "node" / "ui.toml").read_text())
    assert saved["overrides"]["a"] == {"translate_to": ["uk"]}
    assert saved["run"]["stations"] == ["radio-nova"]  # started stations come back next time
    assert saved["station"][0]["stream"] == "http://example.test/nova"

    status, err = node.call("POST", "/api/manage/stations/a", {"language": "nonsense!"})
    assert status == 400 and "BCP-47" in err["error"]
    status, err = node.call("DELETE", "/api/manage/stations/a")
    assert status == 400 and "stations.toml" in err["error"]
    status, _ = node.call("DELETE", "/api/manage/stations/radio-nova")
    assert status == 200 and "radio-nova" not in {s.id for s in node.app.config.stations}

    # From another machine the control centre refuses, whatever the header says.
    assert node.call("GET", "/api/manage/stations")[0] == 200
    status, _ = node.call("POST", "/api/manage/stations/a", {"priority": 1}, {"X-Wavescribe": ""})
    assert status == 403


def test_status_retention_and_node(node: Running) -> None:
    status, view = node.call("GET", "/api/manage/status")
    assert status == 200
    assert {"pipeline", "system", "store", "history", "outputs"} <= set(view)
    assert view["store"]["retention"]["text_days"] == 0  # no limit given in this test
    status, kept = node.call("POST", "/api/manage/retention", {"text_days": 30})
    assert status == 200 and kept["text_days"] == 30
    assert node.call("POST", "/api/manage/retention", {"text_days": -1})[0] == 400
    named = {"name": "kitchen", "public_url": "http://mac.local:9"}
    status, n = node.call("POST", "/api/manage/node", named)
    assert n == {"name": "kitchen", "url": "http://mac.local:9"}


def test_rules_from_the_page(node: Running) -> None:
    rule = {"id": "bridge", "severity": "high", "terms": {"pt": ["ponte fechada"]}}
    status, view = node.call("POST", "/api/manage/rules", rule)
    assert status == 200 and any(r["id"] == "bridge" and r["origin"] == "page"
                                 for r in view["rules"])  # fmt: skip
    sentence = {"text": "A ponte fechada desde ontem", "language": "pt-PT"}
    status, tried = node.call("POST", "/api/manage/rules/try", sentence)
    assert [h["rule"] for h in tried["hits"]] == ["bridge"]
    status, view = node.call("POST", "/api/manage/rules/water/enable", {"on": False})
    assert next(r for r in view["rules"] if r["id"] == "water")["enabled"] is False
    assert node.call("POST", "/api/manage/rules/try",
                     {"text": "Corte de água", "language": "pt"})[1]["hits"] == []  # fmt: skip
    llm = {"id": "evac", "kind": "llm", "prompt": "people are told to leave", "severity": "high"}
    status, view = node.call("POST", "/api/manage/rules", llm)
    assert status == 200 and [r.id for r in node.app.alerter.config.rules.llm_rules] == ["evac"]
    assert node.call("POST", "/api/manage/rules", {"id": "x", "kind": "llm"})[0] == 400


def test_webhooks_get_alerts_and_the_feed_streams_utterances(tmp_path: Path) -> None:
    rec = Recorder()
    r = Running(make_app(tmp_path, transport=rec))
    try:
        status, out = r.call("POST", "/api/manage/outputs", {
            "id": "home", "url": "http://automation.test/hook", "min_severity": "low",
            "secret": "s3cret",
        })  # fmt: skip
        assert status == 200 and out["secret"] is True  # never sent back
        saved = r.app.settings.get("output")[0]
        assert saved["secret"] == "s3cret"
        r.call("POST", "/api/manage/outputs", {**out, "secret": True, "context": 0})
        assert r.app.settings.get("output")[0]["secret"] == "s3cret"  # kept when masked

        conn = http.client.HTTPConnection(*r.server.server_address, timeout=15)
        conn.request("GET", "/feed/v1/events?events=utterance,alert")
        resp = conn.getresponse()
        assert resp.status == 200
        r.call("POST", "/api/manage/stations/a/start")
        events = []
        while len({e for e, _ in events}) < 2:
            line = resp.readline().decode()
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
                data = json.loads(resp.readline().decode().split(":", 1)[1])
                events.append((name, data))
        conn.close()
        utt = next(d for e, d in events if e == "utterance")
        assert utt["station"]["name"] == "Rádio A" and utt["text"] == "Corte de água amanhã"
        assert utt["node"]["name"] == "node" and utt["rules"] == ["water"]
        alert = next(d for e, d in events if e == "alert")
        assert alert["rule"] == "water"

        wait_for(lambda: rec.requests)
        body = rec.json(0)
        assert body["type"] == "alert" and body["station"]["id"] == "a"
        assert (
            body["text"] == "Corte de água amanhã"
            and "X-Wavescribe-Signature" in rec.requests[0][2]
        )

        status, csv_body = r.call("GET", "/feed/v1/utterances?format=csv&since=0")
        assert status == 200 and b"Corte de" in csv_body and csv_body.startswith(b"id,time")
        status, metrics = r.call("GET", "/metrics")
        assert b'wavescribe_utterances_total{station="a"} 1' in metrics
        assert r.call("GET", "/healthz")[1]["ok"] is True
    finally:
        r.close()


def test_malformed_bodies_get_a_400_not_a_traceback(node: Running) -> None:
    # A field of the wrong type (a number where a name goes).
    status, answer = node.call("POST", "/api/manage/radio/heard/100000000", {"name": 5})
    assert status == 400 and "bad request" in answer["error"]
    # Pushed JSON nested deeply enough to exhaust the parser's recursion (ingest takes 2 MiB;
    # the page's API takes 16 KiB, too little to nest that deep).
    status, tok = node.call("POST", "/api/manage/tokens", {"label": "c", "scopes": ["push"]})
    deep = "[" * 100_000 + "]" * 100_000
    conn = http.client.HTTPConnection(*node.server.server_address, timeout=15)
    conn.request("POST", f"/feed/v1/ingest?token={tok['token']}", deep,
                 {"Content-Type": "application/json"})  # fmt: skip
    resp = conn.getresponse()
    assert resp.status == 400 and b"nested" in resp.read()
    conn.close()


def test_feed_parameters_that_arent_numbers_get_a_400(node: Running) -> None:
    for path in ("/feed/v1/alerts?since=yesterday", "/feed/v1/events?since=yesterday",
                 "/feed/v1/utterances?since=1e", "/feed/v1/utterances?limit=many",
                 "/feed/v1/utterances?since_id=" + "9" * 30):  # fmt: skip
        status, answer = node.call("GET", path)
        assert status == 400 and "error" in answer, path
    assert "since" in node.call("GET", "/feed/v1/alerts?since=yesterday")[1]["error"]
    # An id that isn't a segment number is no id: the stream starts from now.
    conn = http.client.HTTPConnection(*node.server.server_address, timeout=15)
    conn.request("GET", "/feed/v1/events", headers={"Last-Event-ID": "\u00b2"})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.readline() == b"retry: 3000\n"
    conn.close()


def test_csv_cells_cant_be_formulas(node: Running) -> None:
    """Text pushed by another machine may be written to run as a spreadsheet formula."""
    import csv
    import io

    from tests.test_store import result

    said = ['=HYPERLINK("http://evil.example")', "+1+1", "-2", "@SUM(A1)", "\tx", "Olá = bom"]
    for i, text in enumerate(said):
        utterance = result("a", text, time.time() - 10 + i, en=text)
        node.app.store.add_transcript(utterance)
        node.app.store.add_translations(utterance)
    status, body = node.call("GET", "/feed/v1/utterances?format=csv")
    rows = list(csv.DictReader(io.StringIO(body.decode())))
    assert status == 200 and [r["text"] for r in rows] == [
        "'=HYPERLINK(\"http://evil.example\")", "'+1+1", "'-2", "'@SUM(A1)", "'\tx", "Olá = bom",
    ]  # fmt: skip
    assert [r["text_en"] for r in rows] == [r["text"] for r in rows]
    assert rows[0]["duration"] == "1.0" and rows[0]["station"] == "a"  # numbers stay numbers


def test_feed_tokens_guard_other_machines(node: Running, monkeypatch) -> None:
    from wavescribe.web.server import Handler

    status, tok = node.call("POST", "/api/manage/tokens", {"label": "central", "scopes": ["read"]})
    assert status == 200 and tok["token"].startswith("wst_")
    assert "hash" not in json.dumps(node.call("GET", "/api/manage/outputs")[1])
    monkeypatch.setattr(Handler, "_is_local", lambda self: False)  # as if from the LAN
    assert node.call("GET", "/feed/v1/node")[0] == 401
    assert node.call("GET", "/feed/v1/node", headers={"Authorization": "Bearer nope"})[0] == 401
    status, n = node.call("GET", f"/feed/v1/node?token={tok['token']}")
    assert status == 200 and n["name"] == "node"
    push = {"type": "utterance", "station": {"id": "x"}, "text": "hi"}
    assert node.call("POST", f"/feed/v1/ingest?token={tok['token']}", push)[0] == 401  # read only
    assert node.call("GET", "/healthz")[0] == 200
    assert node.call("GET", "/api/manage/stations")[0] == 403


def test_web_pages_on_this_machine_cant_push_or_rebind(node: Running) -> None:
    # Any page open here can post a plain form to localhost: pushing needs a token anyway.
    push = {"type": "utterance", "station": {"id": "x"}, "text": "hi"}
    assert node.call("POST", "/feed/v1/ingest", push)[0] == 401
    _, tok = node.call("POST", "/api/manage/tokens", {"label": "p", "scopes": ["push"]})
    as_text = {"Authorization": f"Bearer {tok['token']}", "Content-Type": "text/plain"}
    conn = http.client.HTTPConnection(*node.server.server_address, timeout=15)
    conn.request("POST", "/feed/v1/ingest", json.dumps(push), as_text)
    assert conn.getresponse().status == 415  # JSON only: a plain form can't be it
    bearer = {"Authorization": f"Bearer {tok['token']}"}
    counts = (200, {"accepted": 1, "rejected": 0})
    assert node.call("POST", "/feed/v1/ingest", push, bearer) == counts
    # DNS rebinding: a website's own name resolving to 127.0.0.1 is refused.
    assert node.call("GET", "/api/manage/stations", headers={"Host": "evil.example:80"})[0] == 421
    assert node.call("POST", "/api/manage/stations/a/stop",
                     headers={"Host": "evil.example"})[0] == 421  # fmt: skip
    for host in ("localhost:8765", "127.0.0.1", "[::1]:8765", "wavescribe.localhost"):
        assert node.call("GET", "/api/manage/stations", headers={"Host": host})[0] == 200


def test_web_pages_on_a_viewers_machine_cant_rebind_either(node: Running, monkeypatch) -> None:
    import socket

    from wavescribe.web.server import Handler

    monkeypatch.setattr(Handler, "_is_local", lambda self: False)  # as if from the LAN
    status, why = node.call("GET", "/api/state", headers={"Host": "evil.example:8765"})
    assert status == 421 and b"public URL" in why
    assert node.call("POST", "/api/ask", {"question": "?"}, {"Host": "evil.example"})[0] == 421
    assert node.call("GET", "/feed/v1/utterances", headers={"Host": "evil.example"})[0] == 421
    typed = ("192.168.1.20:8765", "[fd00::20]:8765", "kitchen-mac.local:8765", "localhost:8765")
    for host in (*typed, socket.gethostname()):
        assert node.call("GET", "/api/state", headers={"Host": host})[0] == 200
    # A name of its own (a proxy, a VPN) is the public URL the owner sets.
    assert node.call("GET", "/api/state", headers={"Host": "radio.example.org"})[0] == 421
    node.app.set_node(None, "https://Radio.example.org/")
    assert node.call("GET", "/api/state", headers={"Host": "radio.example.org"})[0] == 200
    assert node.call("GET", "/api/state", headers={"Host": "evil.example"})[0] == 421


def test_lan_viewers_dont_see_radio_that_stays_here(tmp_path: Path, monkeypatch) -> None:
    """Under --lan the page's own reads follow the privacy classes, as /feed/v1 does."""
    import dataclasses

    from wavescribe.web.server import Handler

    node = Running(make_app(tmp_path, start=["a", "b"]))
    try:
        app = node.app
        wait_for(lambda: app.store.recent("a") and app.store.recent("b"))
        wait_for(lambda: len(app.store.alerts()) >= 1)
        utility = dataclasses.replace(app.config.get("a"), privacy="utility")
        stations = tuple(utility if s.id == "a" else s for s in app.hub.config.stations)
        app.hub.config = dataclasses.replace(app.hub.config, stations=stations)
        assert app.hub.exports().stations == {"b"}  # utility radio isn't exported by default

        monkeypatch.setattr(Handler, "_is_local", lambda self: False)  # as if from the LAN
        state = node.call("GET", "/api/state")[1]
        assert [s["id"] for s in state["stations"]] == ["b"]
        assert {u["station"] for u in state["recent"]} == {"b"}
        assert all(a["quote"] == "" for a in state["alerts"] if "a" in a["stations"])
        assert "a" not in state["status"]["stations"]
        assert node.call("GET", "/api/history?station=a")[1] == []
        assert node.call("GET", "/api/history?station=b")[1]
        found = node.call("GET", "/api/search?q=amanh%C3%A3")[1]
        assert found and {u["station"] for u in found} == {"b"}
        assert node.call("GET", "/audio/a.pcm")[0] == 404
        assert node.call("GET", "/audio/live.x.pcm")[0] == 404  # the Listen tab stays here

        lan, here = app.hub.subscribe(lan=True), app.hub.subscribe()
        app.hub.publish("utterance", {"station": "a", "text": "x"})
        app.hub.publish("utterance", {"station": "b", "text": "y"})

        def said(q) -> list[str]:  # the utterances queued (the status ticks along too)
            items = []
            while not q.empty():
                items.append(q.get_nowait())
            return [text for event, text in items if event == "utterance"]

        assert said(lan) == ['{"station": "b", "text": "y"}']
        assert len(said(here)) == 2  # this machine sees everything
        assert [s["id"] for s in node.call("GET", "/feed/v1/stations")[1]] == ["b"]
        assert node.call("GET", "/feed/v1/node")[1]["stations"] == 1  # what it is shown
        # Paging the feed past a run of hidden rows: they're left out before the limit, so the
        # page isn't empty while more follows.
        from tests.test_store import result

        now = time.time()
        first = app.store.add_transcript(result("a", "oculto um", now))
        app.store.add_transcript(result("a", "oculto dois", now + 1))
        app.store.add_transcript(result("b", "visível", now + 2))
        page = node.call("GET", f"/feed/v1/utterances?since_id={first - 1}&limit=2")[1]
        assert [u["text"] for u in page] == ["visível"]
        # Programs following /feed/v1/events count towards the same cap as the page's viewers.
        from wavescribe.web.server import MAX_REMOTE_SUBSCRIBERS

        held = [app.hub.subscribe(lan=True) for _ in range(MAX_REMOTE_SUBSCRIBERS - 1)]
        assert node.call("GET", "/feed/v1/events")[0] == 503
        for q in held:
            app.hub.unsubscribe(q)
        assert 'station="a"' not in node.call("GET", "/metrics")[1].decode()
        assert "a" not in app.hub.for_lan("status", app.hub.status()).get("lag", {})
        followed = {"stations": {"b": {"connected": False, "remote": "http://u:pw@vpn:8765",
                                       "error": "[Errno 8] nodename nor servname"}}}  # fmt: skip
        assert app.hub.for_lan("status", followed)["stations"] == {"b": {"connected": False}}
        failed = {"stations": ["b"], "actions": {"notify": "ok", "speak:en": "failed: /Users/x/y"}}
        assert app.hub.for_lan("alert", failed)["actions"] == {"notify": "ok", "speak:en": "failed"}
        # The stored alerts too: the page's first load and /api/alerts (feed events have none).
        app.store._db.execute("UPDATE alerts SET actions = ?", (json.dumps(failed["actions"]),))
        for alerts in (node.call("GET", "/api/state")[1]["alerts"],
                       node.call("GET", "/api/alerts")[1]):  # fmt: skip
            assert alerts and all(a["actions"] == {"notify": "ok", "speak:en": "failed"}
                                  for a in alerts)  # fmt: skip

        monkeypatch.setattr(Handler, "_is_local", lambda self: True)
        assert len(node.call("GET", "/api/state")[1]["stations"]) == 2
        assert len(node.call("GET", "/feed/v1/stations")[1]) == 2
    finally:
        node.close()


def test_text_ages_out_while_no_station_is_running(node: Running) -> None:
    from tests.test_store import result

    store = node.app.store
    store.add_transcript(result("a", "há dois dias", time.time() - 2 * 86400))
    assert len(store.recent("a")) == 1  # this run keeps text for good
    assert node.call("POST", "/api/manage/retention", {"text_days": 1})[0] == 200
    wait_for(lambda: store.recent("a") == [])


def kept(store: Store, station_id: str, text: str, privacy: str = "broadcast") -> int:
    """An utterance in the store as if heard a minute ago on a station of that class."""
    import dataclasses

    from tests.test_store import result

    r = result(station_id, text, time.time() - 60)
    r.station = dataclasses.replace(r.station, privacy=privacy)
    segment_id = store.add_transcript(r)
    assert segment_id is not None
    return segment_id


def alert_on(store: Store, station_id: str, quote: str, segment_id: int, privacy: list[str]):
    from wavescribe.alerts import Alert

    now = time.time()
    alert = Alert(
        "water",
        "high",
        [station_id],
        "pt-PT",
        quote,
        ["água"],
        now,
        now,
        segment_ids=[segment_id],
        privacy=privacy,
    )
    store.add_alert(alert)
    return alert


def test_what_a_deleted_station_left_behind_stays_here(tmp_path: Path, monkeypatch) -> None:
    """A station that is no longer configured can't say whether its words may leave: its
    text, alerts and audio aren't given to other machines."""
    from wavescribe.store import AudioArchive
    from wavescribe.web.server import Handler

    node = Running(make_app(tmp_path))
    try:
        store = node.app.store
        store.archive = AudioArchive(tmp_path / "audio")
        ham = kept(store, "ham", "cq cq falta água no repetidor", "amateur")
        gone = kept(store, "gone", "notícias da água de ontem")  # a stream, deleted since
        store.archive.root.mkdir()
        (store.archive.root / "ham.ogg").write_bytes(b"OggS")
        store._saved(ham, "ham.ogg", 4)
        classed = alert_on(store, "ham", "cq cq falta água no repetidor", ham, ["amateur"])
        older = alert_on(store, "gone", "notícias da água de ontem", gone, [])  # no class kept

        # This machine reads all of it.
        found = node.call("GET", "/api/search?q=%C3%A1gua")[1]
        assert {u["station"] for u in found} == {"ham", "gone"}
        assert len(node.call("GET", "/api/history?station=ham")[1]) == 1
        assert all(a["quote"] for a in node.call("GET", "/api/alerts")[1])
        assert store.audio_file(ham) is not None

        monkeypatch.setattr(Handler, "_is_local", lambda self: False)  # as if from the LAN
        assert node.call("GET", "/api/search?q=%C3%A1gua")[1] == []
        assert node.call("GET", "/api/history?station=ham")[1] == []
        assert node.call("GET", "/api/history?station=gone")[1] == []
        state = node.call("GET", "/api/state")[1]
        assert state["recent"] == [] and len(state["alerts"]) == 2
        for a in [*state["alerts"], *node.call("GET", "/api/alerts")[1]]:
            assert (a["quote"], a["terms"], a["segment_id"]) == ("", [], None)
        for a in (classed, older):
            status, cap = node.call("GET", f"/api/alerts/{a.id}.cap")
            assert status == 200 and "água" not in cap.decode()
        assert node.call("GET", f"/api/segments/{ham}/audio.wav")[0] == 404
        assert node.call("GET", f"/api/segments/{ham}/audio.pcm")[0] == 404
        assert node.call("GET", "/feed/v1/utterances?since=0")[1] == []
        assert all(a["text"] == "" for a in node.call("GET", "/feed/v1/alerts")[1])
    finally:
        node.close()


def test_a_retuned_station_keeps_its_old_words_here(tmp_path: Path, monkeypatch) -> None:
    """Station "a" is a broadcast stream now. What was heard under its id on amateur radio is
    judged by the class kept with it, on the page for other machines and in the feed."""
    from wavescribe.ask import SearchPlan
    from wavescribe.web.server import Handler

    node = Running(make_app(tmp_path))
    try:
        app, store = node.app, node.app.store
        ham = kept(store, "a", "cq cq falta água no repetidor", "amateur")
        news = kept(store, "a", "notícias: corte de água amanhã")
        alert_on(store, "a", "cq cq falta água no repetidor", ham, ["amateur"])
        alert_on(store, "a", "notícias: corte de água amanhã", news, ["broadcast"])
        everything = SearchPlan([], 0.0, time.time() + 60, overview=True)

        assert len(node.call("GET", "/api/history?station=a")[1]) == 2
        assert len(app.asker.retrieve(everything)) == 2
        # The feed is what leaves this machine, whoever reads it.
        assert [u["id"] for u in node.call("GET", "/feed/v1/utterances?since=0")[1]] == [news]
        assert [u["id"] for u in node.call("GET", "/feed/v1/utterances?since_id=0")[1]] == [news]
        quotes = {a["text"] for a in node.call("GET", "/feed/v1/alerts")[1]}
        assert quotes == {"", "notícias: corte de água amanhã"}

        monkeypatch.setattr(Handler, "_is_local", lambda self: False)  # as if from the LAN
        assert [u["id"] for u in node.call("GET", "/api/history?station=a")[1]] == [news]
        assert [u["id"] for u in node.call("GET", "/api/search?q=%C3%A1gua")[1]] == [news]
        state = node.call("GET", "/api/state")[1]
        assert [u["id"] for u in state["recent"]] == [news]
        assert {a["quote"] for a in state["alerts"]} == {"", "notícias: corte de água amanhã"}
        assert [u["id"] for u in app.asker.retrieve(everything, app.hub.exports())] == [news]
        assert (
            app.asker.retrieve(
                SearchPlan([], 0.0, time.time() + 60, ["repetidor"]), app.hub.exports()
            )
            == []
        )

        # Live: an utterance still on its way when the station was retuned.
        lan = app.hub.subscribe(lan=True)
        app.hub.publish("utterance", {"station": "a", "privacy": "amateur", "text": "x"})
        app.hub.publish("utterance", {"station": "a", "privacy": "broadcast", "text": "y"})
        app.hub.publish("made-up", {"text": "z"})  # an event nobody decided about stays here
        seen = []
        while not lan.empty():
            seen.append(lan.get_nowait())
        assert [json.loads(text)["text"] for event, text in seen if event != "status"] == ["y"]
    finally:
        node.close()


def test_a_listener_elsewhere_is_cut_off_when_the_station_stops_being_public(
    node: Running, monkeypatch
) -> None:
    import dataclasses

    from wavescribe.web.server import Handler

    hub = node.app.hub
    monkeypatch.setattr(hub, "status", lambda: {"ready": True, "stations": {"a": {}}})
    monkeypatch.setattr(Handler, "_is_local", lambda self: False)  # as if from the LAN
    conn = http.client.HTTPConnection(*node.server.server_address, timeout=15)
    conn.request("GET", "/audio/a.pcm")
    resp = conn.getresponse()
    assert resp.status == 200
    wait_for(lambda: hub.tap("a").listeners == 1)
    hub.tap("a").feed(tone(0.5))
    assert len(resp.read(16000)) == 16000
    # The station is retuned to radio that stays on this machine while they listen.
    utility = dataclasses.replace(hub.config.get("a"), privacy="utility")
    stations = tuple(utility if s.id == "a" else s for s in hub.config.stations)
    hub.config = dataclasses.replace(hub.config, stations=stations)
    hub.tap("a").feed(tone(0.5))
    assert resp.read() == b""
    wait_for(lambda: hub.tap("a").listeners == 0)


def test_security_headers_and_requests_through_a_proxy(tmp_path: Path) -> None:
    node = Running(make_app(tmp_path, start=["a"]))
    try:
        conn = http.client.HTTPConnection(*node.server.server_address, timeout=15)
        conn.request("GET", "/")
        resp = conn.getresponse()
        resp.read()
        assert "frame-ancestors 'none'" in resp.getheader("Content-Security-Policy")
        assert resp.getheader("X-Frame-Options") == "DENY"
        assert resp.getheader("X-Content-Type-Options") == "nosniff"
        assert resp.getheader("Server") == "wavescribe"
        # A control-centre GET with no header, as an <img> on another site would send it.
        conn.request("GET", "/api/manage/status")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 403
        assert node.call("GET", "/api/manage/status")[0] == 200
        # A proxy on this machine forwards other machines' requests.
        fwd = {"X-Forwarded-For": "203.0.113.5"}
        assert node.call("GET", "/api/manage/status", headers=fwd)[0] == 403
        assert node.call("GET", "/api/state", headers={"Via": "1.1 p"})[1]["can_control"] is False
        node.server.behind_proxy = True  # serve --behind-proxy: every request
        assert node.call("GET", "/api/manage/status")[0] == 403
        assert node.call("GET", "/feed/v1/stations")[0] in (401, 403)  # no token made yet
    finally:
        node.close()


def test_a_central_server_follows_one_node_and_takes_pushes_from_another(tmp_path: Path) -> None:
    node = Running(make_app(tmp_path, "node", start=["a"]))
    central = Running(make_app(tmp_path, "central", transcriber=FakeTranscriber))
    try:
        wait_for(lambda: node.app.store.recent("a"))
        # Pull: a remote station on the central server follows the node's station "a".
        status, far = central.call("POST", "/api/manage/stations", {
            "id": "node-a", "name": "A at the node", "remote": node.url,
            "remote_station": "a", "translate_to": ["en", "uk"], "start": True,
        })  # fmt: skip
        assert status == 200 and far["kind"] == "remote"
        wait_for(lambda: central.app.store.recent("node-a"))
        assert central.app.config.get("node-a").privacy == "broadcast"  # as the node said
        got = central.app.store.recent("node-a")[0]
        assert got["text"] == "Corte de água amanhã"
        # English came with it; Ukrainian was added here.
        wait_for(lambda: "uk" in central.app.store.recent("node-a")[0]["translations"])
        assert (
            central.app.store.recent("node-a")[0]["translations"]["en"] == "en:Corte de água amanhã"
        )
        assert central.call("GET", "/api/manage/status")[1]["remote"]["node-a"]["connected"]
        # The central server's own rules run on the text it follows.
        wait_for(lambda: central.app.store.alerts())
        assert central.app.store.alerts()[0]["rule"] == "water"

        # Push: the node pushes everything to the central server's ingest with a push token.
        _, tok = central.call("POST", "/api/manage/tokens", {"label": "n", "scopes": ["push"]})
        status, _ = node.call("POST", "/api/manage/outputs", {
            "id": "central", "url": f"{central.url}/feed/v1/ingest",
            "events": ["utterance"], "headers": {"Authorization": f"Bearer {tok['token']}"},
        })  # fmt: skip
        assert status == 200
        node.call("POST", "/api/manage/stations/b/start")
        wait_for(lambda: "node-b" in {s.id for s in central.app.config.stations})
        wait_for(lambda: central.app.store.recent("node-b"))
        pushed = next(s for s in central.app.stations_view() if s["id"] == "node-b")
        assert pushed["remote"] == "push" and pushed["name"] == "Rádio B (node)"
        assert "node-b" in central.app.hub.exports().stations
    finally:
        central.close()
        node.close()


def heard(station: str, text: str = "Olá", **more) -> dict:
    """An utterance event as another machine pushes it."""
    return {"type": "utterance", "station": {"id": station, "name": station.upper(),
                                             "language": "pt-PT"},
            "t_start": time.time(), "duration": 2.0, "language": "pt-PT", "text": text,
            **more}  # fmt: skip


def push(app: App, events: list[dict], node: str = "north") -> dict:
    return app.ingest({"type": "batch", "node": {"name": node}, "events": events})


@pytest.fixture
def central(tmp_path: Path):
    """An App that takes pushes; nothing of its own is started."""
    app = make_app(tmp_path, "central")
    yield app
    app.pipeline.stop()
    app.close()
    app.store.close()


def test_a_push_only_writes_into_stations_that_pushing_made(central: App) -> None:
    # A station of this machine whose id is what node "north" + station "1" would get.
    central.add_station({"id": "north-1", "name": "Ours", "stream": "http://example.test/n"})
    assert push(central, [heard("1", "forged"), heard("tsf")]) == {"accepted": 1, "rejected": 1}
    assert central.store.recent("north-1") == []
    assert [u["text"] for u in central.store.recent("north-tsf")] == ["Olá"]
    made = central.config.get("north-tsf")
    assert (made.remote, made.remote_station, made.name) == ("push", "tsf", "TSF (north)")
    assert push(central, [heard("tsf", "Bom dia")]) == {"accepted": 1, "rejected": 0}
    untagged = {**heard("es"), "station": {"id": "es", "language": "not a tag"}}
    assert push(central, [untagged]) == {"accepted": 0, "rejected": 1}

    # Two of a node's stations whose ids here come out the same: the first keeps the id.
    long = "n" * 38
    assert push(central, [heard("ab"), heard("ac", "other")], long)["rejected"] == 1
    assert [u["text"] for u in central.store.recent(f"{long}-a")] == ["Olá"]


def test_pushed_stations_are_capped_and_saved_once_a_batch(central: App, monkeypatch) -> None:
    monkeypatch.setattr("wavescribe.app.MAX_PUSH_STATIONS_PER_NODE", 2)
    monkeypatch.setattr("wavescribe.app.MAX_PUSH_STATIONS", 3)
    saves = []
    write = central.settings._write
    monkeypatch.setattr(central.settings, "_write", lambda data: (saves.append(1), write(data)))
    batch = [heard("one"), heard("two"), heard("three"), heard("one", "again")]
    assert push(central, batch) == {"accepted": 3, "rejected": 1}
    assert len(saves) == 1
    assert {s.id for s in central.config.stations if s.remote} == {"north-one", "north-two"}
    assert push(central, [heard("x"), heard("y")], "south") == {"accepted": 1, "rejected": 1}
    assert push(central, [heard("z")], "east") == {"accepted": 0, "rejected": 1}  # full
    assert push(central, [heard("two")]) == {"accepted": 1, "rejected": 0}


def test_pushed_values_are_checked_before_anything_is_kept(central: App) -> None:
    events = [
        heard("tsf", t_start=float("inf")),
        heard("tsf", t_start=time.time() + 86400),
        heard("tsf", duration=float("nan")),
        heard("tsf", text="a" * 100_000),
        {**heard("tsf"), "station": "tsf"},
        heard("tsf", "Bom dia"),
    ]
    assert push(central, events) == {"accepted": 1, "rejected": 5}
    assert [u["text"] for u in central.store.recent("north-tsf")] == ["Bom dia"]
    # A sender whose own description is malformed still gets an answer, not a crash.
    assert central.ingest({"type": "batch", "node": "north", "events": [heard("tsf")]}) == {
        "accepted": 1, "rejected": 0,
    }  # fmt: skip
    assert central.config.get("remote-tsf").remote == "push"


def classed(sid: str, cls: str | None, text: str = "Olá") -> dict:
    """A pushed utterance whose station states a privacy class, or none."""
    station = {"id": sid, "language": "pt-PT"} | ({"privacy": cls} if cls else {})
    return {**heard(sid, text), "station": station}


def test_pushed_text_is_handled_by_the_class_its_sender_gave_it(central: App) -> None:
    of = classed
    batch = [of("fm", "broadcast"), of("2m", "amateur"), of("pmr", "private"),
             of("script", None), of("sea", "maritime")]  # fmt: skip
    assert push(central, batch) == {"accepted": 5, "rejected": 0}
    classes = {s.id: s.privacy for s in central.config.stations if s.remote}
    # A sender that states no class is taken to send what may be passed on; a class this
    # version doesn't know is handled as private.
    assert classes == {"north-fm": "broadcast", "north-2m": "amateur", "north-pmr": "private",
                       "north-script": "broadcast", "north-sea": "private"}  # fmt: skip
    # Only broadcast is shown to other machines and sent on, as for radio heard here.
    pushed = {s.id for s in central.config.stations if s.remote}
    assert central.hub.exports().stations & pushed == {"north-fm", "north-script"}
    assert central.store.recent("north-pmr") == []  # private radio is never written down
    assert [u["text"] for u in central.store.recent("north-script")] == ["Olá"]
    assert [u["text"] for u in central.store.recent("north-2m")] == ["Olá"]
    # The classes are there after a restart, before anything new is heard.
    again, _ = effective_config(central.base, Settings(central.settings.path).snapshot())
    assert {s.id: s.privacy for s in again.stations if s.remote} == classes

    # The sender retunes a station: its class here follows what it says from then on.
    saves = []
    central.settings.observe(saves.append)
    assert push(central, [of("fm", "utility", "um"), of("fm", "utility", "dois")])["accepted"] == 2
    assert central.config.get("north-fm").privacy == "utility" and len(saves) == 1
    push(central, [of("fm", None, "três")])  # an event that doesn't say changes nothing
    assert central.config.get("north-fm").privacy == "utility" and len(saves) == 1
    central.delete_station("north-fm")
    assert "north-fm" not in central.settings.get("remote_privacy")


def test_a_sender_that_starts_stating_a_stricter_class_tightens_its_station(
    central: App, monkeypatch
) -> None:
    import dataclasses

    from wavescribe.radio import bandplan

    assert push(central, [classed("scan", None, "before")])["accepted"] == 1
    assert central.config.get("north-scan").privacy == "broadcast"
    assert "north-scan" in central.hub.exports().stations
    assert push(central, [classed("scan", "amateur", "after")])["accepted"] == 1
    assert central.config.get("north-scan").privacy == "amateur"
    assert "north-scan" not in central.hub.exports().stations  # none of it is shown elsewhere
    push(central, [classed("scan", None, "later")])  # saying nothing again relaxes nothing
    assert central.config.get("north-scan").privacy == "amateur"
    # What was stored before the sender said so keeps the retention it was stored with; what
    # came after ages out with its class.
    gone = dataclasses.replace(bandplan.POLICIES["amateur"], text_days=0.0)
    monkeypatch.setitem(bandplan.POLICIES, "amateur", gone)
    central.store.prune_short_lived()
    assert [u["text"] for u in central.store.recent("north-scan")] == ["before"]


def test_a_pushed_name_with_control_characters_leaves_a_ui_toml_that_loads(central: App) -> None:
    station = {"id": "tsf", "name": "T\x7fS\x00F\n", "language": "pt-PT"}
    assert push(central, [{**heard("tsf"), "station": station}])["accepted"] == 1
    again = Settings(central.settings.path)  # as the next start reads it
    assert again.get("station")[0]["name"] == "T\x7fS\x00F\n (north)"


def test_a_stored_time_that_is_not_a_number_doesnt_stop_the_page(node: Running) -> None:
    """A database written before pushed values were checked may hold one."""
    from tests.test_store import result

    def not_json(constant: str) -> None:
        raise AssertionError(f"{constant} is not JSON")

    node.app.store.add_transcript(result("a", "Olá", float("inf")))
    for path in ("/api/state", "/api/history?station=a", "/feed/v1/utterances?since_id=0",
                 "/feed/v1/utterances?since_id=0&format=ndjson"):  # fmt: skip
        conn = http.client.HTTPConnection(*node.server.server_address, timeout=15)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode()
        assert resp.status == 200, path
        for line in body.splitlines():
            json.loads(line, parse_constant=not_json)
        assert "Olá" in body, path


def test_receivers_parse_dongles_and_spectrum() -> None:
    from wavescribe.config import SdrDevice
    from wavescribe.sources.sdr import parse_dongles, spectrum

    text = (
        "Found 2 device(s):\n  0:  Realtek, RTL2838UHIDIR, SN: 00001234\n"
        "  1:  Realtek, RTL2838UHIDIR, SN: 00001239\n\nUsing device 0: Generic RTL2832U\n"
    )
    assert [d["serial"] for d in parse_dongles(text)] == ["00001234", "00001239"]
    dev = SdrDevice("a", "00001234", 94_000_000, 20.0, 2_400_000)
    t = np.arange(65536) / 2.4e6
    iq = (0.5 * np.exp(2j * np.pi * 300e3 * t)).astype(np.complex64)  # a carrier 300 kHz up
    s = spectrum(iq, dev, 1.0, 0.0)
    assert len(s["db"]) == 512 and s["center_hz"] == 94_000_000
    peak = int(np.argmax(s["db"]))
    assert abs((peak - 256) * 2.4e6 / 512 - 300e3) < 2 * 2.4e6 / 512


def test_the_page_offers_the_control_centre_only_to_this_machine(
    node: Running, monkeypatch
) -> None:
    from importlib import resources

    from wavescribe.web.server import Handler

    status, page = node.call("GET", "/")
    assert status == 200 and b'<script type="module"' in page
    # The control centre is a chunk of the built page, served like every asset.
    assets = resources.files("wavescribe.web").joinpath("dist", "assets")
    chunks = [f.name for f in assets.iterdir() if f.name.endswith(".js")]
    served = [node.call("GET", f"/assets/{name}") for name in chunks]
    assert all(status == 200 for status, _ in served)
    assert any(b"/api/manage" in js for _, js in served)
    assert node.call("GET", "/assets/../server.py")[0] == 404
    assert node.call("GET", "/api/state")[1]["manage"] is True
    monkeypatch.setattr(Handler, "_is_local", lambda self: False)
    assert node.call("GET", "/api/state")[1]["manage"] is False


def test_dongles_are_added_and_removed_from_the_page(node: Running) -> None:
    dongle = {"id": "x", "serial": "SN0042", "center_mhz": 95.0, "gain_db": 20}
    status, dev = node.call("POST", "/api/manage/receivers", dongle)
    assert status == 200 and dev["origin"] == "page" and dev["span_mhz"] == [93.92, 96.08]
    assert node.call("POST", "/api/manage/receivers", {"id": "y", "serial": "1"})[0] == 400
    for bad in ({"serial": "00000001", "center_mhz": 98, "gain_db": 30}, {**dongle, "id": "z "}):
        assert node.call("POST", "/api/manage/receivers", bad)[0] == 400  # nothing half-saved
    assert len(node.app.settings.get("device")) == 1
    station = {"id": "s", "name": "S", "language": "es", "stream": "http://x/s"}
    assert node.call("POST", "/api/manage/stations", station)[0] == 200
    status, view = node.call("GET", "/api/manage/receivers")
    assert [d["id"] for d in view["devices"]] == ["x"]
    assert node.call("DELETE", "/api/manage/receivers/x")[0] == 200
    assert node.call("GET", "/api/manage/receivers")[1]["devices"] == []


def test_language_detection_ignores_music(monkeypatch) -> None:
    """A song in another language mustn't decide a station's language: only speech is kept."""
    import wavescribe.gate.yamnet as yamnet
    from wavescribe.admin import speech_only
    from wavescribe.gate import Verdict

    class LoudIsMusic:
        def __init__(self, path) -> None:
            pass

        def classify(self, samples):
            loud = float(np.abs(samples).mean()) > 0.3
            return Verdict(
                "music" if loud else "speech", 0.0 if loud else 0.9, 0.9 if loud else 0.0
            )

    class Reg:
        def resolve(self, capability):
            return None, Path("yamnet.tflite")

    monkeypatch.setattr(yamnet, "YamnetGate", LoudIsMusic)
    sr = 16000
    music, speech = np.full(8 * sr, 0.5, np.float32), np.full(4 * sr, 0.1, np.float32)
    kept, music_s = speech_only(Reg(), np.concatenate([music, speech]))
    assert len(kept) == 4 * sr and music_s == 8.0
    assert speech_only(Reg(), music) == (None, 8.0)


def test_a_filtered_event_stream_still_gets_its_keepalive(node: Running, monkeypatch) -> None:
    from wavescribe.web import feed

    monkeypatch.setattr(feed, "KEEPALIVE_S", 0.3)
    conn = http.client.HTTPConnection(*node.server.server_address, timeout=5)
    conn.request("GET", "/feed/v1/events?events=alert")
    resp = conn.getresponse()
    assert resp.readline() == b"retry: 3000\n"
    stop = threading.Event()

    def chatter() -> None:  # utterances flow all the time; this client takes only alerts
        while not stop.is_set():
            node.app.hub.publish("utterance", {"station": "a", "text": "x", "final": True})
            time.sleep(0.05)

    threading.Thread(target=chatter, daemon=True).start()
    try:
        assert b": keepalive\n" in [resp.readline() for _ in range(3)]
    finally:
        stop.set()
        conn.close()


def test_a_station_a_stricter_region_switched_off_says_why(tmp_path: Path) -> None:
    """147 MHz is amateur radio in the Americas and unlisted (private) in Europe: under the
    page's region the station stays off until confirmed, and says that, not "on the page"."""
    raw = {
        "defaults": {"language": "en-US"},
        "radio": {"region": "2"},
        "device": [{"id": "v", "serial": "00001234", "center_mhz": 147.25, "gain_db": 20}],
        "station": [{"id": "repeater", "mhz": 147.0, "device": "v"}],
    }
    settings = Settings(tmp_path / "ui.toml")
    settings.update(lambda d: d.setdefault("radio", {}).update(region="cept"))
    models = registry(tmp_path)

    def view() -> dict:
        app = App(parse_stations(raw, tmp_path / "stations.toml"), settings, models,
                  alerts_enabled=False, start=[])  # fmt: skip
        try:
            [station] = app.stations_view()
        finally:
            app.close()
        assert station["disabled"] and "acknowledge = true" in station["error"]
        return station

    assert view()["reason"].startswith("switched off until you confirm")
    settings.update(lambda d: d.setdefault("overrides", {}).update(repeater={"disabled": True}))
    assert view()["reason"] == "switched off on the page"  # and the page switched it off too
