# SPDX-License-Identifier: Apache-2.0
"""Station alerts read from the pipeline's status snapshot: a station gone silent, and an FM
station's RDS traffic announcement (from synthetic RDS groups, never a receiver)."""

import dataclasses
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from tests.test_alerts import CONFIG, Clock, FakeActions
from tests.test_pipeline import PLAN, FakeTranscriber, FakeTranslator, station
from tests.test_rds import bits_of, mpx, name_groups
from tests.test_rules import pack
from tests.test_segmenter import EnergyVad
from wavescribe.alerts import Alerter, parse_alerts
from wavescribe.alerts.cap import NS, to_cap
from wavescribe.alerts.watch import StationWatch
from wavescribe.audio import SAMPLE_RATE, AudioChunk
from wavescribe.config import ConfigError
from wavescribe.dsp.rds import RdsDecoder
from wavescribe.pipeline import Pipeline
from wavescribe.sources import SourceHealth
from wavescribe.store import Store

SILENCE = {"id": "station-silent", "kind": "silence", "after_minutes": 5, "severity": "critical"}
SOUNDING = {"connected": True, "reconnects": 0, "last_data_age": 0.3, "level": 0.1,
            "stopping": False}  # fmt: skip
DOWN = {**SOUNDING, "connected": False, "reconnects": 2, "last_data_age": 40.0}
DEAD_AIR = {**SOUNDING, "level": 0.001}  # -60 dBFS


class Rig:
    """An Alerter with fake actions and clock, and a watch over a status the test writes."""

    def __init__(self, tmp_path: Path, *rules: dict, stations=None, store=None,
                 translations=None) -> None:  # fmt: skip
        raw = {**CONFIG, "rule": list(rules)}
        self.actions, self.clock = FakeActions(), Clock()
        self.alerter = Alerter(parse_alerts(raw, None, pack(tmp_path)), actions=self.actions,
                               clock=self.clock, store=store)  # fmt: skip
        if translations is not None:
            self.alerter.attach(lambda text, source, targets, callback: callback(
                {t: translations[t] for t in targets if t in translations}, "fake-mt"))  # fmt: skip
        self.stations = stations or [dataclasses.replace(station("a"), name="Rádio A"),
                                     dataclasses.replace(station("b"), name="Rádio B")]  # fmt: skip
        self.alerter.station_names = {s.id: s.name for s in self.stations}
        self.live: dict[str, dict] = {s.id: dict(SOUNDING) for s in self.stations}
        self.ready = True
        self.reads = 0
        self.watch = StationWatch(self.alerter, self.status, lambda: self.stations,
                                  clock=self.clock)  # fmt: skip

    def status(self) -> dict:
        self.reads += 1
        return {"ready": self.ready, "stations": {k: dict(v) for k, v in self.live.items()}}

    def tick(self, seconds: float = 0.0) -> None:
        self.clock.now += seconds
        self.watch.check()
        self.alerter.drain()

    def run(self, seconds: float, step: float = 5.0) -> None:
        for _ in range(round(seconds / step)):
            self.tick(step)

    def notes(self) -> list[tuple[str, str]]:
        return [(c[1], c[2]) for c in self.actions.calls if c[0] == "notify"]


def test_a_silent_station_raises_one_alert_and_says_once_when_it_is_back(tmp_path: Path) -> None:
    rig = Rig(tmp_path, SILENCE, translations={"en": "unused", "uk": "Немає звуку з Rádio A."})
    rig.tick()
    rig.live["a"] = dict(DOWN)
    rig.tick()
    rig.run(4 * 60 + 55)
    assert rig.actions.calls == []  # not yet five minutes
    rig.tick(5)
    assert rig.actions.calls == [
        ("notify", "CRITICAL · station-silent · Rádio A", "No audio from Rádio A for 5 minutes."),
        ("speak", "Lesya", "Немає звуку з Rádio A."),  # the voice's intro is for words heard
    ]
    [alert] = rig.alerter._open.values()
    assert (alert.kind, alert.language, alert.quote, alert.terms, alert.segment_ids) == (
        "silence", "en", "No audio from Rádio A for 5 minutes.", [], [])  # fmt: skip
    rig.run(10 * 60)  # still silent: nothing more
    assert len(rig.actions.calls) == 2 and alert.mentions == 1

    rig.live["a"] = dict(SOUNDING)
    rig.tick(5)
    assert len(rig.actions.calls) == 2  # a few seconds of audio aren't back yet
    rig.tick(10)
    assert rig.actions.calls[2:] == [
        ("notify", "BACK · station-silent · Rádio A", "Audio is back on Rádio A after 15 minutes.")
    ]  # never spoken
    assert alert.ended and alert.reason == "Audio is back on Rádio A after 15 minutes."
    assert alert.status == "delivered" and "station-silent" not in rig.alerter._open
    rig.run(10 * 60)
    assert len(rig.actions.calls) == 3

    rig.live["a"] = dict(DOWN)  # the next silence is a new alert
    rig.run(5 * 60 + 5)
    assert rig.notes()[-1] == ("CRITICAL · station-silent · Rádio A",
                               "No audio from Rádio A for 5 minutes.")  # fmt: skip
    assert rig.alerter._open["station-silent"] is not alert


def test_dead_air_is_silence_and_one_sound_starts_the_wait_again(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {**SILENCE, "level_db": -50})
    rig.live["a"] = dict(DEAD_AIR)
    rig.tick()
    rig.run(4 * 60)
    rig.live["a"] = dict(SOUNDING)
    rig.tick(5)
    rig.live["a"] = dict(DEAD_AIR)
    rig.run(4 * 60)
    assert rig.actions.calls == []
    rig.run(65)
    assert rig.notes() == [("CRITICAL · station-silent · Rádio A",
                            "Rádio A has been silent for 5 minutes.")]  # fmt: skip
    rig.live["b"] = {**DEAD_AIR, "level": 0.01}  # -40 dBFS: quiet, but above the level
    rig.run(10 * 60)
    assert len(rig.notes()) == 1


def test_listen_only_switched_off_and_stopped_stations_never_alert(tmp_path: Path) -> None:
    listen = dataclasses.replace(station("listen"), transcribe=False)
    off = dataclasses.replace(station("off"), disabled=True)
    stopping = station("stopping")
    rig = Rig(tmp_path, SILENCE, stations=[listen, off, stopping, station("gone")])
    rig.live = {"listen": dict(DOWN), "off": dict(DOWN), "stopping": {**DOWN, "stopping": True}}
    rig.tick()
    rig.run(20 * 60)  # "gone" isn't running at all
    assert rig.actions.calls == [] and rig.alerter._open == {}


def test_a_station_stopped_or_switched_to_listening_while_silent_ends_quietly(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, SILENCE)
    rig.live["a"] = dict(DOWN)
    rig.live["b"] = dict(DOWN)
    rig.tick()
    rig.run(5 * 60)
    [alert] = rig.alerter._open.values()
    rig.stations[1] = dataclasses.replace(rig.stations[1], transcribe=False)  # b: listen only
    del rig.live["a"]  # a: stopped
    rig.run(60)
    assert len(rig.notes()) == 1 and alert.ended and alert.reason is None


def test_a_narrowband_channel_quiet_between_calls_is_not_silent(tmp_path: Path) -> None:
    rig = Rig(tmp_path, SILENCE)
    rig.live["a"] = {**SOUNDING, "level": 0.0, "squelch_open": False}
    rig.tick()
    rig.run(20 * 60)
    assert rig.actions.calls == []
    rig.live["a"] = {**DOWN, "squelch_open": False}  # the receiver itself is down
    rig.run(5 * 60 + 5)
    assert len(rig.notes()) == 1


def test_stations_silent_together_are_one_alert_and_back_when_all_are(tmp_path: Path) -> None:
    rig = Rig(tmp_path, SILENCE)
    rig.live["a"] = dict(DOWN)
    rig.tick()
    rig.run(20)
    rig.live["b"] = dict(DOWN)  # the network went: every stream goes quiet within seconds
    rig.run(5 * 60 + 5)
    assert rig.notes() == [("CRITICAL · station-silent · Rádio A",
                            "No audio from Rádio A for 5 minutes.")]  # fmt: skip
    [alert] = rig.alerter._open.values()
    assert alert.station_ids == ["a", "b"] and alert.mentions == 2
    rig.live["a"] = dict(SOUNDING)
    rig.run(60)
    assert len(rig.notes()) == 1  # b is still silent
    rig.live["b"] = dict(SOUNDING)
    rig.run(15)
    assert rig.notes()[1] == ("BACK · station-silent · Rádio A, Rádio B",
                              "Audio is back on Rádio A, Rádio B after 7 minutes.")  # fmt: skip


def test_a_rule_keeps_to_its_stations_and_waits_for_the_models(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {**SILENCE, "stations": ["b"]})
    rig.ready = False  # sources aren't read while the models load
    rig.live["a"] = dict(DOWN)
    rig.live["b"] = dict(DOWN)
    rig.tick()
    rig.run(10 * 60)
    assert rig.actions.calls == []
    rig.ready = True
    rig.run(5 * 60 + 5)
    assert rig.notes() == [("CRITICAL · station-silent · Rádio B",
                            "No audio from Rádio B for 5 minutes.")]  # fmt: skip


def test_without_station_rules_the_status_is_not_read(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"id": "water", "topics": ["water"], "severity": "high"})
    rig.run(60)
    assert rig.reads == 0


def test_a_silence_alert_is_stored_and_its_cap_says_how_it_was_found(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    rig = Rig(tmp_path, SILENCE, store=store)
    rig.live["a"] = dict(DOWN)
    rig.tick()
    rig.run(5 * 60)
    rig.live["a"] = dict(SOUNDING)
    rig.run(15)
    [kept] = store.alerts()
    assert (kept["rule"], kept["kind"], kept["language"], kept["status"]) == (
        "station-silent", "silence", "en", "delivered")  # fmt: skip
    assert kept["quote"] == "No audio from Rádio A for 5 minutes."
    assert kept["reason"] == "Audio is back on Rádio A after 5 minutes."
    root = ET.fromstring(to_cap(kept))
    q = lambda el, tag: el.find(f"{{{NS}}}{tag}").text  # noqa: E731
    assert "station's status (silence)" in q(root, "note")
    [info] = root.findall(f"{{{NS}}}info")
    assert q(info, "category") == "Infra" and q(info, "language") == "en"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"after_minutes": "5"}, "after_minutes must be a number, e.g. 5"),
        ({"after_minutes": 0}, "after_minutes must be from 0.5 to 1440"),
        ({"level_db": 6}, "level_db must be from -120 to 0"),
        ({"kind": "silent"}, "kind is keyword, llm, silence or traffic"),
    ],
)
def test_silence_rule_values_are_checked(tmp_path: Path, change: dict, message: str) -> None:
    from wavescribe.rules import parse_rule

    with pytest.raises(ValueError, match=f"^rule station-silent: {message}"):
        parse_rule({**SILENCE, **change})
    if change.get("kind") != "silent":
        with pytest.raises(ConfigError, match=message):
            parse_alerts({**CONFIG, "rule": [{**SILENCE, **change}]}, None, pack(tmp_path))


class QuietSource:
    """A stream that connects and then sends nothing but zeros, as dead air does."""

    def __init__(self, station_id: str) -> None:
        self.station_id = station_id
        self.health = SourceHealth()
        self._closed = threading.Event()

    def __iter__(self) -> Iterator[AudioChunk]:
        while not self._closed.wait(0.05):
            self.health.connected, self.health.last_data_at = True, time.time()
            yield AudioChunk(np.zeros(SAMPLE_RATE // 20, np.float32), t_end=time.time(),
                             discontinuity=False)  # fmt: skip

    def close(self) -> None:
        self._closed.set()


def test_the_watch_reads_the_real_pipeline_status(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {**SILENCE, "after_minutes": 1})
    pipeline = Pipeline([rig.stations[0]], PLAN, lambda _r: None,
                        source_factory=lambda st: QuietSource(st.id),
                        vad_factory=lambda _p: EnergyVad(),
                        transcriber_factory=lambda _e, _p: FakeTranscriber(),
                        translator_factory=lambda _e, _p: FakeTranslator())  # fmt: skip
    rig.watch = StationWatch(rig.alerter, pipeline.status, lambda: rig.stations, clock=rig.clock)
    pipeline.start()
    try:
        deadline = time.time() + 10
        while not pipeline.status()["stations"].get("a", {}).get("connected"):
            assert time.time() < deadline, "the station never started"
            time.sleep(0.05)
        rig.tick()
        rig.run(65)
    finally:
        pipeline.stop()
    assert rig.notes() == [("CRITICAL · station-silent · Rádio A",
                            "Rádio A has been silent for 1 minute.")]  # fmt: skip


TRAFFIC = {"id": "traffic", "kind": "traffic", "severity": "medium"}


def rds(tp: bool = True, ta: bool = False) -> dict:
    return {"pi": "2F15", "ps": "RADIO A", "tp": tp, "ta": ta, "groups": 400}


def test_a_traffic_announcement_alerts_when_it_starts_and_is_rate_limited(tmp_path: Path) -> None:
    rig = Rig(tmp_path, TRAFFIC)
    rig.live["a"]["rds"] = rds()
    rig.tick()
    assert rig.actions.calls == []  # a station that carries traffic news, none on now
    rig.live["a"]["rds"] = rds(ta=True)
    rig.tick(5)
    assert rig.notes() == [("MEDIUM · traffic · Rádio A", "Traffic announcement on Rádio A.")]
    [alert] = rig.alerter._open.values()
    assert (alert.kind, alert.quote, alert.language) == (
        "traffic", "Traffic announcement on Rádio A.", "en")  # fmt: skip
    rig.run(3 * 60)  # it goes on: one alert, not one per reading
    rig.live["a"]["rds"] = rds()
    rig.run(5 * 60)
    rig.live["a"]["rds"] = rds(ta=True)  # the next one within the cooldown folds in
    rig.run(60)
    assert len(rig.notes()) == 1 and alert.mentions == 2
    rig.live["a"]["rds"] = rds()
    rig.run(20 * 60)
    rig.live["a"]["rds"] = rds(ta=True)  # after it: a new alert
    rig.tick(5)
    assert len(rig.notes()) == 2 and rig.alerter._open["traffic"] is not alert


def test_traffic_alerts_are_opt_in_per_rule_and_station(tmp_path: Path) -> None:
    listen = dataclasses.replace(station("music"), name="Music FM", transcribe=False)
    off = dataclasses.replace(station("off"), disabled=True)
    rig = Rig(tmp_path, SILENCE, stations=[listen, off, station("a")])
    rig.live = {sid: {**SOUNDING, "rds": rds()} for sid in ("music", "off", "a")}
    rig.tick()
    for s in rig.live.values():
        s["rds"] = rds(ta=True)
    rig.run(60)
    assert rig.actions.calls == []  # no traffic rule, no traffic alerts

    (tmp_path / "2").mkdir()
    rig = Rig(tmp_path / "2", {**TRAFFIC, "stations": ["music", "off"]},
              stations=[listen, off, station("a")])  # fmt: skip
    rig.live = {sid: {**SOUNDING, "rds": rds()} for sid in ("music", "off", "a")}
    rig.tick()
    for s in rig.live.values():
        s["rds"] = rds(ta=True)
    rig.run(60)
    # A listen-only station is decoded too (RDS needs no model); switched off and other
    # stations aren't.
    assert rig.notes() == [("MEDIUM · traffic · Music FM", "Traffic announcement on Music FM.")]


def test_ta_without_tp_is_another_stations_announcement(tmp_path: Path) -> None:
    rig = Rig(tmp_path, TRAFFIC)
    rig.live["a"]["rds"] = rds(tp=False)
    rig.tick()
    rig.live["a"]["rds"] = rds(tp=False, ta=True)  # it relays another station's, by EON
    rig.run(60)
    rig.live["b"]["rds"] = None  # and a station with no RDS at all
    rig.run(60)
    assert rig.actions.calls == []


class RdsSource:
    """A listen-only FM station whose health carries what an RDS decoder made of synthetic
    groups, as SdrChannelSource's does."""

    def __init__(self, station_id: str) -> None:
        self.station_id = station_id
        self.health = SourceHealth()
        self.decoder = RdsDecoder()
        self._closed = threading.Event()

    def send(self, ta: int) -> None:
        bits = bits_of(name_groups(0x2F15, "RADIO A", tp=1, ta=ta) * 3)
        self.decoder(mpx(bits, len(bits) / 1187.5))
        self.health.rds = self.decoder.info.to_dict()

    def __iter__(self) -> Iterator[AudioChunk]:
        while not self._closed.wait(0.05):
            self.health.connected, self.health.last_data_at = True, time.time()
            yield AudioChunk(np.full(SAMPLE_RATE // 20, 0.1, np.float32), t_end=time.time(),
                             discontinuity=False)  # fmt: skip

    def close(self) -> None:
        self._closed.set()


def test_a_traffic_announcement_decoded_from_rds_groups_alerts(tmp_path: Path) -> None:
    listen = dataclasses.replace(station("a"), name="Rádio A", transcribe=False)
    rig = Rig(tmp_path, TRAFFIC, stations=[listen])
    source = RdsSource("a")
    pipeline = Pipeline([listen], dataclasses.replace(PLAN, asr={}), lambda _r: None,
                        source_factory=lambda _st: source)  # fmt: skip
    rig.watch = StationWatch(rig.alerter, pipeline.status, lambda: rig.stations, clock=rig.clock)
    pipeline.start()
    try:
        source.send(ta=0)
        deadline = time.time() + 10
        while "rds" not in pipeline.status()["stations"].get("a", {}):
            assert time.time() < deadline, "the station never started"
            time.sleep(0.05)
        rig.tick()
        assert rig.actions.calls == [] and source.health.rds["tp"]
        source.send(ta=1)
        rig.tick(5)
    finally:
        pipeline.stop()
    assert source.health.rds["ta"]
    assert rig.notes() == [("MEDIUM · traffic · Rádio A", "Traffic announcement on Rádio A.")]
    [alert] = rig.alerter._open.values()
    root = ET.fromstring(to_cap(alert.payload()))
    [info] = root.findall(f"{{{NS}}}info")
    assert info.find(f"{{{NS}}}category").text == "Transport"
