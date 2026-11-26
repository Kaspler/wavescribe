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
from wavescribe.digest import Digester
from wavescribe.store import Store

LOCAL = {"X-Wavescribe": "1", "Content-Type": "application/json"}
FROM_LAN = {**LOCAL, "X-Forwarded-For": "192.168.1.20"}  # every proxied request is another's


def post(server, body: object, headers: dict[str, str] = LOCAL) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(*server.server_address, timeout=10)
    conn.request("POST", "/api/digest", json.dumps(body), headers)
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read() or b"{}")


def at_once(llm):
    def submit(run) -> bool:
        run(llm)
        return True

    return submit


class Serving:
    """The web server with a digester over ``store`` whose model is ``llm``."""

    def __init__(self, store: Store, llm=None, *, submit=None) -> None:
        self.server, self.hub, self.pipeline = llm_server(store)
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
    web = Serving(store, submit=lambda run: False)
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
        app.store.add_transcript(result("a", "Corte de água amanhã no centro", time.time() - 60))
        entry = dataclasses.replace(app.hub.plan.mt[0], backend="mlx-lm")
        app.hub.plan = dataclasses.replace(app.hub.plan, mt=(entry, Path()))
        status, out = post(node.server, {"minutes": 10})
        # It reached the model through the pipeline's queue: this run's model isn't an LLM.
        assert status == 409 and out["error"].startswith("a digest needs an LLM; this run")
    finally:
        node.close()
