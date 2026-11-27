# SPDX-License-Identifier: Apache-2.0
"""Digests read aloud and on a schedule, heard through a fake speaker: no sound is made."""

import threading
from pathlib import Path

import pytest

import tests.test_app
from tests.test_agent import Mcp, url_of
from tests.test_alerts import FakeActions
from tests.test_app import make_app, wait_for
from tests.test_digest import NOW, FakeLlm, digester, stocked
from tests.test_digest_web import FROM_LAN, Serving, post
from wavescribe.alerts import Alert, Voice
from wavescribe.cli import main
from wavescribe.config import ConfigError
from wavescribe.digest.schedule import Schedule
from wavescribe.digest.speech import Speaker, Speech, spoken

VOICES = {"en": Voice("Lesya", "An alert.")}
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


def made(tmp_path: Path, reply: str = TWO_POINTS, minutes: float = 60):
    store = stocked(tmp_path)
    alert = Alert(
        "water", "high", ["a"], "pt-PT", "Corte de água amanhã", ["água"], NOW - 600, NOW - 600
    )
    store.add_alert(alert)
    store.add_alert_summaries(alert, {"en": "Water cut tomorrow"}, "fake-mt")
    d = digester(store, FakeLlm(reply))
    return store, d.run(d.request({"minutes": minutes, "language": "en"}))


# -- what is said -------------------------------------------------------------------------------


def test_a_digest_is_read_as_its_alerts_then_its_points_with_stations(tmp_path: Path) -> None:
    store, digest = made(tmp_path)
    assert spoken(digest) == [
        "Water cut tomorrow",
        "a: Water is cut tomorrow.",
        "b: Heavy rain this afternoon.",
    ]
    assert spoken(digest, seconds=1) == ["Water cut tomorrow"]  # at least one, however short
    store.close()


def test_nothing_of_substance_is_read_as_the_models_sentence(tmp_path: Path) -> None:
    store, digest = made(tmp_path, "NONE: Only music and adverts.", minutes=15)
    assert spoken(digest) == ["Nothing stood out in the 1 line read of 1 said."]
    store.close()


# -- taking turns with alerts -------------------------------------------------------------------


def test_an_alert_waits_only_for_the_sentence_being_said() -> None:
    actions = HeldActions("first")
    speech = Speech(actions)
    assert speech.claim()
    count: list[int] = []
    reader = in_background(
        lambda: count.append(speech.read("Lesya", ["first", "second", "third"], threading.Event()))
    )
    assert actions.speaking.wait(5)
    alert = in_background(lambda: speech.speak("Lesya", "Evacuate the riverside"))
    wait_for(lambda: speech._alerts == 1)  # the alert is waiting for the voice
    actions.go.set()
    reader.join(5)
    alert.join(5)
    # The alert is the news then: the rest of the digest isn't read after it.
    assert said(actions) == ["first", "Evacuate the riverside"] and count == [1]
    assert speech.claim()  # the turn was given back


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
    actions.go.set()
    alert.join(5)
    reader.join(5)
    assert said(actions) == ["Evacuate the riverside", "first", "second"] and count == [2]


def test_closing_stops_a_digest_between_sentences() -> None:
    actions, closed = FakeActions(), threading.Event()
    speech = Speech(actions)
    closed.set()
    assert speech.claim()
    assert speech.read("Lesya", ["first"], closed) == 0 and actions.calls == []


def test_the_speaker_reads_in_the_alerts_voice_or_says_why_not(tmp_path: Path) -> None:
    store, digest = made(tmp_path)
    actions = FakeActions()
    voices = dict(VOICES)
    speaker = Speaker(Speech(actions), lambda: voices, threading.Event())

    assert speaker.say(digest) is None
    assert said(actions) == spoken(digest) and {c[1] for c in actions.calls} == {"Lesya"}

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
        wait_for(lambda: len(actions.calls) == 2)
        assert said(actions) == ["a: Water is cut tomorrow.", "b: Heavy rain this afternoon."]

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
        wait_for(lambda: len(actions.calls) == 2)

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
        assert mcp.request("tools/call", speak)["error"]["code"] == -32602
        mcp.close()

        mcp = Mcp(url_of(web.server), "--allow-control", cwd=tmp_path)
        tools = {t["name"]: t for t in mcp.request("tools/list")["result"]["tools"]}
        for name in ("digest", "now"):
            assert tools[name]["inputSchema"]["properties"]["speak"]["type"] == "boolean"
            assert tools[name]["annotations"] == {"readOnlyHint": False, "destructiveHint": False}
        failed, out = mcp.call("digest", language="en", speak=True)
        assert not failed and out["speech"] == {"started": True}
        wait_for(lambda: len(actions.calls) == 2)
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
