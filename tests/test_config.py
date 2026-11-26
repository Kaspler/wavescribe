# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest

from wavescribe.config import (
    EXAMPLE_STATIONS_PATH,
    ConfigError,
    load_stations,
    parse_sdr_source,
    parse_stations,
)

P = Path("test.toml")


def test_example_config_loads() -> None:
    cfg = load_stations(EXAMPLE_STATIONS_PATH)
    antena1 = cfg.get("antena1")
    assert antena1.language == "pt-PT"
    assert antena1.translate_to == ("en",)
    assert antena1.alert_languages == ("en", "es")
    assert antena1.priority == 1
    assert antena1.stream and antena1.stream.startswith("https://")
    assert antena1.fm_mhz == (99.0,)
    assert antena1.mw_khz == 666.0
    assert {s.id for s in cfg.stations} >= {"antena1", "tsf"}


def test_station_overrides_defaults() -> None:
    cfg = parse_stations(
        {
            "defaults": {"language": "pt-PT", "translate_to": ["en"]},
            "station": [
                {"id": "a", "stream": "https://example.org/a.mp3"},
                {"id": "b", "language": "es-ES", "translate_to": ["en", "uk"]},
            ],
        },
        P,
    )
    assert cfg.get("a").language == "pt-PT"
    assert cfg.get("b").language == "es-ES"
    assert cfg.get("b").translate_to == ("en", "uk")
    assert cfg.get("a").priority == 5


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"station": [{"id": "a", "language": "pt", "strem": "x"}]}, "unknown keys"),
        ({"station": [{"id": "a"}]}, "'language' is required"),
        ({"station": [{"id": "a", "language": "pt", "stream": "file:///etc/hosts"}]}, "an http"),
        ({"station": [{"id": "a", "language": "pt", "stream_hls": "concat:a|b"}]}, "an http"),
        ({"station": [{"id": "a", "language": "Portuguese!"}]}, "not a BCP-47"),
        ({"station": [{"id": "a", "language": "pt\n"}]}, "not a BCP-47"),
        ({"station": [{"id": "A b", "language": "pt"}]}, "'id' must be"),
        ({"station": [{"id": "a\n", "language": "pt"}]}, "'id' must be"),
        (
            {
                "device": [{"id": "d\n", "serial": "00001234", "center_mhz": 94.0, "gain_db": 20}],
                "station": [{"id": "a", "language": "pt"}],
            },
            "'id' must be",
        ),
        (
            {"station": [{"id": "a", "language": "pt"}, {"id": "a", "language": "pt"}]},
            "duplicate",
        ),
        ({"station": [{"id": "a", "language": "pt", "priority": 0}]}, "priority"),
        ({"station": []}, "no \\[\\[station\\]\\]"),
        ({"stations": []}, "unknown top-level"),
        # A value of the wrong shape is named, never read letter by letter or key by key.
        (
            {"station": [{"id": "a", "language": "pt", "translate_to": "en"}]},
            r"station #1 \(a\): 'translate_to' must be a list of language tags, e.g. \[\"en\"\]",
        ),
        (
            {"station": [{"id": "a", "language": "pt", "alert_languages": "uk"}]},
            r"'alert_languages' must be a list of language tags, e.g. \[\"uk\"\]",
        ),
        (
            {"station": {"id": "a", "language": "pt"}},
            r"test.toml: stations are written as \[\[station\]\], with double brackets",
        ),
        (
            {"device": {"id": "a"}, "station": [{"id": "a", "language": "pt"}]},
            r"test.toml: devices are written as \[\[device\]\], with double brackets",
        ),
        ({"station": ["antena1"]}, r"test.toml station #1: must be a \[\[station\]\] table"),
        ({"radio": 5, "station": [{"id": "a", "language": "pt"}]}, r"\[radio\] must be a table"),
        ({"defaults": "pt", "station": [{"id": "a"}]}, r"\[defaults\] must be a table"),
        (
            {"station": [{"id": "a", "language": "pt", "fm_mhz": "abc"}]},
            r"station #1 \(a\): 'fm_mhz' must be a frequency in MHz or a list of them, e.g. 99.0",
        ),
        ({"station": [{"id": "a", "language": "pt", "mhz": [446.1, True]}]}, "'mhz' must be a"),
        (
            {"station": [{"id": "a", "language": "pt", "mw_khz": "666 kHz"}]},
            r"station #1 \(a\): 'mw_khz' must be a frequency in kHz, e.g. 666",
        ),
    ],
)
def test_invalid_configs(raw: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_stations(raw, P)


def test_unknown_station_lists_known_ids() -> None:
    cfg = parse_stations({"station": [{"id": "a", "language": "pt"}]}, P)
    with pytest.raises(ConfigError, match="known: a"):
        cfg.get("zz")


@pytest.mark.parametrize("path", sorted(Path("config").glob("stations*.example.toml")), ids=str)
def test_every_example_config_loads(path: Path) -> None:
    cfg = load_stations(path)
    for s in cfg.stations:
        assert s.stream or s.stream_hls or s.fm_mhz or s.mw_khz, s.id


def test_english_station_needs_no_translation_to_english() -> None:
    cfg = load_stations(Path("config/stations.world.example.toml"))
    assert cfg.get("bbc-ws").translation_targets == ()
    assert cfg.get("dlf").translation_targets == ("en",)


SDR_RAW = {
    "defaults": {"language": "pt-PT"},
    "device": [{"id": "a", "serial": "00001234", "center_mhz": 94.0, "gain_db": 20}],
    "station": [
        {"id": "near", "fm_mhz": [99.9, 94.4], "device": "a"},
        {"id": "far", "fm_mhz": 100.0, "stream": "https://example.org/far.mp3"},
    ],
}


def test_station_on_a_device_gets_the_frequency_it_can_hear() -> None:
    cfg = parse_stations(SDR_RAW, P)
    (dev,) = cfg.devices
    assert (dev.serial, dev.center_hz, dev.gain_db, dev.sample_rate) == (
        "00001234",
        94_000_000,
        20.0,
        2_400_000,
    )
    near = cfg.get("near").sdr
    assert near is not None and near.freq_hz == 94_400_000 and near.device is dev
    assert near.deemphasis_us == 50.0
    assert cfg.get("far").sdr is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"station": [{"id": "s", "fm_mhz": 99.0, "device": "a"}]}, "within device 'a'"),
        ({"station": [{"id": "s", "fm_mhz": 94.4, "device": "b"}]}, "unknown device 'b'"),
        ({"device": [{"id": "a", "serial": "has space", "center_mhz": 94.0, "gain_db": 20}]},
         "no spaces"),
        ({"device": [{"id": "a", "serial": "00001234", "center_mhz": 94.0}]}, "gain_db"),
        ({"device": [{"id": "a", "serial": "00001234", "center_mhz": 94.0, "gain_db": 20,
                      "sample_rate": 2_048_000}]}, "multiple of 240000"),
        ({"device": [{"id": "a", "serial": "00001234", "center_mhz": 94.0, "gain_db": 20,
                      "sample_rate": 720_000}]}, "that the dongle takes"),
        ({"device": [{"id": "a", "serial": "00001234", "center_mhz": 94.0, "gain_db": 20},
                     {"id": "b", "serial": "00001234", "center_mhz": 103.5, "gain_db": 20}]},
         "already used"),
        ({"device": [{"id": "a", "serial": "00001234", "center_mhz": 94_000_000, "gain_db": 20}]},
         "94000000 looks like Hz; frequencies are in MHz: 94"),
        ({"device": [{"id": "a", "serial": "00001234", "center_mhz": 5.0, "gain_db": 20}]},
         "tunes 24 to 1766 MHz"),
        ({"station": [{"id": "s", "mhz": 446_006_250, "device": "a"}]},
         "looks like Hz; frequencies are in MHz: 446.00625"),
        ({"station": [{"id": "s", "mhz": 145_500, "device": "a"}]},
         "looks like kHz; frequencies are in MHz: 145.5"),
        ({"station": [{"id": "s", "mhz": float("inf"), "device": "a"}]}, "give a frequency in MHz"),
        ({"device": [{"id": "a", "serial": "00001234", "center_mhz": 94.0, "gain_db": 20,
                      "ppm": 1.5}]}, "'ppm' must be a whole number"),
        ({"device": [{"id": "a", "serial": "00001234", "center_mhz": 94.0, "gain_db": 20,
                      "ppm": 5200}]}, "between -500 and 500"),
        ({"station": [{"id": "s", "fm_mhz": -94.4, "device": "a"}]}, "give a frequency in MHz"),
    ],
)  # fmt: skip
def test_invalid_sdr_configs(change: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_stations({**SDR_RAW, **change}, P)


def test_a_narrowband_channel_on_the_devices_centre_is_refused() -> None:
    def station(mhz: float, centre: float, **more: object) -> dict:
        return {
            "defaults": {"language": "pt-PT"},
            "radio": {"region": "cept"},
            "device": [{"id": "a", "serial": "00001234", "center_mhz": centre, "gain_db": 20}],
            "station": [{"id": "s", "mhz": mhz, "device": "a", "acknowledge": True, **more}],
        }

    # Walkie-talkie channel 1 on a device set to the round number next to it: 6.25 kHz off.
    with pytest.raises(ConfigError, match=r"at the centre of device 'a'.*e\.g\. 445\.75625"):
        parse_stations(station(446.00625, 446.0), P)
    with pytest.raises(ConfigError, match="at the centre"):  # a 25 kHz channel reaches further
        parse_stations(station(156.8, 156.78, channel_khz=25), P)
    parse_stations(station(446.00625, 445.99), P)  # 16 kHz off, a 12.5 kHz channel
    parse_stations(station(118.1, 118.075, mode="am"), P)  # the next 25 kHz airband channel
    # A broadcast station is far above the spike and may sit on it.
    parse_stations({**SDR_RAW, "station": [{"id": "s", "fm_mhz": 94.0, "device": "a"}]}, P)


def test_a_device_carries_its_frequency_correction() -> None:
    assert parse_stations(SDR_RAW, P).devices[0].ppm == 0
    raw = {**SDR_RAW, "device": [{**SDR_RAW["device"][0], "ppm": -37}]}
    assert parse_stations(raw, P).devices[0].ppm == -37
    assert parse_sdr_source("sdr:00001234/94.4?ppm=52").device.ppm == 52
    with pytest.raises(ConfigError, match="ppm"):
        parse_sdr_source("sdr:00001234/94.4?ppm=1.5")


def test_sdr_source_override() -> None:
    t = parse_sdr_source("sdr:00001234/94.4?gain=12.5")
    assert (t.device.serial, t.freq_hz, t.device.gain_db) == ("00001234", 94_400_000, 12.5)
    assert t.device.center_hz == 94_100_000  # 300 kHz below, off the centre bin
    for bad, message in [
        ("sdr:00001234", "expected"),
        ("sdr:00001234/94.4?gian=3", "unknown options"),
        ("sdr:00001234/94.4?center=97", "outside the band"),
        ("sdr:00001234/94400000", "looks like Hz"),
        ("sdr:00001234/nan", "give a frequency in MHz"),
        # librtlsdr takes 225-300 kHz and 0.9-3.2 MHz only; rtl_sdr would exit at once.
        ("sdr:00001234/94.4?rate=480000", "multiple of 240000 that the dongle takes"),
        ("sdr:00001234/94.4?rate=3360000", "multiple of 240000 that the dongle takes"),
        # A narrowband channel on the dongle's own spike, as stations.toml refuses it.
        ("sdr:00001234/446.00625?mode=nfm&ack=1&center=446.00625", "at the centre"),
        # Checked as stations.toml checks them, not on the dongle's thread.
        ("sdr:00001234/446.00625?mode=nfm&ack=1&channel=100", "channel must be 2.5 to 30 kHz"),
        ("sdr:00001234/94.4?deemphasis=0", "deemphasis must be a positive number"),
        ("sdr:00001234/94.4?deemphasis=nan", "deemphasis must be a positive number"),
    ]:
        with pytest.raises(ConfigError, match=message):
            parse_sdr_source(bad)
    assert parse_sdr_source("sdr:00001234/446.00625?mode=nfm&ack=1").device.center_hz == 445_756_250
    assert parse_sdr_source("sdr:00001234/94.4?rate=960000").device.sample_rate == 960_000
