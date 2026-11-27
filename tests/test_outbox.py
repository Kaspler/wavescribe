# SPDX-License-Identifier: Apache-2.0
"""Alerts on their way to an output outlive a restart: the store's outbox, the delivery id the
other end tells a repeat by, the age limit, and what may sit on disk."""

import http.client
import json
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_outputs import ALERT, NODE, STATIONS, UTTERANCE, Recorder, wait_for
from tests.test_pipeline import station
from wavescribe import outputs as module
from wavescribe.events import alert_event, to_json
from wavescribe.outputs import OutputConfig, OutputError, Outputs, sign
from wavescribe.radio.bandplan import apply_policies
from wavescribe.store import Store

ID = "X-Wavescribe-Delivery"
HOOK = {"id": "hook", "url": "http://h/x"}
WORDS = "A burst pipe has closed the old bridge road."  # invented, as every sentence here is


@pytest.fixture(autouse=True)
def short_waits(monkeypatch: pytest.MonkeyPatch):
    """While the other end is away an output tries again after 5 to 60 s: here, at once."""
    monkeypatch.setattr(module, "HOLD_MIN_S", 0.01)
    monkeypatch.setattr(module, "HOLD_MAX_S", 0.03)
    yield
    apply_policies()  # each test starts from the default privacy policies


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class Hook:
    """The other end: away (nothing comes back) until ``up`` is set; records every request."""

    def __init__(self, up: bool = False) -> None:
        self.up = threading.Event()
        if up:
            self.up.set()
        self.refused: list[tuple[str, bytes, dict[str, str]]] = []
        self.got: list[tuple[str, bytes, dict[str, str]]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        if not self.up.is_set():
            self.refused.append((url, body, headers))
            raise OSError("connection refused")
        self.got.append((url, body, headers))
        return 200

    def ids(self, requests: list | None = None) -> list[str]:
        return [h[ID] for _, _, h in (self.got if requests is None else requests)]

    def json(self, i: int = -1) -> dict:
        return json.loads(self.got[i][1])


def outputs(raw: list[dict], transport, store=None, clock=time.time, stations=STATIONS) -> Outputs:
    return Outputs(
        [OutputConfig.from_dict(r) for r in raw],
        store=store,
        stations=lambda: stations,
        node=lambda: NODE,
        transport=transport,
        sleep=lambda s: None,
        clock=clock,
    )


def alert(i: int = 3, **changes) -> dict:
    return {**ALERT, "id": i, **changes}


def test_an_alert_raised_while_the_other_end_is_away_goes_after_a_restart(tmp_path: Path) -> None:
    hook = Hook()
    store = Store(tmp_path / "w.db")
    outs = outputs([{**HOOK, "secret": "s3cret"}], hook, store)
    outs.on_alert(alert(), "high", WORDS, {"en": WORDS})
    wait_for(lambda: len(hook.refused) > 6)  # more than one round of tries
    [first] = set(hook.ids(hook.refused))  # every try carries the same id
    [row] = store.outbox()
    assert row["output_id"] == "hook" and row["delivery_id"] == first and row["attempts"] >= 6
    assert outs.status()["hook"]["alerts_waiting"] == 1
    outs.close()
    store.close()

    store = Store(tmp_path / "w.db")  # the next run, with the other end back
    hook.up.set()
    outs = outputs([{**HOOK, "secret": "s3cret"}], hook, store)
    wait_for(lambda: hook.got and not store.outbox())
    [(_, body, headers)] = hook.got
    assert headers[ID] == first and json.loads(body)["delivery_id"] == first
    assert json.loads(body)["text"] == WORDS and json.loads(body)["id"] == 3
    # The id is in the JSON body, so the signature covers it.
    assert headers["X-Wavescribe-Signature"] == sign("s3cret", headers["X-Wavescribe-Timestamp"],
                                                     body)  # fmt: skip
    entry = outs.recent("hook")[0]
    assert entry["ok"] and entry["after_restart"] is True and entry["delivery"] == first
    assert all(e["after_restart"] is False for e in outs.recent("hook")[1:])  # the first run's
    status = outs.status()["hook"]
    assert status["alerts_waiting"] == 0 and status["alerts_oldest_age_s"] is None
    outs.close()
    store.close()


def test_a_delivery_id_is_one_alert_to_one_output_and_says_nothing_else() -> None:
    hook = Hook(up=True)
    outs = outputs([HOOK, {"id": "other", "url": "http://h/y", "format": "text"}], hook)
    outs.on_alert(alert(), "high", WORDS, {})
    outs.on_alert(alert(change="reminder"), "high", WORDS, {})  # the same alert, said again
    wait_for(lambda: len(hook.got) == 4)
    ids = hook.ids()
    assert len(set(ids)) == 4  # each its own: per output, and per time the alert is announced
    assert all(len(i) == 32 and int(i, 16) >= 0 for i in ids)  # random: 32 hex digits
    text = next(body for url, body, _ in hook.got if url == "http://h/y").decode()
    assert not any(i in text for i in ids)  # plain text has the id in the header only
    outs.close()


def test_an_alert_too_old_to_be_news_is_given_up_and_counted(tmp_path: Path) -> None:
    hook, clock = Hook(), Clock()
    store = Store(tmp_path / "w.db")
    outs = outputs([HOOK], hook, store, clock)
    outs.on_alert(alert(), "high", WORDS, {})
    wait_for(lambda: hook.refused)
    assert outs.status()["hook"]["alerts_oldest_age_s"] == 0.0
    clock.now += 3600
    assert outs.status()["hook"]["alerts_oldest_age_s"] == 3600.0
    outs.close()
    store.close()

    clock.now += 24 * 3600  # a day and an hour after it was raised
    hook.up.set()
    store = Store(tmp_path / "w.db")
    outs = outputs([HOOK], hook, store, clock)
    wait_for(lambda: outs.status()["hook"]["alerts_expired"] == 1 and not store.outbox())
    assert hook.got == []
    status = outs.status()["hook"]
    assert status["alerts_waiting"] == 0 and status["sent"] == 0 and status["failed"] == 0
    entry = outs.recent("hook")[0]
    assert not entry["ok"] and entry["after_restart"] and entry["ref"] == "3"
    assert entry["error"] == "not delivered within 24 h of the alert: given up"
    outs.close()
    store.close()


def test_the_age_limit_is_each_outputs_own_and_never_off(tmp_path: Path) -> None:
    assert OutputConfig.from_dict(HOOK).alert_max_age_hours == 24
    assert OutputConfig.from_dict({**HOOK, "alert_max_age_hours": 0.5}).alert_max_age_hours == 0.5
    for bad in (0, -1, 169, "soon", None, float("nan")):
        with pytest.raises(OutputError, match="alert_max_age_hours is more than 0"):
            OutputConfig.from_dict({**HOOK, "alert_max_age_hours": bad})
    hook, clock = Hook(), Clock()
    outs = outputs([{**HOOK, "alert_max_age_hours": 2}], hook, Store(tmp_path / "w.db"), clock)
    outs.on_alert(alert(), "high", WORDS, {})
    wait_for(lambda: hook.refused)
    clock.now += 2 * 3600 + 1  # while it runs, too: at the next try
    wait_for(lambda: outs.status()["hook"]["alerts_expired"] == 1 and not outs.store.outbox())
    tried = len(hook.refused)
    time.sleep(0.1)
    assert len(hook.refused) == tried
    assert outs.recent()[0]["error"] == "not delivered within 2 h of the alert: given up"
    outs.close()
    outs.store.close()


def test_what_waited_for_an_output_that_is_gone_is_dropped(tmp_path: Path) -> None:
    hook = Hook()
    store = Store(tmp_path / "w.db")
    outs = outputs([HOOK, {"id": "other", "url": "http://h/y"}], hook, store)
    outs.on_alert(alert(), "high", WORDS, {})
    wait_for(lambda: {url for url, _, _ in hook.refused} == {"http://h/x", "http://h/y"})
    assert {r["output_id"] for r in store.outbox()} == {"hook", "other"}
    outs.configure([OutputConfig.from_dict(HOOK)])  # "other" is removed while it runs
    assert [r["output_id"] for r in store.outbox()] == ["hook"]
    outs.close()
    store.close()

    store = Store(tmp_path / "w.db")  # and "hook" while it was stopped
    hook.up.set()
    outs = outputs([{"id": "new", "url": "http://h/z"}], hook, store)
    assert store.outbox() == []
    time.sleep(0.1)
    assert hook.got == [] and outs.status()["new"]["alerts_waiting"] == 0
    outs.close()
    store.close()


def test_a_changed_output_goes_on_with_the_alerts_it_still_asks_for(tmp_path: Path) -> None:
    sent: list[tuple[str, dict, str]] = []

    def transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        if url.endswith("/old"):
            raise OSError("no route to host")
        sent.append((url, json.loads(body), headers[ID]))
        return 200

    store = Store(tmp_path / "w.db")
    old = {"id": "hook", "url": "http://h/old", "min_severity": "low"}
    outs = outputs([old], transport, store)
    outs.on_alert(alert(1), "high", WORDS, {"en": WORDS, "fr": "…"})
    outs.on_alert(alert(2, rule="power"), "low", WORDS, {})
    wait_for(lambda: len(store.outbox()) == 2 and outs.status()["hook"]["waiting"])
    ids = {json.loads(r["event"])["id"]: r["delivery_id"] for r in store.outbox()}
    # The address is put right, and the output narrowed: one language, medium and up.
    outs.configure([OutputConfig.from_dict({"id": "hook", "url": "http://h/new",
                                            "languages": ["en"]})])  # fmt: skip
    wait_for(lambda: sent and not store.outbox())
    [(url, body, delivery)] = sent
    assert url == "http://h/new" and body["id"] == 1 and delivery == ids[1]
    assert body["translations"] == {"en": WORDS}  # as the output sends alerts now
    assert outs.recent("hook")[0]["after_restart"] is False
    assert outs.status()["hook"]["alerts_waiting"] == 0  # the low one is no longer asked for
    outs.close()
    store.close()


def test_an_output_switched_off_sends_nothing_and_keeps_what_waits(tmp_path: Path) -> None:
    hook, clock = Hook(), Clock()
    store = Store(tmp_path / "w.db")
    outs = outputs([HOOK], hook, store, clock)
    outs.on_alert(alert(1), "high", WORDS, {})
    outs.on_alert(alert(2), "high", WORDS, {})
    wait_for(lambda: hook.refused)
    outs.configure([OutputConfig.from_dict({**HOOK, "enabled": False})])
    hook.up.set()
    time.sleep(0.1)
    assert hook.got == [] and outs.status()["hook"]["alerts_waiting"] == 2
    outs.close()
    store.close()
    store = Store(tmp_path / "w.db")  # still off after a restart: still waiting
    outs = outputs([{**HOOK, "enabled": False}], hook, store, clock)
    time.sleep(0.1)
    assert hook.got == [] and len(store.outbox()) == 2
    outs.configure([OutputConfig.from_dict(HOOK)])  # on again: they go, the oldest first
    wait_for(lambda: len(hook.got) == 2 and not store.outbox())
    assert [hook.json(0)["id"], hook.json(1)["id"]] == [1, 2]
    outs.close()
    store.close()


def test_the_oldest_alerts_make_room_when_too_many_wait(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(module, "QUEUE_MAX", 3)
    store = Store(tmp_path / "w.db")
    for i in range(1, 6):  # five were waiting when the last run ended
        ev = alert_event(alert(i), STATIONS, NODE, text=WORDS)
        store.outbox_add("hook", f"{i:032x}", to_json(ev), 1_800_000_000.0 + i)
    hook, clock = Hook(), Clock()
    clock.now += 60
    outs = outputs([HOOK], hook, store, clock)
    status = outs.status()["hook"]
    assert status["alerts_waiting"] == 3 and status["alerts_dropped"] == 2
    assert status["dropped"] == 2  # counted with everything else a full queue dropped
    assert [json.loads(r["event"])["id"] for r in store.outbox()] == [3, 4, 5]
    lost = [e for e in outs.recent() if e["error"] and "dropped" in e["error"]]
    assert sorted(e["ref"] for e in lost) == ["1", "2"]
    assert lost[0]["error"] == "more than 3 events were waiting: dropped"
    # And while it runs: a sixth comes in, the oldest that waits goes.
    wait_for(lambda: outs.status()["hook"]["waiting"])
    outs.on_alert(alert(6), "high", WORDS, {})
    wait_for(lambda: outs.status()["hook"]["alerts_dropped"] == 3)
    hook.up.set()
    wait_for(lambda: len(hook.got) == 3 and not store.outbox())
    assert sorted(json.loads(body)["id"] for _, body, _ in hook.got) == [4, 5, 6]
    assert outs.status()["hook"]["alerts_waiting"] == 0
    outs.close()
    store.close()


def test_utterances_wait_in_memory_only(tmp_path: Path) -> None:
    hook = Hook()
    store = Store(tmp_path / "w.db")
    feed = {"id": "feed", "url": "http://h/in", "events": ["utterance", "alert"]}
    outs = outputs([feed], hook, store)
    outs.on_utterance({**UTTERANCE, "text": WORDS})
    wait_for(lambda: hook.refused and outs.status()["feed"]["queued"] == 1)
    assert store.outbox() == [] and ID not in hook.refused[0][2]
    assert outs.status()["feed"]["alerts_waiting"] == 0
    outs.close()
    store.close()
    hook.up.set()
    store = Store(tmp_path / "w.db")
    outs = outputs([feed], hook, store)
    time.sleep(0.1)
    assert hook.got == []  # a restart emptied the queue
    outs.close()
    store.close()


def test_without_a_store_alerts_wait_in_memory_with_the_same_id() -> None:
    hook = Hook()
    outs = outputs([HOOK], hook)  # --no-store
    outs.on_alert(alert(), "high", WORDS, {})
    wait_for(lambda: len(hook.refused) > 6)
    status = outs.status()["hook"]
    assert status["alerts_waiting"] == 1 and status["waiting"] is True
    hook.up.set()
    wait_for(lambda: hook.got)
    assert set(hook.ids(hook.refused)) == set(hook.ids()) and len(hook.got) == 1
    assert hook.json()["delivery_id"] == hook.ids()[0]
    assert outs.recent()[0]["delivery"] == hook.ids()[0]
    assert outs.status()["hook"]["alerts_waiting"] == 0
    outs.close()
    again = outputs([HOOK], hook)  # nothing is carried to the next run
    assert again.status()["hook"]["alerts_waiting"] == 0
    again.close()


def test_a_crash_between_the_delivery_and_its_row_going_sends_it_again(tmp_path: Path) -> None:
    hook = Hook(up=True)
    store = Store(tmp_path / "w.db")

    def crashed(ids) -> None:
        raise sqlite3.OperationalError("the process ended here")

    store.outbox_remove = crashed  # type: ignore[method-assign]
    outs = outputs([HOOK], hook, store)
    outs.on_alert(alert(), "high", WORDS, {})
    wait_for(lambda: outs.status()["hook"]["sent"] == 1)
    outs.close()
    del store.outbox_remove
    assert len(store.outbox()) == 1  # delivered, and still there
    store.close()

    store = Store(tmp_path / "w.db")
    outs = outputs([HOOK], hook, store)
    wait_for(lambda: len(hook.got) == 2 and not store.outbox())
    first, second = hook.ids()
    assert first == second  # a repeat the other end can tell, never a loss
    assert outs.recent("hook")[0]["after_restart"] is True
    outs.close()
    store.close()


def test_closing_keeps_what_waits_and_stops_trying(tmp_path: Path) -> None:
    hook = Hook()
    store = Store(tmp_path / "w.db")
    outs = outputs([HOOK], hook, store)
    for i in range(1, 4):
        outs.on_alert(alert(i), "high", WORDS, {})
    wait_for(lambda: hook.refused)
    outs.close()
    tried = len(hook.refused)
    assert [json.loads(r["event"])["id"] for r in store.outbox()] == [1, 2, 3]
    hook.up.set()
    time.sleep(0.1)
    assert hook.got == [] and len(hook.refused) == tried
    store.close()


def test_a_stopped_output_does_not_try_again_after_its_wait() -> None:
    rec = Recorder([503, 200])
    outs: Outputs

    def sleep(seconds: float) -> None:
        outs.configure([])  # the output is removed while it waits between two tries

    outs = Outputs([OutputConfig.from_dict(HOOK)], stations=lambda: STATIONS, node=lambda: NODE,
                   transport=rec, sleep=sleep)  # fmt: skip
    outs.on_alert(alert(), "high", WORDS, {})
    wait_for(lambda: outs.recent())
    time.sleep(0.1)
    assert len(rec.requests) == 1 and outs.recent()[0]["attempts"] == 1
    outs.close()


def test_an_answer_that_is_not_http_counts_as_no_answer() -> None:
    rec = Recorder([http.client.BadStatusLine("garbage"), http.client.IncompleteRead(b""), 200])
    outs = outputs([HOOK], rec)
    outs.on_alert(alert(), "high", WORDS, {})
    wait_for(lambda: outs.status()["hook"]["sent"] == 1)
    assert outs.recent()[0]["attempts"] == 3 and outs.status()["hook"]["alerts_waiting"] == 0
    outs.close()


def test_after_a_restart_an_output_waits_for_the_try_that_was_planned(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(module, "HOLD_MIN_S", 0.6)
    monkeypatch.setattr(module, "HOLD_MAX_S", 0.6)
    hook = Hook()
    store = Store(tmp_path / "w.db")
    outs = outputs([HOOK], hook, store)
    outs.on_alert(alert(), "high", WORDS, {})
    wait_for(lambda: store.outbox() and store.outbox()[0]["next_at"] > 0)
    planned = store.outbox()[0]["next_at"]
    outs.close()
    hook.up.set()
    outs = outputs([HOOK], hook, store)  # restarted at once
    assert outs.status()["hook"]["alerts_waiting"] == 1
    wait_for(lambda: hook.got)
    assert time.time() >= planned - 0.05  # not sooner, and no later than the longest wait
    outs.close()
    store.close()


def test_the_status_says_what_waits_for_each_output() -> None:
    outs = outputs([HOOK], Hook(up=True))
    assert outs.status() == {"hook": {
        "sent": 0, "failed": 0, "dropped": 0, "alerts_expired": 0, "alerts_dropped": 0,
        "last_ok_at": None, "last_error": None, "last_error_at": None, "last_status": None,
        "waiting": False, "queued": 0, "alerts_waiting": 0, "alerts_oldest_age_s": None,
    }}  # fmt: skip
    entry = outs.test(outs.configs()[0])
    assert set(entry) == {"t", "output", "event", "ref", "ok", "status", "error", "attempts",
                          "ms", "delivery", "after_restart"}  # fmt: skip
    assert len(entry["delivery"]) == 32 and entry["after_restart"] is False
    outs.close()


# -- what may sit on disk -----------------------------------------------------------------------


def radio(privacy: str) -> dict:
    return {"a": station("a"), "p": replace(station("p"), privacy=privacy)}


def test_an_alert_from_radio_that_stays_here_waits_without_its_words(tmp_path: Path) -> None:
    hook = Hook()
    store = Store(tmp_path / "w.db")
    outs = outputs([HOOK], hook, store, stations=radio("utility"))  # not exported, by default
    outs.on_alert(alert(stations=["p"], privacy=["utility"]), "high", WORDS, {"en": WORDS})
    wait_for(lambda: hook.refused)
    [row] = store.outbox()
    assert row["privacy"] is None and WORDS not in row["event"]
    assert "bridge" not in row["event"] and json.loads(row["event"])["rule"] == "water"
    outs.close()
    store.close()


def test_words_the_store_does_not_keep_never_reach_the_outbox(tmp_path: Path) -> None:
    # The user lets private radio's words leave in alerts; its text is still never kept.
    apply_policies({"private": {"export": True, "quote_in_alerts": True}})
    hook = Hook()
    store = Store(tmp_path / "w.db")
    outs = outputs([HOOK], hook, store, stations=radio("private"))
    outs.on_alert(alert(stations=["p"], privacy=["private"], first_at=time.time(),
                        segment_id=None), "high", WORDS, {})  # fmt: skip
    wait_for(lambda: hook.refused)
    assert store.outbox() == []  # in memory only
    assert outs.status()["hook"]["alerts_waiting"] == 1
    hook.up.set()
    wait_for(lambda: hook.got)
    assert hook.json()["text"] == WORDS and hook.ids() == hook.ids(hook.refused)[:1]
    outs.close()
    store.close()


def test_a_waiting_alert_leaves_the_disk_when_its_words_would(tmp_path: Path) -> None:
    apply_policies({"amateur": {"export": True}})  # kept for 7 days, by default
    hook = Hook()
    store = Store(tmp_path / "w.db")
    outs = outputs([HOOK], hook, store, stations=radio("amateur"))
    heard = time.time() - 2 * 86400
    outs.on_alert(alert(1, stations=["p"], privacy=["amateur"], first_at=heard, segment_id=None),
                  "high", WORDS, {})  # fmt: skip
    outs.on_alert(alert(2, first_at=heard, segment_id=None), "high", WORDS, {})  # broadcast
    wait_for(lambda: len(store.outbox()) == 2)
    assert [r["privacy"] for r in store.outbox()] == [["amateur"], ["broadcast"]]
    assert store.prune_outbox() == 0
    apply_policies({"amateur": {"export": True, "text_days": 1}})  # shortened while it waits
    assert store.prune_outbox() == 1
    assert [json.loads(r["event"])["id"] for r in store.outbox()] == [2]
    assert outs.status()["hook"]["alerts_waiting"] == 2  # still on its way, from memory
    # Text kept for less time than an alert may wait: the words don't outstay it on disk.
    store.set_retention(text_days=1 / 24)
    store.outbox_add("hook", "f" * 32, "{}", time.time() - 2 * 3600, privacy=["broadcast"])
    store.outbox_add("hook", "e" * 32, "{}", time.time() - 2 * 3600)  # no words: it stays
    assert store.prune_outbox() == 1
    assert [r["delivery_id"] for r in store.outbox()][1:] == ["e" * 32]
    store.outbox_remove(["e" * 32])
    hook.up.set()
    wait_for(lambda: len(hook.got) == 2)
    assert sorted(json.loads(body)["id"] for _, body, _ in hook.got) == [1, 2]
    outs.close()
    store.close()


def test_after_a_restart_words_that_may_no_longer_leave_do_not(tmp_path: Path) -> None:
    hook = Hook()
    store = Store(tmp_path / "w.db")
    outs = outputs([HOOK], hook, store)
    outs.on_alert(alert(), "high", WORDS, {"en": WORDS})
    wait_for(lambda: hook.refused)
    first = store.outbox()[0]["delivery_id"]
    assert WORDS in store.outbox()[0]["event"]
    outs.close()
    # The station is deleted before the next run: nothing says its words may leave.
    outs = outputs([HOOK], hook, store, stations={"b": station("b")})
    [row] = store.outbox()
    assert WORDS not in row["event"] and row["privacy"] is None and row["delivery_id"] == first
    hook.up.set()
    wait_for(lambda: hook.got)
    body = hook.json()
    assert body["text"] == "" and body["translations"] == {} and body["terms"] == []
    assert body["rule"] == "water" and hook.ids() == [first]
    assert WORDS.encode() not in hook.got[0][1]
    outs.close()
    store.close()


# -- the store ----------------------------------------------------------------------------------


def test_a_data_directory_from_before_the_outbox_opens(tmp_path: Path) -> None:
    Store(tmp_path / "w.db").close()
    old = sqlite3.connect(tmp_path / "w.db")
    old.execute("DROP TABLE outbox")
    old.execute("ALTER TABLE deliveries DROP COLUMN delivery")
    old.execute("ALTER TABLE deliveries DROP COLUMN after_restart")
    old.execute(
        "INSERT INTO deliveries (output_id, event, ref, t, ok, status, attempts, ms) "
        "VALUES ('hook', 'alert', '1', 1000, 1, 200, 1, 12.5)"
    )
    old.execute("PRAGMA user_version = 7")
    old.commit()
    old.close()
    store = Store(tmp_path / "w.db")
    assert store.outbox() == []
    hook = Hook(up=True)
    outs = outputs([HOOK], hook, store)
    outs.on_alert(alert(), "high", WORDS, {})
    wait_for(lambda: hook.got and not store.outbox())
    new, before = outs.recent("hook")
    assert new["delivery"] == hook.ids()[0] and new["after_restart"] is False
    assert before["delivery"] is None and before["after_restart"] is False and before["ok"]
    outs.close()
    store.close()
    db = sqlite3.connect(tmp_path / "w.db")
    assert db.execute("PRAGMA user_version").fetchone()[0] == 8
    db.close()


def test_a_row_nothing_can_read_is_dropped_not_tried_forever(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    store.outbox_add("hook", "a" * 32, "not json", time.time())
    store.outbox_add("hook", "b" * 32, '{"type": "utterance"}', time.time())
    hook = Hook(up=True)
    outs = outputs([HOOK], hook, store)
    assert store.outbox() == [] and outs.status()["hook"]["alerts_waiting"] == 0
    outs.close()
    store.close()


def test_outputs_working_at_once_leave_the_outbox_whole(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    lock = threading.Lock()
    calls = [0]
    got: list[str] = []

    def flaky(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        with lock:
            calls[0] += 1
            n = calls[0]
        if n % 3 == 0:
            raise OSError("connection reset")
        if n % 7 == 0:
            return 503
        with lock:
            got.append(headers[ID])
        return 200

    names = [f"out-{i}" for i in range(4)]
    outs = outputs([{"id": n, "url": f"http://h/{n}"} for n in names], flaky, store)

    def raise_alerts(first: int) -> None:
        for i in range(25):
            outs.on_alert(alert(first + i), "high", WORDS, {})

    threads = [threading.Thread(target=raise_alerts, args=(100 * t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wait_for(lambda: len(got) == 400 and not store.outbox(), timeout=20)
    assert len(set(got)) == 400  # every alert to every output, once each
    status = outs.status()
    assert all(status[n]["sent"] == 100 and status[n]["alerts_waiting"] == 0 for n in names)
    outs.close()
    assert store._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    store.close()
