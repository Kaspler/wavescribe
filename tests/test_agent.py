# SPDX-License-Identifier: Apache-2.0
"""Agent tools: ``--json`` on the read commands, ``wavescribe follow`` and the MCP adapter, run
as their own processes against a real server with fake models."""

import io
import json
import os
import re
import select
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.test_app import Running, alert_on, make_app, wait_for
from tests.test_ask import FakeLlm, llm_server
from tests.test_pipeline import station
from tests.test_store import result
from tests.test_web import idle_server
from wavescribe import __version__
from wavescribe.agent.client import Client, follow
from wavescribe.ask import Asker
from wavescribe.cli import main
from wavescribe.store import Store

# What every utterance event carries (docs/FEED.md); audio_url only with a known address.
UTTERANCE_KEYS = {
    "type",
    "version",
    "id",
    "node",
    "station",
    "time",
    "t_start",
    "duration",
    "language",
    "text",
    "translations",
    "flags",
    "rules",
}
# The other shapes the --json commands and the MCP tools give, pinned so that they can't drift
# from docs/FEED.md (feed events) and docs/AGENT_TOOLS.md (the rest) without a test saying so.
ALERT_KEYS = {
    "type", "version", "id", "node", "rule", "kind", "severity", "status", "time", "first_at",
    "last_at", "mentions", "terms", "reason", "station", "stations", "language", "text",
    "translations", "context", "segment_id",
}  # fmt: skip
MODEL_KEYS = {"id", "capability", "license", "license_url", "tier", "size_gb", "languages",
              "accepted", "installed"}  # fmt: skip
HEARD_KEYS = {"freq_hz", "mhz", "band", "label", "mode", "privacy", "level_db", "snr_db",
              "first_heard", "last_heard", "times", "seen", "passes", "missing",
              "device"}  # fmt: skip
CATCH_KEYS = {"id", "station", "freq_hz", "mhz", "t_start", "t_end", "duration", "mode", "band",
              "label", "snr_db", "peak_db", "voice", "reason", "privacy", "utterances"}  # fmt: skip
CHECK_KEYS = {"type", "group", "check", "status", "summary", "fix", "details"}
SUMMARY_KEYS = {"type", "status", "problems", "warnings", "version", "folder", "options",
                "from_service"}  # fmt: skip
SERVICE_KEYS = {"label", "plist", "installed", "loaded", "running", "pid", "state", "runs",
                "last_exit", "last_signal", "workdir", "serve_args", "keep_awake", "log",
                "log_bytes", "full_log"}  # fmt: skip
TOOL_STATION_KEYS = {"id", "name", "language", "translate_to", "kind", "running", "connected",
                     "last_heard", "lag"}  # fmt: skip
TOOL_RESULT_KEYS = {
    "stations": {"stations"},
    "recent": {"utterances", "next_after_id", "more"},
    "search": {"utterances"},
    "alerts": {"alerts"},
    "wait_for_alert": {"alert", "waited_s"},
    "ask": {"answer", "sources", "plan"},
    "status": {"node", "stations", "status", "ask", "mt_model", "noncommercial", "store"},
}
DOCS = Path(__file__).parents[1] / "docs"


def test_every_json_shape_is_documented_field_by_field() -> None:
    feed = (DOCS / "FEED.md").read_text(encoding="utf-8")
    tools = (DOCS / "AGENT_TOOLS.md").read_text(encoding="utf-8")
    assert [k for k in sorted(UTTERANCE_KEYS | ALERT_KEYS) if f'"{k}"' not in feed] == []
    shapes = [MODEL_KEYS, HEARD_KEYS, CATCH_KEYS, CHECK_KEYS, SUMMARY_KEYS, SERVICE_KEYS,
              TOOL_STATION_KEYS, *TOOL_RESULT_KEYS.values()]  # fmt: skip
    named = set()  # each field in code: `name`, or in a result written `{a, b: null}`
    prose = re.sub(r"```.*?```", "", tools, flags=re.S)  # not the examples
    for span in re.findall(r"`([^`\n]+)`", prose):
        named |= set(re.findall(r"(\w+)(?=[,:}])", span)) if span.startswith("{") else {span}
    assert sorted({k for keys in shapes for k in keys} - named) == []
    assert all(f"| `{tool}` |" in tools for tool in TOOL_RESULT_KEYS)


def stored(store: Store, sid: str, text: str, ago: float = 60.0, **translations: str) -> int:
    r = result(sid, text, time.time() - ago, **translations)
    segment_id = store.add_transcript(r)
    store.add_translations(r)
    assert segment_id is not None
    return segment_id


def url_of(server) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


def wavescribe(*args: str, cwd: Path) -> subprocess.Popen:
    """``wavescribe ARGS`` in a process of its own, its stdout unbuffered on our side."""
    return subprocess.Popen(
        [sys.executable, "-m", "wavescribe", *args],
        cwd=cwd,
        bufsize=0,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class Lines:
    """A child's stdout, a line at a time, each with a deadline: a child that goes quiet fails
    the test instead of hanging it."""

    def __init__(self, stream) -> None:
        self.fd, self.buf = stream.fileno(), b""

    def read(self, timeout: float = 10.0) -> bytes:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            ready, _, _ = select.select([self.fd], [], [], max(0.0, deadline - time.monotonic()))
            assert ready, "no line in time"
            chunk = os.read(self.fd, 65536)
            assert chunk, "the output ended"
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line


class Mcp:
    """A small MCP client: ``wavescribe mcp`` as a child process, spoken to through its pipes."""

    def __init__(self, url: str, *flags: str, cwd: Path) -> None:
        self.proc = wavescribe("mcp", "--url", url, *flags, cwd=cwd)
        self.out = Lines(self.proc.stdout)
        self.last_id = 0
        self.early: dict = {}  # answers that came while another was awaited

    def send(self, message) -> int | None:
        raw = message if isinstance(message, bytes) else json.dumps(message).encode()
        self.proc.stdin.write(raw + b"\n")
        return message.get("id") if isinstance(message, dict) else None

    def receive(self, timeout: float = 10.0):
        return json.loads(self.out.read(timeout))  # every line must be JSON

    def start(self, method: str, params=None) -> int:
        self.last_id += 1
        message = {"jsonrpc": "2.0", "id": self.last_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self.last_id

    def answer(self, rid: int, timeout: float = 10.0) -> dict:
        while rid not in self.early:
            message = self.receive(timeout)
            self.early[message.get("id")] = message
        return self.early.pop(rid)

    def request(self, method: str, params=None, timeout: float = 10.0) -> dict:
        return self.answer(self.start(method, params), timeout)

    def call(self, tool: str, timeout: float = 10.0, **arguments):
        """(isError, the result's text: parsed when it is JSON, as it is for a success)."""
        res = self.request("tools/call", {"name": tool, "arguments": arguments}, timeout)["result"]
        [content] = res["content"]
        assert content["type"] == "text"
        return res["isError"], content["text"] if res["isError"] else json.loads(content["text"])

    def close(self) -> str:
        self.proc.stdin.close()
        assert self.proc.wait(10) == 0
        err = self.proc.stderr.read().decode()
        assert "Traceback" not in err, err
        return err


# -- --json on the read commands ------------------------------------------------------------


def test_search_prints_a_row_from_a_wrong_clock(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / "data" / "wavescribe.db")
    seg = stored(store, "a", "Corte de água amanhã")
    with store._tx() as db:  # past year 9999: an old database, from before times were checked
        db.execute("UPDATE segments SET t_start = 1e13, t_end = 1e13 WHERE id = ?", (seg,))
    store.close()
    assert main(["search", "água"]) == 0
    assert capsys.readouterr().out.startswith("[?] a")


def test_read_commands_print_one_json_object_a_line(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)  # data/ and config/ are looked up from the working directory
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "stations.toml").write_text(
        '[[station]]\nid = "a"\nname = "Rádio A"\nlanguage = "pt-PT"\n'
        'stream = "http://example.test/a"\n'
    )
    store = Store(tmp_path / "data" / "wavescribe.db")
    seg = stored(store, "a", "Corte de água amanhã", en="Water cut tomorrow")
    stored(store, "a", "Previsão de chuva", ago=30)
    alert_on(store, "a", "Corte de água amanhã", seg, ["broadcast"])
    store.close()

    def lines(argv: list[str], code: int = 0) -> list[dict]:
        assert main(argv) == code
        out = capsys.readouterr().out
        return [json.loads(line) for line in out.splitlines()]

    [hit] = lines(["search", "água", "--json"])
    assert set(hit) == UTTERANCE_KEYS and hit["type"] == "utterance" and hit["id"] == seg
    assert hit["station"] == {
        "id": "a",
        "name": "Rádio A",
        "language": "pt-PT",
        "privacy": "broadcast",
    }
    assert hit["translations"] == {"en": "Water cut tomorrow"} and hit["node"]["url"] is None
    assert lines(["search", "neve", "--json"], code=1) == []  # nothing found, nothing printed

    for argv in (["alerts", "--json"], ["alerts", "list", "--json"]):
        [alert] = lines(argv)
        assert set(alert) == ALERT_KEYS  # audio_url and cap_url only with a known address
        assert alert["type"] == "alert" and alert["rule"] == "water"
        assert alert["text"] == "Corte de água amanhã" and alert["segment_id"] == seg
        assert alert["station"]["name"] == "Rádio A" and alert["severity"] == "high"

    models = lines(["models", "list", "--json"])
    assert models and all(
        set(m) == MODEL_KEYS
        and isinstance(m["installed"], bool)
        and isinstance(m["accepted"], bool)
        and m["capability"]
        and m["license"]
        for m in models
    )

    assert main(["radio", "heard", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and "Nothing heard yet" in captured.err  # stdout: JSON only
    heard = {"freq_hz": 99000000, "mhz": 99.0, "privacy": "broadcast", "last_heard": 1.0}
    (tmp_path / "data" / "radio").mkdir(parents=True)
    (tmp_path / "data" / "radio" / "heard.json").write_text(
        json.dumps({"version": 1, "entries": {"99000000": heard}})
    )
    assert lines(["radio", "heard", "--json"]) == [heard]


# -- follow -----------------------------------------------------------------------------------


def test_follow_prints_json_lines_and_stops_when_its_reader_goes(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    server, hub = idle_server(store)
    try:
        seg = stored(store, "a", "Bom dia", en="Good morning")
        proc = wavescribe("follow", "--url", url_of(server), cwd=tmp_path)
        out = Lines(proc.stdout)
        wait_for(lambda: len(hub._clients) == 1)
        hub.publish("utterance", store.after(0)[0])
        alert = alert_on(store, "a", "Bom dia", seg, ["broadcast"])
        hub.on_alert({**alert.payload(), "status": "delivered"})
        utterance, announced = json.loads(out.read()), json.loads(out.read())
        assert set(utterance) == UTTERANCE_KEYS and utterance["text"] == "Bom dia"
        assert utterance["id"] == seg and utterance["translations"] == {"en": "Good morning"}
        assert announced["type"] == "alert" and announced["rule"] == "water"

        proc.stdout.close()  # as `| head -2` does once it has its lines
        later = stored(store, "a", "Boa tarde", ago=1)
        hub.publish("utterance", store.after(later - 1)[0])
        assert proc.wait(10) == 0
        assert proc.stderr.read() == b""
    finally:
        hub.close()
        server.shutdown()
        store.close()


def test_follow_says_to_start_wavescribe_when_none_answers(capsys) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # free again once closed: nothing listens there
    assert main(["follow", "--url", f"http://127.0.0.1:{port}"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and len(captured.err.splitlines()) == 1
    assert "no Wavescribe answers" in captured.err and "wavescribe serve" in captured.err


def test_follow_reconnects_and_resumes_after_the_last_utterance(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    server, hub = idle_server(store)
    written, waits, ended = [], [], []

    class Reader:
        """Takes two lines, then goes away as a closed pipe does."""

        def write(self, text: str) -> None:
            written.append(json.loads(text))

        def flush(self) -> None:
            if len(written) == 2:
                raise BrokenPipeError

    def run() -> None:
        try:
            follow(Client(url_of(server)), {}, Reader(), sleep=waits.append, err=io.StringIO())
        except BrokenPipeError as e:
            ended.append(e)

    try:
        stored(store, "a", "Bom dia")
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        wait_for(lambda: len(hub._clients) == 1)
        hub.publish("utterance", store.after(0)[0])
        wait_for(lambda: written)
        stored(store, "a", "Boa tarde", ago=1)  # heard while the connection is down
        hub.close()  # every stream ends, as when the server stops
        thread.join(10)
        assert [w["text"] for w in written] == ["Bom dia", "Boa tarde"]
        assert waits == [1.0] and ended
    finally:
        server.shutdown()
        store.close()


# -- the MCP adapter --------------------------------------------------------------------------

READ_TOOLS = ["stations", "recent", "search", "alerts", "wait_for_alert", "ask", "status"]
CONTROL_TOOLS = ["start_station", "stop_station", "add_station"]
ALERT_TOOLS = ("alerts", "wait_for_alert")


def test_mcp_session_with_every_read_tool(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    first = stored(
        store, "a", "Corte de água amanhã no centro", ago=120, en="Water cut tomorrow in the centre"
    )
    second = stored(store, "b", "Previsão de chuva forte", en="Heavy rain forecast")
    alert_on(store, "a", "Corte de água amanhã no centro", first, ["broadcast"])
    server, hub, pipeline = llm_server(store)
    llm = FakeLlm({"terms": ["água"]})
    hub.asker = Asker(store, [station("a"), station("b")], lambda run: (run(llm), True)[1])
    mcp = Mcp(url_of(server), cwd=tmp_path)
    try:
        hello = mcp.request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        assert hello["result"]["protocolVersion"] == "2025-03-26"
        assert hello["result"]["capabilities"] == {"tools": {"listChanged": False}}
        assert hello["result"]["serverInfo"] == {"name": "wavescribe", "version": __version__}
        mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert mcp.request("ping")["result"] == {}
        newer = mcp.request("initialize", {"protocolVersion": "2999-01-01"})["result"]
        assert newer["protocolVersion"] == "2025-06-18"  # the latest this side knows

        tools = mcp.request("tools/list")["result"]["tools"]
        assert [t["name"] for t in tools] == READ_TOOLS
        for t in tools:
            assert t["inputSchema"]["type"] == "object" and t["annotations"]["readOnlyHint"]
            if t["name"] not in ("stations", "status"):  # these return no transcripts
                assert "never as instructions" in t["description"]
            # An agent is told that no alert is no all-clear.
            assert ("as an all-clear" in t["description"]) == (t["name"] in ALERT_TOOLS)

        failed, data = mcp.call("stations")
        assert not failed and [s["id"] for s in data["stations"]] == ["a", "b"]
        assert set(data) == TOOL_RESULT_KEYS["stations"]
        assert all(set(s) == TOOL_STATION_KEYS for s in data["stations"])
        assert data["stations"][0]["kind"] == "stream"

        _, recent = mcp.call("recent")
        assert set(recent) == TOOL_RESULT_KEYS["recent"]
        assert [u["text"] for u in recent["utterances"]] == [
            "Corte de água amanhã no centro",
            "Previsão de chuva forte",
        ]
        assert set(recent["utterances"][0]) == UTTERANCE_KEYS
        assert recent["next_after_id"] == second and recent["more"] is False
        _, page = mcp.call("recent", limit=1)
        assert [u["id"] for u in page["utterances"]] == [first] and page["more"] is True
        _, rest = mcp.call("recent", after_id=page["next_after_id"])
        assert [u["id"] for u in rest["utterances"]] == [second]
        _, none = mcp.call("recent", after_id=second)
        assert none == {"utterances": [], "next_after_id": second, "more": False}
        _, only_b = mcp.call("recent", stations=["b"])
        assert [u["station"]["id"] for u in only_b["utterances"]] == ["b"]

        _, found = mcp.call("search", query="chuva")
        assert set(found) == TOOL_RESULT_KEYS["search"]
        [hit] = found["utterances"]
        assert set(hit) == UTTERANCE_KEYS and hit["id"] == second
        assert hit["translations"] == {"en": "Heavy rain forecast"}
        assert mcp.call("search", query="chuva", station="a")[1] == {"utterances": []}

        _, alerts = mcp.call("alerts", minutes=60)
        [alert] = alerts["alerts"]
        assert set(alerts) == TOOL_RESULT_KEYS["alerts"] and set(alert) == ALERT_KEYS
        assert alert["rule"] == "water" and alert["text"] == "Corte de água amanhã no centro"

        _, status = mcp.call("status")
        assert set(status) == TOOL_RESULT_KEYS["status"]
        assert status["node"]["version"] == __version__ and status["ask"] is None
        assert {"ready", "models", "lag"} <= set(status["status"])

        failed, answer = mcp.call("ask", question="Is there a water cut?")
        assert set(answer) == TOOL_RESULT_KEYS["ask"]
        assert not failed and answer["answer"] == "Water is back on [1]."
        assert answer["plan"]["terms"] == ["água"]
        assert answer["sources"][0]["n"] == 1 and answer["sources"][0]["id"] == first

        # What isn't a valid call is refused as JSON-RPC asks, and the session goes on.
        assert mcp.request("resources/list")["error"]["code"] == -32601
        mcp.send(b"{this is not json")
        parse_error = mcp.receive()
        assert parse_error["id"] is None and parse_error["error"]["code"] == -32700
        for params in (
            {"name": "recent", "arguments": {"limit": "ten"}},
            {"name": "recent", "arguments": {"since": 0}},
            {"name": "search", "arguments": {}},
            {"name": "start_station", "arguments": {"station": "a"}},
        ):
            assert mcp.request("tools/call", params)["error"]["code"] == -32602, params
        assert mcp.request("tools/call", ["recent"])["error"]["code"] == -32602
        mcp.send(
            [
                {"jsonrpc": "2.0", "id": "x", "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            ]
        )
        assert mcp.receive() == [{"jsonrpc": "2.0", "id": "x", "result": {}}]
        assert mcp.early == {}  # nothing answered a notification
    finally:
        mcp.close()
        hub.close()
        pipeline.stop()
        server.shutdown()
        store.close()


def test_mcp_waits_for_the_next_announced_alert(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    seg = stored(store, "a", "Corte de água")
    payload = alert_on(store, "a", "Corte de água", seg, ["broadcast"]).payload()
    server, hub = idle_server(store)
    mcp = Mcp(url_of(server), cwd=tmp_path)
    try:
        assert mcp.call("wait_for_alert", timeout_s=0.2) == (
            False,
            {"alert": None, "waited_s": 0.2},
        )
        failed, why = mcp.call("ask", question="water?")
        assert failed and why.startswith("asking needs an LLM")  # the server's own words

        before = len(hub._clients)
        waiting = mcp.start(
            "tools/call",
            {"name": "wait_for_alert", "arguments": {"timeout_s": 20, "min_severity": "medium"}},
        )
        wait_for(lambda: len(hub._clients) > before)
        assert mcp.request("ping")["result"] == {}  # answered while the tool waits
        hub.on_alert({**payload, "severity": "low", "status": "delivered", "change": "new"})
        hub.on_alert({**payload, "status": "pending"})  # not announced yet
        # Another mention of an alert already announced: an update, not news.
        hub.on_alert({**payload, "status": "delivered", "change": "update", "mentions": 7})
        hub.on_alert({**payload, "status": "delivered", "change": "new"})
        res = mcp.answer(waiting)["result"]
        alert = json.loads(res["content"][0]["text"])["alert"]
        assert not res["isError"] and alert["severity"] == "high"
        assert alert["status"] == "delivered" and alert["text"] == "Corte de água"
        assert alert["change"] == "new" and alert["mentions"] == 1

        # A call the client gives up on gets no answer.
        given_up = mcp.start(
            "tools/call", {"name": "wait_for_alert", "arguments": {"timeout_s": 20}}
        )
        mcp.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": given_up},
            }
        )
        assert mcp.call("wait_for_alert", timeout_s=0.6)[1]["alert"] is None
        assert given_up not in mcp.early
    finally:
        mcp.close()
        hub.close()
        server.shutdown()
        store.close()


def test_mcp_control_tools_only_with_allow_control(tmp_path: Path) -> None:
    node = Running(make_app(tmp_path))
    try:
        plain = Mcp(node.url, cwd=tmp_path)
        assert [t["name"] for t in plain.request("tools/list")["result"]["tools"]] == READ_TOOLS
        plain.close()

        mcp = Mcp(node.url, "--allow-control", cwd=tmp_path)
        tools = {t["name"]: t for t in mcp.request("tools/list")["result"]["tools"]}
        assert list(tools) == READ_TOOLS + CONTROL_TOOLS
        assert tools["add_station"]["annotations"] == {
            "readOnlyHint": False,
            "destructiveHint": False,
        }
        failed, added = mcp.call(
            "add_station", name="Rádio Nova", url="http://example.test/nova", language="es-ES"
        )
        assert not failed and added["id"] == "radio-nova"
        failed, why = mcp.call(  # a name in use, whatever its case: refused, as documented
            "add_station", name=" rádio NOVA", url="http://example.test/two", language="es-ES"
        )
        assert failed and "there is already a station called 'rádio NOVA'" in why
        assert [s["id"] for s in node.app.stations_view()].count("radio-nova-2") == 0
        assert mcp.call("start_station", station="a") == (False, {"ok": True})

        def running() -> list[str]:  # remembered for the next start, as the page's are
            return (node.app.settings.get("run") or {}).get("stations", [])

        assert "a" in running()
        assert mcp.call("stop_station", station="a") == (False, {"ok": True})
        assert "a" not in running()
        failed, why = mcp.call("start_station", station="nope")
        assert failed and "unknown station 'nope'" in why  # passed on as the server said it
        bad = {
            "name": "add_station",
            "arguments": {"name": "x", "url": "file:///etc/hosts", "language": "en"},
        }
        assert mcp.request("tools/call", bad)["error"]["code"] == -32602
        mcp.close()
    finally:
        node.close()


def test_start_right_after_stop_starts_the_station_or_says_why_not(tmp_path, monkeypatch) -> None:
    from tests.test_pipeline import SlowToClose

    node = Running(make_app(tmp_path))
    try:
        node.app.pipeline._source_factory = lambda st: SlowToClose(st.id, 0.4)
        assert node.call("POST", "/api/manage/stations/a/start") == (200, {"ok": True})
        wait_for(lambda: "a" in node.app.pipeline.status()["stations"])
        assert node.call("POST", "/api/manage/stations/a/stop") == (200, {"ok": True})
        assert node.call("POST", "/api/manage/stations/a/start") == (200, {"ok": True})
        time.sleep(0.6)  # the first run has let go of its source by now
        assert "a" in node.app.running()

        monkeypatch.setattr("wavescribe.pipeline.STOP_WAIT_S", 0.1)
        node.app.pipeline._source_factory = lambda st: SlowToClose(st.id, 2.0)
        node.call("POST", "/api/manage/stations/a/stop")
        wait_for(lambda: "a" not in node.app.running())
        node.call("POST", "/api/manage/stations/a/start")
        assert node.call("POST", "/api/manage/stations/a/stop") == (200, {"ok": True})
        status, body = node.call("POST", "/api/manage/stations/a/start")
        assert (status, body) == (400, {"error": "Rádio A is still stopping: try again in a "
                                                 "few seconds"})  # fmt: skip
    finally:
        node.close()


@pytest.mark.parametrize(
    ("value", "schema", "fits"),
    [
        (3, {"type": "integer", "minimum": 1}, True),
        (True, {"type": "integer"}, False),  # JSON true is no number
        (float("nan"), {"type": "number"}, False),
        ("de", {"type": "string", "minLength": 3}, False),
        (["a", 1], {"type": "array", "items": {"type": "string"}}, False),
        ({}, {"type": "object", "properties": {}, "required": ["q"]}, False),
    ],
)
def test_arguments_are_checked_against_the_tools_schema(value, schema, fits) -> None:
    from wavescribe.agent.mcp import BadParams, check

    if fits:
        check(value, schema)
    else:
        with pytest.raises(BadParams):
            check(value, schema)
