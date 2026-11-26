# SPDX-License-Identifier: Apache-2.0
"""What another machine sends, pushed or followed: checked before anything is kept."""

import dataclasses
import io
import json
import math
import threading
import time

import pytest

from tests.test_pipeline import station
from wavescribe.events import iso, to_json, utterance_event
from wavescribe.remote import (
    MAX_EVENT_BYTES,
    BadEvent,
    RemoteFollower,
    read_utterance,
    sse_events,
    to_result,
)

NOW = 1_795_010_600.0
EVENT = {
    "type": "utterance", "version": 1, "id": 4182,
    "node": {"name": "kitchen-mac", "url": "http://kitchen-mac.local:8766"},
    "station": {"id": "tsf", "name": "TSF", "language": "pt-PT"},
    "time": "2026-11-26T14:03:12Z", "t_start": NOW - 8, "duration": 6.2,
    "language": "pt-PT", "text": "A circulação na ponte está cortada.",
    "translations": {"en": "Traffic on the bridge is closed."},
    "flags": ["lang:es"], "rules": ["roads"],
}  # fmt: skip


def test_an_event_from_another_machine_becomes_a_result_here() -> None:
    heard = read_utterance(EVENT, now=NOW)
    assert (heard.station_id, heard.station_name, heard.station_language) == (
        "tsf", "TSF", "pt-PT",
    )  # fmt: skip
    result = to_result(heard, station("north-tsf"))
    assert result.station.id == "north-tsf" and result.utterance.seq == 4182
    assert (result.utterance.t_start, result.utterance.t_end) == (NOW - 8, NOW - 8 + 6.2)
    assert result.transcript.text == EVENT["text"] and result.transcript.language == "pt-PT"
    assert result.transcript.model == "remote:kitchen-mac"
    assert result.translations == EVENT["translations"] and result.flags == ("lang:es",)

    # What a sender may leave out: the time is when it arrived, the language the station's.
    bare = {"type": "utterance", "station": {"id": "tsf"}, "text": "Olá"}
    result = to_result(read_utterance(bare, now=NOW), station("north-tsf", language="pt-BR"))
    assert result.utterance.t_start == result.utterance.t_end == NOW
    assert result.transcript.language == "pt-BR" and result.transcript.model == "remote:remote"
    week_old = {**EVENT, "t_start": NOW - 6 * 86400}  # catching up after days away
    assert read_utterance(week_old, now=NOW).t_start == NOW - 6 * 86400


@pytest.mark.parametrize("ahead_s", [0.5, 59, 9 * 60, 10 * 60])
def test_a_time_from_a_clock_that_runs_fast_is_stored_as_the_arrival_time(ahead_s: float) -> None:
    """Offline for weeks, machines' clocks drift: the text is kept, its time is ours."""
    heard = read_utterance({**EVENT, "t_start": NOW + ahead_s}, now=NOW)
    assert heard.t_start == NOW
    result = to_result(heard, station("north-tsf"))
    assert (result.utterance.t_start, result.utterance.t_end) == (NOW, NOW + 6.2)


def test_the_privacy_class_travels_with_the_text() -> None:
    ours = dataclasses.replace(station("north-2m"), remote="push", privacy="utility")
    sent = utterance_event(
        {"id": 3, "station": "2m", "t_start": NOW, "text": "CQ CQ"},
        {"2m": dataclasses.replace(station("2m"), privacy="amateur")},
        {"name": "north", "url": None},
    )
    assert sent["station"]["privacy"] == "amateur"
    heard = read_utterance(sent, now=NOW)
    assert heard.privacy == "amateur" and to_result(heard, ours).station.privacy == "amateur"
    # A sender that doesn't say leaves the station as it is here.
    assert read_utterance(EVENT, now=NOW).privacy is None
    assert to_result(read_utterance(EVENT, now=NOW), ours).station.privacy == "utility"
    # A class from a later version is one this version has no rules for: nothing is kept.
    newer = {**EVENT, "station": {"id": "tsf", "privacy": "maritime"}}
    assert read_utterance(newer, now=NOW).privacy == "private"


def test_a_class_set_in_the_config_is_the_least_strict_a_followed_station_gets(tmp_path) -> None:
    from wavescribe.config import ConfigError, parse_stations
    from wavescribe.settings import effective_config

    far = {"id": "far", "language": "pt-PT", "remote": "http://n.test:8", "remote_station": "tsf"}
    config = parse_stations({"station": [{**far, "privacy": "amateur"}]}, tmp_path / "s.toml")
    ours = config.get("far")
    assert (ours.privacy, ours.privacy_set) == ("amateur", "amateur")

    def sent(cls: str) -> object:
        return read_utterance({**EVENT, "station": {"id": "tsf", "privacy": cls}}, now=NOW)

    assert to_result(sent("broadcast"), ours).station.privacy == "amateur"
    assert to_result(sent("private"), ours).station.privacy == "private"
    # The same after a restart, from the class the sender gave last (kept in ui.toml).
    for said, held in (("broadcast", "amateur"), ("utility", "utility")):
        again, errors = effective_config(config, {"remote_privacy": {"far": said}})
        assert again.get("far").privacy == held and not errors
    stream = {"id": "s", "language": "pt-PT", "stream": "http://s.test/a", "privacy": "private"}
    for bad in ({**far, "privacy": "secret"}, stream):
        with pytest.raises(ConfigError, match="'privacy' is for remote stations"):
            parse_stations({"station": [bad]}, tmp_path / "s.toml")


BAD = [
    ("t_start", math.inf), ("t_start", -math.inf), ("t_start", math.nan), ("t_start", 1e18),
    ("t_start", 10**400), ("t_start", -1.0), ("t_start", 0), ("t_start", NOW - 30 * 86400),
    ("t_start", NOW + 11 * 60), ("t_start", NOW + 3600), ("t_start", str(NOW)),
    ("t_start", True), ("t_start", [NOW]),
    ("duration", math.inf), ("duration", math.nan), ("duration", -0.5), ("duration", 1e18),
    ("duration", 601), ("duration", "6.2"), ("duration", False), ("duration", {}),
    ("text", "a" * 4001), ("text", 7), ("text", ["a"]), ("text", {"pt": "a"}), ("text", ""),
    ("text", None), ("text", "half a pair \ud83d"),
    ("language", "x" * 36), ("language", "not a tag"), ("language", "pt\n"), ("language", 5),
    ("id", -1), ("id", 2**63), ("id", 1.5), ("id", "4182"), ("id", True),
    ("station", None), ("station", "tsf"), ("station", ["tsf"]), ("station", {}),
    ("station", {"id": ""}), ("station", {"id": 5}), ("station", {"id": "x" * 200}),
    ("station", {"id": "tsf", "name": "n" * 500}), ("station", {"id": "tsf", "name": 5}),
    ("station", {"id": "tsf", "language": "português"}),
    ("station", {"id": "tsf", "privacy": 5}), ("station", {"id": "tsf", "privacy": ["amateur"]}),
    ("station", {"id": "tsf", "name": "\udcff"}),
    ("node", "kitchen"), ("node", ["kitchen"]), ("node", {"name": 5}),
    ("node", {"name": "n" * 500}),
    ("translations", "en"), ("translations", ["en"]), ("translations", {"en": 5}),
    ("translations", {"not a tag": "x"}), ("translations", {"en": "a" * 4001}),
    ("translations", {f"x{i:02}": "a" for i in range(40)}),
    ("flags", "lang:es"), ("flags", [5]), ("flags", ["f" * 100]), ("flags", ["f"] * 40),
    ("flags", {"lang": "es"}),
]  # fmt: skip


@pytest.mark.parametrize(("field", "value"), BAD, ids=lambda v: repr(v)[:24])
def test_an_event_with_a_bad_value_is_not_taken(field: str, value: object) -> None:
    with pytest.raises(BadEvent) as refusal:
        read_utterance({**EVENT, field: value}, now=NOW)
    assert field in str(refusal.value) and len(str(refusal.value)) < 80  # names it, no value


@pytest.mark.parametrize("event", [None, "utterance", 5, [EVENT], True])
def test_an_event_that_is_not_an_object_is_not_taken(event: object) -> None:
    with pytest.raises(BadEvent):
        read_utterance(event, now=NOW)


def test_what_is_sent_is_always_json_a_browser_reads() -> None:
    row = {"id": 1, "t_start": math.inf, "duration": math.nan, "text": "Olá",
           "nested": [{"lag": -math.inf, "ok": 1.5}, (math.nan,)]}  # fmt: skip
    assert json.loads(to_json(row)) == {
        "id": 1, "t_start": None, "duration": None, "text": "Olá",
        "nested": [{"lag": None, "ok": 1.5}, [None]],
    }  # fmt: skip
    assert to_json({"text": "água", "n": 1.5}) == '{"text": "água", "n": 1.5}'


UNSHOWABLE = [math.inf, -math.inf, math.nan, 1e18, -1e18, 10**400, None, "x"]


@pytest.mark.parametrize("epoch", UNSHOWABLE, ids=lambda v: repr(v)[:12])
def test_a_time_that_cant_be_shown_never_ends_a_feed(epoch: object) -> None:
    assert iso(epoch) == "1970-01-01T00:00:00Z"
    stored = {"id": 3, "station": "a", "t_start": epoch, "text": "Olá"}
    event = utterance_event(stored, {"a": station("a")}, {"name": "n", "url": None})
    assert event["time"] == "1970-01-01T00:00:00Z" and json.loads(to_json(event))["text"] == "Olá"
    assert iso(1795010592.1) == "2026-11-18T14:03:12Z"


class Feed:
    """A followed machine: each connection gets the next scripted response."""

    def __init__(self, *bodies: object) -> None:
        self.bodies = list(bodies)
        self.requests: list = []
        self.waits: list[float] = []
        self.done = threading.Event()

    def open(self, request, timeout: float):
        self.requests.append(request)
        body = self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body
        return body

    def sleep(self, seconds: float) -> bool:
        """The follower's wait between connections: none here, and it ends with the script."""
        self.waits.append(seconds)
        if not self.bodies:
            self.done.set()
        return not self.bodies

    def follow(self, ingest, **more) -> RemoteFollower:
        far = dataclasses.replace(station("far"), remote="http://n:8766", remote_station="tsf",
                                  remote_token="wst_s3cret")  # fmt: skip
        follower = RemoteFollower(far, ingest, opener=self.open, sleep=self.sleep, **more)
        assert self.done.wait(5)
        follower._thread.join(5)
        return follower


class Status(io.BytesIO):
    status = 200


def sse(data: object, event_id: int | None = None) -> bytes:
    head = f"id: {event_id}\n" if event_id is not None else ""
    payload = data if isinstance(data, str) else json.dumps(data)
    return f"{head}event: utterance\ndata: {payload}\n\n".encode()


def said(text: str, **more: object) -> dict:
    return {**EVENT, "t_start": time.time() - 5, "text": text, **more}


def test_a_follower_skips_what_isnt_an_utterance_and_keeps_following() -> None:
    got: list[str] = []
    stream = b"".join([
        sse(said("um"), 1), sse("[1, 2]", 2), sse("5", 3), sse("null", 4), sse("{not json", 5),
        sse(said("old", t_start=1e18), 6), sse(said("", duration=-1), 7),
        sse(said("a" * 5000), 8), sse(said("dois"), 9),
    ])  # fmt: skip
    feed = Feed(Status(stream), Status(sse(said("três"), 10)))
    follower = feed.follow(lambda result: got.append(result.transcript.text))
    assert got == ["um", "dois", "três"]
    assert (follower.received, follower.skipped) == (3, 6)
    assert follower.status()["skipped"] == 6
    assert feed.requests[1].get_header("Last-event-id") == "9"  # resumes after the last one
    # The token goes in a header only: URLs end up in logs, here and on the way.
    assert all(r.get_header("Authorization") == "Bearer wst_s3cret" for r in feed.requests)
    assert not any("s3cret" in r.full_url for r in feed.requests)


def test_a_restarted_follower_skips_what_a_fast_clock_replays_of_what_is_stored() -> None:
    """Stored as it arrived, behind the sender's own times: asked from the last time stored
    here, the sender sends the last minutes again."""
    got: list[str] = []
    replay = sse(said("Bom dia"), 1) + sse(said("Bom dia"), 2) + sse(said("novo"), 3)
    later = sse(said("novo"), 4)  # said again, and asked for by id: taken
    feed = Feed(Status(replay), Status(later))
    stored = [("Bom dia", 6.2), ("Bom dia", 6.21), ("novo", 1.0)]  # twice; once at another length
    follower = feed.follow(lambda result: got.append(result.transcript.text), since=NOW,
                           stored=stored)  # fmt: skip
    assert got == ["novo", "novo"] and follower.repeated == 2
    assert "since=" in feed.requests[0].full_url
    assert feed.requests[1].get_header("Last-event-id") == "3"


class Endless:
    """A response whose first line never ends."""

    status = 200

    def __init__(self) -> None:
        self.asked: list[int] = []
        self.closed = False

    def readline(self, limit: int = -1) -> bytes:
        assert limit > 0, "an unbounded read of a line that never ends"
        self.asked.append(limit)
        return b"x" * limit

    def close(self) -> None:
        self.closed = True


def test_a_feed_that_never_ends_a_line_is_dropped_and_tried_again() -> None:
    got: list[str] = []
    endless, huge = Endless(), b"data: " + b"x" * (MAX_EVENT_BYTES // 2) + b"\n"
    feed = Feed(endless, Status(huge * 3), Status(sse(said("um"), 1)), Status(b""), Status(b""))
    follower = feed.follow(lambda result: got.append(result.transcript.text))
    assert endless.asked == [MAX_EVENT_BYTES + 1] and endless.closed
    assert got == ["um"] and follower.reconnects == 5
    # Each failure doubles the wait; a connection that brought text starts it over.
    assert feed.waits == [2.0, 4.0, 2.0, 4.0, 8.0]
    with pytest.raises(ValueError, match="too large"):
        list(sse_events(iter([b"data: " + b"x" * MAX_EVENT_BYTES + b"\n"] * 2)))


def test_a_follower_outlives_errors_nobody_expected(caplog) -> None:
    got: list[str] = []

    def ingest(result) -> None:
        if result.transcript.text == "boom":
            raise RuntimeError("the store is on fire")
        got.append(result.transcript.text)

    feed = Feed(Status(sse(said("boom"), 1)), OSError("refused"), Status(sse(said("dois"), 2)))
    follower = feed.follow(ingest)
    assert got == ["dois"] and follower.received == 1
    assert "the store is on fire" in caplog.text
    assert feed.waits == [2.0, 4.0, 2.0]
