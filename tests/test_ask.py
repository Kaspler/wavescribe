# SPDX-License-Identifier: Apache-2.0
import dataclasses
import http.client
import json
from datetime import datetime
from pathlib import Path

import numpy as np

from tests.test_pipeline import station
from tests.test_store import result
from tests.test_web import start_server
from wavescribe.ask import PLAN_SYSTEM, Asker
from wavescribe.store import Store

NOW = datetime(2026, 3, 4, 12, 0).timestamp()
HOUR = 3600.0


class FakeLlm:
    """Answers the planning prompt with ``plan`` and the answer prompt with ``answer``."""

    model_id = "fake-llm"

    def __init__(self, plan: str | dict, answer: str = "Water is back on [1].") -> None:
        self.plan = plan if isinstance(plan, str) else json.dumps(plan)
        self.answer = answer
        self.prompts: list[list[dict[str, str]]] = []
        self.last_stats = {"prompt_tokens": 1}

    def complete(self, messages, *, max_tokens=512, on_text=None) -> str:
        self.prompts.append(messages)
        text = self.plan if messages[0]["content"] == PLAN_SYSTEM else self.answer
        if on_text:
            for i in range(0, len(text), 5):
                on_text(text[i : i + 5])
        return text


def stocked_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "w.db")
    rows = [
        ("a", "Corte de água amanhã no centro da vila", NOW - 30 * HOUR, "Water cut tomorrow"),
        ("a", "O abastecimento de água foi reposto", NOW - 2 * HOUR, "The water supply is back"),
        ("b", "Previsão de chuva forte para a tarde", NOW - 1 * HOUR, "Heavy rain this afternoon"),
        ("b", "Água engarrafada distribuída no pavilhão", NOW - 0.5 * HOUR, "Bottled water"),
    ]
    for sid, text, t, en in rows:
        r = result(sid, text, t)
        store.add_transcript(r)
        r.translations["en"] = en
        store.add_translations(r)
    return store


def make_asker(store: Store, llm: FakeLlm) -> Asker:
    stations = [station("a"), station("b"), station("c", language="uk")]
    return Asker(store, stations, lambda run: (run(llm), True)[1], clock=lambda: NOW)


def ask(asker: Asker, question: str, history=()) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    assert asker.ask(question, list(history), lambda name, data: events.append((name, data)))
    return events


def test_answers_from_the_matching_utterances_in_the_planned_range(tmp_path: Path) -> None:
    plan = {"stations": [], "from": "2026-03-04 00:00", "to": "2026-03-04 12:00",
            "terms": ["water", "água", "x"], "overview": False}  # fmt: skip
    llm = FakeLlm(plan)
    events = ask(make_asker(stocked_store(tmp_path), llm), "Is there water again?")

    names = [e for e, _ in events]
    assert names[:2] == ["plan", "sources"] and names[-1] == "done" and "text" in names
    assert events[0][1]["terms"] == ["water", "água", "x"]
    sources = events[1][1]["sources"]
    # Yesterday's water cut is outside the range; the rain forecast doesn't match: oldest first.
    assert [s["text"] for s in sources] == [
        "O abastecimento de água foi reposto",
        "Água engarrafada distribuída no pavilhão",
    ]
    assert [s["n"] for s in sources] == [1, 2] and sources[0]["name"] == "a"
    assert "".join(d["text"] for e, d in events if e == "text") == "Water is back on [1]."

    answer_prompt = llm.prompts[1][-1]["content"]
    assert (
        "[1] a, Wed 04 Mar 10:00\n(translated from Portuguese (Portugal)) The water supply"
        in answer_prompt
    )
    assert "Question: Is there water again?" in answer_prompt


def test_plan_prompt_lists_stations_languages_and_the_time(tmp_path: Path) -> None:
    llm = FakeLlm({"terms": ["rain"]})
    ask(make_asker(stocked_store(tmp_path), llm), "Чи буде дощ?")
    prompt = llm.prompts[0][1]["content"]
    assert "- c: c, Ukrainian, nothing stored" in prompt
    assert "- a: a, Portuguese (Portugal), last heard 2026-03-04 10:00" in prompt
    assert "also translated into English" in prompt
    assert "Now: Wednesday 2026-03-04 12:00" in prompt and "Question: Чи буде дощ?" in prompt
    assert "30 min ago 2026-03-04 11:30" in prompt
    assert "Yesterday 2026-03-03 00:00 to 2026-03-04 00:00" in prompt


def test_a_question_about_one_station_and_no_topic_samples_its_time_range(tmp_path: Path) -> None:
    plan = {"stations": ["b", "nope"], "from": "2026-03-04 10:00", "to": "2026-03-04 12:00",
            "terms": [], "overview": True}  # fmt: skip
    events = ask(make_asker(stocked_store(tmp_path), FakeLlm(plan)), "What did b say lately?")
    assert events[0][1]["stations"] == ["b"]  # unknown ids are dropped
    assert [s["station"] for s in events[1][1]["sources"]] == ["b", "b"]


def test_an_unreadable_plan_falls_back_to_the_last_day(tmp_path: Path) -> None:
    events = ask(make_asker(stocked_store(tmp_path), FakeLlm("sorry, no JSON")), "news?")
    plan = events[0][1]
    assert plan["overview"] and plan["stations"] == []
    assert plan["until"] - plan["since"] == 24 * HOUR + 60
    assert len(events[1][1]["sources"]) == 3  # the 30-hour-old one is out


def test_a_plan_cut_off_mid_way_still_counts_and_everything_means_all(tmp_path: Path) -> None:
    reply = '{"stations": ["a", "b", "c"], "from": "2026-03-04 09:00", "terms": ["water", "ág'
    asker = make_asker(stocked_store(tmp_path), FakeLlm(reply))
    plan = asker.parse_plan(reply, NOW)
    assert plan.stations == [] and plan.terms == ["water"] and not plan.overview
    assert plan.since == NOW - 3 * HOUR and plan.until == NOW + 60


def test_an_empty_narrow_range_widens_to_the_last_day(tmp_path: Path) -> None:
    plan = {"terms": ["water"], "from": "2026-03-04 11:50", "to": "2026-03-04 12:00"}
    llm = FakeLlm(plan)
    events = ask(make_asker(stocked_store(tmp_path), llm), "water in the last ten minutes?")
    assert events[0][1]["widened"] is True and len(events[1][1]["sources"]) == 2
    assert "these excerpts are from the last 24 hours" in llm.prompts[1][-1]["content"]


def test_terms_that_match_nothing_fall_back_to_a_sample(tmp_path: Path) -> None:
    plan = {"terms": ["volcano"], "from": "2026-03-04 00:00", "to": "2026-03-04 12:00"}
    llm = FakeLlm(plan)
    events = ask(make_asker(stocked_store(tmp_path), llm), "Any volcano news?")
    assert events[0][1]["sampled"] is True and events[0][1]["overview"] is True
    assert events[0][1]["widened"] is False  # the range asked about had something to sample
    assert len(events[1][1]["sources"]) == 3
    assert (
        "None of these excerpts contain the search words (volcano)" in llm.prompts[1][-1]["content"]
    )


def test_follow_up_questions_carry_the_previous_turn(tmp_path: Path) -> None:
    llm = FakeLlm({"terms": ["rain"]})
    ask(make_asker(stocked_store(tmp_path), llm), "and on b?", [("Water?", "It's back [1].")])
    assert "Earlier question: Water?" in llm.prompts[0][1]["content"]
    assert llm.prompts[1][1:3] == [
        {"role": "user", "content": "Water?"},
        {"role": "assistant", "content": "It's back [1]."},
    ]


def test_a_translator_that_isnt_an_llm_gets_a_clear_error(tmp_path: Path) -> None:
    class Madlad:
        model_id = "madlad"

    store = stocked_store(tmp_path)
    asker = Asker(store, [station("a")], lambda run: (run(Madlad()), True)[1])
    events: list = []
    asker.ask("water?", [], lambda name, data: events.append((name, data)))
    assert events == [("error", {"error": "asking needs an LLM; this run translates with madlad"})]


def test_why_an_answer_failed_stays_on_this_machine(tmp_path: Path) -> None:
    from wavescribe.radio.bandplan import exports

    class Broken(FakeLlm):
        def complete(self, messages, *, max_tokens=512, on_text=None) -> str:
            raise OSError("cannot read /home/owner/models/llm/tokenizer.json")

    store = stocked_store(tmp_path)
    asker = make_asker(store, Broken({}))
    [(name, here)] = ask(asker, "water?")
    assert name == "error" and "/home/owner/models" in here["error"]
    elsewhere: list[tuple[str, dict]] = []
    leaving = exports([station("a"), station("b")])
    assert asker.ask(
        "water?", [], lambda name, data: elsewhere.append((name, data)), exports=leaving
    )
    [(name, told)] = elsewhere
    assert name == "error" and "owner" not in told["error"]
    assert "log on the Wavescribe machine" in told["error"]
    store.close()


def llm_server(store: Store):
    server, hub, pipeline = start_server(np.zeros(1), store)
    entry = dataclasses.replace(hub.plan.mt[0], backend="mlx-lm")  # an LLM translator
    hub.plan = dataclasses.replace(hub.plan, mt=(entry, Path()))
    return server, hub, pipeline


def post_question(server, headers: dict[str, str]) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection(*server.server_address, timeout=10)
    conn.request("POST", "/api/ask", json.dumps({"question": "water?"}), headers)
    return conn.getresponse()


def test_the_web_endpoint_streams_the_answer(tmp_path: Path) -> None:
    store = stocked_store(tmp_path)
    server, hub, pipeline = llm_server(store)
    llm = FakeLlm({"terms": ["water"], "from": "2026-03-04 00:00", "to": "2026-03-04 12:00"})
    hub.asker = make_asker(store, llm)
    try:
        assert hub.state()["ask"] is None  # available
        resp = post_question(server, {"X-Wavescribe": "1"})
        assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"
        text = resp.read().decode()
        assert "event: plan" in text and "event: sources" in text and "event: done" in text
        assert post_question(server, {}).status == 403  # no header: another website's form
    finally:
        hub.close()
        pipeline.stop()
        server.shutdown()
        store.close()


def test_without_an_llm_asking_is_unavailable(tmp_path: Path) -> None:
    store = stocked_store(tmp_path)
    server, hub, pipeline = start_server(np.zeros(1), store)  # its translator isn't an LLM
    try:
        assert "needs an LLM" in hub.state()["ask"]
        assert post_question(server, {"X-Wavescribe": "1"}).status == 409
    finally:
        hub.close()
        pipeline.stop()
        server.shutdown()
        store.close()


def test_asks_are_limited_while_the_model_is_busy(tmp_path: Path) -> None:
    store = stocked_store(tmp_path)
    server, hub, pipeline = llm_server(store)
    hub.asker = Asker(store, [station("a")], lambda run: True)  # queued, never answered
    try:
        for _ in range(3):
            assert hub.asks.acquire(blocking=False)
        assert post_question(server, {"X-Wavescribe": "1"}).status == 429
    finally:
        hub.close()
        pipeline.stop()
        server.shutdown()
        store.close()


def test_a_row_from_a_wrong_clock_does_not_end_the_answer(tmp_path: Path) -> None:
    store = stocked_store(tmp_path)
    store.add_transcript(result("b", "Hora errada", 1e13))  # past year 9999: a wrong clock
    llm = FakeLlm({"terms": ["água"]})
    events = ask(make_asker(store, llm), "Há água?")
    assert events[-1][0] == "done"
    assert "last heard ?" in llm.prompts[0][1]["content"]
