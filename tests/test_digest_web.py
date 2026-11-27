# SPDX-License-Identifier: Apache-2.0
import dataclasses
import http.client
import json
import time
from pathlib import Path

import numpy as np

from tests.test_app import Running, make_app
from tests.test_ask import llm_server
from tests.test_digest import NOW, FakeLlm, add, stocked
from tests.test_pipeline import station
from tests.test_store import result
from tests.test_web import start_server
from wavescribe.config import StationsConfig
from wavescribe.digest import MAX_RUNNING, Digester
from wavescribe.store import Store
from wavescribe.web.server import REMOTE_ASK_OFF, Handler

LOCAL = {"X-Wavescribe": "1", "Content-Type": "application/json"}
FROM_LAN = {**LOCAL, "X-Forwarded-For": "192.168.1.20"}  # every proxied request is another's


def post(server, body: object, headers: dict[str, str] = LOCAL) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(*server.server_address, timeout=10)
    conn.request("POST", "/api/digest", json.dumps(body), headers)
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read() or b"{}")


def at_once(llm):
    def submit(run, given_up) -> bool:
        run(llm)
        return True

    return submit


class Serving:
    """The web server with a digester over ``store`` whose model is ``llm``."""

    def __init__(self, store: Store, llm=None, *, submit=None) -> None:
        self.server, self.hub, self.pipeline = llm_server(store)
        # Other machines get past the gate (as with --allow-remote-ask --public-read): these
        # tests are about what they get. The gate has its own test.
        self.server.remote_ask = self.server.public_read = True
        self.llm = llm or FakeLlm("- Water is cut tomorrow [1].")
        self.hub.digester = Digester(
            store, lambda: self.hub.config.stations, submit or at_once(self.llm), clock=lambda: NOW
        )

    def close(self) -> None:
        self.hub.close()
        self.pipeline.stop()
        self.server.shutdown()


def test_a_digest_comes_back_as_json_with_its_sources(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    web = Serving(store)
    try:
        assert web.hub.state()["digest"] is None  # available
        status, out = post(web.server, {"minutes": 60, "language": "en"})
        assert status == 200
        assert out["kind"] == "digest" and out["stations"] == ["a", "b"]
        assert out["points"] == [
            {"text": "Water is cut tomorrow.", "stations": ["a"], "sources": [1]}
        ]
        [source] = out["sources"]
        assert source["n"] == 1 and source["station"] == "a" and source["name"] == "a"
        assert source["translations"]["en"] == "Water cut tomorrow in the town centre"
        assert out["window"]["minutes"] == 60.0 and out["counts"]["said"] == 6
        assert out["text"] == "- a: Water is cut tomorrow. [1]"
    finally:
        web.close()
        store.close()


def test_asking_wrongly_says_what_to_change(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    web = Serving(store)
    try:
        assert post(web.server, {"stations": ["zz"]}) == (400, {"error": "unknown station 'zz'"})
        status, out = post(web.server, ["a"])
        assert status == 400 and "send a JSON object" in out["error"]
        status, out = post(web.server, {"minutes": 5000})
        assert status == 400 and out["error"] == "minutes must be a number from 1 to 1440"
        status, _ = post(web.server, {}, {"Content-Type": "application/json"})
        assert status == 403  # no header: another website's form
        assert web.llm.prompts == []
    finally:
        web.close()
        store.close()


def test_another_machine_gets_only_what_may_leave(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    add(store, "b", "cq cq encontro na ponte às dez", NOW - 10 * 60, privacy="amateur")
    web = Serving(store)
    ham = dataclasses.replace(station("b"), privacy="amateur")
    web.hub.config = StationsConfig((station("a"), ham), Path("t.toml"))
    try:
        assert post(web.server, {"stations": ["b"]}, FROM_LAN) == (
            400,
            {"error": "unknown station 'b'"},
        )
        status, out = post(web.server, {"language": "en"}, FROM_LAN)
        assert status == 200 and out["stations"] == ["a"]
        assert "ponte" not in web.llm.prompts[-1]
        status, out = post(web.server, {"language": "en"})  # this machine sees everything
        assert out["stations"] == ["a", "b"] and "ponte" in web.llm.prompts[-1]
    finally:
        web.close()
        store.close()


def test_why_a_digest_failed_stays_on_this_machine(tmp_path: Path) -> None:
    class Broken(FakeLlm):
        def complete(self, messages, *, max_tokens=512, on_text=None) -> str:
            raise OSError("cannot read /home/owner/models/llm/tokenizer.json")

    store = stocked(tmp_path)
    web = Serving(store, Broken())
    try:
        status, here = post(web.server, {})
        assert status == 500 and "/home/owner/models" in here["error"]
        status, elsewhere = post(web.server, {}, FROM_LAN)
        assert status == 500 and "owner" not in elsewhere["error"]
        assert "log on the Wavescribe machine" in elsewhere["error"]
    finally:
        web.close()
        store.close()


def test_a_full_queue_is_busy(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    web = Serving(store, submit=lambda run, given_up: False)
    try:
        status, out = post(web.server, {})
        assert status == 503 and "queue is full" in out["error"]
    finally:
        web.close()
        store.close()


def test_without_an_llm_there_are_no_digests(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    server, hub, pipeline = start_server(np.zeros(1), store)  # its translator isn't an LLM
    try:
        assert "needs an LLM translator" in hub.state()["digest"]
        status, out = post(server, {})
        assert status == 409 and "needs an LLM translator" in out["error"]
    finally:
        hub.close()
        pipeline.stop()
        server.shutdown()
        store.close()


def test_the_app_queues_digests_on_the_translation_model(tmp_path: Path) -> None:
    node = Running(make_app(tmp_path))
    app = node.app
    try:
        assert app.hub.digester.started == app.started_at  # a digest says when it started
        app.store.add_transcript(result("a", "Corte de água amanhã no centro", time.time() - 60))
        entry = dataclasses.replace(app.hub.plan.mt[0], backend="mlx-lm")
        app.hub.plan = dataclasses.replace(app.hub.plan, mt=(entry, Path()))
        status, out = post(node.server, {"minutes": 10})
        # It reached the model through the pipeline's queue: this run's model isn't an LLM.
        assert status == 409 and out["error"].startswith("a digest needs an LLM; this run")
    finally:
        node.close()


def test_a_digest_passes_the_gate_a_question_does(tmp_path: Path, monkeypatch) -> None:
    store = stocked(tmp_path)
    web = Serving(store)
    web.server.remote_ask = web.server.public_read = False

    class Tokens:
        def token_id(self, token, scope) -> str | None:
            return "viewer" if (token, scope) == ("secret", "read") else None

    web.hub.app = Tokens()
    with_token = {**FROM_LAN, "Authorization": "Bearer secret"}
    try:
        # This machine: no flag and no token.
        assert post(web.server, {"language": "en"})[0] == 200
        # Another machine: it spends the model, so not without serve --allow-remote-ask.
        status, out = post(web.server, {"language": "en"}, with_token)
        assert (status, out["error"]) == (403, REMOTE_ASK_OFF)
        with monkeypatch.context() as m:
            m.setattr(Handler, "_is_local", lambda self: False)  # on the LAN, no proxy
            assert post(web.server, {"language": "en"})[0] == 403
            web.server.remote_ask = True
            assert post(web.server, {"language": "en"})[0] == 200  # the LAN reads freely
        # Through a proxy (a proxy's header even from here), a read token as for a question.
        status, out = post(web.server, {"language": "en"}, FROM_LAN)
        assert status == 401 and out["token_required"] is True
        assert post(web.server, {"language": "en"}, with_token)[0] == 200
        web.server.public_read = True
        assert post(web.server, {"language": "en"}, FROM_LAN)[0] == 200
        # Reading aloud is a control: this machine only, never through a proxy.
        status, out = post(web.server, {"language": "en", "speak": True}, with_token)
        assert status == 403 and "only the Wavescribe machine" in out["error"]
        web.server.behind_proxy = True
        status, out = post(web.server, {"language": "en", "speak": True}, LOCAL)
        assert status == 403 and "only the Wavescribe machine" in out["error"]
    finally:
        web.close()
        store.close()


def test_another_machine_is_told_why_it_cant_have_a_digest(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    web = Serving(store)
    web.server.remote_ask = False
    try:
        conn = http.client.HTTPConnection(*web.server.server_address, timeout=10)
        conn.request("GET", "/api/state", headers=FROM_LAN)
        state = json.loads(conn.getresponse().read())
        assert state["digest"] == REMOTE_ASK_OFF  # the page and agents say why
    finally:
        web.close()
        store.close()


def test_a_busy_digester_says_when_to_try_again(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    web = Serving(store)
    for _ in range(MAX_RUNNING):
        web.hub.digester._running.acquire()
    try:
        conn = http.client.HTTPConnection(*web.server.server_address, timeout=10)
        conn.request("POST", "/api/digest", json.dumps({}), LOCAL)
        resp = conn.getresponse()
        assert resp.status == 503 and resp.getheader("Retry-After") == "60"
        assert "already being made" in json.loads(resp.read())["error"]
        status, out = post(web.server, {"minutes": 600}, FROM_LAN)
        assert status == 400 and "from another machine" in out["error"]
    finally:
        web.close()
        store.close()
