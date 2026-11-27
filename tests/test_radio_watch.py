# SPDX-License-Identifier: Apache-2.0
"""Watches as stations: their windows, their config, language "auto", and their catches
through the pipeline (synthetic IQ, a fake VAD and a fake speech model)."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from tests.test_pipeline import ENTRY, FakeTranslator
from tests.test_radio_watch_detect import BELOW, FS, PMR, T0, Burst, air, dominant_hz
from tests.test_segmenter import EnergyVad
from wavescribe.asr import Transcript
from wavescribe.asr.language import AUTO, likeliest, plan_detecting, transcribe_any
from wavescribe.config import ConfigError, SdrDevice, Station, build_station
from wavescribe.events import station_ref
from wavescribe.pipeline import Pipeline, Plan, Result
from wavescribe.radio import watch as watches
from wavescribe.radio.bandplan import privacy_of
from wavescribe.radio.watch import Catch, WatchError, plan_centre, plan_window
from wavescribe.registry import ModelEntry, ModelError, ModelRegistry
from wavescribe.settings import station_dict
from wavescribe.sources.watch import WatchSource

RATE = 2_400_000
DEVICE = SdrDevice("a", "00001234", BELOW, 30.0, FS)


def watch_station(**extra: object) -> Station:
    d: dict[str, object] = {
        "id": "walkies",
        "language": AUTO,
        "device": "a",
        "watch": "pmr446",
        "acknowledge": True,
    }
    d.update(extra)
    return build_station(d, "test", {"a": DEVICE}, "cept")


# -- windows ---------------------------------------------------------------------------------


def test_a_band_is_watched_on_its_channels_from_below_its_lowest() -> None:
    w, mode, width = plan_window("pmr446", "cept", rate=RATE)
    assert w.channels == PMR and mode == "nfm" and width == 12_500
    assert plan_centre(w, width, RATE) == PMR[0] - 250_000  # its mirror image falls outside


def test_a_wide_band_is_centred_between_two_channels() -> None:
    w, _, width = plan_window("2m", "1", rate=RATE)
    assert len(w.channels) == 161 and w.lo_hz == 144_000_000 and w.hi_hz == 146_000_000
    centre = plan_centre(w, width, RATE)
    assert centre == 145_006_250
    assert all(abs(c - centre) <= RATE / 2 - 60_000 - width / 2 for c in w.channels)


def suggested(e: pytest.ExceptionInfo[WatchError]) -> tuple[float, float]:
    import re

    found = re.search(r"e\.g\. ([\d.]+):([\d.]+) \((\d+) channels\)", str(e.value))
    assert found, str(e.value)
    return float(found[1]), float(found[2])


@pytest.mark.parametrize(("band", "region"), [("airband", "1"), ("airband-25", "2"),
                                               ("70cm-wide", "2")])  # fmt: skip
def test_a_window_wider_than_one_receiver_is_refused_with_a_slice_that_fits(
    band: str, region: str
) -> None:
    from wavescribe.radio.watch import outside

    with pytest.raises(WatchError, match=r"about 2\.25 MHz: watch a slice of it, e\.g\. ") as e:
        plan_window(band, region, rate=RATE)
    a, b = suggested(e)
    # The slice as a watch has it: planned, then centred between two of its channels.
    w, _, width = plan_window((a, b), region, rate=RATE)
    assert w.channels and f"({len(w.channels)} channels)" in str(e.value)
    assert outside(w, plan_centre(w, width, RATE), RATE, width) == []
    assert b - a > 2.2  # and no narrower than it needs to be: about 2.25 MHz is true
    assert "kind of radio" not in str(e.value)  # one class all through


@pytest.mark.parametrize(
    ("band", "region", "kind", "other"),
    [
        ("marine", "1", "utility", "157.45-160.6 MHz is Land mobile radio (VHF), private radio"),
        ("70cm", "1", "amateur",
         "433.05-434.7875 MHz is 433 MHz short-range devices, private radio"),
    ],
)  # fmt: skip
def test_a_band_of_two_classes_suggests_the_slice_with_most_of_its_own_channels(
    band: str, region: str, kind: str, other: str
) -> None:
    """Marine VHF's middle is land mobile radio: private, so a slice there would keep nothing,
    and the old slice about the middle was mostly that."""
    with pytest.raises(WatchError) as e:
        plan_window(band, region, rate=RATE)
    a, b = suggested(e)
    w, _, width = plan_window((a, b), region, rate=RATE)
    assert all(privacy_of(c, region, width) == kind for c in w.channels)
    if band == "marine":
        assert (a, b) == (156.0, 157.425) and len(w.channels) == 58  # the ships' channels
    assert str(e.value).endswith(
        f" Part of it is another kind of radio: {other}. A watch is kept as the strictest "
        "kind of radio in it."
    )


def test_a_range_takes_the_raster_and_mode_of_its_band() -> None:
    w, mode, _ = plan_window((446.0, 446.05), "cept", rate=RATE)
    assert w.channels == PMR[:4] and mode == "nfm" and w.label == "446-446.05 MHz"
    with pytest.raises(WatchError, match="narrowband voice"):
        plan_window("fm", "1", rate=RATE)
    with pytest.raises(WatchError, match="no band 'nowhere'"):
        plan_window("nowhere", "1", rate=RATE)


def test_a_band_is_watched_in_the_mode_its_region_uses() -> None:
    # CB is FM in the CEPT countries and AM elsewhere.
    assert plan_window("cb", "cept", rate=RATE)[1:] == ("nfm", 10_000)
    assert plan_window("cb", "2", rate=RATE)[1:] == ("am", 10_000)
    assert plan_window((26.965, 27.405), "cept", rate=RATE)[1] == "nfm"  # a range: its band's


# -- as a station ----------------------------------------------------------------------------


def test_a_watch_takes_the_strictest_class_and_the_acknowledgement() -> None:
    s = watch_station()
    assert s.privacy == "private" and s.band == "pmr446" and s.sdr is not None
    assert s.sdr.watch is not None and s.sdr.watch.channels == PMR and s.sdr.mode == "nfm"
    with pytest.raises(ConfigError, match=r"private radio.*acknowledge = true"):
        watch_station(acknowledge=False)
    # A range across amateur 70 cm and the 433 MHz devices band is as private as its most
    # private channel.
    near = SdrDevice("c", "00001234", 433_256_250, 30.0, RATE)
    d = {"id": "w", "language": AUTO, "device": "c", "watch": [432.5, 434.0]}
    with pytest.raises(ConfigError, match="private radio"):
        build_station(d, "test", {"c": near}, "cept")
    assert build_station({**d, "acknowledge": True}, "test", {"c": near}, "cept").privacy == (
        "private"
    )


def test_each_channel_of_a_watch_takes_the_class_its_own_edges_reach() -> None:
    # Digital services start at 380 MHz: a 12.5 kHz channel at 379.9875 MHz stays clear of
    # them, a 30 kHz one reaches in, whatever the region.
    near = SdrDevice("c", "00001234", 379_600_000, 30.0, RATE)
    d = {"id": "w", "language": "en", "device": "c", "watch": [379.9, 379.99], "acknowledge": True}
    assert build_station(d, "test", {"c": near}, "cept").privacy == "private"
    wide = build_station({**d, "channel_khz": 30}, "test", {"c": near}, "cept")
    assert wide.privacy == "digital" and wide.sdr is not None
    assert wide.sdr.policed("2") == (379_987_500, 30_000)


@pytest.mark.parametrize("spec", [446.1, [446.0], [446.0, "446.2"], [446.0, True], {"a": 1}])
def test_a_watch_that_is_neither_a_band_nor_a_range_is_a_config_error(
    spec: object, tmp_path: Path
) -> None:
    from wavescribe.config import load_stations

    with pytest.raises(ConfigError, match=r"two frequencies in MHz.*or a band id"):
        watch_station(watch=spec)
    if isinstance(spec, float):  # at start-up, from the stations file
        path = tmp_path / "stations.toml"
        path.write_text(EDGE.replace("watch = [432.0, 434.0]", f"watch = {spec}"))
        with pytest.raises(ConfigError, match="'watch' is two frequencies in MHz"):
            load_stations(path)


def test_a_watch_its_device_cant_hear_all_of_is_out_of_reach_with_a_centre_that_works() -> None:
    far = SdrDevice("a", "00001234", 445_000_000, 30.0, RATE)
    station = build_station(
        {"id": "w", "language": "en", "device": "a", "watch": "pmr446", "acknowledge": True},
        "test",
        {"a": far},
        "cept",
    )
    assert "Centre it on 445.75625 MHz" in station.out_of_reach


EDGE = """
[radio]
region = "2"

[[device]]
id = "a"
serial = "00001234"
center_mhz = 433.00625
gain_db = 30

[[station]]
id = "edge"
device = "a"
watch = [432.0, 434.0]
language = "en"
"""


def test_a_region_the_page_chooses_checks_a_file_watch_on_its_strictest_channel(
    tmp_path: Path,
) -> None:
    from wavescribe.config import load_stations
    from wavescribe.settings import effective_config

    path = tmp_path / "stations.toml"
    path.write_text(EDGE)
    base = load_stations(path)
    assert base.get("edge").privacy == "amateur"  # all of 432-434 MHz is amateur in region 2
    # In Europe the top of the window is the 433 MHz devices band, though its middle isn't:
    # unconfirmed, the watch can't go on as amateur radio.
    config, errors = effective_config(base, {"radio": {"region": "cept"}})
    edge = config.get("edge")
    assert edge.disabled and "433 MHz short-range devices" in errors["edge"]


def test_a_change_of_region_keeps_a_watchs_class_by_its_strictest_channel(
    tmp_path: Path,
) -> None:
    from tests.test_app import Running, make_app
    from wavescribe.radio import service
    from wavescribe.radio.catalog import Catalog

    r = Running(make_app(tmp_path))
    try:
        r.app._radio = service.RadioService(r.app, Catalog(tmp_path / "radio"))
        r.app.settings.update(lambda d: d.setdefault("radio", {}).update(region="cept"))
        dongle = {"id": "w", "serial": "00001234", "center_mhz": 433.00625, "gain_db": 30}
        assert r.call("POST", "/api/manage/receivers", dongle)[0] == 200
        body = {
            "id": "edge",
            "name": "Edge",
            "device": "w",
            "watch": [432.0, 434.0],
            "language": "en",
            "acknowledge": True,
        }
        status, st = r.call("POST", "/api/manage/stations", body)
        assert status == 200 and st["privacy"] == "private"
        # ITU Region 1 has the 433 MHz devices band too: nothing to keep stricter than it is.
        assert r.call("POST", "/api/manage/radio/region", {"region": "1"})[0] == 200
        edge = r.app.config.get("edge")
        assert (edge.privacy, edge.privacy_region) == ("private", None)
        entry = next(e for e in r.app.settings.get("station") if e["id"] == "edge")
        assert "privacy_floor" not in entry  # which would outlive a narrower window
        # In region 2 it is all amateur: the watch stays private until that is confirmed.
        assert r.call("POST", "/api/manage/radio/region", {"region": "2"})[0] == 200
        edge = r.app.config.get("edge")
        assert (edge.privacy, edge.privacy_region) == ("private", "amateur")
    finally:
        r.close()


def test_language_auto_suits_any_station_and_round_trips() -> None:
    """A station whose language isn't known (found on the air, or an internet station that
    switches language) transcribes each line in the language heard, as a watch does."""
    for d in ({"id": "x", "language": AUTO, "stream": "https://e.x/s"},
              {"id": "x", "language": AUTO, "languages": ["es", "en"], "device": "a",
               "mhz": 145.5, "mode": "nfm", "acknowledge": True}):  # fmt: skip
        s = build_station(d, "test", {"a": DEVICE}, "cept")
        assert s.language == AUTO and s.languages == tuple(d.get("languages", ()))
        assert build_station(station_dict(s), "again", {"a": DEVICE}, "cept") == s
    with pytest.raises(ConfigError, match="'languages' narrows language 'auto'"):
        build_station(
            {"id": "x", "language": "en", "languages": ["en"], "stream": "https://e.x/s"}, "test"
        )
    s = watch_station(languages=["pt-PT", "es"], threshold_db=10)
    again = build_station(station_dict(s), "again", {"a": DEVICE}, "cept")
    assert again == s
    assert station_ref(s.id, {s.id: s})["language"] is None  # not a language tag elsewhere


# -- language "auto" -------------------------------------------------------------------------


class Speaker:
    """A fake speech model that hears the language from the tone: 800 Hz is Portuguese,
    anything else sounds most like English, then Spanish."""

    model_id = "fake-whisper"

    def __init__(self) -> None:
        self.calls: list[tuple[float, str]] = []

    def identify_language(self, samples: np.ndarray) -> dict[str, float]:
        if abs(dominant_hz(samples) - 800) < 50:
            return {"pt": 0.7, "es": 0.2, "en": 0.1}
        return {"en": 0.5, "es": 0.4, "pt": 0.1}

    def transcribe(self, samples: np.ndarray, language: str) -> Transcript:
        self.calls.append((dominant_hz(samples), language))
        return Transcript(f"{language} words", language, self.model_id)


def test_the_likeliest_candidate_is_chosen_by_its_primary_subtag() -> None:
    probs = {"en": 0.5, "es": 0.4, "pt": 0.1}
    assert likeliest(probs) == "en"
    assert likeliest(probs, ("pt-PT", "es")) == "es"  # English isn't among them
    assert likeliest(probs, ("es", "pt-PT")) == "es"
    # Clearly none of them: as heard, or Whisper would translate and call it a transcript.
    assert likeliest({"ru": 0.97, "en": 0.02, "pt": 0.01}, ("pt-PT", "en")) == "ru"


def test_transcribe_any_falls_back_to_the_first_candidate_for_a_model_that_cant_tell() -> None:
    class Plain:
        model_id = "plain"

        def transcribe(self, samples: np.ndarray, language: str) -> Transcript:
            return Transcript("", language, self.model_id)

    assert transcribe_any(Plain(), np.zeros(16_000), ("uk", "en")).language == "uk"
    with pytest.raises(ValueError, match="can't tell"):
        transcribe_any(Plain(), np.zeros(16_000))


def test_auto_needs_a_model_that_tells_languages_apart(tmp_path: Path) -> None:
    whisper = ModelEntry(
        "whisper-x",
        "asr",
        "mlx-audio",
        "url:x",
        "u",
        "MIT",
        "u",
        "default",
        False,
        ("en", "pt", "es"),
        "",
    )
    english = replace(whisper, id="whisper-en", source="url:en")
    chain = {"asr": ["m", "whisper-en", "whisper-x"]}
    registry = ModelRegistry([ENTRY, english, whisper], chain, models_dir=tmp_path)
    s = watch_station(languages=["pt-PT", "es"])
    with pytest.raises(ModelError, match=r"fetch whisper-large-v3-turbo.*--language pt-PT"):
        plan_detecting(registry, s)  # not downloaded
    for name, vocab in (("whisper-en", 51_864), ("whisper-x", 51_866)):
        (tmp_path / name).mkdir()
        (tmp_path / name / name.split("-")[1]).write_bytes(b"")
        (tmp_path / name / "config.json").write_text(
            f'{{"model_type": "whisper", "n_vocab": {vocab}}}'
        )
    # Skips the one that isn't Whisper, and the Whisper that only knows English.
    assert plan_detecting(registry, s)[0].id == "whisper-x"
    with pytest.raises(ModelError, match="m can't tell languages"):
        plan_detecting(registry, s, "m")
    with pytest.raises(ModelError, match="whisper-en can't tell languages"):
        plan_detecting(registry, watch_station(), "whisper-en")
    with pytest.raises(ModelError, match="doesn't know uk"):
        plan_detecting(registry, watch_station(languages=["uk"]), "whisper-x")


# -- through the pipeline --------------------------------------------------------------------


class FakeDevice:
    """Feeds prepared IQ blocks to whoever attaches, as RtlSdrDevice does from rtl_sdr."""

    available = True
    clipped = 0.0

    def __init__(self, blocks: list[tuple[np.ndarray, float]]) -> None:
        self._blocks = blocks
        self.feeding: threading.Thread | None = None

    def attach(self, consumer: WatchSource) -> None:
        def feed() -> None:
            for i, (iq, t_end) in enumerate(self._blocks):
                consumer._feed(iq, t_end, i == 0)

        self.feeding = threading.Thread(target=feed, daemon=True)
        self.feeding.start()

    def detach(self, consumer: WatchSource) -> None:
        pass


def test_a_watch_made_deaf_by_noise_says_so_in_its_health() -> None:
    from tests.test_radio_watch_detect import louder

    station = watch_station()
    source = WatchSource(station.id, station.sdr, device=FakeDevice([]))
    warnings = []
    heard: list[watches.Hearing] = []
    stop_listening = watches.subscribe_hearing(heard.append)
    try:
        for i, (iq, t_end) in enumerate(air(13.0, [], extra=louder(1.0))):
            source._feed(iq, t_end, i == 0)
            warnings.append(source.health.warning)
    finally:
        stop_listening()
    assert any(w and w.startswith("deaf") for w in warnings)
    assert warnings[-1] is None  # the noise is the floor now
    # Said once each way, for the command line, which has no status to show it in.
    deaf, hearing = heard
    assert deaf.station_id == "walkies" and deaf.warning and deaf.warning.startswith("deaf")
    assert hearing.warning is None and hearing.learnt and deaf.t < hearing.t
    assert hearing.deaf_s == pytest.approx(10, abs=0.5)


def test_a_watch_uses_a_spur_list_only_where_it_says_how_finely_it_was_learnt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from wavescribe.sources import open_source, sdr

    monkeypatch.chdir(tmp_path)  # where the catalogue keeps data/radio/spurs.json
    monkeypatch.setattr(sdr, "_devices", {})  # the dongle is shared by serial: this test's own
    station = watch_station()
    path = tmp_path / "data" / "radio" / "spurs.json"
    path.parent.mkdir(parents=True)
    learnt = {"t": 0, "start_hz": 24e6, "stop_hz": 1766e6, "freqs_hz": [PMR[3] + 1_500]}
    path.write_text(json.dumps({"00001234": learnt}))  # an earlier version's: no bin_hz
    old = open_source(station)
    assert isinstance(old, WatchSource) and (old._spurs, old._spur_bin_hz) == ((), 0.0)
    path.write_text(json.dumps({"00001234": {**learnt, "bin_hz": 6_000}}))
    new = open_source(station)
    assert isinstance(new, WatchSource)
    assert (new._spurs, new._spur_bin_hz) == ((PMR[3] + 1_500,), 6_000)


def test_a_watch_that_failed_starts_afresh_when_iterated_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wavescribe.sources.watch import Catcher

    real, calls = Catcher.feed, []

    def flaky(self: Catcher, iq: np.ndarray, t_end: float) -> list[Catch]:
        calls.append(t_end)
        if len(calls) == 3:
            raise ValueError("a bad block")
        return real(self, iq, t_end)

    monkeypatch.setattr(Catcher, "feed", flaky)
    station = watch_station()
    device = FakeDevice(list(air(4.0, [Burst(PMR[1], 1.0, 2.0)])))
    source = WatchSource(station.id, station.sdr, device=device)
    with pytest.raises(RuntimeError, match="catching failed: a bad block"):
        list(source)
    assert device.feeding is not None
    device.feeding.join(5)
    heard = []
    for chunk in source:  # as the pipeline does after a failed source's delay
        heard.append(chunk.freq_hz)
        source.close()
    assert set(heard) == {PMR[1]}


def test_catches_the_pipeline_is_too_far_behind_for_are_counted_off_the_dongles_thread(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="wavescribe")
    station = watch_station()
    source = WatchSource(station.id, station.sdr, device=FakeDevice([]), queue_catches=1)
    published: list[Catch] = []
    stop_listening = watches.subscribe(published.append)
    bursts = [Burst(PMR[k], 1.0 + k, 1.6 + k) for k in (0, 2, 4)]  # one after another
    try:
        for i, (iq, t_end) in enumerate(air(7.5, bursts)):
            source._feed(iq, t_end, i == 0)  # the test is the dongle's thread
    finally:
        stop_listening()
    assert published == [] and source.dropped == 2  # nothing written on the dongle's thread
    behind = [r for r in caplog.records if r.getMessage() == "watch_behind"]
    # Private radio keeps nothing, so the log says how many, not where or when.
    assert [r.fields["dropped"] for r in behind] == [1, 2]
    assert all("mhz" not in r.fields for r in behind)


def test_a_missed_transmission_is_logged_with_its_frequency_only_where_text_is_kept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from wavescribe.sources.watch import Catcher

    caplog.set_level("INFO", logger="wavescribe")
    station = watch_station()
    bursts = [Burst(PMR[k], 1.0, 2.0) for k in (0, 2)]
    for privacy in ("private", "amateur"):
        c = Catcher(station.sdr, max_active=1, privacy=privacy)
        for iq, t_end in air(3.5, bursts):
            c.feed(iq, t_end)
    missed = [r.fields for r in caplog.records if r.getMessage() == "watch_missed"]
    assert len(missed) == 2 and "mhz" not in missed[0] and "active" not in missed[0]
    assert missed[1]["mhz"] == PMR[2] / 1e6


def test_what_was_being_caught_when_the_watch_stops_is_still_logged() -> None:
    station = watch_station()
    # Still on when the IQ ends, and one that ended a moment before (within its hang time).
    blocks = list(air(3.0, [Burst(PMR[1], 1.0, 9.0), Burst(PMR[5], 1.0, 2.5)]))
    source = WatchSource(station.id, station.sdr, device=FakeDevice(blocks))
    caught: list[Catch] = []
    stop_listening = watches.subscribe(caught.append)
    chunks = iter(source)
    reader = threading.Thread(target=lambda: list(chunks), daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 5
        while getattr(source._device.feeding, "ident", None) is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        source._device.feeding.join(5)
        t0 = time.monotonic()
        source.close()
        reader.join(5)
        assert time.monotonic() - t0 < 1.0 and not reader.is_alive()
    finally:
        stop_listening()
    assert sorted(c.freq_hz for c in caught) == [PMR[1], PMR[5]]
    assert {c.voice for c in caught} == {None}  # not heard by the VAD: not checked


def test_each_catch_is_transcribed_on_its_own_in_the_language_heard() -> None:
    station = watch_station(languages=["pt-PT", "es"], translate_to=["en"])
    bursts = [
        Burst(PMR[1], 1.0, 2.5, tone_hz=800),
        Burst(PMR[8], 1.8, 3.3, tone_hz=1_500),  # overlaps the first
        Burst(PMR[12], 4.0, 5.0, deviation_hz=0.0),  # a carrier with nobody speaking
    ]
    blocks = list(air(7.0, bursts))
    speaker = Speaker()
    results: list[Result] = []
    caught: list[Catch] = []
    stop_listening = watches.subscribe(caught.append)
    plan = Plan(vad=(ENTRY, Path()), asr={station.id: (ENTRY, Path())}, mt=(ENTRY, Path()))
    pipeline = Pipeline(
        [station],
        plan,
        results.append,
        source_factory=lambda st: WatchSource(
            st.id, st.sdr, device=FakeDevice(blocks), privacy=st.privacy
        ),
        vad_factory=lambda _path: EnergyVad(),
        transcriber_factory=lambda _e, _p: speaker,
        translator_factory=lambda _e, _p: FakeTranslator(),
    )
    runner = threading.Thread(target=pipeline.run, daemon=True)  # until the watch is closed
    runner.start()
    try:
        deadline = time.monotonic() + 20
        while (len(caught) < 3 or len(results) < 2) and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        pipeline.stop(drain=True)
        runner.join(10)
        stop_listening()
    assert not runner.is_alive()

    by_freq = {r.utterance.freq_hz: r for r in results}
    assert sorted(by_freq) == [PMR[1], PMR[8]]
    pt, es = by_freq[PMR[1]], by_freq[PMR[8]]
    assert pt.station.language == "pt-PT" and pt.transcript.text == "pt-PT words"
    assert es.station.language == "es"  # English was likelier, but isn't a candidate
    assert pt.translations == {"en": "en:pt-PT words"}
    # Each transcribed alone: one tone each, never both in one utterance.
    assert sorted(round(hz, -2) for hz, _ in speaker.calls) == [800, 1_500]
    assert pt.utterance.t_start == pytest.approx(1_000_000 + 1.0, abs=0.35)

    assert [c.freq_hz for c in caught] == [PMR[1], PMR[8], PMR[12]]
    assert [c.voice for c in caught] == [True, True, False]
    assert {c.station_id for c in caught} == {"walkies"}
    assert {c.privacy for c in caught} == {"private"}
    assert caught[0].band == "pmr446" and caught[0].label == "PMR446 walkie-talkies, channel 2"


def test_a_silence_alert_takes_a_watchs_quiet_air_for_what_it_is() -> None:
    from wavescribe.alerts.watch import quiet

    station = watch_station()
    start = time.time() - 2.0  # air that ends now: the receiver is sending
    blocks = [(iq, start + t_end - T0) for iq, t_end in air(2.0, [])]
    pipeline = Pipeline(
        [station],
        Plan(vad=(ENTRY, Path()), asr={station.id: (ENTRY, Path())}, mt=None),
        lambda _r: None,
        source_factory=lambda st: WatchSource(st.id, st.sdr, device=FakeDevice(blocks)),
        vad_factory=lambda _path: EnergyVad(),
        transcriber_factory=lambda _e, _p: Speaker(),
    )
    runner = threading.Thread(target=pipeline.run, daemon=True)
    runner.start()
    status: dict = {}
    try:
        deadline = time.monotonic() + 10
        while status.get("last_data_age") is None and time.monotonic() < deadline:
            time.sleep(0.05)
            status = pipeline.status()["stations"].get(station.id, {})
    finally:
        pipeline.stop(drain=True)
        runner.join(10)
    # Nothing on the air: no audio level at all, yet no silence. Station alerts read it as a
    # narrowband channel's quiet, where only an outage counts.
    assert status["connected"] and status["squelch_open"] is False
    assert quiet(status, -50.0) is None
    assert quiet({**status, "squelch_open": None}, -50.0) == "dead air"


def test_a_watch_only_listened_to_says_its_catches_werent_checked() -> None:
    station = watch_station(transcribe=False)
    blocks = list(air(4.0, [Burst(PMR[1], 1.0, 2.5, tone_hz=800)]))
    caught: list[Catch] = []
    stop_listening = watches.subscribe(caught.append)
    pipeline = Pipeline(
        [station],
        Plan(vad=(ENTRY, Path()), asr={}, mt=None),
        lambda _r: None,
        source_factory=lambda st: WatchSource(
            st.id, st.sdr, device=FakeDevice(blocks), privacy=st.privacy
        ),
        vad_factory=lambda _path: EnergyVad(),
    )
    runner = threading.Thread(target=pipeline.run, daemon=True)
    runner.start()
    try:
        deadline = time.monotonic() + 10
        while not caught and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        pipeline.stop(drain=True)
        runner.join(10)
        stop_listening()
    # Nobody's speech was looked for, so whether anyone spoke is unknown, not "no".
    assert [(c.freq_hz, c.voice) for c in caught] == [(PMR[1], None)]
