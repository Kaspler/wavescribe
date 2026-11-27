# SPDX-License-Identifier: Apache-2.0
"""Digests read aloud and on a schedule, heard through a fake speaker: no sound is made."""

import dataclasses
import os
import threading
import time
from pathlib import Path

import pytest

import tests.test_app
from tests.test_agent import Mcp, url_of
from tests.test_alerts import FakeActions
from tests.test_app import make_app, wait_for
from tests.test_digest import MIN, NOW, FakeLlm, digester, kept, stocked
from tests.test_digest_web import FROM_LAN, Serving, post
from wavescribe.alerts import Alert, Voice
from wavescribe.alerts.actions import ActionError, LogOnlyActions, MacActions
from wavescribe.cli import main
from wavescribe.config import ConfigError
from wavescribe.digest.schedule import Schedule
from wavescribe.digest.speech import SPOKEN_CHARS, Speaker, Speech, spoken

EN = Voice("Lesya", "An alert.")
VOICES = {"en": EN}
TWO_POINTS = "- Water is cut tomorrow [1].\n- Heavy rain this afternoon [2]."


class HeldActions(FakeActions):
    """Holds each text that starts with one of ``held`` until ``go`` is set, to see what the
    others do meanwhile."""

    def __init__(self, *held: str) -> None:
        super().__init__()
        self.held = held
        self.speaking = threading.Event()
        self.go = threading.Event()

    def speak(self, voice: str, text: str) -> None:
        if text.startswith(self.held):
            self.speaking.set()
            assert self.go.wait(5)
        super().speak(voice, text)


def in_background(work) -> threading.Thread:
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def said(actions: FakeActions) -> list[str]:
    return [c[2] for c in actions.calls if c[0] == "speak"]


def made(tmp_path: Path, reply: str = TWO_POINTS, minutes: float = 60, *alerts: Alert):
    store = stocked(tmp_path)
    water = Alert(
        "water",
        "high",
        ["a"],
        "pt-PT",
        "Corte de água amanhã",
        ["água"],
        NOW - 600,
        NOW - 600,
        status="delivered",
    )
    for alert in (water, *alerts):
        kept(store, alert)
    store.add_alert_summaries(water, {"en": "Water cut tomorrow"}, "fake-mt")
    d = digester(store, FakeLlm(reply))
    return store, d.run(d.request({"minutes": minutes, "language": "en"}))


OPENING = (
    "Wavescribe summary of a, b, 11:00 to 12:00. Written by the local model from 3 of 6 lines; "
    "it can miss things or get them wrong."
)


# -- what is said -------------------------------------------------------------------------------


def test_a_digest_says_what_it_is_names_earlier_alerts_and_reads_its_points(tmp_path) -> None:
    store, digest = made(tmp_path)
    assert spoken(digest, EN) == [
        OPENING,
        "Earlier alerts, 1 in all: 11:50, water.",  # never in the words it was announced in
        "a: Water is cut tomorrow.",
        "b: Heavy rain this afternoon.",
    ]
    assert spoken(digest, EN, seconds=1)[2:] == ["a: Water is cut tomorrow."]  # one at least
    store.close()


def test_a_station_alert_or_one_still_pending_is_not_named(tmp_path: Path) -> None:
    quiet = Alert(
        "silence",
        "medium",
        ["a"],
        "en",
        "No audio from a for 10 minutes.",
        [],
        NOW - 55 * MIN,
        NOW - 55 * MIN,
        status="delivered",
        kind="silence",
    )
    waiting = Alert("fire", "high", ["b"], "pt-PT", "Fogo", ["fogo"], NOW - 60, NOW - 60)
    logged = Alert(
        "said", "medium", ["a"], "pt-PT", "Disse", ["disse"], NOW - 120, NOW - 120, status="logged"
    )
    store, digest = made(tmp_path, TWO_POINTS, 60, quiet, waiting, logged)
    # Neither a station's alert, whose end the store doesn't keep, nor one being delivered,
    # nor one whose severity is only logged: none of them was announced.
    assert [a["rule"] for a in digest.alerts] == ["water"]
    assert spoken(digest, EN)[1] == "Earlier alerts, 1 in all: 11:50, water."
    store.close()


def test_every_utterance_is_a_sentence_or_two_however_long_the_point(tmp_path: Path) -> None:
    rambling = "The station reported that water will be cut tomorrow " + "and so on " * 300
    raining = rambling.replace("water will be cut", "HEAVY RAIN IS FORECAST").upper()
    store, digest = made(tmp_path, f"- {rambling}[1].\n- {raining}[2].")
    long_names = {**digest.names, "a": "A station with a very long name " * 5}
    lines = spoken(dataclasses.replace(digest, names=long_names), EN)
    assert len(lines) == 4 and all(len(line) <= SPOKEN_CHARS for line in lines)
    assert lines[0].endswith("it can miss things or get them wrong.")
    store.close()


def test_a_digest_without_points_is_shown_never_read(tmp_path: Path) -> None:
    store, digest = made(tmp_path, "NONE: Only music and adverts.", minutes=15)
    assert spoken(digest, EN) == []
    speaker = Speaker(Speech(FakeActions()), lambda: VOICES, threading.Event())
    assert speaker.say(digest) == (
        "there is nothing to read aloud: Nothing stood out in the 1 line read of 1 said."
    )
    store.close()


def test_another_language_opens_in_its_own_words(tmp_path: Path) -> None:
    store, digest = made(tmp_path)
    digest = dataclasses.replace(digest, request=dataclasses.replace(digest.request, language="uk"))
    voices = {"uk": Voice("Lesya", "Увага.")}
    speaker = Speaker(Speech(FakeActions()), lambda: voices, threading.Event())
    assert "give [speak.uk] in alerts.toml a summary and an earlier sentence" in speaker.say(digest)
    voices["uk"] = Voice(
        "Lesya", "Увага.", summary="Підсумок {station}.", earlier="Раніше: {alerts}."
    )
    assert speaker.say(digest) == (
        "[speak.uk] in alerts.toml: summary may use only {stations}, {start}, {end}, {read} and "
        "{said}, each in braces"
    )
    voices["uk"] = Voice(
        "Lesya", "Увага.", summary="Підсумок: {stations}.", earlier="Раніше: {alerts}."
    )
    assert spoken(digest, voices["uk"])[:2] == ["Підсумок: a, b.", "Раніше: 11:50, water."]
    store.close()


# -- taking turns with alerts -------------------------------------------------------------------


class LongSay(FakeActions):
    """Each sentence of a digest goes on until it is stopped, as a long one would."""

    def __init__(self) -> None:
        super().__init__()
        self.speaking = threading.Event()

    def speak_until(self, voice: str, text: str, stopped) -> bool:
        self.speaking.set()
        deadline = time.monotonic() + 5
        while not stopped():
            assert time.monotonic() < deadline, "a digest's sentence was never stopped"
            time.sleep(0.01)
        self.calls.append(("cut", voice, text))
        return False


def test_an_alert_stops_a_digest_being_read_at_once() -> None:
    actions = LongSay()
    speech = Speech(actions)
    assert speech.claim()
    count: list[int] = []
    reader = in_background(
        lambda: count.append(speech.read("Lesya", ["first", "second"], threading.Event()))
    )
    assert actions.speaking.wait(5)
    t0 = time.monotonic()
    speech.speak("Lesya", "Evacuate the riverside")
    assert time.monotonic() - t0 < 1  # not the length of the sentence being read
    reader.join(5)
    # The alert is the news then: the rest of the digest isn't read after it.
    assert actions.calls == [
        ("cut", "Lesya", "first"),
        ("speak", "Lesya", "Evacuate the riverside"),
    ]
    assert count == [0] and speech.claim()  # the turn was given back


def test_a_reminder_or_any_other_notification_leaves_a_digest_being_read() -> None:
    actions = LongSay()
    speech = Speech(actions)
    assert speech.claim()
    reader = in_background(lambda: speech.read("Lesya", ["first"], threading.Event()))
    assert actions.speaking.wait(5)
    # Only an announcement is spoken; a reminder (still being mentioned) is only shown.
    speech.notify("REMINDER · water · a", "Still being mentioned: 12 times since 10:05")
    time.sleep(0.2)
    assert reader.is_alive() and not any(c[0] == "cut" for c in actions.calls)
    speech.close()
    reader.join(5)


def test_a_digest_starts_once_an_alert_being_said_has_ended() -> None:
    actions = HeldActions("Evacuate")
    speech = Speech(actions)
    alert = in_background(lambda: speech.speak("Lesya", "Evacuate the riverside"))
    assert actions.speaking.wait(5)
    assert speech.claim()
    count: list[int] = []
    reader = in_background(
        lambda: count.append(speech.read("Lesya", ["first", "second"], threading.Event()))
    )
    wait_for(lambda: not reader.is_alive())
    assert count == [0]  # it came while an alert was being said: that alert is the news
    actions.go.set()
    alert.join(5)
    assert speech.claim()
    assert speech.read("Lesya", ["first", "second"], threading.Event()) == 2
    assert said(actions) == ["Evacuate the riverside", "first", "second"]


def test_closing_stops_a_digest_mid_sentence() -> None:
    actions = LongSay()
    speech = Speech(actions)
    assert speech.claim()
    reader = in_background(lambda: speech.read("Lesya", ["first"], threading.Event()))
    assert actions.speaking.wait(5)
    t0 = time.monotonic()
    speech.close()
    assert time.monotonic() - t0 < 1 and not reader.is_alive()
    assert actions.calls == [("cut", "Lesya", "first")]
    assert speech.claim() and speech.read("Lesya", ["again"], threading.Event()) == 0


def fake_say(tmp_path: Path, monkeypatch, script: str) -> Path:
    """A `say` that makes no sound, first on the PATH; it writes its process id to a file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    say = bin_dir / "say"
    say.write_text(f"#!/bin/sh\necho $$ > {tmp_path / 'pid'}\ncat > /dev/null\n{script}\n")
    say.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path / "pid"


def test_say_is_stopped_at_once_and_doesnt_outlive_its_turn(tmp_path: Path, monkeypatch) -> None:
    pid_file = fake_say(tmp_path, monkeypatch, "sleep 30")
    stop = threading.Event()
    stopped_at: list[float] = []

    def stop_once_saying() -> None:  # a loaded machine may take a while to start it
        deadline = time.monotonic() + 30
        while not (pid_file.exists() and pid_file.read_text().strip()):
            if time.monotonic() > deadline:
                break
            time.sleep(0.01)
        stopped_at.append(time.monotonic())
        stop.set()

    threading.Thread(target=stop_once_saying, daemon=True).start()
    assert MacActions().speak_until("Daniel", "A long sentence.", stop.is_set) is False
    assert time.monotonic() - stopped_at[0] < 2
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_say_that_ends_or_fails_says_so(tmp_path: Path, monkeypatch) -> None:
    fake_say(tmp_path, monkeypatch, "echo 'Voice `Nobody` not found.' >&2; exit 1")
    with pytest.raises(ActionError, match="Voice `Nobody` not found"):
        MacActions().speak_until("Nobody", "Hello.", lambda: False)
    (tmp_path / "bin" / "say").write_text("#!/bin/sh\ncat > /dev/null\n")
    assert MacActions().speak_until("Daniel", "Hello.", lambda: False) is True


def test_the_speaker_reads_in_the_alerts_voice_or_says_why_not(tmp_path: Path) -> None:
    store, digest = made(tmp_path)
    actions = FakeActions()
    voices = dict(VOICES)
    speaker = Speaker(Speech(actions), lambda: voices, threading.Event())

    assert speaker.say(digest) is None
    assert said(actions) == spoken(digest, EN) and {c[1] for c in actions.calls} == {"Lesya"}

    assert speaker.speech.claim()  # another digest is being read
    assert speaker.start(digest) == {
        "started": False,
        "error": "another digest is being read aloud; try again when it ends",
    }
    speaker.speech.read("Lesya", [], threading.Event())

    voices["en"] = Voice("Daniel", "An alert.")  # the page changed the alerts' voice
    assert "'Daniel' is not installed" in (speaker.say(digest) or "")
    voices.clear()
    assert speaker.say(digest) == (
        "no voice speaks en: give it one in alerts.toml, [speak.en], as for spoken alerts"
    )
    no_say = Speaker(Speech(LogOnlyActions()), lambda: VOICES, threading.Event())
    assert no_say.start(digest) == {
        "started": False,
        "error": "this machine has no speech to read it with (macOS `say`)",
    }
    store.close()


def test_a_digest_with_nothing_to_say_is_not_read(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    d = digester(store, FakeLlm("unused"))
    digest = d.run(d.request({"stations": ["b"], "kind": "now", "language": "en"}))
    speaker = Speaker(Speech(FakeActions()), lambda: VOICES, threading.Event())
    assert speaker.start(digest) == {
        "started": False,
        "error": "there is nothing to read aloud: Nothing was said on b in the last 5 minutes.",
    }
    store.close()


# -- asking for it to be read aloud -------------------------------------------------------------


def speaking(store, llm=None) -> tuple[Serving, FakeActions]:
    web, actions = Serving(store, llm or FakeLlm(TWO_POINTS)), FakeActions()
    web.hub.speaker = Speaker(Speech(actions), lambda: VOICES, web.hub._closed)
    return web, actions


def test_this_machine_can_have_a_digest_read_aloud(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    web, actions = speaking(store)
    try:
        status, out = post(web.server, {"language": "en", "speak": True})
        assert status == 200 and out["speech"] == {"started": True}
        wait_for(lambda: len(actions.calls) == 3)
        assert said(actions)[0].startswith("Wavescribe summary of a, b, ")
        assert said(actions)[1:] == ["a: Water is cut tomorrow.", "b: Heavy rain this afternoon."]

        status, out = post(web.server, {"language": "en", "speak": True}, FROM_LAN)
        assert (status, out["error"]) == (
            403,
            "only the Wavescribe machine itself can have a digest read aloud",
        )
        assert post(web.server, {"speak": "yes"}) == (400, {"error": "speak is true or false"})
        assert len(web.llm.prompts) == 1  # refused before the model was asked

        web.hub.speaker = None
        status, out = post(web.server, {"language": "en", "speak": True})
        assert out["speech"] == {
            "started": False,
            "error": "reading aloud isn't set up in this run",
        }
        status, out = post(web.server, {"language": "en"})
        assert "speech" not in out
    finally:
        web.close()
        store.close()


def test_the_command_reads_aloud_or_says_why_not(tmp_path: Path, capsys) -> None:
    store = stocked(tmp_path)
    web, actions = speaking(store)
    url = url_of(web.server)
    try:
        assert main(["digest", "--url", url, "--language", "en", "--speak"]) == 0
        assert "- a: Water is cut tomorrow. [1]" in capsys.readouterr().out
        wait_for(lambda: len(actions.calls) == 3)

        assert main(["digest", "--url", url, "--language", "uk", "--speak"]) == 1
        captured = capsys.readouterr()
        assert "- a: Water is cut tomorrow. [1]" in captured.out  # the digest is still shown
        assert captured.err == (
            "wavescribe: not read aloud: no voice speaks uk: give it one in alerts.toml, "
            "[speak.uk], as for spoken alerts\n"
        )
    finally:
        web.close()
        store.close()


def test_an_agent_may_read_a_digest_aloud_only_with_allow_control(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    web, actions = speaking(store)
    try:
        mcp = Mcp(url_of(web.server), cwd=tmp_path)
        tools = {t["name"]: t for t in mcp.request("tools/list")["result"]["tools"]}
        assert "speak" not in tools["digest"]["inputSchema"]["properties"]
        speak = {"name": "now", "arguments": {"station": "b", "speak": True}}
        error = mcp.request("tools/call", speak)["error"]
        assert error == {  # what to do about it, rather than that there is no such argument
            "code": -32602,
            "message": "Invalid params: speak needs control, which this server was started "
            "without (wavescribe mcp --allow-control)",
        }
        mcp.close()

        mcp = Mcp(url_of(web.server), "--allow-control", cwd=tmp_path)
        tools = {t["name"]: t for t in mcp.request("tools/list")["result"]["tools"]}
        for name in ("digest", "now"):
            assert tools[name]["inputSchema"]["properties"]["speak"]["type"] == "boolean"
            assert tools[name]["annotations"] == {"readOnlyHint": False, "destructiveHint": False}
        failed, out = mcp.call("digest", language="en", speak=True)
        assert not failed and out["speech"] == {"started": True}
        wait_for(lambda: len(actions.calls) == 3)
        failed, out = mcp.call("digest", language="en")
        assert not failed and "speech" not in out
        mcp.close()
    finally:
        web.close()
        store.close()


# -- in the app ---------------------------------------------------------------------------------


def test_the_app_schedules_digests_from_alerts_toml(tmp_path: Path, monkeypatch) -> None:
    alerts = tests.test_app.ALERTS + '\n[digest]\nevery_minutes = 30\nstations = ["a"]\n'
    monkeypatch.setattr(tests.test_app, "ALERTS", alerts)
    app = make_app(tmp_path)
    try:
        assert app.digests is not None
        assert app.digests.schedule == Schedule(30, "en", ("a",))  # notify_language is en
        assert app.alerter is not None and app.alerter.actions is app.speech

        app.alerts_path.write_text(alerts + "speak = true\n")  # in [digest]
        with pytest.raises(ConfigError, match=r"\[digest\]: speak = true needs a \[speak.en\]"):
            app._digest_schedule()  # as at the next start, which it stops with that message
    finally:
        app.pipeline.stop()
        app.close()
        app.store.close()
