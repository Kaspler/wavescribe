# SPDX-License-Identifier: Apache-2.0
import dataclasses
import http.client
import itertools
import json
import threading
import time
from pathlib import Path

import numpy as np

from tests.test_app import Running, make_app
from tests.test_ask import llm_server
from tests.test_digest import MIN, NOW, FakeLlm, add, stocked
from tests.test_pipeline import station
from tests.test_store import result
from tests.test_web import start_server
from wavescribe.config import StationsConfig
from wavescribe.digest import MAX_RUNNING, MAX_WAITING, Digester
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

    def __init__(self, store: Store, llm=None, *, submit=None, **digester) -> None:
        self.server, self.hub, self.pipeline = llm_server(store)
        # Other machines get past the gate (as with --allow-remote-ask --public-read): these
        # tests are about what they get. The gate has its own test.
        self.server.remote_ask = self.server.public_read = True
        self.llm = llm or FakeLlm("- Water is cut tomorrow [1].")
        self.hub.digester = Digester(
            store,
            lambda: self.hub.config.stations,
            submit or at_once(self.llm),
            clock=lambda: NOW,
            **digester,
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


def test_a_digest_not_made_in_time_says_what_kept_the_model_busy(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    looks = itertools.count()  # the wait is counted in looks at the time: a second of them
    web = Serving(
        store,
        submit=lambda run, given_up: False,  # no room in the model's queue, however often asked
        timer=lambda: next(looks) / 100,
        timeout_s=1.0,
        poll_s=0.001,
        spent=lambda last_s: {("translation", ("North FM",)): 0.7, ("rules", ("roads",)): 0.2},
    )
    try:
        conn = http.client.HTTPConnection(*web.server.server_address, timeout=60)
        conn.request("POST", "/api/digest", json.dumps({"id": "asked-here-1"}), LOCAL)
        resp = conn.getresponse()
        out = json.loads(resp.read())
        assert resp.status == 503 and resp.getheader("Retry-After") == "60"
        assert out["error"] == (
            "live translation of North FM and the alert rule 'roads' kept the model busy for "
            "a second, so no digest was made in time; try again in a minute"
        )
        assert (out["code"], out["id"], out["retry_after_s"]) == ("busy", "asked-here-1", 60)
        assert 1.0 <= out["waited_s"] < 2.0
        assert out["busy_with"] == [
            {"kind": "translation", "names": ["North FM"], "seconds": 0.7},
            {"kind": "rules", "names": ["roads"], "seconds": 0.2},
        ]
        # Another machine learns what kinds of work, and no station's or rule's name.
        status, elsewhere = post(web.server, {}, FROM_LAN)
        assert status == 503 and elsewhere["error"].startswith(
            "live translation and alert rules kept the model busy for a second, so no digest"
        )
        assert [b["names"] for b in elsewhere["busy_with"]] == [[], []]
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


def until(condition, what: str) -> None:
    deadline = time.monotonic() + 60
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.005)


def test_digests_wait_in_line_and_the_status_says_where_each_stands(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    web = Serving(store, submit=lambda run, given_up: True, poll_s=0.005)  # never the model's turn
    d = web.hub.digester
    ids = [f"waiting-{n}" for n in range(1, MAX_RUNNING + MAX_WAITING + 1)]
    answers: dict[str, tuple[int, dict]] = {}

    def ask(n: int, rid: str) -> threading.Thread:
        def one() -> None:
            conn = http.client.HTTPConnection(*web.server.server_address, timeout=120)
            headers = FROM_LAN if rid == "waiting-2" else LOCAL
            conn.request("POST", "/api/digest", json.dumps({"id": rid}), headers)
            resp = conn.getresponse()
            answers[rid] = resp.status, json.loads(resp.read())

        thread = threading.Thread(target=one, daemon=True)
        thread.start()
        until(lambda: len(d.status()) == n, f"{rid} was never begun")
        return thread

    threads = [ask(n, rid) for n, rid in enumerate(ids, 1)]
    try:
        until(lambda: all(r["calls"] for r in d.status()[:MAX_RUNNING]), "none was begun")
        # A seventh is refused at once, with about when a place in the line comes free.
        conn = http.client.HTTPConnection(*web.server.server_address, timeout=60)
        conn.request("POST", "/api/digest", json.dumps({}), LOCAL)
        resp = conn.getresponse()
        out = json.loads(resp.read())
        assert resp.status == 503 and resp.getheader("Retry-After") == "30"
        assert out["error"] == (
            "6 digests are already being made or waiting for their turn; try again in 30 seconds"
        )
        assert (out["code"], out["retry_after_s"]) == ("busy", 30)
        assert "waited_s" not in out and "busy_with" not in out
        # This machine's page sees them all, by the ids that cancel them.
        conn.request("GET", "/api/state", headers=LOCAL)
        rows = json.loads(conn.getresponse().read())["status"]["digests"]
        assert [r["id"] for r in rows] == ids
        assert [r["state"] for r in rows] == ["waiting"] * 2 + ["in_line"] * 4
        assert [r["behind"] for r in rows] == [0, 0, 1, 2, 3, 4]
        assert [r["here"] for r in rows] == [True, False, True, True, True, True]
        first, third = rows[0], rows[2]
        assert (first["kind"], first["call"], first["calls"]) == ("digest", 1, 1)
        assert first["behind_what"] == [] and 0 <= first["turn_in_s"] <= 120
        assert 0 <= first["waited_s"] < 600 and 0 < first["gives_up_in_s"] <= 600
        assert (third["behind_what"], third["call"], third["turn_in_s"]) == (["digest"], None, None)
        # Another machine sees the one another machine asked for, and no id.
        conn.request("GET", "/api/state", headers=FROM_LAN)
        [theirs] = json.loads(conn.getresponse().read())["status"]["digests"]
        assert "id" not in theirs and theirs["here"] is False and theirs["state"] == "waiting"
        # One that is cancelled leaves the line, and the next takes its turn.
        assert d.cancel("waiting-1") == "cancelled"
        until(lambda: [r["id"] for r in d.status()] == ids[1:], "the line never moved up")
        threads[0].join(60)
        assert answers["waiting-1"][0] == 409 and answers["waiting-1"][1]["code"] == "cancelled"

        status, out = post(web.server, {"minutes": 600}, FROM_LAN)
        assert status == 400 and "from another machine" in out["error"]
        assert (out["code"], out["max_minutes"]) == ("bad_window", 360)
    finally:
        for rid in ids:
            d.cancel(rid)
        for thread in threads:
            thread.join(60)
        web.close()
        store.close()


def call(server, path: str, body: object = None, headers: dict[str, str] = LOCAL):
    conn = http.client.HTTPConnection(*server.server_address, timeout=10)
    conn.request("POST", path, json.dumps(body) if body is not None else None, headers)
    resp = conn.getresponse()
    raw = resp.read()
    return resp.status, json.loads(raw) if raw.startswith(b"{") else raw


def test_the_state_says_why_digests_are_off_with_a_code(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    web = Serving(store)
    web.server.remote_ask = False
    try:
        assert web.hub.state()["digest_code"] is None
        conn = http.client.HTTPConnection(*web.server.server_address, timeout=10)
        conn.request("GET", "/api/state", headers=FROM_LAN)
        state = json.loads(conn.getresponse().read())
        assert (state["digest"], state["digest_code"]) == (REMOTE_ASK_OFF, "remote_off")
    finally:
        web.close()
    server, hub, pipeline = start_server(np.zeros(1), store)  # its translator isn't an LLM
    try:
        assert hub.state()["digest_code"] == "no_llm"
        status, out = post(server, {})
        assert (status, out["code"]) == (409, "no_llm")
    finally:
        hub.close()
        pipeline.stop()
        server.shutdown()
        store.close()


def test_an_answer_carries_its_id_the_askers_or_one_made_for_it(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    web = Serving(store)
    try:
        status, out = post(web.server, {"language": "en"})
        assert status == 200 and len(out["id"]) >= 16
        assert post(web.server, {"id": "9b2c4e1a-page"})[1]["id"] == "9b2c4e1a-page"
        for bad in ("short", "has space in it", 12345678, "x" * 65):
            status, out = post(web.server, {"id": bad})
            assert (status, out["code"]) == (400, "bad_id"), bad
    finally:
        web.close()
        store.close()


def test_until_ends_the_window_before_now(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    web = Serving(store)
    try:
        body = {"stations": ["a", "b"], "minutes": 30, "until": NOW - 35 * MIN, "language": "en"}
        status, out = post(web.server, body)
        assert status == 200
        assert out["window"] == {"since": NOW - 65 * MIN, "until": NOW - 35 * MIN, "minutes": 30}
        assert out["counts"]["stations"] == {"a": 3, "b": 0}  # b spoke after it
        assert "Water cut" in web.llm.prompts[-1] and "rain" not in web.llm.prompts[-1]
        status, out = post(web.server, {"since": NOW - 40 * MIN, "until": NOW - 25 * MIN})
        assert out["window"]["minutes"] == 15 and out["counts"]["stations"]["b"] == 1
        # Nothing said then: the digest names the times, not "the last 10 minutes".
        status, out = post(web.server, {"stations": ["b"], "minutes": 10, "until": NOW - 35 * MIN})
        assert out["text"] == "Nothing was said on b between 11:15 and 11:25."

        for wrong, code in (
            ({"until": NOW + 60}, "bad_until"),  # still to come
            ({"until": "noon"}, "bad_until"),
            ({"kind": "now", "until": NOW - 600}, "bad_until"),
            ({"since": NOW - 30 * MIN, "until": NOW - 40 * MIN}, "bad_window"),
            ({"since": NOW - 3 * 1440 * MIN, "until": NOW - 30 * MIN}, "bad_window"),
            ({"minutes": 2000, "until": NOW - 30 * MIN}, "bad_window"),
        ):
            status, out = post(web.server, wrong)
            assert (status, out["code"]) == (400, code), wrong
        status, out = post(web.server, {"since": NOW - 9 * 60 * MIN, "until": NOW - MIN}, FROM_LAN)
        assert out["max_minutes"] == 360 and "from another machine" in out["error"]
    finally:
        web.close()
        store.close()


class Queued:
    """A model queue that holds each call until ``release``: a digest waits as behind a
    backlog."""

    def __init__(self, llm) -> None:
        self.llm = llm
        self.calls: list = []

    def __call__(self, run, given_up) -> bool:
        self.calls.append(run)
        return True

    def release(self) -> None:
        while self.calls:
            self.calls.pop(0)(self.llm)


def test_the_page_cancels_a_digest_by_its_id(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    queue = Queued(FakeLlm("- Water is cut tomorrow [1]."))
    web = Serving(store, queue.llm, submit=queue)
    answers: list = []
    try:
        asking = threading.Thread(
            target=lambda: answers.append(post(web.server, {"id": "page-0001-abc"})), daemon=True
        )
        asking.start()
        deadline = time.monotonic() + 5
        while not queue.calls:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        # The same gate as asking: no header, no cancelling.
        assert call(web.server, "/api/digest/page-0001-abc/cancel", headers={})[0] == 403
        # Another machine can't cancel what this one asked for: as if there were no such id.
        status, out = call(web.server, "/api/digest/page-0001-abc/cancel", headers=FROM_LAN)
        assert (status, out["code"]) == (404, "not_found")
        status, out = call(web.server, "/api/digest/page-0001-abc/cancel")
        assert (status, out) == (200, {"id": "page-0001-abc", "cancelled": True})
        asking.join(5)
        [(status, out)] = answers
        assert (status, out["code"], out["id"]) == (409, "cancelled", "page-0001-abc")
        queue.release()
        assert queue.llm.prompts == []  # it never reached the model
        status, out = call(web.server, "/api/digest/page-0001-abc/cancel")
        assert (status, out["code"]) == (404, "not_found")

        done = threading.Thread(target=lambda: answers.append(post(web.server, {})), daemon=True)
        done.start()
        while not queue.calls:
            time.sleep(0.01)
        queue.release()
        done.join(5)
        made = answers[-1][1]["id"]
        status, out = call(web.server, f"/api/digest/{made}/cancel")
        assert (status, out["code"]) == (409, "finished")
    finally:
        web.close()
        store.close()
