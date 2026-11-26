# SPDX-License-Identifier: Apache-2.0
import random
import tomllib
from pathlib import Path

import pytest

from wavescribe.config import ConfigError, parse_stations
from wavescribe.settings import Settings, dumps, effective_config

BASE = {
    "defaults": {"language": "pt-PT", "translate_to": ["en"], "priority": 3},
    "device": [{"id": "a", "serial": "00001234", "center_mhz": 94.0, "gain_db": 20}],
    "station": [
        {"id": "one", "name": "One", "stream": "http://x/one", "fm_mhz": [94.4], "device": "a"},
        {"id": "two", "name": "Two", "language": "en-GB", "stream": "http://x/two"},
    ],
}


def base():
    return parse_stations(BASE, Path("stations.toml"))


def test_toml_writer_round_trips_what_the_page_stores() -> None:
    data = {
        "node": {"name": "Kitchen \"Mac\"", "public_url": "http://mac.local:8766"},
        "retention": {"audio_days": 7.0, "audio_max_gb": 2, "text_days": 365.0},
        "overrides": {"one": {"translate_to": ["en", "uk"], "disabled": True}},
        "station": [{"id": "new", "name": "Nová\tstanice", "stream": "http://x/new"}],
        "output": [{"id": "hook", "url": "http://h/x", "headers": {"X-Key": "s3cr=t"},
                    "events": ["alert"], "secret": None}],
    }  # fmt: skip
    text = dumps(data)
    back = tomllib.loads(text)
    data["output"][0].pop("secret")  # None is left out
    assert back == data


def test_toml_writer_round_trips_any_text() -> None:
    """Names come from station directories and from other machines: any character may turn up
    in a value or a key, and the file must still load."""
    rng = random.Random(7)
    awkward = [chr(c) for c in (*range(0x20), 0x7F, 0x80, 0x85, 0x9F, 0xA0, 0x2028, 0xFEFF)]
    alphabet = [*awkward, *"\"'\\#=[]{}.,- aé", "日", "\U0001f4fb"]

    def text() -> str:
        return "".join(rng.choices(alphabet, k=rng.randint(0, 12)))

    for c in awkward:  # each on its own, as a value and as a key
        assert tomllib.loads(dumps({"t": {"name": c, c: 1}})) == {"t": {"name": c, c: 1}}
    for _ in range(300):
        data = {
            "node": {"name": text(), text(): text()},
            "overrides": {text(): {"name": text(), "translate_to": [text(), text()]}},
            "station": [{"id": text(), "name": text(), "headers": {text(): text()}}],
        }
        assert tomllib.loads(dumps(data)) == data


@pytest.mark.parametrize("key", ["id\n", "a b", "", "é", "a.b", "\x7f"])
def test_toml_writer_quotes_keys_that_arent_bare(key: str) -> None:
    assert tomllib.loads(dumps({"t": {key: 1}, "list": [{key: "x"}]})) == {
        "t": {key: 1}, "list": [{key: "x"}],
    }  # fmt: skip


def test_a_file_that_wouldnt_load_is_never_written(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "ui.toml"
    s = Settings(path)
    s.update(lambda d: d.setdefault("node", {}).update(name="kitchen"))
    before = path.read_text()
    # As if the writer got a character wrong: what it produces doesn't read back.
    monkeypatch.setattr("wavescribe.settings._string", lambda text: f'"{text}"')
    with pytest.raises(ConfigError, match="not saved"):
        s.update(lambda d: d["node"].update(name="kitchen\x7fmac"))
    with pytest.raises(ConfigError, match="not saved"):
        s.update(lambda d: d["node"].update(name='the "big" one'))  # loads, as something else
    assert path.read_text() == before and s.get("node") == {"name": "kitchen"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ui.toml"]  # no temporary file left
    monkeypatch.undo()
    s.update(lambda d: d["node"].update(name="kitchen\x7fmac"))
    assert Settings(path).get("node") == {"name": "kitchen\x7fmac"}


def test_settings_save_atomically_and_tell_observers(tmp_path: Path) -> None:
    path = tmp_path / "ui.toml"
    s = Settings(path)
    seen = []
    s.observe(lambda d: seen.append(d))
    s.update(lambda d: d.setdefault("retention", {}).update(audio_days=3.0))
    assert tomllib.loads(path.read_text())["retention"] == {"audio_days": 3.0}
    assert path.read_text().startswith("# Written by the Wavescribe page")
    assert path.stat().st_mode & 0o777 == 0o600
    assert seen == [{"retention": {"audio_days": 3.0}}]
    assert Settings(path).get("retention") == {"audio_days": 3.0}  # read back after a restart

    with pytest.raises(ZeroDivisionError):  # a failing change leaves file and memory alone
        s.update(lambda d: 1 / 0)
    assert s.get("retention") == {"audio_days": 3.0}

    s.update(lambda d: d["retention"].clear())  # empty tables are pruned
    assert "retention" not in tomllib.loads(path.read_text())


def test_overrides_added_stations_and_devices_lay_over_the_base() -> None:
    data = {
        "overrides": {
            "one": {"translate_to": ["en", "uk"], "asr_model": "whisper-large-v3-turbo"},
            "two": {"disabled": True, "priority": 1},
        },
        "station": [
            {"id": "far", "name": "Far", "remote": "http://node.local:8766/",
             "remote_station": "tsf", "remote_token": "t0k"},
            {"id": "es", "name": "ES", "language": "es-ES", "stream": "http://x/es"},
            {"id": "bad", "name": "Bad"},  # no language override, so the default pt-PT; fine
            {"id": "two", "name": "Duplicate"},
        ],
        "device_overrides": {"a": {"gain_db": 12.5, "ppm": -7}},
    }  # fmt: skip
    config, errors = effective_config(base(), data)
    by = {s.id: s for s in config.stations}
    assert by["one"].translate_to == ("en", "uk")
    assert by["one"].asr_model == "whisper-large-v3-turbo" and not by["one"].disabled
    assert by["one"].sdr.device.gain_db == 12.5 and by["one"].sdr.device.ppm == -7
    assert by["two"].disabled and by["two"].priority == 1
    assert by["far"].remote == "http://node.local:8766" and by["far"].remote_station == "tsf"
    assert by["far"].language == "pt-PT"  # the base file's defaults apply to added stations
    assert by["es"].language == "es-ES"
    assert list(by) == ["one", "two", "far", "es", "bad"]
    assert "two" in errors and "already exists" in errors["two"]


def test_a_page_change_to_a_dongle_keeps_its_frequency_correction() -> None:
    raw = {**BASE, "device": [{**BASE["device"][0], "ppm": 52}]}
    config, errors = effective_config(
        parse_stations(raw, Path("stations.toml")), {"device_overrides": {"a": {"gain_db": 12.5}}}
    )
    assert not errors
    assert (config.devices[0].gain_db, config.devices[0].ppm) == (12.5, 52)


def test_a_broken_override_keeps_the_base_station_and_reports_it() -> None:
    data = {
        "overrides": {"one": {"language": "not a tag"}},
        "device_overrides": {"a": {"center_mhz": 100.0}},  # 94.4 MHz falls out of its band
    }
    config, errors = effective_config(base(), data)
    by = {s.id: s for s in config.stations}
    assert by["one"].language == "pt-PT" and not by["one"].disabled
    assert "BCP-47" in errors["one"] or "band" in errors["one"]


def test_remote_stations_have_the_class_the_other_machine_gave_them() -> None:
    far = {"remote": "http://node:8766", "remote_station": "x"}
    data = {
        "station": [{"id": "known", **far}, {"id": "unstated", **far}, {"id": "odd", **far}],
        "remote_privacy": {"known": "amateur", "odd": "nonsense", "two": "private"},
    }
    config, errors = effective_config(base(), data)
    by = {s.id: s.privacy for s in config.stations}
    assert not errors and by["known"] == "amateur"
    assert by["unstated"] == "broadcast"  # a sender that says nothing sends what may be passed on
    assert by["odd"] == "private"  # a class nobody knows: nothing kept, nothing sent on
    assert by["two"] == "broadcast"  # a station heard here has the class of its frequency


def test_a_stricter_region_switches_off_what_it_makes_private() -> None:
    """147 MHz is amateur radio in the Americas and in no European band plan. A station the
    file made under region 2 mustn't go on running as amateur once the page says Europe."""
    raw = {
        "defaults": {"language": "en-US"},
        "radio": {"region": "2"},
        "device": [{"id": "v", "serial": "00001234", "center_mhz": 147.25, "gain_db": 20}],
        "station": [{"id": "repeater", "mhz": 147.0, "device": "v"}],
    }
    file = parse_stations(raw, Path("stations.toml"))
    assert (file.stations[0].privacy, file.stations[0].disabled) == ("amateur", False)

    config, errors = effective_config(file, {"radio": {"region": "cept"}})
    assert config.get("repeater").disabled
    assert "private radio" in errors["repeater"] and "acknowledge = true" in errors["repeater"]

    # Switching it on isn't a confirmation; confirming is, and then it is what it is over here.
    page = {"radio": {"region": "cept"}, "overrides": {"repeater": {"disabled": False}}}
    config, errors = effective_config(file, page)
    assert config.get("repeater").disabled and "repeater" in errors
    page["overrides"]["repeater"] = {"acknowledge": True}
    config, errors = effective_config(file, page)
    assert not errors
    assert (config.get("repeater").privacy, config.get("repeater").disabled) == ("private", False)


def test_remote_stations_need_their_id_over_there() -> None:
    data = {"station": [{"id": "far", "remote": "http://node:8766"}]}
    config, errors = effective_config(base(), data)
    assert "far" not in {s.id for s in config.stations}
    assert "remote_station" in errors["far"]
