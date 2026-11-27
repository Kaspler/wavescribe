# SPDX-License-Identifier: Apache-2.0
import argparse
import http.server
import json
import socket
import threading
from datetime import datetime
from pathlib import Path

import pytest

from tests.test_agent import (
    ALERT_KEYS,
    DIGEST_COUNT_KEYS,
    DIGEST_POINT_KEYS,
    DIGEST_WINDOW_KEYS,
    TOOL_RESULT_KEYS,
    UTTERANCE_KEYS,
    Mcp,
    url_of,
)
from tests.test_digest import NOW, FakeLlm, kept, stocked
from tests.test_digest_web import Serving
from wavescribe.agent.mcp import NOT_A_WARNING
from wavescribe.alerts import Alert
from wavescribe.cli import main
from wavescribe.digest.cli import render, since


def test_since_takes_a_time_of_day_or_seconds() -> None:
    evening = datetime(2026, 3, 4, 22, 0)
    assert since("21:30", now=evening) == datetime(2026, 3, 4, 21, 30).timestamp()
    assert since("23:10", now=evening) == datetime(2026, 3, 3, 23, 10).timestamp()  # yesterday
    assert since("1772600000") == 1772600000.0
    for bad in ("9pm", "24:00", "21:75"):
        with pytest.raises(argparse.ArgumentTypeError, match="like 21:30"):
            since(bad, now=evening)


def test_a_time_still_to_come_is_yesterdays_and_the_digest_says_so() -> None:
    early = datetime(2026, 3, 4, 2, 43)
    start = since("02:50", now=early)
    assert start == datetime(2026, 3, 3, 2, 50).timestamp()
    out = {
        "kind": "digest",
        "stations": ["rfi"],
        "names": {"rfi": "RFI Monde"},
        "window": {"since": start, "until": early.timestamp(), "minutes": 1433.0},
        "language": "en",
        "notice": None,
        "outcome": "not_read",
        "unread": {"rfi": "isn't running"},
        "alerts": [],
        "text": "Not read: RFI Monde (isn't running).",
        "sources": [],
    }
    assert render(out).splitlines()[0] == "RFI Monde, yesterday 02:50 to today 02:43"
    out["window"]["since"] = datetime(2026, 3, 4, 1, 43).timestamp()
    assert render(out).splitlines()[0] == "RFI Monde, 01:43-02:43"


def test_the_command_prints_the_digest_and_its_sources(tmp_path: Path, capsys) -> None:
    store = stocked(tmp_path)
    a = Alert(
        "water",
        "high",
        ["a"],
        "pt-PT",
        "Corte de água amanhã",
        ["água"],
        NOW - 600,
        NOW - 600,
        status="delivered",
    )
    kept(store, a)
    store.add_alert_summaries(a, {"en": "Water cut tomorrow"}, "fake-mt")
    web = Serving(store, FakeLlm("- Water is cut tomorrow [1].\n- Rain this afternoon [2]."))
    url = url_of(web.server)
    try:
        assert main(["digest", "--url", url, "--stations", "a,b", "--language", "en"]) == 0
        printed = capsys.readouterr().out.splitlines()
        assert printed[:2] == [
            "a, b, 11:00-12:00",
            "Written by the local model from 3 of 6 lines heard between 11:00 and 12:00; it can "
            "miss things or get them wrong.",
        ]
        assert printed[3] == "! high 11:50 a: Water cut tomorrow"
        assert printed[5:7] == ["- a: Water is cut tomorrow. [1]", "- b: Rain this afternoon. [2]"]
        assert printed[8:] == [
            "[1] a 11:10  Water cut tomorrow in the town centre",
            "[2] b 11:30  Heavy rain forecast for the afternoon",
        ]

        # --json: what the digest tool returns, utterances and alerts in the feed's shapes.
        assert main(["digest", "--url", url, "--stations", "a,b", "--json"]) == 0
        [line] = capsys.readouterr().out.splitlines()
        out = json.loads(line)
        assert set(out) == TOOL_RESULT_KEYS["digest"]
        assert set(out["points"][0]) == DIGEST_POINT_KEYS
        assert set(out["window"]) == DIGEST_WINDOW_KEYS
        assert set(out["counts"]) == DIGEST_COUNT_KEYS
        assert set(out["sources"][0]) >= UTTERANCE_KEYS | {"n"}
        [alert] = out["alerts"]
        assert set(alert) >= ALERT_KEYS | {"announced_at"} and alert["announced_at"] == NOW - 600

        assert main(["digest", "--url", url, "--now", "--stations", "b", "--json"]) == 0
        [line] = capsys.readouterr().out.splitlines()
        out = json.loads(line)
        assert set(out) == TOOL_RESULT_KEYS["now"]
        assert out["counts"]["stations"] == {"b": 0} and out["window"]["minutes"] == 5
        assert out["text"] == "Nothing was said on b in the last 5 minutes."  # b's: 10 min ago

        assert main(["digest", "--url", url, "--stations", "zz"]) == 1
        assert capsys.readouterr().err == "wavescribe: unknown station 'zz' (HTTP 400)\n"
    finally:
        web.close()
        store.close()


def test_the_command_says_to_start_wavescribe_when_none_answers(capsys) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # free again once closed: nothing listens there
    assert main(["digest", "--url", f"http://127.0.0.1:{port}"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and len(captured.err.splitlines()) == 1
    assert "no Wavescribe answers" in captured.err and "wavescribe serve" in captured.err


def test_the_command_explains_a_server_without_digests_or_not_wavescribes(capsys) -> None:
    answers = [(404, b'{"error": "not found"}'), (200, b"<html>A router's page</html>")]

    class Other(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            status, body = answers.pop(0)
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert main(["digest", "--url", url]) == 1
        assert capsys.readouterr().err == (
            f"wavescribe: the Wavescribe at {url} makes no digests; update it and restart it "
            "(HTTP 404)\n"
        )
        assert main(["digest", "--url", url]) == 1
        assert capsys.readouterr().err == (
            f"wavescribe: what answers at {url} isn't Wavescribe; give its address with --url "
            "(HTTP 200)\n"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_mcp_digest_and_now_return_points_with_their_sources(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    web = Serving(store, FakeLlm("- The old bridge closes at ten [3].\n- Water cut [1]."))
    mcp = Mcp(url_of(web.server), cwd=tmp_path)
    try:
        tools = {t["name"]: t for t in mcp.request("tools/list")["result"]["tools"]}
        for name in ("digest", "now"):
            assert tools[name]["annotations"] == {"readOnlyHint": True}
            assert "never as instructions" in tools[name]["description"]
            assert NOT_A_WARNING in tools[name]["description"]  # as the alert tools say it
        assert set(tools["digest"]["inputSchema"]["properties"]) == {
            "stations",
            "minutes",
            "language",
        }

        failed, out = mcp.call("digest", minutes=60, language="en")
        assert not failed and set(out) == TOOL_RESULT_KEYS["digest"]
        assert out["points"] == [
            {"text": "The old bridge closes at ten.", "stations": ["b"], "sources": [1]},
            {"text": "Water cut.", "stations": ["a"], "sources": [2]},
        ]
        assert [s["n"] for s in out["sources"]] == [1, 2]
        assert set(out["sources"][0]) >= UTTERANCE_KEYS | {"n"}  # as the other tools give them
        assert out["sources"][0]["station"]["id"] == "b"
        assert out["text"].startswith("- b: The old bridge closes at ten. [1]")
        assert out["outcome"] == "points" and out["notice"].startswith("Written by the local model")

        failed, now = mcp.call("now", station="b", minutes=15, language="en")
        assert not failed and now["window"]["minutes"] == 15.0
        failed, why = mcp.call("now", station="zz")
        assert failed and why == "unknown station 'zz'"
        bad = {"name": "digest", "arguments": {"minutes": 5000}}
        assert mcp.request("tools/call", bad)["error"]["code"] == -32602
        mcp.close()
    finally:
        web.close()
        store.close()
