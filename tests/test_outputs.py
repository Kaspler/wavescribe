# SPDX-License-Identifier: Apache-2.0
import json
import threading
import time
from pathlib import Path

import pytest

from tests.test_pipeline import station
from tests.test_store import result
from wavescribe.events import alert_event, as_text, utterance_event
from wavescribe.outputs import OutputConfig, OutputError, Outputs, sign
from wavescribe.store import Store

NODE = {"name": "kitchen", "url": "http://mac.local:8766"}
STATIONS = {"a": station("a"), "b": station("b", language="uk")}


class Recorder:
    """A fake HTTP endpoint: records requests, answers with scripted statuses."""

    def __init__(self, statuses: list[int | Exception] | None = None) -> None:
        self.requests: list[tuple[str, bytes, dict[str, str]]] = []
        self.statuses = list(statuses or [])
        self.got = threading.Event()

    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        self.requests.append((url, body, headers))
        self.got.set()
        status = self.statuses.pop(0) if self.statuses else 200
        if isinstance(status, Exception):
            raise status
        return status

    def json(self, i: int = -1) -> dict:
        return json.loads(self.requests[i][1])


def outputs(raw: list[dict], transport, store=None, sleeps=None) -> Outputs:
    return Outputs(
        [OutputConfig.from_dict(r) for r in raw],
        store=store,
        stations=lambda: STATIONS,
        node=lambda: NODE,
        transport=transport,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


def wait_for(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


UTTERANCE = {"id": 7, "station": "a", "t_start": 1764165792.0, "duration": 3.2,
             "language": "pt-PT", "text": "Corte de água", "translations": {"en": "Water cut",
             "uk": "Відключення води"}, "flags": [], "hits": [{"rule": "water"}],
             "audio": True}  # fmt: skip
ALERT = {"id": 3, "rule": "water", "severity": "high", "status": "delivered",
         "first_at": 1764165792.0, "last_at": 1764165800.0, "stations": ["a"],
         "language": "pt-PT", "quote": "Corte de água", "terms": ["corte de água"],
         "mentions": 2, "segment_id": 7, "kind": "keyword"}  # fmt: skip


def test_events_carry_text_station_time_and_links() -> None:
    ev = utterance_event(UTTERANCE, STATIONS, NODE, languages=["en"])
    assert ev["type"] == "utterance" and ev["time"] == "2025-11-26T14:03:12Z"
    assert ev["station"] == {"id": "a", "name": "a", "language": "pt-PT",
                             "privacy": "broadcast"}  # fmt: skip
    assert ev["translations"] == {"en": "Water cut"} and ev["rules"] == ["water"]
    assert ev["audio_url"] == "http://mac.local:8766/api/segments/7/audio.wav"
    al = alert_event(ALERT, STATIONS, NODE, text="Corte de água em Vila Nova",
                     translations={"en": "Water cut in Vila Nova"},
                     context=[UTTERANCE])  # fmt: skip
    assert al["cap_url"] == "http://mac.local:8766/api/alerts/3.cap"
    assert al["text"] == "Corte de água em Vila Nova" and al["context"][0]["id"] == 7
    assert (
        as_text(al, ["en"])
        == "[HIGH] water · a\nWater cut in Vila Nova\n(Corte de água em Vila Nova)"
    )
    assert "change" not in al  # a stored alert; live events and pushes say what changed
    again = alert_event({**ALERT, "change": "reminder"}, STATIONS, NODE)
    assert again["change"] == "reminder"
    assert as_text(again).startswith("[HIGH] water · a (still being mentioned)\n")
    no_url = utterance_event(UTTERANCE, STATIONS, {"name": "x"})
    assert "audio_url" not in no_url


def test_config_validation() -> None:
    ok = OutputConfig.from_dict({"id": "hook", "url": "https://example.test/x"})
    assert ok.events == ("alert",) and ok.min_severity == "medium"
    assert ok.public()["secret"] is False
    for bad in (
        {"id": "Hook", "url": "https://x"},
        {"id": "hook\n", "url": "https://x"},
        {"id": "h", "url": "ftp://x"},
        {"id": "h", "url": "https://x", "events": ["everything"]},
        {"id": "h", "url": "https://x", "format": "cap", "events": ["utterance"]},
        {"id": "h", "url": "https://x", "headers": {"X": "a\nb"}},
    ):
        with pytest.raises(OutputError):
            OutputConfig.from_dict(bad)


def test_alerts_go_to_the_matching_webhooks_signed() -> None:
    rec = Recorder()
    outs = outputs([
        {"id": "all", "url": "http://h/all", "min_severity": "low", "secret": "s3cret"},
        {"id": "water-only", "url": "http://h/water", "rules": ["water"], "format": "text",
         "languages": ["en"]},
        {"id": "power-only", "url": "http://h/power", "rules": ["power"]},
        {"id": "critical", "url": "http://h/crit", "min_severity": "critical"},
        {"id": "station-b", "url": "http://h/b", "stations": ["b"]},
    ], rec)  # fmt: skip
    outs.on_alert(ALERT, "high", "Corte de água", {"en": "Water cut"})
    wait_for(lambda: len(rec.requests) == 2)
    time.sleep(0.1)
    by_url = {url: (body, headers) for url, body, headers in rec.requests}
    assert set(by_url) == {"http://h/all", "http://h/water"}
    body, headers = by_url["http://h/all"]
    assert json.loads(body)["severity"] == "high"
    assert headers["X-Wavescribe-Signature"] == sign("s3cret", headers["X-Wavescribe-Timestamp"],
                                                     body)  # fmt: skip
    assert by_url["http://h/water"][0].decode().startswith("[HIGH] water · a\nWater cut")
    outs.close()


def test_utterances_are_batched_and_alerts_jump_ahead() -> None:
    rec = Recorder()
    outs = outputs([{"id": "feed", "url": "http://central/ingest", "events": ["utterance", "alert"],
                     "batch_s": 0.3, "min_severity": "low"}], rec)  # fmt: skip
    for i in range(3):
        outs.on_utterance({**UTTERANCE, "id": 10 + i})
    outs.on_alert(ALERT, "low", "x", {})
    wait_for(lambda: len(rec.requests) == 2)
    batch, alert = rec.json(0), rec.json(1)
    assert batch["type"] == "batch" and [e["id"] for e in batch["events"]] == [10, 11, 12]
    assert batch["node"]["name"] == "kitchen" and alert["type"] == "alert"
    outs.close()


def test_failures_retry_with_backoff_and_are_logged(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    rec, sleeps = Recorder([503, OSError("connection refused"), 200]), []
    outs = outputs([{"id": "hook", "url": "http://h/x"}], rec, store=store, sleeps=sleeps)
    outs.on_alert(ALERT, "high", "x", {})
    wait_for(lambda: outs.status()["hook"]["sent"] == 1)
    assert sleeps == [2.0, 4.0]
    [entry] = outs.recent("hook")
    assert entry["ok"] and entry["attempts"] == 3 and entry["event"] == "alert"

    rec.statuses = [404]  # a client error isn't retried
    outs.on_alert(ALERT, "high", "x", {})
    wait_for(lambda: outs.status()["hook"]["failed"] == 1)
    assert outs.status()["hook"]["last_error"] == "HTTP 404" and sleeps == [2.0, 4.0]
    outs.close()
    store.close()


def test_alert_context_comes_from_the_store(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    ids = []
    for i, text in enumerate(["Boa tarde.", "Notícias da região.", "Corte de água amanhã."]):
        r = result("a", text, 1000.0 + i * 10)
        ids.append(store.add_transcript(r))
    rec = Recorder()
    outs = outputs([{"id": "hook", "url": "http://h/x", "context": 1}], rec, store=store)
    outs.on_alert({**ALERT, "segment_id": ids[2]}, "high", "Corte de água amanhã.", {})
    wait_for(lambda: rec.requests)
    assert [c["text"] for c in rec.json()["context"]] == ["Notícias da região."]
    outs.close()
    store.close()


def test_test_button_and_reconfigure() -> None:
    rec = Recorder([500])
    outs = outputs([{"id": "hook", "url": "http://h/x"}], rec)
    cfg = outs.configs()[0]
    entry = outs.test(cfg)
    assert not entry["ok"] and entry["status"] == 500 and entry["attempts"] == 1
    assert rec.json()["rule"] == "test"
    outs.configure([OutputConfig.from_dict({"id": "other", "url": "http://h/y",
                                            "events": ["utterance"]})])  # fmt: skip
    assert list(outs.status()) == ["other"]
    outs.on_utterance(UTTERANCE)
    wait_for(lambda: len(rec.requests) == 2)
    assert rec.requests[-1][0] == "http://h/y"
    outs.close()


class Endpoint:
    """A real HTTP server on this machine: answers each request with ``status`` (and Location),
    and records what it got."""

    def __init__(self, status: int = 200, location: str | None = None) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        got = self.got = []

        class Handler(BaseHTTPRequestHandler):
            def answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                got.append((self.command, self.path, dict(self.headers), self.rfile.read(length)))
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = answer

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("code", [300, 301, 302, 303, 307, 308])
def test_a_webhook_that_redirects_failed_and_nothing_goes_elsewhere(code: int) -> None:
    from wavescribe.outputs import urllib_transport

    elsewhere = Endpoint()
    hook = Endpoint(code, f"{elsewhere.url}/collect")
    outs = outputs([{"id": "hook", "url": f"{hook.url}/in", "secret": "s3cret",
                     "headers": {"Authorization": "Bearer abc"}}], urllib_transport)  # fmt: skip
    try:
        entry = outs.test(outs.configs()[0])
        assert len(hook.got) == 1 and hook.got[0][2]["Authorization"] == "Bearer abc"
        assert elsewhere.got == []  # neither the header nor the signature went there
        assert not entry["ok"] and entry["status"] == code and entry["attempts"] == 1
        assert f"redirected to {elsewhere.url}/collect" in entry["error"]
        assert "not followed" in entry["error"]
    finally:
        outs.close()
        hook.close()
        elsewhere.close()


def test_a_redirected_alert_is_not_retried() -> None:
    from wavescribe.httpclient import Redirected

    rec = Recorder([Redirected(302, "https://elsewhere.test/")])
    sleeps: list[float] = []
    outs = outputs([{"id": "hook", "url": "http://h/x"}], rec, sleeps=sleeps)
    outs.on_alert(ALERT, "high", "x", {})
    wait_for(lambda: outs.recent())
    entry = outs.recent()[0]
    assert entry["status"] == 302 and entry["attempts"] == 1 and sleeps == []
    assert "redirected to https://elsewhere.test/" in entry["error"]
    outs.close()
