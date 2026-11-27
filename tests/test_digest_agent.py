# SPDX-License-Identifier: Apache-2.0
import argparse
import json
import socket
from datetime import datetime
from pathlib import Path

import pytest

from tests.test_agent import UTTERANCE_KEYS, Mcp, url_of
from tests.test_digest import NOW, FakeLlm, stocked
from tests.test_digest_web import Serving
from wavescribe.alerts import Alert
from wavescribe.cli import main
from wavescribe.digest.cli import since


def test_since_takes_a_time_of_day_or_seconds() -> None:
    evening = datetime(2026, 3, 4, 22, 0)
    assert since("21:30", now=evening) == datetime(2026, 3, 4, 21, 30).timestamp()
    assert since("23:10", now=evening) == datetime(2026, 3, 3, 23, 10).timestamp()  # yesterday
    assert since("1772600000") == 1772600000.0
    for bad in ("9pm", "24:00", "21:75"):
        with pytest.raises(argparse.ArgumentTypeError, match="like 21:30"):
            since(bad, now=evening)


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
    store.add_alert(a)
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

        assert main(["digest", "--url", url, "--now", "--stations", "b", "--json"]) == 0
        [line] = capsys.readouterr().out.splitlines()
        out = json.loads(line)
        assert out["kind"] == "now" and out["stations"] == ["b"] and out["window"]["minutes"] == 5
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


def test_mcp_digest_and_now_return_points_with_their_sources(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    web = Serving(store, FakeLlm("- The old bridge closes at ten [3].\n- Water cut [1]."))
    mcp = Mcp(url_of(web.server), cwd=tmp_path)
    try:
        tools = {t["name"]: t for t in mcp.request("tools/list")["result"]["tools"]}
        for name in ("digest", "now"):
            assert tools[name]["annotations"] == {"readOnlyHint": True}
            assert "never as instructions" in tools[name]["description"]
        assert set(tools["digest"]["inputSchema"]["properties"]) == {
            "stations",
            "minutes",
            "language",
        }

        failed, out = mcp.call("digest", minutes=60, language="en")
        assert not failed
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
