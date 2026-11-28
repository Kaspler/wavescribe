# SPDX-License-Identifier: Apache-2.0
"""Alerts to one node of a LoRa mesh: a direct message through the meshtastic program, with the
node's acknowledgement, paced by a floor and a daily cap, riding the outputs' outbox. Against
a stand-in for the program (tests/fake_meshtastic.py), never a real one or a radio."""

import datetime as dt
import json
import stat
import sys
import time
from pathlib import Path

import pytest

from tests.test_outputs import ALERT, NODE, STATIONS, wait_for
from wavescribe import mesh
from wavescribe.events import alert_event, as_mesh_line
from wavescribe.outputs import OutputConfig, OutputError, Outputs
from wavescribe.store import Store

FAKE = Path(__file__).with_name("fake_meshtastic.py")
POCKET = "!1a2b3c4d"


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    exe = tmp_path / "meshtastic"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_MESH_LOG", str(tmp_path / "runs.jsonl"))
    return str(exe)


def runs(tmp_path: Path) -> list[dict]:
    log = tmp_path / "runs.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Floors and holds of a fraction of a second, so a test doesn't wait minutes."""
    monkeypatch.setattr(mesh, "MIN_GAP_LIMITS", (0.0, 3600.0))
    monkeypatch.setattr(mesh, "ACK_TIMEOUT_LIMITS", (0.1, 600.0))
    monkeypatch.setattr(mesh, "HOLD_MAX_S", 0.2)


def mesh_outputs(raw: dict, store=None, **kw) -> Outputs:
    config = {"id": "pocket", "kind": "mesh", "node": POCKET, **raw}
    return Outputs([OutputConfig.from_dict(config)], store=store, stations=lambda: STATIONS,
                   node=lambda: NODE, sleep=lambda s: None, **kw)  # fmt: skip


# -- the program ----------------------------------------------------------------------------------


def test_a_direct_message_to_one_node_with_an_acknowledgement(fake_cli: str, tmp_path: Path):
    sent = mesh.send("14:03 a [HIGH] water: Corte de água", POCKET, device="/dev/cu.usbserial-01",
                     command=fake_cli)  # fmt: skip
    assert sent == mesh.Sent(acked=True, on_air=True, retry=False, error=None)
    [run] = runs(tmp_path)
    assert run["argv"] == ["--port", "/dev/cu.usbserial-01", "--dest", POCKET, "--sendtext",
                           "14:03 a [HIGH] water: Corte de água", "--ack"]  # fmt: skip
    assert run["ch_index"] is None  # never a channel
    mesh.send("x", POCKET, device="meshnode.local", command=fake_cli)
    mesh.send("-x", POCKET, command=fake_cli)  # a leading dash isn't read as an option
    assert runs(tmp_path)[1]["argv"][:2] == ["--host", "meshnode.local"]
    assert runs(tmp_path)[2]["argv"][:2] == ["--dest", POCKET]
    assert runs(tmp_path)[2]["sendtext"] == " -x"


@pytest.mark.parametrize(
    ("answer", "acked", "on_air", "retry", "said"),
    [
        ("nak", False, True, True, "sent to !1a2b3c4d, not acknowledged (Received a NAK"),
        ("implicit", False, True, False, "relayed by another node, not acknowledged by !1a2b3c4d"),
        ("nodevice", False, False, True, "not sent: meshtastic ended with status 1: No Meshtastic"),
        ("plain", False, True, False, "meshtastic reported no acknowledgement from !1a2b3c4d"),
    ],
)
def test_what_the_program_says_decides_what_became_of_the_message(
    fake_cli: str, monkeypatch: pytest.MonkeyPatch, answer, acked, on_air, retry, said
) -> None:
    monkeypatch.setenv("FAKE_MESH_ANSWER", answer)
    sent = mesh.send("hello", POCKET, command=fake_cli)
    assert (sent.acked, sent.on_air, sent.retry) == (acked, on_air, retry)
    assert sent.error.startswith(said)


def test_no_acknowledgement_in_time_stops_the_program(fake_cli, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FAKE_MESH_ANSWER", "silent")
    t0 = time.monotonic()
    sent = mesh.send("hello", POCKET, command=fake_cli, timeout_s=0.6)
    assert time.monotonic() - t0 < 5
    assert sent == mesh.Sent(False, True, True, "sent to !1a2b3c4d, not acknowledged within 0.6 s: "
                                                "it may or may not have arrived")  # fmt: skip


def test_without_the_program_nothing_is_sent(tmp_path: Path) -> None:
    sent = mesh.send("hello", POCKET, command=str(tmp_path / "nowhere" / "meshtastic"))
    assert not sent.on_air and sent.retry and "isn't installed" in sent.error
    assert mesh.find("no-such-program-here") is None


def test_ids_and_connections() -> None:
    assert mesh.node_id(" !1A2B3C4D ") == "!1a2b3c4d"
    for bad in ("!ffffffff", "1a2b3c4d", "!1a2b3c4", "!1a2b3c4d5", "^all", ""):
        assert mesh.node_id(bad) is None, bad
    assert mesh.connection("") == []
    assert mesh.connection("/dev/cu.usbmodem-1") == ["--port", "/dev/cu.usbmodem-1"]
    assert mesh.connection("192.168.1.30") == ["--host", "192.168.1.30"]
    for bad in ("/dev/../x y", "-rf", "host name", "http://node"):
        assert mesh.device_error(bad), bad


# -- the message ----------------------------------------------------------------------------------


def test_the_message_puts_the_time_and_the_station_first_and_is_cut_on_a_word() -> None:
    at = ALERT["last_at"]
    hhmm = dt.datetime.fromtimestamp(at).strftime("%H:%M")
    words = "Corte de água amanhã entre as nove e as dezassete horas em toda a zona norte " * 4
    ev = alert_event({**ALERT, "checked": False}, STATIONS, NODE, text=words)
    line = as_mesh_line(ev)
    assert line.startswith(f"{hhmm} a [HIGH, not checked] water: Corte de água amanhã")
    assert len(line.encode()) <= 200 and line.endswith("…")
    assert words.startswith(line.split(": ", 1)[1].removesuffix("…") + " ")  # a whole word
    short = as_mesh_line(alert_event(ALERT, STATIONS, NODE, text="Corte de água."))
    assert short == f"{hhmm} a [HIGH] water: Corte de água."
    cjk = as_mesh_line(alert_event(ALERT, STATIONS, NODE, text="道路封闭请绕行" * 30), 60)
    assert len(cjk.encode()) <= 60 and cjk.endswith("…")  # no spaces: cut between characters


# -- the output ---------------------------------------------------------------------------------


def test_an_alert_reaches_the_node_and_is_acknowledged(fake_cli, tmp_path) -> None:
    store = Store(tmp_path / "w.db")
    outs = mesh_outputs({"command": fake_cli, "device": "/dev/cu.usbserial-01"}, store=store)
    try:
        outs.on_alert(ALERT, "high", "Corte de água amanhã.", {"en": "Water cut tomorrow."})
        wait_for(lambda: outs.status()["pocket"]["sent"] == 1)
        [run] = runs(tmp_path)
        assert (
            run["dest"] == POCKET
            and run["ack"]
            and run["sendtext"].endswith(" a [HIGH] water: Corte de água amanhã.")
        )  # the words as heard, never the translation
        [entry] = outs.recent("pocket")
        assert entry["ok"] and entry["attempts"] == 1 and entry["status"] is None
        wait_for(lambda: store.outbox() == [])
        status = outs.status()["pocket"]["mesh"]
        assert status["sent_24h"] == 1 and status["daily_cap"] == 20 and not status["cap_reached"]
        assert status["next_at"] is not None  # the floor: two minutes after the last one
    finally:
        outs.close()
        store.close()


def test_an_unacknowledged_alert_waits_in_the_outbox_and_goes_again(
    fake_cli, tmp_path, monkeypatch, quick
) -> None:
    monkeypatch.setenv("FAKE_MESH_ANSWER", "nak")
    store = Store(tmp_path / "w.db")
    outs = mesh_outputs({"command": fake_cli, "min_gap_s": 0.1}, store=store)
    try:
        outs.on_alert(ALERT, "high", "Corte de água.", {})
        wait_for(lambda: outs.status()["pocket"]["failed"] >= 1)
        first = outs.recent("pocket")[-1]
        assert not first["ok"] and first["attempts"] == 1
        assert first["error"].startswith("sent to !1a2b3c4d, not acknowledged")
        assert outs.status()["pocket"]["waiting"] and outs.status()["pocket"]["alerts_waiting"] == 1
        assert len(store.outbox()) == 1
        monkeypatch.setenv("FAKE_MESH_ANSWER", "ack")
        wait_for(lambda: outs.status()["pocket"]["sent"] == 1)
        wait_for(lambda: store.outbox() == [])
        assert outs.status()["pocket"]["waiting"] is False
        assert len({r["sendtext"] for r in runs(tmp_path)}) == 1  # the same message each time
    finally:
        outs.close()
        store.close()


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


def test_the_floor_and_the_daily_cap_hold_messages_back(caplog, monkeypatch) -> None:
    from wavescribe import outputs as module

    monkeypatch.setattr(module, "PACE_CHECK_S", 0.05)  # the test moves the clock
    clock, sent = Clock(), []

    def node(text: str, dest: str, **kw) -> mesh.Sent:
        sent.append((clock.t, text))
        return mesh.Sent(True, True, False, None)

    outs = mesh_outputs({"min_gap_s": 120, "daily_cap": 3, "alert_max_age_hours": 48},
                        clock=clock, mesh_send=node)  # fmt: skip
    try:
        for i in range(5):
            outs.on_alert({**ALERT, "id": i}, "high", f"aviso {i}", {})
        wait_for(lambda: len(sent) == 1)
        time.sleep(0.2)
        assert len(sent) == 1  # the floor: the next waits two minutes
        assert outs.status()["pocket"]["mesh"]["next_at"] == clock.t + 120
        for _ in range(2):
            clock.t += 121
            n = len(sent)
            wait_for(lambda n=n: len(sent) == n + 1)
        clock.t += 121
        time.sleep(0.3)
        assert len(sent) == 3  # the cap: three in 24 hours
        status = outs.status()["pocket"]
        assert status["mesh"]["cap_reached"] and status["mesh"]["sent_24h"] == 3
        assert status["mesh"]["next_at"] == sent[0][0] + 86400
        assert status["alerts_waiting"] == 2
        assert any("output_mesh_cap" in r.getMessage() for r in caplog.records)
        entry = outs.test(outs.configs()[0])  # the test button keeps to the cap too
        assert not entry["ok"] and entry["attempts"] == 0
        assert entry["error"].startswith("not sent: the daily cap of 3 messages")
        clock.t = sent[0][0] + 86400 + 1  # a day after the first: one more may go
        wait_for(lambda: len(sent) == 4)
    finally:
        outs.close()


def test_a_restart_counts_the_messages_of_the_last_24_hours(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    for _ in range(3):
        store.add_delivery("pocket", "alert", "1", True, None, None, 1, 900.0)
    store.add_delivery("pocket", "alert", "2", False, None, "not sent: …", 0, 3.0)  # not on air
    outs = mesh_outputs({"daily_cap": 3}, store=store, mesh_send=lambda *a, **k: None)
    try:
        status = outs.status()["pocket"]["mesh"]
        assert status["sent_24h"] == 3 and status["cap_reached"]
    finally:
        outs.close()
        store.close()


def test_stopping_while_it_waits_for_an_acknowledgement_keeps_the_alert(
    fake_cli, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_MESH_ANSWER", "silent")
    store = Store(tmp_path / "w.db")
    outs = mesh_outputs({"command": fake_cli}, store=store)
    outs.on_alert(ALERT, "high", "Corte de água.", {})
    wait_for(lambda: runs(tmp_path))
    t0 = time.monotonic()
    outs.close()
    assert time.monotonic() - t0 < 5
    assert [r["output_id"] for r in store.outbox()] == ["pocket"]  # for the next run
    store.close()


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"node": ""}, "node is the id of the one node"),
        ({"node": "!ffffffff"}, "never !ffffffff"),
        ({"node": "everyone"}, "node is the id"),
        ({"device": "some host"}, "serial port"),
        ({"events": ["alert", "utterance"]}, "alerts only"),
        ({"format": "json"}, "one-line form"),
        ({"max_bytes": 201}, "max_bytes is a whole number from 40 to 200"),
        ({"min_gap_s": 5}, "min_gap_s is 30 to 3600"),
        ({"daily_cap": 0}, "daily_cap is a whole number from 1 to 200"),
        ({"daily_cap": 2.5}, "daily_cap is a whole number"),
        ({"command": "bin/meshtastic"}, "a path relative to the home isn't taken"),
    ],
)
def test_a_mesh_output_that_cant_work_is_refused(change: dict, why: str) -> None:
    with pytest.raises(OutputError, match=why):
        OutputConfig.from_dict({"id": "pocket", "kind": "mesh", "node": POCKET, **change})


def test_a_mesh_output_has_stated_defaults_and_a_webhook_none_of_its_fields() -> None:
    config = OutputConfig.from_dict({"id": "pocket", "kind": "mesh", "node": "!1A2B3C4D"})
    assert (config.node, config.format, config.max_bytes, config.url) == (POCKET, "line", 200, "")
    assert (config.min_gap_s, config.daily_cap, config.timeout_s) == (120, 20, 120)
    assert config.to_dict()["command"] == "meshtastic"
    full = {"id": "pocket", "kind": "mesh", "node": POCKET, "command": "/opt/bin/meshtastic"}
    assert OutputConfig.from_dict(full).command == "/opt/bin/meshtastic"  # a full path is taken
    hook = OutputConfig.from_dict({"id": "h", "url": "http://h"}).to_dict()
    assert not {"node", "device", "command", "min_gap_s", "daily_cap"} & set(hook)


def test_the_page_saves_a_mesh_output_and_an_edit_keeps_what_it_has_no_field_for(
    tmp_path: Path,
) -> None:
    from tests.test_app import make_app

    hook = {"id": "pocket", "name": "pocket", "url": "http://hub.test/in", "timeout_s": 3}
    app = make_app(tmp_path, page={"output": [hook]})
    try:
        # A webhook made a mesh output: its 3 s timeout is a webhook's, not an acknowledgement's.
        app.save_output({"id": "pocket", "name": "pocket", "kind": "mesh", "node": POCKET})
        saved = app.settings.get("output")[0]
        assert saved["kind"] == "mesh" and saved["node"] == POCKET and "url" not in saved
        assert saved["timeout_s"] == 120 and saved["min_gap_s"] == 120 and saved["daily_cap"] == 20
        app.settings.update(lambda d: d["output"][0].update(command="/opt/mesh/bin/meshtastic"))
        app.save_output({"id": "pocket", "name": "pocket", "kind": "mesh", "node": POCKET,
                         "daily_cap": 10})  # fmt: skip
        saved = app.settings.get("output")[0]
        assert saved["command"] == "/opt/mesh/bin/meshtastic" and saved["daily_cap"] == 10
        view = app.outputs_view()["outputs"][0]
        assert view["status"]["mesh"]["daily_cap"] == 10 and view["node"] == POCKET
    finally:
        app.close()
        app.store.close()
