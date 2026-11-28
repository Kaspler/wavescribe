# SPDX-License-Identifier: Apache-2.0
"""ntfy as a kind of webhook: a topic on a server the user names gets each alert as its message,
with the title, priority and tags in the headers ntfy reads. Against a stand-in for an ntfy
server on this machine, never the network."""

import json
import threading
from email.header import decode_header, make_header
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.test_outputs import ALERT, NODE, STATIONS, Recorder, outputs, wait_for
from wavescribe.config import Station
from wavescribe.outputs import OutputConfig, OutputError, ntfy_headers, urllib_transport

FAR = {**STATIONS, "c": Station("c", "Радіо Приклад", "uk", ("en",), ())}


class FakeNtfy:
    """A stand-in for an ntfy server, as its publishing API is documented: a POST to
    /<topic> publishes the body as the message, with the title, priority and tags from the
    headers (non-ASCII ones as RFC 2047 encoded words), and answers with the message as JSON.
    With ``token`` set, a request without ``Authorization: Bearer <token>`` gets 401."""

    def __init__(self, token: str | None = None, status: int = 200) -> None:
        got = self.got = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if token and self.headers.get("Authorization") != f"Bearer {token}":
                    return self.answer(401, {"code": 40101, "error": "unauthorized"})
                if status != 200:
                    return self.answer(status, {"error": "not now"})
                message = {
                    "id": f"m{len(got)}", "event": "message", "topic": self.path.strip("/"),
                    "message": body.decode("utf-8"),
                    "title": str(make_header(decode_header(self.headers.get("Title", "")))),
                    "priority": int(self.headers.get("Priority", "3")),
                    "tags": [t for t in self.headers.get("Tags", "").split(",") if t],
                }  # fmt: skip
                got.append(message)
                self.answer(200, message)

            def answer(self, code: int, data: dict) -> None:
                out = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def ntfy_outputs(raw: list[dict], transport, stations=FAR):
    outs = outputs(raw, transport)
    outs._stations = lambda: stations
    return outs


def test_an_alert_reaches_the_topic_with_its_title_priority_tags_and_one_line() -> None:
    server = FakeNtfy()
    outs = ntfy_outputs([{"id": "phone", "kind": "ntfy", "url": f"{server.url}/alerts-k3q9x"}],
                        urllib_transport)  # fmt: skip
    try:
        alert = {**ALERT, "stations": ["c"], "checked": False}
        outs.on_alert(alert, "critical", "Пожежа біля мосту, дим іде на північ.", {"en": "Fire"})
        wait_for(lambda: server.got)
        [message] = server.got
        assert message["topic"] == "alerts-k3q9x"
        assert message["title"] == "CRITICAL, not checked · water · Радіо Приклад"
        assert message["priority"] == 5 and message["tags"] == ["rotating_light", "water"]
        assert message["message"] == (
            "[CRITICAL, not checked] water · Радіо Приклад: Пожежа біля мосту, дим іде на північ."
        )
        wait_for(lambda: outs.status()["phone"]["sent"] == 1)
    finally:
        outs.close()
        server.close()


def test_a_server_that_asks_for_a_token_gets_it_and_the_page_never_sees_it() -> None:
    server = FakeNtfy(token="tk_invented0token")
    raw = {"id": "phone", "kind": "ntfy", "url": f"{server.url}/alerts-k3q9x"}
    without = ntfy_outputs([raw], urllib_transport)
    with_token = ntfy_outputs(
        [{**raw, "headers": {"Authorization": "Bearer tk_invented0token"}}], urllib_transport
    )
    try:
        refused = without.test(without.configs()[0])
        assert not refused["ok"] and refused["status"] == 401 and server.got == []
        entry = with_token.test(with_token.configs()[0])
        assert entry["ok"] and server.got[0]["title"] == "MEDIUM · test"
        assert with_token.configs()[0].public()["headers"] == {"Authorization": "•••"}
    finally:
        without.close()
        with_token.close()
        server.close()


def test_a_busy_server_is_tried_again_and_a_refusal_is_not() -> None:
    rec, sleeps = Recorder([429, 503, 200]), []
    outs = outputs([{"id": "phone", "kind": "ntfy", "url": "https://ntfy.example.org/a-topic"}],
                   rec, sleeps=sleeps)  # fmt: skip
    outs.on_alert(ALERT, "high", "Corte de água", {})
    wait_for(lambda: outs.status()["phone"]["sent"] == 1)
    assert sleeps == [2.0, 4.0] and len({r[2]["X-Wavescribe-Delivery"] for r in rec.requests}) == 1
    rec.statuses = [403]
    outs.on_alert(ALERT, "high", "Corte de água", {})
    wait_for(lambda: outs.status()["phone"]["failed"] == 1)
    assert outs.status()["phone"]["queued"] == 0 and sleeps == [2.0, 4.0]
    outs.close()


def test_the_text_form_puts_the_translation_first_and_the_words_as_heard_after() -> None:
    rec = Recorder()
    outs = outputs([{"id": "phone", "kind": "ntfy", "format": "text", "languages": ["en"],
                     "url": "https://ntfy.example.org/a-topic"}], rec)  # fmt: skip
    outs.on_alert(ALERT, "medium", "Corte de água amanhã.", {"en": "Water cut tomorrow."})
    wait_for(lambda: rec.requests)
    _url, body, headers = rec.requests[0]
    assert body.decode() == "Water cut tomorrow.\n(Corte de água amanhã.)"
    assert str(make_header(decode_header(headers["Title"]))) == "MEDIUM · water · a"
    assert headers["Priority"] == "3" and headers["Title"].isascii()
    outs.close()


def test_headers_from_each_severity_and_a_rule_that_is_no_tag() -> None:
    from wavescribe.events import alert_event

    priorities = {}
    for severity in ("low", "medium", "high", "critical"):
        ev = alert_event({**ALERT, "severity": severity, "change": "ended"}, STATIONS, NODE)
        priorities[severity] = ntfy_headers(ev)["Priority"]
    assert priorities == {"low": "2", "medium": "3", "high": "4", "critical": "5"}
    ev = alert_event({**ALERT, "rule": "água, já"}, STATIONS, NODE)
    headers = ntfy_headers(ev)
    assert headers["Tags"] == "warning" and headers["Title"].startswith("=?UTF-8?B?")
    assert str(make_header(decode_header(headers["Title"]))) == "HIGH · água, já · a"


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"url": "https://ntfy.example.org"}, "an ntfy URL is"),
        ({"url": "https://ntfy.example.org/"}, "an ntfy URL is"),
        ({"url": "https://ntfy.example.org/a topic"}, "an ntfy URL is"),
        ({"url": "https://ntfy.example.org/" + "t" * 65}, "an ntfy URL is"),
        ({"events": ["utterance"]}, "ntfy carries alerts only"),
        ({"format": "json"}, "one line"),
        ({"format": "cap"}, "one line"),
        ({"kind": "pager"}, "kind is one of"),
    ],
)
def test_an_ntfy_output_that_cant_work_is_refused(change: dict, why: str) -> None:
    raw = {"id": "phone", "kind": "ntfy", "url": "https://ntfy.example.org/a-topic", **change}
    with pytest.raises(OutputError, match=why):
        OutputConfig.from_dict(raw)
    ok = OutputConfig.from_dict({"id": "p", "kind": "ntfy", "url": "http://ntfy.lan/x/a_topic-2"})
    assert ok.format == "line" and ok.events == ("alert",) and ok.to_dict()["kind"] == "ntfy"
