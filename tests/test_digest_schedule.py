# SPDX-License-Identifier: Apache-2.0
"""Digests on a schedule, shown and heard through fakes: no notification or sound is made."""

import itertools
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

import tests.test_app
from tests.test_alerts import FakeActions
from tests.test_app import Running, make_app, wait_for
from tests.test_digest import MIN, NOW, STATIONS, FakeLlm, add, digester, stocked
from wavescribe.alerts import Voice
from wavescribe.config import ConfigError
from wavescribe.digest import Digester
from wavescribe.digest.schedule import MIN_GAP_S, Schedule, Scheduler, parse_schedule, scheduler
from wavescribe.digest.speech import Speaker, Speech
from wavescribe.radio.bandplan import exports

VOICES = {"en": Voice("Lesya", "An alert.")}
TWO_POINTS = "- Water is cut tomorrow [1].\n- Heavy rain this afternoon [2]."


def shown(actions: FakeActions) -> list[tuple[str, str]]:
    return [(c[1], c[2]) for c in actions.calls if c[0] == "show"]


def said(actions: FakeActions) -> list[str]:
    return [c[2] for c in actions.calls if c[0] == "speak"]


@pytest.fixture
def lisbon(monkeypatch: pytest.MonkeyPatch):
    """The local clock of a place whose clocks change: on Sun 29 Mar 2026 at 01:00 they go to
    02:00, on Sun 25 Oct 2026 at 02:00 back to 01:00."""
    monkeypatch.setenv("TZ", "Europe/Lisbon")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def wall(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


# -- the table ----------------------------------------------------------------------------------


def parse(**raw):
    return parse_schedule(raw, "alerts.toml", voices=VOICES, language="en")


def test_the_schedule_is_read_from_its_table() -> None:
    assert parse_schedule(None, "alerts.toml", voices=VOICES, language="en") is None
    assert parse() == Schedule(60, "en")  # on the hour
    assert parse(every_minutes=30, stations=["a", "a", "b"], speak=True) == Schedule(
        30, "en", ("a", "b"), speak=True
    )
    assert parse(quiet_hours=["22:00", "7:00"]).quiet == (1320, 420)


@pytest.mark.parametrize(
    ("raw", "says", "code"),
    [
        ({"every_minutes": 7}, "every_minutes must divide a day into equal parts", "bad_every"),
        ({"every_minutes": 2}, "every_minutes must divide a day", "bad_every"),
        ({"stations": "a"}, "stations must be a list of station ids", "bad_stations"),
        ({"language": "English"}, "language must be a BCP-47 tag", "bad_language"),
        ({"speak": "yes"}, "speak is true or false", "bad_speak"),
        ({"speak": True, "language": "uk"}, r"speak = true needs a \[speak.uk\] voice", "no_voice"),
        ({"quiet_hours": ["22:00"]}, "quiet_hours is a start and an end", "bad_quiet_hours"),
        (
            {"quiet_hours": ["22:00", "25:00"]},
            "quiet_hours is a start and an end",
            "bad_quiet_hours",
        ),
        (
            {"quiet_hours": ["00:00", "00:00"]},
            "quiet_hours starts and ends at the same time",
            "bad_quiet_hours",
        ),
        ({"notify": True}, r"unknown keys \['notify'\]", "unknown_keys"),
    ],
)
def test_a_schedule_written_wrongly_says_what_to_change(raw, says, code) -> None:
    with pytest.raises(ConfigError, match=r"alerts.toml \[digest\]: " + says) as refused:
        parse(**raw)
    assert refused.value.code == code
    # The page's: the same refusal, without a file to name.
    with pytest.raises(ConfigError, match="^" + says) as refused:
        parse_schedule(raw, None, voices=VOICES, language="en")
    assert refused.value.code == code


def test_a_refusal_names_the_field_and_what_it_takes() -> None:
    with pytest.raises(ConfigError) as refused:
        parse(every_minutes=7)
    assert refused.value.extra["field"] == "every_minutes"
    assert refused.value.extra["choices"][:6] == [5, 6, 8, 9, 10, 12]
    assert refused.value.extra["choices"][-1] == 1440 and 7 not in refused.value.extra["choices"]
    with pytest.raises(ConfigError) as refused:
        parse(speak=True, language="uk")
    assert refused.value.extra == {"field": "speak", "language": "uk"}


def test_a_station_the_schedule_names_must_be_set_up(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    with pytest.raises(ConfigError, match=r"\[digest\]: no station 'tfs' is set up"):
        scheduler(
            {"stations": ["a", "tfs"]},
            "alerts.toml",
            digester=digester(store),
            speaker=Speaker(Speech(FakeActions()), lambda: VOICES, threading.Event()),
            voices=VOICES,
            language="en",
            exports=lambda: exports(STATIONS),
            closed=threading.Event(),
        )
    store.close()


# -- when -------------------------------------------------------------------------------------


def test_digests_come_at_whole_periods_of_the_local_clock(lisbon) -> None:
    hourly, quarterly = Schedule(60, "en"), Schedule(15, "en")
    assert hourly.next_run(wall("2026-03-04 11:52")) == wall("2026-03-04 12:00")
    assert hourly.next_run(wall("2026-03-04 12:00")) == wall("2026-03-04 13:00")
    assert quarterly.next_run(wall("2026-03-04 12:01")) == wall("2026-03-04 12:15")
    night = Schedule(60, "en", quiet=(22 * 60, 7 * 60))
    moments = ("2026-03-04 23:30", "2026-03-04 06:59", "2026-03-04 07:00", "2026-03-04 12:00")
    assert [night.is_quiet(wall(m)) for m in moments] == [True, True, False, False]


def test_the_next_digest_made_is_the_first_outside_the_quiet_hours(lisbon) -> None:
    night = Schedule(60, "en", quiet=(22 * 60, 7 * 60))
    assert night.next_delivery(wall("2026-03-04 20:30")) == wall("2026-03-04 21:00")
    assert night.next_delivery(wall("2026-03-04 21:30")) == wall("2026-03-05 07:00")
    assert Schedule(15, "en").next_delivery(wall("2026-03-04 21:31")) == wall("2026-03-04 21:45")


def test_a_change_of_the_clocks_never_puts_the_next_digest_in_the_past(lisbon) -> None:
    hourly = Schedule(60, "en")
    # Forward: 01:00 doesn't happen, the next is 02:00, half an hour after 00:30.
    early = wall("2026-03-29 00:30")
    assert hourly.next_run(early) - early == 30 * MIN
    for moment in ("2026-03-29 03:30", "2026-03-29 12:10", "2026-03-29 23:59"):
        now = wall(moment)
        assert 0 < hourly.next_run(now) - now <= 60 * MIN
    # Back: 01:00 happens twice, an hour apart, and each has its digest.
    first = hourly.next_run(wall("2026-10-25 00:30"))
    second = hourly.next_run(first)
    assert second - first == 60 * MIN and hourly.next_run(second) - second == 60 * MIN


def run_day(schedule: Schedule, start: float, end: float) -> list[float]:
    """The times the scheduler's loop makes a digest between ``start`` and ``end``, with a
    clock that moves on as far as the loop waits."""
    clock, ticks = [start], []

    def wait(seconds: float) -> bool:
        assert len(ticks) < 1000, "digests without end: the loop doesn't wait"
        clock[0] += seconds
        return clock[0] > end

    class Counting(Scheduler):
        def tick(self):
            ticks.append(clock[0])

    loop = Counting(
        schedule,
        None,
        None,
        exports=lambda: exports(STATIONS),
        closed=threading.Event(),
        clock=lambda: clock[0],
        wait=wait,
    )
    loop._loop()
    return ticks


def test_a_day_has_as_many_hourly_digests_as_hours(lisbon) -> None:
    for day, after, hours in (
        ("2026-03-04", "2026-03-05", 24),
        ("2026-03-29", "2026-03-30", 23),
        ("2026-10-25", "2026-10-26", 25),
    ):
        ticks = run_day(Schedule(60, "en"), wall(f"{day} 00:00") + 30, wall(f"{after} 00:00") + 30)
        assert len(ticks) == hours, day
        assert min(b - a for a, b in itertools.pairwise(ticks)) >= 60 * MIN - 1


def test_a_schedule_that_goes_wrong_still_waits_between_digests(lisbon) -> None:
    class Behind(Schedule):
        def next_run(self, now: float) -> float:
            return now - 3600  # as an hour of a day with a change of the clocks once did

    start = wall("2026-03-29 12:10")
    ticks = run_day(Behind(60, "en"), start, start + 10 * MIN)
    assert 9 <= len(ticks) <= 11 and all(b - a >= MIN_GAP_S for a, b in itertools.pairwise(ticks))


# -- what is delivered --------------------------------------------------------------------------


def scheduled(store, llm, schedule: Schedule, actions, *, clock=None, submit=None, **kw):
    clock = clock or (lambda: NOW)

    def at_once(run, given_up) -> bool:
        run(llm)
        return True

    speaker = Speaker(Speech(actions), lambda: VOICES, threading.Event())
    return Scheduler(
        schedule,
        Digester(store, lambda: STATIONS, submit or at_once, clock=clock),
        speaker,
        exports=lambda: exports(STATIONS),
        closed=threading.Event(),
        clock=clock,
        **kw,
    )


def test_a_scheduled_digest_is_shown_as_a_summary_and_its_points_read_aloud(tmp_path) -> None:
    store = stocked(tmp_path)
    actions = FakeActions()
    digest = scheduled(store, FakeLlm(TWO_POINTS), Schedule(60, "en", speak=True), actions).tick()
    assert digest is not None and digest.request.stations == ("a", "b")
    assert actions.calls[0] == (
        "show",  # never with the alerts' sound
        "Wavescribe summary, 11:00-12:00",
        "a: Water is cut tomorrow.\nb: Heavy rain this afternoon.\nWritten by the local model "
        "from 3 of 6 lines heard between 11:00 and 12:00; it can miss things or get them wrong.",
    )
    assert said(actions)[-2:] == ["a: Water is cut tomorrow.", "b: Heavy rain this afternoon."]
    store.close()


def test_whatever_became_of_a_scheduled_digest_is_shown(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    actions = FakeActions()
    # Nothing of substance: said so, and nothing is read aloud.
    scheduled(store, FakeLlm("NONE: x."), Schedule(60, "en", speak=True), actions).tick()
    assert shown(actions)[-1][1].startswith("Nothing stood out in the 3 lines read of 6 said.\n")
    assert said(actions) == []
    # Nothing said on a in the last quarter of an hour: the model isn't asked.
    llm = FakeLlm("- x [1].")
    scheduled(store, llm, Schedule(15, "en", ("a",)), actions).tick()
    assert (
        shown(actions)[-1][1] == "Nothing was said on a in the last 15 minutes." and not llm.prompts
    )
    # Quiet hours: nothing at all.
    before = len(actions.calls)
    assert scheduled(store, llm, Schedule(60, "en", quiet=(660, 780)), actions).tick() is None
    assert len(actions.calls) == before
    store.close()


def test_scheduled_digests_cover_the_time_between_them_whatever_was_skipped(tmp_path) -> None:
    store = stocked(tmp_path)
    add(
        store,
        "a",
        "Previsão do tempo para amanhã de manhã",
        NOW + 30 * MIN,
        en="The weather for tomorrow morning",
    )
    clock, queue_full = [NOW], [False]
    llm = FakeLlm(TWO_POINTS)

    def submit(run, given_up) -> bool:
        if queue_full[0]:
            return False
        run(llm)
        return True

    actions = FakeActions()
    run = scheduled(store, llm, Schedule(60, "en"), actions, clock=lambda: clock[0], submit=submit)
    assert run.tick().request.since == NOW - 60 * MIN
    clock[0] += 60 * MIN
    queue_full[0] = True
    assert run.tick() is None
    assert shown(actions)[-1][1] == (
        "No summary this time: the model's queue is full; try again in a moment. The next one "
        "covers this time too."
    )
    clock[0] += 60 * MIN
    queue_full[0] = False
    assert run.tick().request.since == NOW  # the two hours since the last one
    store.close()


def test_a_run_that_cant_make_digests_says_so_once(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    actions, why = FakeActions(), ["a digest needs an LLM translator such as qwen3.5-9b"]
    run = scheduled(
        store, FakeLlm(TWO_POINTS), Schedule(60, "en"), actions, unavailable=lambda: why[0]
    )
    assert run.tick() is None and run.tick() is None
    assert shown(actions) == [("Wavescribe summary", f"No summaries for now: {why[0]}.")]
    why[0] = None
    assert run.tick() is not None and len(shown(actions)) == 2
    store.close()


def test_a_schedule_reads_out_only_words_that_may_leave(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    # On a station that may leave, under a class that may not: read out by nobody.
    add(store, "a", "Encontro secreto na ponte às dez", NOW - 15 * MIN, privacy="amateur")
    llm = FakeLlm(TWO_POINTS)
    scheduled(store, llm, Schedule(60, "en"), FakeActions()).tick()
    assert "secreto" not in llm.prompts[0]
    store.close()


# -- set on the page ----------------------------------------------------------------------------

SCHEDULE = "/api/manage/digest/schedule"


@pytest.fixture
def schedulers(monkeypatch: pytest.MonkeyPatch):
    """The schedulers started and stopped, without their threads: no digest is made."""
    started: list[Scheduler] = []
    stopped: list[Scheduler] = []
    stop = Scheduler.stop
    monkeypatch.setattr(Scheduler, "start", lambda self: started.append(self))
    monkeypatch.setattr(Scheduler, "stop", lambda self: (stopped.append(self), stop(self)))
    return started, stopped


def saved(app) -> dict:
    return tomllib.loads(app.settings.path.read_text()).get("alerts", {}).get("digest")


def test_the_page_sets_the_schedule_in_place_of_the_files_at_once(
    tmp_path: Path, monkeypatch, schedulers
) -> None:
    started, stopped = schedulers
    alerts = tests.test_app.ALERTS + '\n[digest]\nevery_minutes = 30\nstations = ["a"]\n'
    monkeypatch.setattr(tests.test_app, "ALERTS", alerts)
    node = Running(make_app(tmp_path))
    app = node.app
    try:
        wait_for(lambda: started)  # the run started alerts.toml's
        status, view = node.call("GET", SCHEDULE)
        assert status == 200 and (view["source"], view["enabled"], view["running"]) == (
            "file",
            True,
            True,
        )
        assert view["schedule"] == {
            "every_minutes": 30,
            "stations": ["a"],
            "language": "en",
            "speak": False,
            "quiet_hours": None,
        }
        assert view["next_at"] > time.time() and view["file"].endswith("alerts.toml")
        assert view["digest_code"] == "no_llm"  # this run's translator is no LLM
        assert 45 in view["every_minutes_choices"] and 7 not in view["every_minutes_choices"]
        assert (view["language"], view["voices"], view["alerts"]) == ("en", [], True)

        body = {"every_minutes": 15, "stations": ["b"], "quiet_hours": ["22:00", "07:00"]}
        status, view = node.call("POST", SCHEDULE, body)
        assert status == 200 and view["source"] == "page" and view["running"]
        assert view["file_has_schedule"] is True  # what "use the file's again" brings back
        assert view["schedule"] == {**body, "language": "en", "speak": False}
        assert stopped == [started[0]] and started[0]._closed.is_set()
        assert app.digests is started[1] and app.digests.schedule.every_minutes == 15
        assert not app.hub._closed.is_set()  # its own event was set, not the app's
        assert saved(app) == body

        for wrong, code, field in (
            ({"every_minutes": 7}, "bad_every", "every_minutes"),
            ({"stations": ["zz"]}, "unknown_station", "stations"),
            ({"speak": True}, "no_voice", "speak"),
            ({"quiet_hours": ["22:00"]}, "bad_quiet_hours", "quiet_hours"),
            ({"enabled": "no"}, "bad_enabled", "enabled"),
            ({"notify": True}, "unknown_keys", "notify"),
        ):
            status, out = node.call("POST", SCHEDULE, wrong)
            assert (status, out["code"], out.get("field")) == (400, code, field), wrong
        assert len(started) == 2 and app.digests is started[1] and saved(app) == body

        # Off: none runs, and what was set is kept for the page to show.
        status, view = node.call("POST", SCHEDULE, {**body, "enabled": False})
        assert (view["enabled"], view["running"], view["next_at"]) == (False, False, None)
        assert view["schedule"]["every_minutes"] == 15 and app.digests is None
        assert saved(app) == {**body, "off": True} and stopped[-1] is started[1]

        # What a form can't send is refused with a code, and the field where there is one.
        assert node.call("POST", SCHEDULE, ["every_minutes"]) == (
            400, {"error": "expected a JSON object", "code": "bad_body"})  # fmt: skip
        status, err = node.call("POST", SCHEDULE, {**body, "enabled": "yes"})
        assert status == 400 and (err["code"], err["field"]) == ("bad_enabled", "enabled")
        status, err = node.call("POST", SCHEDULE, {**body, "evry": 15})
        assert status == 400 and (err["code"], err["field"], err["keys"]) == (
            "unknown_keys", "evry", ["evry"])  # fmt: skip
        # Another machine: never, not even to look.
        status, _ = node.call("GET", SCHEDULE, headers={"X-Forwarded-For": "192.168.1.20"})
        assert status == 403

        # Forgotten: alerts.toml's again.
        status, view = node.call("DELETE", SCHEDULE)
        assert status == 200 and view["source"] == "file" and view["running"]
        assert app.digests is started[-1] and app.digests.schedule.every_minutes == 30
        assert saved(app) is None
    finally:
        node.close()


def test_the_pages_schedule_is_used_at_start_and_a_broken_one_is_skipped(
    tmp_path: Path, schedulers
) -> None:
    page = {"alerts": {"digest": {"every_minutes": 20, "stations": ["a"]}}}
    app = make_app(tmp_path, page=page)
    try:
        assert app.digests.schedule == Schedule(20, "en", ("a",))
        view = app.digest_schedule_view()
        assert view["source"] == "page" and view["file_has_schedule"] is False
    finally:
        app.pipeline.stop()
        app.close()
        app.store.close()

    # Written by the program: a station it names may have been deleted since.
    gone = {"alerts": {"digest": {"every_minutes": 20, "stations": ["gone"]}}}
    app = make_app(tmp_path, "again", page=gone)
    try:
        assert app.digests is None and "no station 'gone'" in (app.digest_error or "")
        view = app.digest_schedule_view()
        assert (view["error_code"], view["running"], view["source"]) == (
            "unknown_station",
            False,
            "page",
        )
    finally:
        app.pipeline.stop()
        app.close()
        app.store.close()
