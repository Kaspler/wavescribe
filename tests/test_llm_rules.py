# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

import pytest

from tests.test_alerts import CONFIG, FakeActions, settle
from tests.test_rules import RULES, pack
from tests.test_store import result
from wavescribe.alerts import Alerter, merge_page, parse_alerts
from wavescribe.rules import Hit, LlmRule, RuleSet, parse_rule
from wavescribe.rules.llm import LlmRuleChecker, parse_matches


def test_page_keyword_rules_carry_their_own_terms_per_language(tmp_path: Path) -> None:
    page = [
        {"id": "bridge", "severity": "high", "terms": {"pt-PT": ["ponte fechada"],
                                                        "uk": ["міст закрито"]}},
        {"id": "water", "severity": "low", "terms": {"pt": ["torneira"]}},  # replaces the file's
        {"id": "only-b", "severity": "medium", "terms": {"pt": ["ponte"]}, "stations": ["b"]},
    ]  # fmt: skip
    rules = RuleSet.load(RULES, pack(tmp_path), page, disabled=("conflict",))
    assert [r.id for r in rules.rules] == ["bridge", "water", "only-b"]
    [hit] = rules.evaluate("A ponte fechada desde ontem.", "pt-PT", station="a")
    assert (hit.rule_id, hit.severity, hit.terms) == ("bridge", "high", ["ponte fechada"])
    assert [h.rule_id for h in rules.evaluate("Міст закрито.", "uk", station="a")] == ["bridge"]
    # Ukrainian has no keyword pack here, but its own terms still match.
    on_b = rules.evaluate("A ponte fechada.", "pt-PT", station="b")
    assert sorted(h.rule_id for h in on_b) == ["bridge", "only-b"]
    assert rules.evaluate("Corte de água.", "pt-PT") == []  # the file's water rule was replaced
    assert rules.evaluate("Sabotagem!", "pt-PT") == []  # switched off on the page


def test_rule_validation() -> None:
    assert isinstance(parse_rule({"id": "x", "kind": "llm", "prompt": "an evacuation"}), LlmRule)
    for bad in (
        {"id": "Bad Id", "terms": {"en": ["a"]}},
        {"id": "x\n", "terms": {"en": ["a"]}},
        {"id": "x", "kind": "llm"},
        {"id": "x"},
        {"id": "x", "terms": {"en": ["a"]}, "severity": "urgent"},
    ):
        with pytest.raises(ValueError):
            parse_rule(bad)


class FakeLlm:
    model_id = "fake-llm"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list = []

    def complete(self, messages, *, max_tokens=512, on_text=None) -> str:
        self.prompts.append(messages)
        return self.reply


def checker(llm: FakeLlm, rules: list[LlmRule], seen: list, clock=None):
    jobs: list = []

    def submit(run, name, priority):
        jobs.append((name, priority))
        run(llm)
        return True

    c = LlmRuleChecker(lambda: rules, submit, lambda r, hits: seen.append((r, hits)),
                       interval_s=30, max_chars=200, clock=clock or (lambda: 0.0),
                       timer=False)  # fmt: skip
    return c, jobs


def test_llm_rules_read_a_stations_text_in_batches(tmp_path: Path) -> None:
    rules = [LlmRule("evacuation", "they tell people to leave an area", "high"),
             LlmRule("elsewhere", "anything", "low", stations=("zzz",))]  # fmt: skip
    matches = [{"rule": "evacuation", "line": 2, "reason": "evacuation ordered"},
               {"rule": "nope", "line": 1}, {"rule": "evacuation", "line": 9},
               {"rule": "evacuation", "line": True}]  # true is not line 1  # fmt: skip
    reply = json.dumps(matches)
    llm, seen, now = FakeLlm(reply), [], [0.0]
    c, jobs = checker(llm, rules, seen, clock=lambda: now[0])
    first = result("a", "Bom dia, são oito horas.", 100.0)
    second = result("a", "A Proteção Civil pede aos moradores que saiam da zona.", 110.0)
    other = result("a", "Isto ouviu-se como espanhol", 120.0)
    other.flags = ("lang:es",)
    for r in (first, second, other):
        c.on_transcript(r)
    assert jobs == []  # not enough text yet, and not due
    now[0] = 31.0
    c.tick()
    assert jobs == [("llm-rules", 0.3)]
    prompt = llm.prompts[0][1]["content"]
    assert "- evacuation: they tell people to leave an area" in prompt
    assert "elsewhere" not in prompt  # that rule is for another station
    assert "[2] " in prompt and "saiam da zona" in prompt and "espanhol" not in prompt
    assert "Portuguese (Portugal)" in prompt and "in English" in prompt
    [(r, [hit])] = seen
    assert r is second and (hit.rule_id, hit.severity, hit.kind) == ("evacuation", "high", "llm")
    assert hit.reason == "evacuation ordered"
    assert c.status()["checks"] == 1 and c.status()["matches"] == 1


def test_a_full_batch_is_checked_at_once_and_a_non_llm_is_reported() -> None:
    class Madlad:
        model_id = "madlad"

    jobs: list = []
    c = LlmRuleChecker(lambda: [LlmRule("x", "y", "high")],
                       lambda run, name, prio: (jobs.append(name), run(Madlad()), True)[2],
                       lambda r, h: None, max_chars=20, timer=False)  # fmt: skip
    c.on_transcript(result("a", "Um texto bastante comprido para encher o lote.", 1.0))
    assert jobs == ["llm-rules"] and "need an LLM" in c.status()["error"]


def test_replies_are_parsed_leniently() -> None:
    assert parse_matches("[]") == []
    assert parse_matches('```json\n[{"rule": "a", "line": 1}]\n```') == [{"rule": "a", "line": 1}]
    assert parse_matches('Sure! {"real": true, "reason": "x"} hope that helps') == [
        {"real": True, "reason": "x"}
    ]
    assert parse_matches('Matches: [{"rule": "a", "line": 2}] done') == [{"rule": "a", "line": 2}]


def make_alerter(tmp_path: Path, page_rules: list, store=None):
    raw, rules, disabled = merge_page(CONFIG, {"rule": page_rules})
    config = parse_alerts(raw, None, pack(tmp_path), rules, disabled)
    actions, delivered = FakeActions(), []
    alerter = Alerter(config, store=store, actions=actions, station_names={"a": "Rádio A"})
    alerter.on_delivered = lambda a, sev, text, tr: delivered.append((a["rule"], sev, a["reason"]))
    return alerter, actions, delivered


def test_verified_keyword_rules_wait_for_the_llm_before_announcing(tmp_path: Path) -> None:
    page = [{"id": "bridge", "severity": "high", "verify": True,
             "terms": {"pt": ["ponte fechada"]}}]  # fmt: skip
    alerter, actions, delivered = make_alerter(tmp_path, page)
    asked: list = []
    alerter.verify = lambda r, hit, rule: asked.append((r, hit, rule))
    r = result("a", "A ponte fechada desde ontem.", 100.0)
    alerter.on_transcript(r)
    [alert] = settle(alerter)
    assert alert.severity == "low" and actions.calls == []  # held until the LLM says yes
    [(_, hit, rule)] = asked
    assert hit.severity == "high" and hit.again and rule.id == "bridge"

    hit.reason = "the bridge is closed"
    alerter.on_hits(r, [hit])
    for _ in range(100):
        if len(delivered) == 2:
            break
        settle(alerter)
    assert alert.severity == "high" and alert.mentions == 1  # same utterance, counted once
    assert [c[0] for c in actions.calls] == ["notify"]
    assert delivered[-1] == ("bridge", "high", "the bridge is closed")
    alerter.close()


def test_llm_rule_hits_become_alerts_with_their_reason(tmp_path: Path) -> None:
    alerter, _actions, delivered = make_alerter(
        tmp_path,
        [{"id": "evacuation", "kind": "llm", "prompt": "leave the area", "severity": "medium"}],
    )
    assert [r.id for r in alerter.config.rules.llm_rules] == ["evacuation"]
    from wavescribe.rules import Hit

    r = result("a", "Saiam da zona.", 100.0)
    alerter.on_hits(r, [Hit("evacuation", "medium", [], [], [], kind="llm", reason="leave")])
    [alert] = settle(alerter)
    assert (alert.kind, alert.reason, alert.severity) == ("llm", "leave", "medium")
    assert delivered == [("evacuation", "medium", "leave")]
    alerter.close()


def test_an_alert_keeps_the_reason_that_goes_with_its_quote(tmp_path: Path) -> None:
    from wavescribe.rules import Hit

    page = [{"id": "evacuation", "kind": "llm", "prompt": "leave the area", "severity": "medium"},
            {"id": "bridge", "severity": "high", "verify": True,
             "terms": {"pt": ["ponte fechada"]}}]  # fmt: skip
    alerter, _actions, _delivered = make_alerter(tmp_path, page)
    for text, reason in (("Saiam da zona.", "people told to leave"),
                         ("A zona foi evacuada.", "the area was cleared")):  # fmt: skip
        hit = Hit("evacuation", "medium", [], [], [], kind="llm", reason=reason)
        alerter.on_hits(result("a", text, 100.0), [hit])
    alert = alerter._open["evacuation"]
    assert (alert.quote, alert.reason, alert.mentions) == (
        "Saiam da zona.", "people told to leave", 2)  # fmt: skip

    # A verified keyword rule opens its alert before the LLM has answered: the reason arrives
    # afterwards and is kept, and a later mention's reason doesn't replace it either.
    asked: list = []
    alerter.verify = lambda r, hit, rule: asked.append((r, hit))
    for text in ("A ponte fechada desde ontem.", "Ponte fechada ao trânsito."):
        alerter.on_transcript(result("a", text, 200.0))
    bridge = alerter._open["bridge"]
    assert bridge.reason is None
    for (r, hit), reason in zip(asked, ("the bridge is closed", "closed to traffic"), strict=True):
        hit.reason = reason
        alerter.on_hits(r, [hit])
    assert (bridge.quote, bridge.reason) == ("A ponte fechada desde ontem.", "the bridge is closed")
    alerter.close()


@pytest.mark.parametrize("why", ["no llm", "queue full", "crash", "garbage", "timeout", "no"])
def test_a_keyword_hit_is_announced_when_the_llm_cant_check_it(why: str) -> None:
    """A rule marked verify must never go silent because the check couldn't run."""
    rule = parse_rule({"id": "bridge", "severity": "high", "verify": True,
                       "terms": {"pt": ["ponte"]}})  # fmt: skip
    seen: list = []

    class NotAnLlm:
        model_id = "madlad"

    replies = {"crash": None, "garbage": "sure!", "no": '{"real": false, "reason": "a song"}'}
    llm = NotAnLlm() if why == "no llm" else FakeLlm(replies.get(why) or "")
    if why == "crash":
        llm.complete = lambda *a, **k: 1 / 0  # type: ignore[method-assign]

    def submit(run, name, priority):
        if why == "queue full":
            return False
        if why != "timeout":  # the LLM never gets to it
            run(llm)
        return True

    c = LlmRuleChecker(lambda: [], submit, lambda r, hits: seen.append(hits), timer=False)
    c.verify_timeout_s = 0.05
    hit = Hit("bridge", "high", ["ponte"], [], [], again=True)
    c.verify(result("a", "A ponte caiu.", 1.0), hit, rule)  # type: ignore[arg-type]
    if why == "timeout":
        import time

        time.sleep(0.3)
    if why == "no":
        assert seen == [] and c.unchecked == 0  # the LLM said no: that's the filter working
        return
    [[announced]] = seen
    assert announced.severity == "high" and announced.reason.startswith("not checked")
    assert c.unchecked == 1
