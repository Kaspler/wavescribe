# SPDX-License-Identifier: Apache-2.0
"""A keyword rule with ``verify`` whose check didn't answer: what ``unverified`` makes of the
match, and how the alert says it wasn't checked."""

import logging
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.test_alerts import settle
from tests.test_llm_rules import FakeLlm, make_alerter
from tests.test_store import result
from wavescribe.alerts.cap import NS, to_cap
from wavescribe.events import alert_event, as_text
from wavescribe.rules import Hit, KeywordRule, parse_rule
from wavescribe.rules.llm import NOT_RUN, LlmRuleChecker
from wavescribe.store import Store

YES = '{"real": true, "reason": "the bridge is shut"}'
NO = '{"real": false, "reason": "a song"}'
# The ways a check can fail to answer, and what each is counted as.
WAYS = {
    "timeout": "timeout",  # queued, and the model never gets to it
    "crash": "failed",  # the model raises
    "garbage": "failed",  # it answers something that can't be read
    "queue full": "queue_full",
    "no model": "no_model",  # nothing is loaded
    "not an llm": "no_model",  # what is loaded translates and can't judge
}


class NotAnLlm:
    model_id = "only-translates"


def rule(unverified: str | None = None, severity: str = "high") -> KeywordRule:
    raw = {"id": "bridge", "severity": severity, "verify": True,
           "terms": {"es": ["puente cerrado"]}}  # fmt: skip
    parsed = parse_rule(raw if unverified is None else {**raw, "unverified": unverified})
    assert isinstance(parsed, KeywordRule)
    return parsed


def until(done, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not done():
        assert time.time() < deadline, "it never happened"
        time.sleep(0.005)


class Verifier:
    """An LlmRuleChecker whose checks of keyword hits go as ``how`` says: one of WAYS, or "yes"
    or "no" for a model that answers. Its clock is ``now``; a check that is to time out does
    after 20 ms."""

    def __init__(self, how: str, on_hits=None) -> None:
        self.now = 1000.0
        self.asked = 0  # checks asked for
        self.waiting: list = []  # jobs queued and never run
        self.raised: list[Hit] = []

        def raised(r, hits: list[Hit]) -> None:
            self.raised.extend(hits)
            if on_hits is not None:
                on_hits(r, hits)

        self.checker = LlmRuleChecker(
            lambda: [], self.submit, raised, timer=False, clock=lambda: self.now,
            has_llm=lambda: self.how != "no model",
        )  # fmt: skip
        self.how = how

    @property
    def how(self) -> str:
        return self._how

    @how.setter
    def how(self, how: str) -> None:
        self._how = how
        self.checker.verify_timeout_s = 0.02 if how == "timeout" else 60.0

    def submit(self, run, name, priority, station=None, **how) -> bool:
        self.asked += 1
        if self.how in ("queue full", "no model"):
            return False
        if self.how == "timeout":
            self.waiting.append(run)
            return True
        llm = FakeLlm({"yes": YES, "no": NO}.get(self.how, "sure!"))
        if self.how == "crash":
            llm.complete = lambda *a, **k: 1 / 0  # type: ignore[method-assign]
        run(NotAnLlm() if self.how == "not an llm" else llm)
        return True

    def verify(self, rule: KeywordRule, severity: str | None = None) -> Hit:
        hit = Hit(rule.id, severity or rule.severity, ["puente cerrado"], [], [], again=True)
        said = result("a", "El puente cerrado desde ayer.", 1.0, "es-ES")
        self.checker.verify(said, hit, rule)
        return hit

    def counts(self) -> dict:
        return self.checker.status()["verify"]

    def unanswered(self, tries: int) -> None:
        """Wait until that many tries are known not to have answered (a timeout takes 20 ms)."""
        until(lambda: sum(self.counts()["not_run"].values()) >= tries)


def not_run(**counted: int) -> dict[str, int]:
    return {**dict.fromkeys(NOT_RUN, 0), **counted}


@pytest.mark.parametrize("how", WAYS)
@pytest.mark.parametrize(
    ("unverified", "severity"), [(None, "medium"), ("lower", "medium"), ("announce", "high")]
)
def test_a_match_whose_check_did_not_answer_is_raised_as_its_rule_says(
    how: str, unverified: str | None, severity: str
) -> None:
    """A rule with verify never goes silent because its check couldn't run."""
    v = Verifier(how)
    hit = v.verify(rule(unverified))
    v.unanswered(1)
    until(lambda: v.raised)
    assert v.raised == [hit] and (hit.severity, hit.checked) == (severity, False)
    assert hit.reason is not None and hit.reason.startswith("not checked by the LLM: ")
    assert hit.reason.endswith("; raised as medium, not high") == (severity == "medium")
    status = v.checker.status()
    assert status["unchecked"] == 1
    assert status["verify"] == {
        "confirmed": 0, "rejected": 0, "held": 0, "not_run": not_run(**{WAYS[how]: 1}),
    }  # fmt: skip


@pytest.mark.parametrize("unverified", [None, "lower", "announce", "hold"])
def test_a_model_that_answers_confirms_the_match_or_drops_it(unverified: str | None) -> None:
    v = Verifier("yes")
    hit = v.verify(rule(unverified))
    assert v.raised == [hit]
    assert (hit.severity, hit.checked, hit.reason) == ("high", True, "the bridge is shut")
    assert v.counts() == {"confirmed": 1, "rejected": 0, "held": 0, "not_run": not_run()}

    v = Verifier("no")  # not a real match: that is the check working, whatever the choice
    v.verify(rule(unverified))
    assert v.raised == [] and v.checker.status()["unchecked"] == 0
    assert v.counts() == {"confirmed": 0, "rejected": 1, "held": 0, "not_run": not_run()}


@pytest.mark.parametrize(
    ("own", "lowered"),
    [("critical", "high"), ("high", "medium"), ("medium", "low"), ("low", "low")],
)
def test_lower_is_one_severity_down_and_never_below_low(own: str, lowered: str) -> None:
    v = Verifier("queue full")
    hit = v.verify(rule("lower"), own)
    assert v.raised == [hit] and (hit.severity, hit.checked) == (lowered, False)
    v = Verifier("queue full")
    assert v.verify(rule("announce"), own).severity == own


@pytest.mark.parametrize("how", WAYS)
@pytest.mark.parametrize("answer", ["yes", "no"])
def test_hold_keeps_a_match_until_its_check_answers(how: str, answer: str) -> None:
    v = Verifier(how)
    hit = v.verify(rule("hold"))
    v.unanswered(1)
    assert v.raised == [] and v.counts()["held"] == 1 and v.checker.status()["unchecked"] == 0
    v.now += 29
    v.checker.tick()
    assert v.asked == 1  # asked again 30 s after the try that didn't answer, not before
    v.how = answer
    v.now += 1
    v.checker.tick()
    assert v.asked == 2
    if answer == "yes":
        assert v.raised == [hit] and (hit.severity, hit.checked) == ("high", True)
    else:
        assert v.raised == [] and hit.checked is None
    assert v.counts() == {
        "confirmed": int(answer == "yes"), "rejected": int(answer == "no"), "held": 0,
        "not_run": not_run(**{WAYS[how]: 1}),
    }  # fmt: skip
    v.now += 3600
    v.checker.tick()  # settled: nothing more is asked or raised
    assert v.asked == 2 and len(v.raised) == int(answer == "yes")


def test_hold_asks_again_twice_as_long_apart_and_gives_up_after_ten_minutes() -> None:
    v = Verifier("queue full")
    hit = v.verify(rule("hold"))
    asked_at = []
    for second in range(5, 600, 5):  # the checker looks every 5 s
        v.now, before = 1000.0 + second, v.asked
        v.checker.tick()
        asked_at += [second] * (v.asked - before)
    assert asked_at == [30, 90, 210, 450] and v.raised == [] and v.counts()["held"] == 1
    v.now = 1600.0
    v.checker.tick()
    # Even a held match can't be swallowed: it is raised as "lower" would have raised it.
    assert v.raised == [hit] and (hit.severity, hit.checked) == ("medium", False)
    assert hit.reason == ("not checked by the LLM: its queue is full, held for 10 min; "
                          "raised as medium, not high")  # fmt: skip
    assert v.checker.status()["unchecked"] == 1
    assert v.counts() == {
        "confirmed": 0, "rejected": 0, "held": 0, "not_run": not_run(queue_full=5),
    }  # fmt: skip
    v.how = "yes"
    v.now += 3600
    v.checker.tick()
    assert v.asked == 5 and v.raised == [hit]


def test_a_try_still_out_when_a_held_match_is_given_up_changes_nothing() -> None:
    v = Verifier("queue full")
    hit = v.verify(rule("hold"))
    v.how = "timeout"
    v.checker.verify_timeout_s = 60  # the second try is still waiting at ten minutes
    v.now += 30
    v.checker.tick()
    [job] = v.waiting
    v.now += 570
    v.checker.tick()
    assert v.raised == [hit] and hit.severity == "medium"
    llm = FakeLlm(YES)
    job(llm)  # the model gets to it at last: too late, and it isn't asked
    assert llm.prompts == [] and v.raised == [hit] and hit.checked is False


def test_an_answer_that_comes_after_the_timeout() -> None:
    held = Verifier("timeout")
    hit = held.verify(rule("hold"))
    held.unanswered(1)
    [job] = held.waiting
    job(FakeLlm(YES))  # a held match takes the verdict whenever it comes
    assert held.raised == [hit] and (hit.severity, hit.checked) == ("high", True)
    assert held.counts() == {
        "confirmed": 1, "rejected": 0, "held": 0, "not_run": not_run(timeout=1),
    }  # fmt: skip

    raised = Verifier("timeout")
    hit = raised.verify(rule("lower"))
    raised.unanswered(1)
    until(lambda: raised.raised)
    llm = FakeLlm(YES)
    raised.waiting[0](llm)  # raised already, one step lower: the model isn't asked any more
    assert llm.prompts == [] and raised.raised == [hit] and hit.severity == "medium"


def test_at_most_so_many_matches_are_held_and_the_oldest_makes_room() -> None:
    v = Verifier("no model")
    v.checker.hold_max = 2
    hits = []
    for _ in range(4):
        hits.append(v.verify(rule("hold", "critical")))
        v.now += 1
    assert v.raised == hits[:2] and v.counts()["held"] == 2
    assert [(h.severity, h.checked) for h in hits[:2]] == [("high", False)] * 2
    assert hits[0].reason == ("not checked by the LLM: no LLM is loaded, and too many matches "
                              "were waiting; raised as high, not critical")  # fmt: skip
    assert v.checker.status()["unchecked"] == 2


def test_stopping_raises_what_is_held() -> None:
    v = Verifier("crash")
    hit = v.verify(rule("hold"))
    assert v.raised == []
    v.checker.close()
    assert v.raised == [hit] and (hit.severity, hit.checked) == ("medium", False)
    assert hit.reason is not None and "Wavescribe was stopped" in hit.reason
    assert v.counts()["held"] == 0
    late = v.verify(rule("hold"))  # nothing would ask again: raised at once
    assert v.raised == [hit, late] and late.severity == "medium"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"unverified": "drop"},
         r"^rule bridge: unverified must be one of \('lower', 'announce', 'hold'\)$"),
        ({"unverified": True}, "rule bridge: unverified must be one of"),
        ({"unverified": "hold", "verify": False},
         "^rule bridge: unverified needs verify = true: it says what becomes of a match the "
         "model couldn't check$"),
        ({"unverified": "lower", "verify": None}, "rule bridge: verify must be true or false"),
    ],
)  # fmt: skip
def test_an_unverified_the_rule_cannot_take_is_refused_by_name(change: dict, message: str) -> None:
    raw = {"id": "bridge", "terms": {"es": ["puente cerrado"]}, "verify": True, **change}
    with pytest.raises(ValueError, match=message):
        parse_rule(raw)
    without = {k: v for k, v in raw.items() if k != "verify"}
    if change.get("verify", True) is True:
        with pytest.raises(ValueError, match=r"rule bridge: unverified (needs verify|must be)"):
            parse_rule(without)  # verify left out is verify off
    assert rule().unverified == "lower" and rule("hold").unverified == "hold"
    assert parse_rule({"id": "bridge", "terms": {"es": ["puente"]}}).unverified == "lower"  # type: ignore[union-attr]


# -- the alert ----------------------------------------------------------------------------------

PAGE = [
    {"id": "bridge", "severity": "high", "verify": True, "terms": {"es": ["puente cerrado"]}},
    {"id": "storm", "severity": "medium", "terms": {"es": ["tormenta"]}},
]


def titles(actions) -> list[str]:
    return [c[1] for c in actions.calls if c[0] == "notify"]


def parameters(alert: dict) -> dict[str, str]:
    info = ET.fromstring(to_cap(alert)).find(f"{{{NS}}}info")
    assert info is not None
    return {
        p.findtext(f"{{{NS}}}valueName") or "": p.findtext(f"{{{NS}}}value") or ""
        for p in info.findall(f"{{{NS}}}parameter")
    }


def test_an_alert_says_that_its_match_was_not_checked(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="wavescribe.alerts")
    store = Store(tmp_path / "w.db")
    alerter, actions, _delivered = make_alerter(tmp_path, PAGE, store=store)
    seen: list[dict] = []
    alerter.on_alert = seen.append
    v = Verifier("queue full", alerter.on_hits)
    alerter.verify = v.checker.verify

    first = result("a", "El puente cerrado desde ayer.", 100.0, "es-ES")
    store.add_transcript(first)
    alerter.on_transcript(first)
    until(lambda: titles(actions))
    [alert] = settle(alerter)
    # Raised one step lower, and the notification says why it is worth less. No word is added
    # to what a voice would say: the title is never spoken.
    assert actions.calls == [
        ("notify", "MEDIUM, not checked · bridge · Rádio A", "El puente cerrado desde ayer.")
    ]
    assert (alert.severity, alert.checked) == ("medium", False)
    assert alert.reason == ("not checked by the LLM: its queue is full; raised as medium, "
                            "not high")  # fmt: skip
    assert seen[-1]["checked"] is False and seen[-1]["severity"] == "medium"
    kept = store.alert(alert.id)
    assert kept is not None and (kept["checked"], kept["severity"]) == (False, "medium")
    event = alert_event(kept, {}, {})
    assert event["checked"] is False
    assert as_text(event).startswith("[MEDIUM, not checked] bridge · ")
    assert parameters(kept)["checked"] == "false"
    logged = [r.fields for r in caplog.records if r.msg == "alert"]
    assert [(f["severity"], f["checked"]) for f in logged] == [("low", None), ("medium", False)]

    # Its next mention is one the model confirms: the alert rises to the rule's severity, and
    # nothing says "not checked" any more.
    v.how = "yes"
    second = result("a", "Sigue el puente cerrado esta tarde.", 160.0, "es-ES")
    store.add_transcript(second)
    alerter.on_transcript(second)
    until(lambda: len(titles(actions)) == 2)
    settle(alerter)
    assert titles(actions)[-1] == "HIGH · bridge · Rádio A"
    assert (alert.severity, alert.checked, alert.mentions) == ("high", True, 2)
    kept = store.alert(alert.id)
    assert kept is not None and kept["checked"] is True and parameters(kept)["checked"] == "true"
    assert as_text(alert_event(kept, {}, {})).startswith("[HIGH] bridge · ")

    # A rule without verify has nothing to say about it.
    alerter.on_transcript(result("a", "Se acerca una tormenta.", 200.0, "es-ES"))
    until(lambda: len(titles(actions)) == 3)
    assert titles(actions)[-1] == "MEDIUM · storm · Rádio A"
    storm = store.alerts()[0]
    assert (storm["rule"], storm["checked"]) == ("storm", None)
    assert alert_event(storm, {}, {})["checked"] is None and "checked" not in parameters(storm)
    assert alerter._open["storm"].payload()["checked"] is None
    alerter.close()
    v.checker.close()
    store.close()


def test_checked_is_about_the_match_the_alerts_severity_comes_from(tmp_path: Path) -> None:
    page = [{**PAGE[0], "severity": "critical", "unverified": "announce"}]
    alerter, actions, _delivered = make_alerter(tmp_path, page)
    said = result("a", "El puente cerrado desde ayer.", 100.0, "es-ES")

    def mention(severity: str, checked: bool) -> bool | None:
        hit = Hit("bridge", severity, ["puente cerrado"], [], [], again=True, checked=checked)
        alerter.on_hits(said, [hit])
        return alerter._open["bridge"].checked

    assert mention("high", True) is True
    assert mention("medium", False) is True  # a lesser match nobody checked takes nothing away
    assert mention("critical", False) is False  # it rose on a match nobody checked
    assert mention("high", True) is False  # and a lesser one confirmed doesn't vouch for that
    until(lambda: len(titles(actions)) == 2)
    assert titles(actions) == [
        "HIGH · bridge · Rádio A",
        "CRITICAL, not checked · bridge · Rádio A",
    ]
    until(lambda: any(c[0] == "speak" for c in actions.calls))
    assert [c for c in actions.calls if c[0] == "speak"] == [("speak", "Lesya", "Увага. Радіо.")]
    assert mention("critical", True) is True
    alerter.close()


def test_a_reminder_says_it_too(tmp_path: Path) -> None:
    from tests.test_alerts import quiet

    alerter, actions, clock, _seen, pushed, _ = quiet(tmp_path, remind=30)
    said = result("a", "Corte de água em Lisboa.", 1000.0)
    for _ in range(8):  # every five minutes: still going on half an hour later
        hit = Hit("water", "high", ["corte de água"], [], [], checked=False)
        alerter.on_hits(said, [hit])
        alerter.drain()
        clock.now += 5 * 60
    assert pushed == ["new", "reminder"]
    assert titles(actions) == [
        "HIGH, not checked · water · a",
        "STILL HIGH, not checked · water · a",
    ]
    alerter.close()


def test_a_held_match_is_announced_when_it_is_checked_or_given_up(tmp_path: Path) -> None:
    page = [{**PAGE[0], "severity": "critical", "unverified": "hold"}]
    alerter, actions, _delivered = make_alerter(tmp_path, page)
    v = Verifier("no model", alerter.on_hits)
    alerter.verify = v.checker.verify
    alerter.on_transcript(result("a", "El puente cerrado desde ayer.", 100.0, "es-ES"))
    [alert] = settle(alerter)
    v.now += 599
    v.checker.tick()
    # On record as low from the start, and nobody is told anything while it is held.
    assert (alert.severity, alert.checked, actions.calls) == ("low", None, [])
    assert v.counts()["held"] == 1
    v.now += 1
    v.checker.tick()
    until(lambda: titles(actions))
    assert titles(actions) == ["HIGH, not checked · bridge · Rádio A"]
    assert (alert.severity, alert.checked, alert.mentions) == ("high", False, 1)
    alerter.close()


def test_the_page_is_told_the_key_and_the_counts(tmp_path: Path) -> None:
    from tests.test_app import make_app

    app = make_app(tmp_path)
    try:
        assert "unverified" in app.rules_view()["rule_keys"]["keyword"]
        assert "unverified" not in app.rules_view()["rule_keys"]["llm"]
        counts = {"confirmed": 0, "rejected": 0, "held": 0, "not_run": not_run()}
        assert app.status_view()["llm_rules"]["verify"] == counts
        assert app.rules_view()["llm"]["verify"] == counts
        app.save_rule({**PAGE[0], "unverified": "hold"})
        [kept] = [r for r in app.rules_view()["rules"] if r["id"] == "bridge"]
        assert kept["unverified"] == "hold"
        assert app.alerter is not None
        assert app.alerter.config.rules.get("bridge").unverified == "hold"  # type: ignore[union-attr]
        from wavescribe.app import ChangeError

        with pytest.raises(ChangeError, match="rule bridge: unverified needs verify = true"):
            app.save_rule({"id": "bridge", "terms": PAGE[0]["terms"], "unverified": "hold"})
    finally:
        app.pipeline.stop()
        if app.store is not None:
            app.store.close()
