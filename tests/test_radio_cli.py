# SPDX-License-Identifier: Apache-2.0
"""`wavescribe radio` output: what a scan says about the lines it marks and the range it cut."""

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_agent import HEARD_KEYS
from wavescribe.cli import main
from wavescribe.radio import cli as radio_cli
from wavescribe.radio import holders

RX = radio_cli.Receiver("00001234", 30.0, 0, "receiver A")
STATIONS = (
    '[[device]]\nid = "a"\nserial = "00001234"\ncenter_mhz = 99.0\ngain_db = 30\n\n'
    '[[station]]\nid = "one"\nlanguage = "es-ES"\nfm_mhz = 99.0\ndevice = "a"\n'
)


def line(**s) -> str:
    return radio_cli._signal_line({"mhz": 122.4, "snr_db": 12.0, "mode": "am",
                                   "privacy": "utility", **s})  # fmt: skip


def test_marked_lines_say_whose_they_probably_are() -> None:
    assert "another receiver's clock, not a transmission" in line(spur=True, clock_hz=2_400_029)
    assert "receiver's own spur" in line(spur=True)
    assert "spur" not in line() and "clock" not in line()


def test_a_scan_says_which_part_of_the_range_it_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(radio_cli, "resolve_receiver", lambda *_a: RX)
    asked = []

    def fake_scan(serial, start, stop, region, **_kw):
        asked.append((start, stop))
        return None, []

    monkeypatch.setattr("wavescribe.radio.scan.scan", fake_scan)
    monkeypatch.setattr("wavescribe.radio.catalog.Catalog.record", lambda *_a, **_k: None)
    assert main(["radio", "scan", "a", "20:30"]) == 0
    err = capsys.readouterr().err
    assert asked == [(24_000_000, 30_000_000)]
    assert "scanning 24 to 30 MHz of the 20 to 30 MHz asked for" in err
    assert "below 24 MHz isn't supported" in err


def test_a_scan_from_the_command_line_is_saved_with_its_receiver_and_what_was_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "stations.toml").write_text(
        '[[device]]\nid = "a"\nserial = "00001234"\ncenter_mhz = 98.0\ngain_db = 30\n'
        '[[station]]\nid = "s"\nlanguage = "es"\nstream = "http://example.test/s"\n'
    )

    def no_dongles():
        raise AssertionError("the receiver comes from the config: no dongle is listed")

    monkeypatch.setattr("wavescribe.sources.sdr.list_dongles", no_dongles)
    monkeypatch.setattr("wavescribe.radio.scan.scan", lambda *_a, **_k: (None, []))
    saved = []
    monkeypatch.setattr("wavescribe.radio.catalog.Catalog.record",
                        lambda _self, _signals, **kw: saved.append(kw))  # fmt: skip
    assert main(["radio", "scan", "a", "144:146", "--config", "stations.toml"]) == 0
    assert main(["radio", "scan", "00001234", "fm", "--region", "cept",
                 "--config", "stations.toml"]) == 0  # fmt: skip
    assert [(kw["serial"], kw["device"], kw["what"]) for kw in saved] == [
        ("00001234", "a", "144 to 146 MHz"),
        ("00001234", "a", "FM broadcast"),
    ]


def test_a_scan_shows_the_sweeps_warnings_and_flags_old_spurs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from types import SimpleNamespace

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(radio_cli, "resolve_receiver", lambda *_a: RX)
    sweep = SimpleNamespace(warnings=["the tuner's PLL didn't lock twice during this sweep"])
    monkeypatch.setattr("wavescribe.radio.scan.scan", lambda *_a, **_k: (sweep, []))
    monkeypatch.setattr("wavescribe.radio.catalog.Catalog.record", lambda *_a, **_k: None)
    old = {"00001234": {"t": 1.0, "count": 3, "bin_hz": None}}
    monkeypatch.setattr("wavescribe.radio.catalog.Catalog.spur_info", lambda _self: old)
    assert main(["radio", "scan", "a", "144:146"]) == 0
    err = capsys.readouterr().err
    assert "# warning: the tuner's PLL didn't lock twice" in err
    assert "learn them again with `wavescribe radio spurs a --learn`" in err


@pytest.mark.parametrize(
    ("found", "said"),
    [
        ([], "# 0 signals in 0 s; kept in data/radio/heard.json"),
        (["clock"] * 4, "# 0 signals in 0 s, and 4 lines that are probably another receiver's "
                        "clock; kept in data/radio/heard.json"),
        (["real", "clock", "spur", "spur"], "# 1 signal in 0 s, and 1 line that is probably "
         "another receiver's clock and 2 of the receiver's own spurs; kept in "
         "data/radio/heard.json"),
    ],
)  # fmt: skip
def test_a_scan_counts_clock_lines_and_spurs_apart_from_signals(
    found: list[str], said: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """An airband scan said "4 signals", all four another receiver's clock."""
    from wavescribe.radio.scan import Signal

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(radio_cli, "resolve_receiver", lambda *_a: RX)
    signals = [
        Signal(122_400_000 + i * 2_400_000, -30.0, 12.0, 2_000.0, mode="am",
               spur=kind != "real", clock_hz=2_400_029 if kind == "clock" else None)
        for i, kind in enumerate(found)
    ]  # fmt: skip
    monkeypatch.setattr("wavescribe.radio.scan.scan", lambda *_a, **_k: (None, signals))
    monkeypatch.setattr("wavescribe.radio.catalog.Catalog.record", lambda *_a, **_k: None)
    monkeypatch.setattr(radio_cli.time, "time", lambda: 1_000.0)
    assert main(["radio", "scan", "a", "airband"]) == 0
    out, err = capsys.readouterr()
    assert err.splitlines()[-1] == said
    assert len(out.splitlines()) == len(found)  # each still listed, and marked


def test_spurs_learnt_at_the_new_resolution_need_no_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(radio_cli, "resolve_receiver", lambda *_a: RX)
    new = {"00001234": {"t": 1.0, "count": 1, "bin_hz": 5_468.75}}
    monkeypatch.setattr("wavescribe.radio.catalog.Catalog.spur_info", lambda _self: new)
    monkeypatch.setattr("wavescribe.radio.catalog.Catalog.spurs", lambda _self, _s: [144_500_000])
    assert main(["radio", "spurs", "a"]) == 0
    out = capsys.readouterr()
    assert "144.50000 MHz" in out.out and "learn them again" not in out.err


def test_no_output_names_a_dongle_by_its_serial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from wavescribe.radio.catalog import Catalog
    from wavescribe.radio.scan import Signal

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "stations.toml").write_text(STATIONS)
    heard = Signal(freq_hz=99_000_000, level_db=-40.0, snr_db=20.0, width_hz=1e5, band="fm",
                   label="FM", mode="wfm", privacy="broadcast")  # fmt: skip
    Catalog().record([heard], serial="00001234", start_hz=88e6, stop_hz=108e6)
    Catalog().record([dataclasses.replace(heard, freq_hz=101_000_000)], serial="00009876",
                     start_hz=100e6, stop_hz=102e6)  # fmt: skip
    assert main(["radio", "heard", "--json"]) == 0
    rows = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [r.get("device") for r in rows] == ["a", "…9876"]  # no receiver uses it any more
    assert all(set(r) == HEARD_KEYS for r in rows)
    assert not any("serial" in r for r in rows)

    monkeypatch.setattr("wavescribe.radio.scan.scan", lambda *_a, **_k: (None, []))
    monkeypatch.setattr("wavescribe.radio.catalog.Catalog.record", lambda *_a, **_k: None)
    assert main(["radio", "scan", "a", "fm", "--region", "cept"]) == 0
    assert main(["radio", "spurs", "a"]) == 0
    assert main(["radio", "spurs", "a", "--forget"]) == 0
    out = capsys.readouterr()
    assert "on receiver A at 30 dB gain" in out.err and "No spurs learnt for receiver A" in out.out
    assert "Forgot receiver A's spurs." in out.out
    assert "1234" not in out.out + out.err

    monkeypatch.setattr("wavescribe.sources.sdr.list_dongles", lambda: [{"serial": "00004321"}])
    with pytest.raises(ValueError, match="plugged in: …4321; Settings → Receivers and") as e:
        radio_cli.resolve_receiver("b", radio_cli._config(SimpleNamespace(config=None)))
    assert "00004321" not in str(e.value)
    assert radio_cli.resolve_receiver("00004321", None).name == "the dongle …4321"


SERVE_HOLDS = {"pid": 4001, "program": "rtl_sdr", "other_wavescribe": True,
               "wavescribe_pid": 4000, "serve": True}  # fmt: skip


@pytest.mark.parametrize(
    ("held", "said"),
    [
        (SERVE_HOLDS,
         "receiver A is busy: Wavescribe itself has it open (`wavescribe serve`, process 4000). "
         "Do it from the page instead: Find → On the air, which pauses the receiver's stations "
         "for the scan. Or stop serve, or use another receiver"),
        ({**SERVE_HOLDS, "serve": False},
         "receiver A is busy: another Wavescribe command has it open (process 4000). Wait for "
         "it to end or stop it, or use another receiver"),
        ({"pid": 3001, "program": "sdrpp", "other_wavescribe": False, "wavescribe_pid": None,
          "serve": False},
         "receiver A is busy: sdrpp (process 3001) has it open. Close it, or use another "
         "receiver"),
        # Not known for sure (not macOS, or the registry didn't say): nothing is named.
        (None, "receiver A is busy: another program has it open (`wavescribe serve`, an SDR "
               "program, or another rtl_ tool). Close it, or use another receiver; if it is "
               "serve, do it from the page instead: Find → On the air, which pauses the "
               "receiver's stations for the scan"),
    ],
)  # fmt: skip
def test_a_busy_receiver_names_what_holds_it_and_only_that(
    held, said, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from wavescribe.radio.scan import Busy

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(radio_cli, "resolve_receiver", lambda *_a: RX)
    # Another serve runs on this machine, but it isn't what holds this receiver.
    others = {"00009999": SERVE_HOLDS}
    monkeypatch.setattr(holders, "held_elsewhere", lambda: {"00001234": held} if held else others)

    def holds(*_a, **_k):
        raise Busy()

    monkeypatch.setattr("wavescribe.radio.scan.scan", holds)
    assert main(["radio", "scan", "a", "fm", "--region", "cept"]) == 1
    assert capsys.readouterr().err.splitlines()[-1] == f"wavescribe: {said}"


@pytest.mark.parametrize(
    ("command", "found"),
    [
        ("/x/.venv/bin/python /x/.venv/bin/wavescribe serve --port 8795", True),
        ("uv run wavescribe serve", True),
        ("/usr/bin/caffeinate -s /x/python -m wavescribe serve --lan", True),
        ("uv run wavescribe radio scan a fm", False),
        ("vim wavescribe-serve.md", False),
    ],
)
def test_a_running_serve_is_found_however_it_was_started(command, found) -> None:
    assert bool(holders.SERVE.search(command)) is found


def test_messages_name_and_list_things_as_the_page_does() -> None:
    from wavescribe.radio.words import joined, masked_serial, receiver_name, sentence, them

    assert receiver_name("a") == "A" and receiver_name("roof") == "roof"
    assert joined(["FM 1"]) == "FM 1" and joined(["FM 1", "FM 2", "the monitor"]) == (
        "FM 1, FM 2 and the monitor"
    )
    assert them(["FM 1"]) == "it" and them(["FM 1", "FM 2"]) == "them"
    assert sentence("which language?") == "Which language?" and sentence("a b") == "A b."
    assert masked_serial("00001234") == "…1234" and masked_serial("12") == "…2"
