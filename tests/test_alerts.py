# SPDX-License-Identifier: Apache-2.0
import dataclasses
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.test_rules import RULES, pack
from tests.test_store import result
from wavescribe.alerts import Alerter, parse_alerts
from wavescribe.alerts.actions import ActionError
from wavescribe.alerts.cap import NS, to_cap
from wavescribe.config import ConfigError
from wavescribe.store import Store

CONFIG = {
    **RULES,
    "delivery": {"notify_language": "en", "translation_wait_s": 2, "cooldown_minutes": 15},
    "speak": {
        "uk": {"voice": "Lesya", "intro": "Увага. Радіо повідомляє:", "fallback": "Увага. Радіо."},
    },
    "actions": {
        "critical": ["notify", "speak:uk", "log"],
        "high": ["notify", "log"],
        "medium": ["notify", "log"],
        "low": ["log"],
    },
}
UK = "Завтра відключать воду в центрі міста через ремонт труб."
EN = "Water will be cut tomorrow in the city centre because of pipe repairs."


class FakeActions:
    def __init__(self, fail: set[str] = frozenset()) -> None:
        self.calls: list[tuple] = []
        self.sounds: list[str | None] = []
        self.fail = fail

    def notify(self, title: str, body: str, *, sound: str | None = None) -> None:
        if "notify" in self.fail:
            raise ActionError("notifications are off")
        self.calls.append(("show" if sound == "" else "notify", title, body))  # "": no sound
        self.sounds.append(sound)

    def speak(self, voice: str, text: str) -> None:
        if "speak" in self.fail:
            raise ActionError("no audio device")
        self.calls.append(("speak", voice, text))

    def speak_until(self, voice: str, text: str, stopped) -> bool:
        if stopped():
            return False
        self.speak(voice, text)
        return True

    def voices(self) -> set[str]:
        return {"Lesya"}


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def make(tmp_path, translations=None, *, fail=frozenset(), store=None, wait=2.0, **delivery):
    raw = {**CONFIG, "delivery": {**CONFIG["delivery"], "translation_wait_s": wait, **delivery}}
    config = parse_alerts(raw, None, pack(tmp_path))
    actions, clock, seen = FakeActions(fail), Clock(), []
    alerter = Alerter(config, store=store, actions=actions, clock=clock, on_alert=seen.append,
                      station_names={"a": "Rádio A"})  # fmt: skip
    requests = []

    def translate(text, source, targets, callback):
        requests.append((text, source, list(targets)))
        if translations is not None:
            threading.Thread(
                target=callback,
                args=({t: translations[t] for t in targets if t in translations}, "fake-mt"),
            ).start()

    alerter.attach(translate)
    return alerter, actions, clock, requests, seen


def settle(alerter: Alerter, n: int = 1, timeout: float = 5) -> list:
    """Wait until n alerts have left 'pending'."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        done = [a for a in alerter._open.values() if a.status != "pending"]
        if len(done) >= n:
            time.sleep(0.05)
            return done
        time.sleep(0.01)
    raise AssertionError("alerts never delivered")


def test_critical_alert_is_notified_in_english_and_spoken_in_ukrainian(tmp_path: Path) -> None:
    alerter, actions, _, requests, seen = make(tmp_path, {"en": EN, "uk": UK})
    r = result("a", "Corte de água em Vila Nova amanhã.", 1000.0)
    alerter.on_transcript(r)
    [alert] = settle(alerter)
    assert [h.rule_id for h in r.hits] == ["water"]  # the page highlights these
    assert requests == [("Corte de água em Vila Nova amanhã.", "pt-PT", ["en", "uk"])]
    assert actions.calls == [
        ("notify", "CRITICAL · water · Rádio A", EN),
        ("speak", "Lesya", f"Увага. Радіо повідомляє: {UK}"),
    ]
    assert alert.status == "delivered" and alert.delivered_severity == "critical"
    assert alert.actions == {"notify": "ok", "speak:uk": "ok", "log": "ok"}
    assert seen[-1]["status"] == "delivered" and seen[-1]["summaries"]["uk"] == UK


def test_words_heard_in_the_voices_language_are_spoken_after_its_intro(tmp_path: Path) -> None:
    config = {
        **CONFIG,
        "speak": {
            "pt": {"voice": "Lesya", "intro": "Atenção. A rádio diz:", "fallback": "Atenção."}
        },
        "actions": {**CONFIG["actions"], "critical": ["speak:pt"]},
    }
    alerter = Alerter(
        parse_alerts(config, None, pack(tmp_path)), actions=(actions := FakeActions())
    )
    alerter.attach(lambda text, source, targets, callback: callback({}, "fake-mt"))
    alerter.on_transcript(result("a", "Corte de água em Vila Nova amanhã.", 1000.0))
    settle(alerter)
    # Untranslated, as they are already in its language, but never as if the radio spoke.
    assert actions.calls == [
        ("speak", "Lesya", "Atenção. A rádio diz: Corte de água em Vila Nova amanhã.")
    ]


def test_translation_in_the_wrong_language_is_never_spoken(tmp_path: Path) -> None:
    # A translator answering in English when asked for Ukrainian (it happened in testing).
    alerter, actions, *_ = make(tmp_path, {"en": EN, "uk": EN})
    alerter.on_transcript(result("a", "Corte de água em Vila Nova amanhã.", 1000.0))
    [alert] = settle(alerter)
    assert ("speak", "Lesya", "Увага. Радіо.") in actions.calls  # the fixed fallback phrase
    assert alert.summaries == {"en": EN}


def test_delivery_does_not_wait_forever_for_a_translation(tmp_path: Path) -> None:
    alerter, actions, *_ = make(tmp_path, translations=None, wait=0.1)  # never answers
    t0 = time.time()
    alerter.on_transcript(result("a", "Corte de água em Vila Nova amanhã.", 1000.0))
    settle(alerter)
    assert time.time() - t0 < 2
    assert actions.calls[0] == ("notify", "CRITICAL · water · Rádio A",
                                "Corte de água em Vila Nova amanhã.")  # fmt: skip
    assert actions.calls[1] == ("speak", "Lesya", "Увага. Радіо.")


def test_repeats_inside_the_cooldown_are_folded_in_not_announced(tmp_path: Path) -> None:
    alerter, actions, clock, *_ = make(tmp_path, {"en": EN})
    alerter.on_transcript(result("a", "Corte de água em Lisboa.", 1000.0))
    settle(alerter)
    clock.now += 600
    alerter.on_transcript(result("b", "Sem água em Lisboa desde manhã.", 1600.0))
    time.sleep(0.2)
    notes = [c for c in actions.calls if c[0] == "notify"]
    assert len(notes) == 1
    [alert] = alerter._open.values()
    assert alert.mentions == 2 and alert.station_ids == ["a", "b"]
    assert alert.terms == ["corte de água", "sem água"]


def test_the_cooldown_ends_and_a_new_alert_is_announced(tmp_path: Path) -> None:
    # The negative path: a guard never seen to decline isn't a guard, and vice versa.
    alerter, actions, clock, *_ = make(tmp_path, {"en": EN})
    alerter.on_transcript(result("a", "Corte de água em Lisboa.", 1000.0))
    settle(alerter)
    first = alerter._open["water"]
    clock.now += 15 * 60 + 1
    alerter.on_transcript(result("a", "Corte de água em Lisboa.", 2000.0))
    settle(alerter)
    assert alerter._open["water"] is not first
    assert len([c for c in actions.calls if c[0] == "notify"]) == 2


def test_an_alert_that_gets_more_serious_is_announced_again(tmp_path: Path) -> None:
    alerter, actions, clock, *_ = make(tmp_path, {"en": EN, "uk": UK})
    alerter.on_transcript(result("a", "Corte de água em Lisboa.", 1000.0))
    settle(alerter)
    clock.now += 60
    alerter.on_transcript(result("a", "Corte de água também em Vila Nova.", 1060.0))
    deadline = time.time() + 5
    while alerter._open["water"].delivered_severity != "critical" and time.time() < deadline:
        time.sleep(0.01)
    assert [c[0] for c in actions.calls] == ["notify", "notify", "speak"]
    assert alerter._open["water"].mentions == 2


def held(tmp_path: Path, n: int = 3) -> tuple[Alerter, FakeActions, Clock]:
    rule = {"id": "water", "topics": ["water"], "near": ["local"], "severity": "high",
            "min_mentions": n}  # fmt: skip
    config = parse_alerts({**CONFIG, "rule": [rule]}, None, pack(tmp_path))
    actions, clock = FakeActions(), Clock()
    return Alerter(config, actions=actions, clock=clock), actions, clock


def test_min_mentions_holds_an_alert_at_low_until_it_is_repeated(tmp_path: Path) -> None:
    alerter, actions, clock = held(tmp_path, n=3)
    for i, station in enumerate("aab"):
        alerter.on_transcript(result(station, "Corte de água em Lisboa.", 1000.0 + 60 * i))
        alerter.drain()
        if i < 2:
            assert actions.calls == [] and alerter._open["water"].severity == "low"
        clock.now += 60
    alerter.drain()
    assert [c[0] for c in actions.calls] == ["notify"]
    [alert] = alerter._open.values()
    assert (alert.severity, alert.mentions, alert.delivered_severity) == ("high", 3, "high")


def test_a_local_place_confirms_at_the_first_mention(tmp_path: Path) -> None:
    alerter, actions, _ = held(tmp_path, n=3)
    alerter.on_transcript(result("a", "Corte de água em Vila Nova.", 1000.0))
    alerter.drain()
    assert alerter._open["water"].severity == "critical"  # raised by the place, not held
    assert [c[0] for c in actions.calls] == ["notify", "speak"]


def test_mentions_heard_as_another_language_never_confirm(tmp_path: Path) -> None:
    alerter, actions, clock = held(tmp_path, n=2)
    for i in range(3):
        r = result("a", "Corte de água em Lisboa.", 1000.0 + 60 * i)
        r.flags = ("lang:es",)
        alerter.on_transcript(r)
        alerter.drain()
        clock.now += 60
    assert actions.calls == [] and alerter._open["water"].severity == "low"


def test_failed_actions_are_recorded_and_never_marked_delivered(tmp_path: Path) -> None:
    alerter, _, *_ = make(tmp_path, {"en": EN}, fail={"notify"})
    alerter.on_transcript(result("a", "Corte de água em Lisboa.", 1000.0))
    [alert] = settle(alerter)
    assert alert.status == "failed" and alert.delivered_at is None
    assert alert.actions["notify"].startswith("failed: notifications are off")
    assert alert.actions["log"] == "ok"


def test_low_alerts_are_only_logged(tmp_path: Path) -> None:
    alerter, actions, *_ = make(tmp_path, {"en": EN})
    heard_as_english = dataclasses.replace(result("a", "Nuclear sabotagem.", 1000.0),
                                           flags=("lang:en",))  # fmt: skip
    alerter.on_transcript(heard_as_english)
    [alert] = settle(alerter)
    assert alert.severity == "low" and alert.status == "logged" and actions.calls == []


ES = "Mañana cortarán el agua en el centro de la ciudad por unas obras en las tuberías."


def wanting(r, *languages: str):
    """The utterance's station with ``alert_languages``."""
    station = dataclasses.replace(r.station, alert_languages=languages)
    return dataclasses.replace(r, station=station)


def wait_for(cond, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline, "timed out"
        time.sleep(0.01)


def test_alerts_are_translated_into_their_stations_alert_languages(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    alerter, actions, _, requests, _ = make(tmp_path, {"en": EN, "es": ES, "uk": UK}, store=store)
    quote = "Corte de água em Lisboa."
    r = wanting(result("a", quote, 1000.0), "es", "en", "pt")
    store.add_transcript(r)
    alerter.on_transcript(r)  # high: a notification in English
    [alert] = settle(alerter)
    wait_for(lambda: "es" in alert.summaries)
    # The delivery's own translation is asked for first and the others aren't waited for; the
    # station's own language and one already asked for aren't asked again.
    assert requests == [(quote, "pt-PT", ["en"]), (quote, "pt-PT", ["es"])]
    assert actions.calls == [("notify", "HIGH · water · Rádio A", EN)]
    wait_for(lambda: store.alerts()[0]["summaries"] == {"en": EN, "es": ES})


def test_low_alerts_wait_for_their_alert_languages_until_they_rise(tmp_path: Path) -> None:
    alerter, actions, clock, requests, _ = make(tmp_path, {"en": EN, "es": ES, "uk": UK})
    quote = "Sabotagem e corte de água em Lisboa."
    alerter.on_transcript(wanting(dataclasses.replace(result("a", quote, 1000.0),
                                                      flags=("lang:en",)), "es"))  # fmt: skip
    settle(alerter, 2)
    assert requests == [] and actions.calls == []  # low: only logged, nothing translated
    clock.now += 60
    alerter.on_transcript(wanting(result("b", "Sem água em Vila Nova.", 1060.0), "es"))
    wait_for(lambda: "es" in alerter._open["water"].summaries)
    assert (quote, "pt-PT", ["es"]) in requests  # the quote, now that the alert matters


def test_a_repeated_call_with_a_message_is_announced(tmp_path: Path) -> None:
    from wavescribe.asr.checks import check_transcript

    text = "Atenção, atenção, atenção, atenção. Corte de água em Vila Nova."
    check = check_transcript(text, 6, "pt-PT")
    assert check.drop is None
    alerter, actions, *_ = make(tmp_path, {"en": EN, "uk": UK})
    alerter.on_transcript(dataclasses.replace(result("a", text, 1000.0), flags=check.flags))
    [alert] = settle(alerter)
    assert alert.severity == "critical" and [c[0] for c in actions.calls] == ["notify", "speak"]


def test_a_dropped_loop_of_a_rule_term_is_kept_low_and_never_announced(tmp_path: Path) -> None:
    alerter, actions, clock, *_ = make(tmp_path, {"en": EN, "uk": UK})
    loop = result("a", "Sabotagem, sabotagem, sabotagem, sabotagem, sabotagem.", 1000.0)
    alerter.on_filtered(loop, "repetition")
    [alert] = settle(alerter)
    assert (alert.rule_id, alert.severity, alert.status) == ("conflict", "low", "logged")
    assert alert.quote == loop.transcript.text and actions.calls == []
    assert [h.capped for h in loop.hits] == ["repetition"]

    alerter.on_filtered(result("a", "corte de água " * 40, 1010.0), "too_long")
    assert "water" not in alerter._open  # more text than the audio holds isn't read at all

    clock.now += 60  # the same thing said in a sentence is announced as usual
    alerter.on_transcript(result("a", "Sabotagem na central.", 1060.0))
    alerter.drain()
    assert alert.severity == "medium" and [c[0] for c in actions.calls] == ["notify"]


def test_store_keeps_alerts_mentions_and_summaries(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    alerter, *_ = make(tmp_path, {"en": EN, "uk": UK}, store=store)
    r = result("a", "Corte de água em Vila Nova amanhã.", 1000.0)
    store.add_transcript(r)
    alerter.on_transcript(r)
    settle(alerter)
    [a] = store.alerts()
    assert (a["rule"], a["severity"], a["status"], a["segment_id"]) == (
        "water", "critical", "delivered", r.segment_id)  # fmt: skip
    assert a["summaries"] == {"en": EN, "uk": UK} and a["summaries_model"] == "fake-mt"
    assert a["actions"]["speak:uk"] == "ok"
    [u] = store.recent("a")
    [hit] = u["hits"]
    assert hit["rule"] == "water" and hit["alert"] == a["id"]
    assert [u["text"][s:e] for s, e in hit["spans"]] == ["Corte de água", "Vila Nova"]

    store.prune_text(max_age_s=0)  # the transcript goes; the alert and its quote stay
    [kept] = store.alerts()
    assert kept["quote"] == "Corte de água em Vila Nova amanhã." and kept["segment_id"] is None


def test_cap_document_with_a_time_no_calendar_holds() -> None:
    alert = {
        "id": 8, "rule": "water", "severity": "low", "first_at": float("nan"), "last_at": 1e300,
        "stations": ["a"], "language": "pt-PT", "quote": "Corte de água.", "terms": [],
        "mentions": 1,
    }  # fmt: skip
    root = ET.fromstring(to_cap(alert))
    assert root.find(f"{{{NS}}}sent").text == "1970-01-01T00:00:00+00:00"


def test_cap_document(tmp_path: Path) -> None:
    alert = {
        "id": 7, "rule": "water", "severity": "critical", "first_at": 1_790_000_000.4,
        "last_at": 1_790_000_060.0, "stations": ["a"], "language": "pt-PT",
        "quote": "Corte de água em Vila Nova.", "terms": ["corte de água"], "mentions": 2,
        "summaries": {"en": EN}, "summaries_model": "fake-mt",
    }  # fmt: skip
    root = ET.fromstring(to_cap(alert, station_names={"a": "Rádio A"}))
    q = lambda el, tag: el.find(f"{{{NS}}}{tag}").text  # noqa: E731
    assert root.tag == f"{{{NS}}}alert" and q(root, "status") == "Actual"
    assert q(root, "sent").endswith("+00:00")
    src, en = root.findall(f"{{{NS}}}info")
    assert (q(src, "language"), q(src, "description")) == ("pt-PT", alert["quote"])
    assert (q(src, "severity"), q(src, "certainty"), q(src, "urgency")) == (
        "Extreme", "Unknown", "Unknown")  # fmt: skip
    assert q(en, "language") == "en" and q(en, "description") == EN
    params = {q(p, "valueName"): q(p, "value") for p in en.findall(f"{{{NS}}}parameter")}
    assert params["machine_translation"] == "fake-mt"
    assert "machine_translation" not in {
        q(p, "valueName") for p in src.findall(f"{{{NS}}}parameter")
    }
    assert q(root, "msgType") == "Alert" and q(root, "identifier") == "wavescribe-7-1790000000"
    assert q(root, "incidents") == "wavescribe-7-1790000000"
    # Sent again as more serious or still mentioned, it is an update in the same incident, with
    # an identifier of its own: CAP consumers drop a message whose identifier they have seen.
    again = ET.fromstring(to_cap({**alert, "change": "reminder", "last_at": 1_790_003_600.0}))
    assert q(again, "msgType") == "Update"
    assert q(again, "identifier") == "wavescribe-7-1790000000-reminder-1790003600"
    assert q(again, "incidents") == "wavescribe-7-1790000000"


def test_one_sentence_tripping_several_rules_is_announced_once(tmp_path: Path) -> None:
    alerter, actions, *_ = make(tmp_path, {"en": EN, "uk": UK})
    alerter.on_transcript(result("a", "Sabotagem: corte de água em Vila Nova.", 1000.0))
    settle(alerter, 2)
    water, conflict = alerter._open["water"], alerter._open["conflict"]
    assert [c[0] for c in actions.calls] == ["notify", "speak"]
    assert actions.calls[0][1] == "CRITICAL · water + conflict · Rádio A"
    assert water.status == "delivered"
    assert conflict.status == "covered"
    assert conflict.actions == {"covered_by": "water"}  # no store, so no alert ids


def test_escalation_during_a_delivery_is_still_announced_once(tmp_path: Path) -> None:
    # A low alert is still being delivered when a sentence makes it serious and trips a
    # critical rule too: one announcement for that sentence, not two.
    alerter, actions, clock, *_ = make(tmp_path, {"en": EN, "uk": UK})
    gate = threading.Event()
    deliver = alerter._deliver
    alerter._deliver = lambda *a: (gate.wait(5), deliver(*a))
    alerter.config.rules.rules[1] = dataclasses.replace(
        alerter.config.rules.rules[1], severity="low", near=("local",)
    )  # conflict: low, medium near a local place
    alerter.on_transcript(result("a", "Sabotagem em Lisboa.", 1000.0))
    clock.now += 30
    alerter.on_transcript(result("a", "Sabotagem e corte de água em Vila Nova.", 1030.0))
    gate.set()
    settle(alerter, 2)
    time.sleep(0.2)
    assert [c[1] for c in actions.calls if c[0] == "notify"] == ["CRITICAL · water · Rádio A"]
    assert alerter._open["conflict"].covered_severity == "medium"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"rule": {"id": "water"}}, "double brackets"),  # [rule] written for [[rule]]
        ({"rule": ["water"]}, "double brackets"),
        ({"actions": ["notify"]}, r"\[actions\] must be a table"),
        ({"delivery": "fast"}, r"\[delivery\] must be a table"),
        ({"speak": "Daniel"}, r"\[speak\] must be a table"),
        ({"speak": {"en": "Daniel"}}, "needs 'voice' and 'fallback'"),
        ({"delivery": {"translation_wait_s": "10s"}}, "translation_wait_s must be a number"),
        ({"delivery": {"cooldown_minutes": None}}, "cooldown_minutes must be a number"),
        ({"delivery": {"cooldown_minutes": float("nan")}}, "cooldown_minutes must be a number"),
        ({"delivery": {"translation_wait_s": -1}}, "translation_wait_s must be a number"),
    ],
)
def test_a_wrongly_shaped_alerts_file_says_what_is_wrong(tmp_path: Path, change, message) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_alerts({**CONFIG, **change}, Path("alerts.toml"), pack(tmp_path))


VOICE = {"voice": "Lesya", "fallback": "Увага. Радіо."}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"delivry": {}}, r"^alerts\.toml: unknown top-level keys \['delivry'\]$"),
        ({"delivery": {"notfy_language": "en", "cooldown_minuts": 5}},
         r"^alerts\.toml \[delivery\]: unknown keys \['cooldown_minuts', 'notfy_language'\]; "
         r"it takes \['cooldown_minutes', 'notify_language', 'notify_sound', 'remind_minutes', "
         r"'translation_wait_s'\]$"),
        ({"speak": {"uk": {**VOICE, "intros": "Увага."}}},
         r"^alerts\.toml \[speak\.uk\]: unknown keys \['intros'\]; it takes \['earlier', "
         r"'fallback', 'intro', 'summary', 'voice'\]$"),
        ({"actions": {"hihg": ["notify"]}}, r"^alerts\.toml \[actions\]: unknown severity 'hihg'$"),
        ({"rule": [{"id": "water", "topics": ["water"], "min_mention": 3, "severty": "high"}]},
         r"^alerts\.toml: rule water: unknown keys \['min_mention', 'severty'\]; a keyword rule "
         r"takes \['id', 'kind', 'min_mentions', 'near', 'regex', 'severity', 'stations', "
         r"'terms', 'topics', 'verify'\]$"),
        # A key of another kind of rule would be ignored just the same.
        ({"rule": [{"id": "leave", "kind": "llm", "prompt": "told to leave", "verify": True}]},
         r"rule leave: unknown keys \['verify'\]; an llm rule takes \['id', 'kind', "
         r"'min_mentions', 'prompt', 'severity', 'stations'\]$"),
        ({"rule": [{"id": "quiet", "kind": "silence", "after_minuts": 5}]},
         r"rule quiet: unknown keys \['after_minuts'\]; a silence rule takes \['after_minutes', "
         r"'id', 'kind', 'level_db', 'severity', 'stations'\]$"),
        ({"rule": [{"id": "jam", "kind": "traffic", "min_mentions": 2}]},
         r"rule jam: unknown keys \['min_mentions'\]; a traffic rule takes \['id', 'kind', "
         r"'severity', 'stations'\]$"),
        # A misspelt kind was skipped, and the station it watched went silent unnoticed.
        ({"rule": [{"id": "quiet", "kind": "silense"}]},
         r"^alerts\.toml: rule quiet: kind is keyword, llm, silence or traffic, not 'silense'$"),
        ({"rule": [{"id": "origin", "topics": ["water"], "origin": "file", "enabled": True}]},
         r"rule origin: unknown keys \['enabled', 'origin'\]"),  # the page's, not the file's
    ],
)  # fmt: skip
def test_a_key_the_alerts_file_does_not_take_is_refused_by_name(
    tmp_path: Path, change, message
) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_alerts({**CONFIG, **change}, Path("alerts.toml"), pack(tmp_path))


def test_every_key_the_alerts_file_takes_loads(tmp_path: Path) -> None:
    from wavescribe.alerts import DELIVERY_KEYS, VOICE_KEYS, load_alerts, merge_page, read_alerts
    from wavescribe.digest.schedule import KEYS
    from wavescribe.rules import RULE_KEYS

    path = tmp_path / "alerts.toml"
    path.write_text(
        '[delivery]\nnotify_language = "en"\nnotify_sound = ""\ntranslation_wait_s = 5\n'
        "cooldown_minutes = 10\nremind_minutes = 0\n"
        '[speak.uk]\nvoice = "Lesya"\nfallback = "Увага."\nintro = "Увага:"\n'
        'summary = "Огляд {stations}."\nearlier = "Раніше: {alerts}."\n'
        '[[rule]]\nid = "water"\nkind = "keyword"\ntopics = ["water"]\nnear = ["local"]\n'
        'severity = "high"\nmin_mentions = 2\nstations = ["a"]\nverify = true\n'
        "regex = ['\\bsem água\\b']\nterms = { es = [\"sin agua\"] }\n"
        '[[rule]]\nid = "leave"\nkind = "llm"\nprompt = "people are told to leave"\n'
        'severity = "high"\nmin_mentions = 2\nstations = ["a"]\n'
        '[[rule]]\nid = "quiet"\nkind = "silence"\nafter_minutes = 5\nlevel_db = -50\n'
        'severity = "medium"\nstations = ["a"]\n'
        '[[rule]]\nid = "jam"\nkind = "traffic"\nseverity = "low"\nstations = ["a"]\n'
        '[actions]\ncritical = ["notify", "speak:uk", "log"]\nhigh = ["notify"]\n'
        'medium = ["log"]\nlow = []\n'
        '[digest]\nevery_minutes = 30\nstations = ["a"]\nlanguage = "uk"\nspeak = true\n'
        'quiet_hours = ["22:00", "07:00"]\n',
        encoding="utf-8",
    )
    raw = read_alerts(path)
    # The file above has every key there is: one added to the program is added here.
    assert set(raw["delivery"]) == DELIVERY_KEYS and set(raw["speak"]["uk"]) == VOICE_KEYS
    assert {r["kind"]: set(r) for r in raw["rule"]} == RULE_KEYS and set(raw["digest"]) == KEYS
    words = pack(tmp_path)
    config = load_alerts(path, words)
    assert len(config.rules.rules + config.rules.llm_rules + config.rules.station_rules) == 4
    # What the page keeps in config/ui.toml lays over it: its delivery settings and voices,
    # its rules, the file rules it switched off and its digest schedule, off or not.
    page = {
        "delivery": {"notify_language": "uk", "translation_wait_s": 3, "cooldown_minutes": 20},
        "actions": {"high": ["notify", "log"]},
        "speak": {"en": {"voice": "Daniel", "intro": "Attention:", "fallback": "Attention."}},
        "rule": [{"id": "bridge", "severity": "high", "terms": {"es": ["puente cerrado"]},
                  "kind": "keyword"}],
        "disabled_rules": ["jam"],
        "digest": {"every_minutes": 60, "off": True},
    }  # fmt: skip
    merged, rules, disabled = merge_page(raw, page)
    config = parse_alerts(merged, path, words, rules, disabled)
    assert config.notify_language == "uk" and set(config.voices) == {"uk", "en"}
    assert [r.id for r in config.rules.rules] == ["water", "bridge"]
    assert config.rules.station_rules[0].id == "quiet" and len(config.rules.station_rules) == 1
    assert "digest" not in merged


def test_a_misspelt_digest_key_is_refused_when_the_file_is_read(tmp_path: Path) -> None:
    from wavescribe.alerts import read_alerts

    path = tmp_path / "alerts.toml"
    path.write_text("[digest]\nevery_minuts = 30\nspeak = true\n", encoding="utf-8")
    message = (r"alerts\.toml \[digest\]: unknown keys \['every_minuts'\]; it takes "
               r"\['every_minutes', 'language', 'quiet_hours', 'speak', 'stations'\]$")  # fmt: skip
    with pytest.raises(ConfigError, match=message):
        read_alerts(path)


def test_the_example_rules_load_with_every_commented_out_part_switched_on(tmp_path) -> None:
    """What the example file offers to uncomment (another voice, own words, an LLM rule, the
    traffic rule, a digest schedule) is in keys the program takes."""
    import re

    from wavescribe.alerts import load_alerts

    switched_on = re.compile(r"^# (\[\[?[a-z.]+\]?\]|[a-z_]+ = .*)$")
    lines = Path("config/alerts.example.toml").read_text(encoding="utf-8").splitlines()
    path = tmp_path / "alerts.toml"
    path.write_text(
        "\n".join(m[1] if (m := switched_on.match(line)) else line for line in lines),
        encoding="utf-8",
    )
    config = load_alerts(path, pack(tmp_path))
    assert {"water-cut", "motorway-closed"} <= {r.id for r in config.rules.rules}
    assert [r.id for r in config.rules.llm_rules] == ["told-to-leave"]
    assert [r.kind for r in config.rules.station_rules] == ["silence", "traffic"]
    assert set(config.voices) == {"en", "es"} and config.voices["es"].summary
    assert config.rules.get("water-cut").verify is True


def test_actions_written_as_one_string_are_refused(tmp_path: Path) -> None:
    raw = {**CONFIG, "actions": {"critical": "notify"}}  # read as a list: "n", "o", "t", ...
    with pytest.raises(ConfigError, match=r"\[actions\]: critical must be a list of actions"):
        parse_alerts(raw, Path("alerts.toml"), pack(tmp_path))


def test_notifications_play_the_sound_set_and_none_when_it_is_empty(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    from wavescribe.alerts.actions import MacActions

    alerter, actions, _, _, _ = make(tmp_path, {"en": EN, "uk": UK}, notify_sound="")
    alerter.on_transcript(result("a", "Corte de água em Vila Nova amanhã.", 1000.0))
    settle(alerter)
    assert actions.sounds == [""] and actions.calls[0][0] == "show"  # shown, without a sound
    with pytest.raises(ConfigError, match="notify_sound must be a sound's name"):
        parse_alerts({**CONFIG, "delivery": {"notify_sound": False}}, None, tmp_path)
    # The app's alerts go through the digests' Speech: an alert's sound passes, a digest has none.
    from wavescribe.digest.speech import Speech

    speech = Speech(FakeActions())
    speech.notify("HIGH · water", "Corte de água", sound="Ping")
    speech.show("Digest", "Três pontos")
    assert speech.actions.sounds == ["Ping", ""]

    ran: list[list[str]] = []

    def run(cmd, **_kwargs):  # no real notification in a test
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    for sound in (None, "Ping", ""):
        MacActions().notify("HIGH · water", "Corte de água", sound=sound)
    default, ping, silent = ran
    assert default[-3:] == ["HIGH · water", "Corte de água", "Glass"]  # unset: the Mac's own
    assert ping[-1] == "Ping" and "sound name (item 3 of argv)" in " ".join(ping)
    assert silent[-2:] == ["HIGH · water", "Corte de água"] and "sound" not in " ".join(silent)


def test_speech_commands_in_the_text_are_not_obeyed(monkeypatch) -> None:
    import subprocess

    from wavescribe.alerts.actions import MacActions

    spoken: list[str] = []

    def run(cmd, *, input, **_kwargs):
        spoken.append(input)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    MacActions().speak("Daniel", "Evacuate [[volm 0]] the [[[slnc 60000]] riverside [now].")
    assert spoken == ["Evacuate [ [volm 0]] the [ [ [slnc 60000]] riverside [now]."]


def test_voice_listing_is_parsed_in_both_formats() -> None:
    from wavescribe.alerts.actions import parse_voices

    listing = (
        "Daniel (English (UK)) en_GB    # Hello! My name is Daniel.\n"
        "Lesya               uk_UA    # Привіт! Мене звуть Леся.\n"
        "Lesya (Ukrainian (Ukraine)) uk_UA    # Привіт! Мене звуть Леся.\n"
        "Bad News            en_US    # Hello! My name is Bad News.\n"
    )
    names = parse_voices(listing)
    assert {"Daniel", "Daniel (English (UK))", "Lesya", "Bad News"} <= names


def test_listen_marks_trigger_words_and_prints_each_announcement_once(tmp_path, capsys) -> None:
    from wavescribe.cli import ResultPrinter

    printer = ResultPrinter(tty=False)
    alerter, *_ = make(tmp_path, {"en": EN, "uk": UK})
    alerter.on_alert = printer.on_alert
    r = result("a", "Corte de água em Vila Nova amanhã.", 1000.0)
    alerter.on_transcript(r)
    settle(alerter)
    printer(r)
    out = capsys.readouterr().out.splitlines()
    alerts = [line for line in out if " !! " in line]
    assert len(alerts) == 1 and "!! CRITICAL water a: Water will be cut" in alerts[0]
    assert any("[Corte de água] em [Vila Nova] amanhã." in line for line in out)


class FailingStore:
    """A store whose named writes fail the first ``times`` times, as with a full disk."""

    def __init__(self, store: Store, failing: str, times: int = 1000) -> None:
        self._store, self._failing, self.left = store, failing, times

    def __getattr__(self, name: str):
        write = getattr(self._store, name)
        if name != self._failing:
            return write

        def fail(*args):
            if self.left > 0:
                self.left -= 1
                raise sqlite3.OperationalError("database or disk is full")
            return write(*args)

        return fail


STORE_WRITES = ["add_alert", "add_mention", "add_alert_summaries", "update_delivery"]


@pytest.mark.parametrize("failing", STORE_WRITES)
def test_alerts_are_delivered_whatever_the_store_does(tmp_path: Path, failing: str) -> None:
    store = FailingStore(Store(tmp_path / "w.db"), failing)
    alerter, actions, clock, *_ = make(tmp_path, {"en": EN, "uk": UK}, store=store)
    pushed: list[str] = []  # webhooks and feed pushes go out after the store is written
    alerter.on_delivered = lambda _payload, severity, _text, _checked: pushed.append(severity)
    alerter.on_transcript(result("a", "Corte de água em Lisboa.", 1000.0))
    [alert] = settle(alerter)
    assert alert.status == "delivered" and [c[0] for c in actions.calls] == ["notify"]

    clock.now += 60  # and the rule isn't stuck behind it: it still announces what gets worse
    alerter.on_transcript(result("a", "Corte de água também em Vila Nova.", 1060.0))
    alerter.drain()
    assert [c[0] for c in actions.calls] == ["notify", "notify", "speak"]
    assert alert.delivered_severity == "critical" and pushed == ["high", "critical"]


def test_an_alert_the_store_refused_is_stored_with_a_later_write(tmp_path: Path) -> None:
    store = FailingStore(Store(tmp_path / "w.db"), "add_alert", times=1)
    alerter, *_ = make(tmp_path, {"en": EN}, store=store)
    alerter.on_transcript(result("a", "Corte de água em Lisboa.", 1000.0))
    settle(alerter)
    [kept] = store.alerts()
    assert (kept["rule"], kept["status"], kept["quote"]) == (
        "water", "delivered", "Corte de água em Lisboa.")  # fmt: skip


def test_a_delivery_that_cannot_run_does_not_leave_the_alert_pending(tmp_path: Path) -> None:
    alerter, actions, clock, *_ = make(tmp_path, {"en": EN, "uk": UK})
    deliver, failures = alerter._deliver, [RuntimeError("unexpected")]

    def flaky(*args):
        if failures:
            raise failures.pop()
        deliver(*args)

    alerter._deliver = flaky
    alerter.on_transcript(result("a", "Corte de água em Lisboa.", 1000.0))
    [alert] = settle(alerter)
    assert alert.status == "failed" and actions.calls == []

    clock.now += 60  # the next mention announces it
    alerter.on_transcript(result("a", "Sem água em Lisboa.", 1060.0))
    alerter.drain()
    assert alert.status == "delivered" and [c[0] for c in actions.calls] == ["notify"]


def test_an_alert_joined_by_radio_kept_only_days_stays_out_of_the_log(tmp_path: Path) -> None:
    import dataclasses

    alerter, *_ = make(tmp_path, {"en": EN})
    alerter.on_transcript(result("a", "Corte de água em Lisboa.", 1000.0))
    assert not alerter._open["water"].short_lived  # broadcast radio opened it
    r = result("b", "Sem água em Lisboa desde manhã.", 1600.0)
    r.station = dataclasses.replace(r.station, privacy="amateur")  # text kept 7 days
    alerter.on_transcript(r)
    assert alerter._open["water"].short_lived


# -- one announcement per incident ------------------------------------------------------------


def quiet(tmp_path: Path, *, fail=frozenset(), remind=None, severity="high"):
    """An Alerter without a translator, so each delivery is queued at once (``drain`` waits for
    it), with the webhooks' calls and the feed's events recorded."""
    tmp_path.mkdir(exist_ok=True)
    rule = {"id": "water", "topics": ["water"], "near": ["local"], "severity": severity}
    delivery = {**CONFIG["delivery"]} | ({} if remind is None else {"remind_minutes": remind})
    config = parse_alerts({**CONFIG, "rule": [rule], "delivery": delivery}, None, pack(tmp_path))
    actions, clock, seen, pushed, tries = FakeActions(fail), Clock(), [], [], []
    alerter = Alerter(config, actions=actions, clock=clock, on_alert=seen.append)
    alerter.on_delivered = lambda payload, severity, _text, _c: pushed.append(payload["change"])
    deliver = alerter._deliver
    alerter._deliver = lambda *a: (tries.append(a[1]), deliver(*a))
    return alerter, actions, clock, seen, pushed, tries


def mention_every(alerter: Alerter, clock: Clock, seconds: float, for_s: float,
                  text: str = "Corte de água em Lisboa.") -> None:  # fmt: skip
    t = 0.0
    while t <= for_s:
        alerter.on_transcript(result("a", text, 1000.0 + t))
        alerter.drain()
        clock.now += seconds
        t += seconds


def test_a_story_repeated_while_notifications_fail_is_not_announced_at_every_mention(
    tmp_path: Path,
) -> None:
    # Seen in testing: notifications that timed out made each later mention announce the alert
    # again, webhooks included, 19 times in ten minutes. A delivery that reached nobody is tried
    # again after five minutes, then ten, and its webhooks go out once.
    alerter, _, clock, _, pushed, tries = quiet(tmp_path, fail={"notify"})
    mention_every(alerter, clock, 10, 10 * 60)
    [alert] = alerter._open.values()
    assert alert.mentions == 61 and alert.status == "failed"
    assert tries == ["high", "high"]  # at 0 s and at 5 minutes; the next one is due at 15
    assert pushed == ["new"]
    mention_every(alerter, clock, 10, 5 * 60)
    assert len(tries) == 3 and pushed == ["new"]


def test_mentions_are_updates_and_a_quiet_period_ends_the_incident(tmp_path: Path) -> None:
    alerter, actions, clock, seen, pushed, _ = quiet(tmp_path)
    mention_every(alerter, clock, 60, 10 * 60)
    first = alerter._open["water"]
    assert first.mentions == 11 and [c[0] for c in actions.calls] == ["notify"]
    changes = [e["change"] for e in seen]
    assert changes.count("new") == 1 and set(changes) == {"new", "update"}
    assert pushed == ["new"]  # webhooks get the announcement, not each mention

    clock.now += 15 * 60 + 1  # no mention for the cooldown: the incident is over
    mention_every(alerter, clock, 60, 0)
    assert alerter._open["water"] is not first
    assert [e["change"] for e in seen].count("new") == 2 and pushed == ["new", "new"]
    assert [c[0] for c in actions.calls] == ["notify", "notify"]


def test_a_story_that_goes_on_is_recalled_now_and_then_and_never_spoken(tmp_path: Path) -> None:
    alerter, actions, clock, seen, pushed, _ = quiet(tmp_path, remind=30, severity="critical")
    mention_every(alerter, clock, 5 * 60, 2 * 3600)  # every five minutes for two hours
    assert pushed == ["new", "reminder", "reminder", "reminder", "reminder"]
    assert [c[0] for c in actions.calls] == ["notify", "speak"] + ["notify"] * 4
    title, body = actions.calls[-1][1:]
    assert title == "STILL CRITICAL · water · a"
    assert body.startswith("Mentioned 25 times since ")
    assert body.endswith("Corte de água em Lisboa.")
    assert [e["change"] for e in seen].count("reminder") == 4

    off, actions, clock, _, pushed, _ = quiet(tmp_path / "off", remind=0, severity="critical")
    mention_every(off, clock, 5 * 60, 2 * 3600)
    assert pushed == ["new"] and [c[0] for c in actions.calls] == ["notify", "speak"]


def test_an_alert_is_logged_as_information_not_as_a_warning(tmp_path: Path, caplog) -> None:
    # An alert is Wavescribe working as meant; warnings are for what needs fixing.
    alerter, _, clock, _, pushed, _ = quiet(tmp_path, remind=30)
    with caplog.at_level("INFO", logger="wavescribe.alerts"):
        mention_every(alerter, clock, 10 * 60, 40 * 60)  # announced, then a reminder
    assert pushed == ["new", "reminder"]
    logged = [r for r in caplog.records if r.getMessage() == "alert"]
    assert [r.levelname for r in logged] == ["INFO", "INFO"]


def test_an_escalation_is_announced_and_marked_so(tmp_path: Path) -> None:
    alerter, actions, clock, seen, pushed, _ = quiet(tmp_path)
    mention_every(alerter, clock, 60, 120)
    mention_every(alerter, clock, 60, 0, "Corte de água também em Vila Nova.")
    assert pushed == ["new", "escalated"]
    assert [c[0] for c in actions.calls] == ["notify", "notify", "speak"]
    assert seen[-1]["change"] == "escalated" and seen[-1]["severity"] == "critical"
