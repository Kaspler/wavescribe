# SPDX-License-Identifier: Apache-2.0
"""LLM rules: "alert me when they tell people to leave an area", checked by the LLM.

Each station's new source-language utterances are collected and read together, about once a
``interval_s`` or when enough text piles up, against every plain-language rule that applies to
the station. One request covers all of a station's rules, so the cost grows with the talk on
the air, not with the number of rules. The LLM sees the source text (never a translation, so
translation stays off the alert path), and its one-line reason travels with the alert.

The same model confirms keyword hits from rules marked ``verify``: a keyword alone can't tell a
water cut from a football team called "Água"; the LLM, reading the sentence, usually can. A hit
whose check didn't answer is raised all the same, marked as not checked: one severity lower, as
it is, or after being held while the check is tried again, as its rule's ``unverified`` says.

Both run as jobs on the translation thread. The check of a keyword hit is an alert on its way:
it goes ahead of station translations and questions. A pass over recent speech waits for a
moment when nothing else wants the model, behind live translation, a question and a digest
someone asked for, since it reads the same lines a little later; after ``RULES_PUT_OFF_S`` it
goes ahead of the station lines and of a digest's next call, so no rule is silent for long. Its
lines are in the job from the moment it is queued, so none are lost while it waits.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from wavescribe.languages import language_name
from wavescribe.log import event
from wavescribe.radio.bandplan import loggable, logged_text
from wavescribe.rules import Hit, KeywordRule, LlmRule, raise_severity

log = logging.getLogger(__name__)

# The order of the LLM's work is listed in pipeline.py, above LLM_JOB_PRIORITY.
VERIFY_PRIORITY = 0.3  # a keyword hit's check: behind an alert's translation (0) only
# A pass over recent speech: behind everything else, a digest's calls included, for at most
# RULES_PUT_OFF_S; then ahead of station lines (1 and up) and of a digest's next call, behind
# questions (0.5). A digest's call under way is never stopped for it.
RULES_PRIORITY = sys.float_info.max
RULES_DUE_PRIORITY = 0.7
RULES_PUT_OFF_S = 60.0
CHECK_TOKENS = 300
VERIFY_TOKENS = 120
# A keyword hit the LLM hasn't judged by then wasn't checked: its rule's `unverified` says what
# becomes of it.
VERIFY_TIMEOUT_S = 90.0
# unverified = "hold" keeps such a hit and asks again: HOLD_RETRY_S after the try that didn't
# answer, then twice as long each time, which is five tries at most. HOLD_GIVE_UP_S after it was
# heard the hit is raised one severity lower, as "lower" would raise it: time enough for a
# model to load or a digest to finish, and inside the default cooldown (15 minutes), so it
# joins the alert it opened instead of opening another. At most HOLD_MAX hits are held, each
# with its utterance (some 2 MB of audio at the longest); one more raises the oldest.
HOLD_RETRY_S = 30.0
HOLD_GIVE_UP_S = 600.0
HOLD_MAX = 32
NOT_RUN = ("timeout", "failed", "queue_full", "no_model")  # why a check didn't answer

SYSTEM = (
    "You watch radio transcripts for events a listener asked to be alerted about. The "
    "transcripts come from automatic speech recognition and can contain recognition errors. "
    "You answer only with JSON."
)


@dataclass
class _Pending:
    results: list[Any] = field(default_factory=list)  # wavescribe.pipeline.Result
    chars: int = 0
    since: float = 0.0


@dataclass(eq=False)
class _Verify:
    """One keyword hit of a rule with ``verify``, until it is raised or dropped."""

    result: Any  # wavescribe.pipeline.Result
    hit: Hit
    rule: KeywordRule
    since: float  # when the alerter handed it over
    tries: int = 0  # checks asked for
    failed: int = 0  # of which known not to have answered
    done: bool = False  # raised or dropped
    retry_at: float | None = None  # held: when to ask again; None while a try is out
    why: str = ""  # why the last try didn't answer, in words
    timer: threading.Timer | None = None  # the try's timeout


# (run, name, priority, station=..., about=..., put_off_s=..., urgent=...) -> queued. A rule
# check that isn't queued is counted and logged by the pipeline (Pipeline.request_llm), against
# its station. ``about`` is the ids of the rules it reads for; ``put_off_s`` and ``urgent`` say
# for how long it may wait at ``priority`` and where it sorts after that.
Submit = Callable[..., bool]


class LlmRuleChecker:
    def __init__(
        self,
        rules: Callable[[], list[LlmRule]],
        submit: Submit,
        on_hits: Callable[[Any, list[Hit]], None],
        *,
        reason_language: str = "en",
        interval_s: float = 45.0,
        max_chars: int = 1500,
        clock: Callable[[], float] = time.time,
        timer: bool = True,
        has_llm: Callable[[], bool] = lambda: True,
    ) -> None:
        self._rules = rules
        self._submit = submit
        self._on_hits = on_hits
        self.reason_language = reason_language
        self.interval_s = interval_s
        self.max_chars = max_chars
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self.checks = 0  # requests answered
        self.matches = 0
        self.error: str | None = None  # why checks can't run (no LLM)
        self.unchecked = 0  # keyword hits raised without the LLM's verdict
        self.verify_timeout_s = VERIFY_TIMEOUT_S
        self.hold_retry_s = HOLD_RETRY_S
        self.hold_give_up_s = HOLD_GIVE_UP_S
        self.hold_max = HOLD_MAX
        # Whether a refused job means there is no LLM at all, rather than a full queue.
        self._has_llm = has_llm
        self._verify_lock = threading.Lock()
        self._held: list[_Verify] = []
        self._verdicts = {"confirmed": 0, "rejected": 0}
        self._not_run = dict.fromkeys(NOT_RUN, 0)  # tries that didn't answer, by why
        self.last_ms: float | None = None
        self._closed = threading.Event()
        if timer:
            threading.Thread(target=self._tick_loop, daemon=True, name="llm-rules").start()

    def close(self) -> None:
        """Stops the timer. Hits held for their check are raised now, as not checked: nothing
        would ask again."""
        self._closed.set()
        with self._verify_lock:
            held = list(self._held)
            for v in held:
                self._settle(v)
        for v in held:
            try:
                self._raise_unchecked(v, f"{v.why}, and Wavescribe was stopped")
            except Exception:
                log.exception("raising a held keyword hit failed")

    def rules_for(self, station_id: str) -> list[LlmRule]:
        return [r for r in self._rules() if not r.stations or station_id in r.stations]

    # -- collecting (ASR thread) ------------------------------------------------------------

    def on_transcript(self, result: Any) -> None:
        text = result.transcript.text
        if not text or any(f.startswith("lang:") for f in result.flags):
            return  # heard as another language: never raises an alert on its own
        if not self.rules_for(result.station.id):
            return
        with self._lock:
            p = self._pending.setdefault(result.station.id, _Pending())
            if not p.results:
                p.since = self._clock()
            p.results.append(result)
            p.chars += len(text)
            full = p.chars >= self.max_chars
        if full:
            self.flush(result.station.id)

    def _tick_loop(self) -> None:
        while not self._closed.wait(5.0):
            self.tick()

    def tick(self) -> None:
        now = self._clock()
        with self._lock:
            due = [sid for sid, p in self._pending.items()
                   if p.results and now - p.since >= self.interval_s]  # fmt: skip
        for sid in due:
            self.flush(sid)
        self._tick_held(now)

    def flush(self, station_id: str) -> None:
        with self._lock:
            p = self._pending.pop(station_id, None)
        if p is None or not p.results:
            return
        rules = self.rules_for(station_id)
        if not rules:
            return
        results = p.results

        def run(llm: Any) -> None:
            self._check(llm, results, rules)

        self._submit(
            run,
            "llm-rules",
            RULES_PRIORITY,
            station=station_id,
            about=tuple(r.id for r in rules),
            put_off_s=RULES_PUT_OFF_S,
            urgent=RULES_DUE_PRIORITY,
        )

    # -- checking (translation thread) ------------------------------------------------------

    def messages(self, results: list[Any], rules: list[LlmRule]) -> list[dict[str, str]]:
        station = results[0].station
        lines = "\n".join(
            f"[{i}] {time.strftime('%H:%M', time.localtime(r.utterance.t_start))} "
            f"{r.transcript.text}"
            for i, r in enumerate(results, 1)
        )
        listed = "\n".join(f"- {r.id}: {r.prompt}" for r in rules)
        reason_in = language_name(self.reason_language)
        user = (
            f"Rules (id: what to alert about):\n{listed}\n\n"
            f"Transcript lines from the radio station {station.name}, in "
            f"{language_name(station.language)}:\n{lines}\n\n"
            "Which lines report something a rule describes, as news or an instruction about "
            "now? Ignore songs, adverts, fiction, and events long past, unless the rule asks "
            "for them. Reply with a JSON list with one object per match, e.g. "
            f'[{{"rule": "{rules[0].id}", "line": 2, "reason": "a few words in {reason_in}"}}]'
            ", or [] when nothing matches."
        )
        return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]

    def _check(self, llm: Any, results: list[Any], rules: list[LlmRule]) -> None:
        if not hasattr(llm, "complete"):
            self.error = f"LLM rules need an LLM; this run translates with {llm.model_id}"
            return
        self.error = None
        t0 = time.perf_counter()
        reply = llm.complete(self.messages(results, rules), max_tokens=CHECK_TOKENS)
        self.last_ms = round((time.perf_counter() - t0) * 1000)
        self.checks += 1
        by_id = {r.id: r for r in rules}
        found: dict[int, list[Hit]] = {}
        for m in parse_matches(reply):
            rule = by_id.get(str(m.get("rule")))
            line = m.get("line")
            # A bool is an int to Python: `"line": true` must not count as line 1.
            numbered = isinstance(line, int) and not isinstance(line, bool)
            if rule is None or not numbered or not 1 <= line <= len(results):
                continue
            hits = found.setdefault(line, [])
            if any(h.rule_id == rule.id for h in hits):
                continue
            reason = str(m.get("reason") or "")[:200] or None
            hits.append(Hit(rule.id, rule.severity, [], [], [], kind="llm", reason=reason))
        event(log, "llm_rules", station=results[0].station.id, lines=len(results),
              rules=len(rules), matches=sum(len(h) for h in found.values()),
              ms=self.last_ms)  # fmt: skip
        for line, hits in sorted(found.items()):
            self.matches += len(hits)
            said = results[line - 1]
            for h in hits:
                event(log, "rule_hit", station=said.station.id, rule=h.rule_id,
                      severity=h.severity, kind="llm",
                      reason=h.reason if loggable(said.station) else None,
                      text=logged_text(said.station, said.transcript.text))  # fmt: skip
            self._on_hits(results[line - 1], hits)

    # -- verifying keyword hits -------------------------------------------------------------

    def verify(self, result: Any, hit: Hit, rule: KeywordRule) -> None:
        """Ask whether a keyword hit is real; on yes, hand back the hit at its own severity,
        marked as checked. On no, the hit is dropped.

        A rule must never go silent because its check couldn't run. With no LLM, a full queue,
        a failure, an unreadable reply or no verdict within ``verify_timeout_s``, the hit is
        handed back marked as not checked, its reason saying why, as the rule's ``unverified``
        says: ``lower``, one severity lower; ``announce``, at its own; ``hold``, not until the
        check answers, which is asked again (``_tick_held``), and after ``hold_give_up_s``
        one severity lower."""
        self._ask(_Verify(result, hit, rule, self._clock()))

    def _ask(self, v: _Verify) -> None:
        with self._verify_lock:
            if v.done:
                return
            v.tries += 1
            n = v.tries
            v.retry_at = None
            timer = threading.Timer(
                self.verify_timeout_s, self._unanswered, (v, n, "timeout", "it was busy")
            )
            timer.daemon = True
            v.timer = timer

        def run(llm: Any) -> None:
            if v.done:  # an earlier try answered meanwhile, or the hit was raised without
                return
            try:
                self._judge(llm, v, n)
            except Exception as e:
                log.exception("verifying a keyword hit failed")
                self._unanswered(v, n, "failed", f"the check failed ({type(e).__name__})")

        timer.start()
        if not self._submit(run, "verify", VERIFY_PRIORITY, about=(v.rule.id,)):
            if self._has_llm():
                self._unanswered(v, n, "queue_full", "its queue is full")
            else:
                self._unanswered(v, n, "no_model", "no LLM is loaded")

    def _judge(self, llm: Any, v: _Verify, n: int) -> None:
        result, hit, rule = v.result, v.hit, v.rule
        if not hasattr(llm, "complete"):
            self.error = f"LLM checks need an LLM; this run translates with {llm.model_id}"
            self._unanswered(v, n, "no_model", f"{llm.model_id} can't judge")
            return
        reason_in = language_name(self.reason_language)
        user = (
            f"A keyword alert rule called '{rule.id}' matched the words "
            f"{', '.join(hit.terms)} in this line from the radio station "
            f"{result.station.name} ({language_name(result.station.language)}):\n"
            f"{result.transcript.text}\n\n"
            "Is the line reporting a real event of that kind, happening now or announced "
            "(not another meaning of the word, sport, a song, an advert, or history)? "
            f'Reply with JSON: {{"real": true or false, "reason": "a few words in '
            f'{reason_in}"}}'
        )
        reply = llm.complete(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            max_tokens=VERIFY_TOKENS,
        )
        verdict = next(iter(parse_matches(reply)), {})
        real = verdict.get("real")
        if not isinstance(real, bool):
            self._unanswered(v, n, "failed", "its answer couldn't be read")
            return
        event(log, "rule_verified", station=result.station.id, rule=rule.id, real=real,
              reason=str(verdict.get("reason", ""))[:120])  # fmt: skip
        with self._verify_lock:
            if v.done:  # raised without the verdict meanwhile: too late to change it
                return
            self._settle(v)
            self._verdicts["confirmed" if real else "rejected"] += 1
        if real:
            hit.reason = str(verdict.get("reason") or "")[:200] or None
            hit.checked = True
            self._on_hits(result, [hit])

    def _settle(self, v: _Verify) -> None:
        """The hit is being raised or dropped: nothing more happens to it (caller holds the
        lock)."""
        v.done = True
        if v.timer is not None:
            v.timer.cancel()
        if v in self._held:
            self._held.remove(v)

    def _unanswered(self, v: _Verify, n: int, why: str, words: str) -> None:
        """Try ``n`` of a check didn't answer (``why``: one of NOT_RUN). The timeout, a failure
        and a late verdict race: the first wins."""
        evicted = None
        with self._verify_lock:
            if v.done or n <= v.failed:
                return
            v.failed = n
            if v.timer is not None:
                v.timer.cancel()
            self._not_run[why] += 1
            v.why = words
            now = self._clock()
            hold = (
                v.rule.unverified == "hold"
                and now - v.since < self.hold_give_up_s
                and not self._closed.is_set()
            )
            if hold:
                v.retry_at = now + self.hold_retry_s * 2 ** (n - 1)
                if v not in self._held:
                    self._held.append(v)
                if len(self._held) > self.hold_max:
                    evicted = min(self._held, key=lambda h: h.since)
                    self._settle(evicted)
            else:
                self._settle(v)
        if hold and evicted is not v:
            event(log, "rule_held", level=logging.WARNING, station=v.result.station.id,
                  rule=v.rule.id, why=words, tries=n)  # fmt: skip
        if evicted is not None:
            self._raise_unchecked(evicted, f"{evicted.why}, and too many matches were waiting")
        if not hold:
            self._raise_unchecked(v, words)

    def _tick_held(self, now: float) -> None:
        """Ask again for the held hits that are due, and raise those held long enough."""
        with self._verify_lock:
            over = [v for v in self._held if now - v.since >= self.hold_give_up_s]
            for v in over:
                self._settle(v)
            again = [v for v in self._held if v.retry_at is not None and now >= v.retry_at]
            for v in again:
                v.retry_at = None
        for v in over:
            try:  # one that can't be raised must not end the timer for the others
                self._raise_unchecked(v, f"{v.why}, held for {round((now - v.since) / 60)} min")
            except Exception:
                log.exception("raising a held keyword hit failed")
        for v in again:
            try:
                self._ask(v)
            except Exception:
                log.exception("asking again about a held keyword hit failed")

    def _raise_unchecked(self, v: _Verify, words: str) -> None:
        """Hand back a hit nobody checked (settled by the caller): at its own severity when
        its rule says ``announce``, else one lower."""
        hit = v.hit
        own = hit.severity
        if v.rule.unverified != "announce":
            hit.severity = raise_severity(own, -1)
        hit.checked = False
        hit.reason = f"not checked by the LLM: {words}"
        if hit.severity != own:
            hit.reason += f"; raised as {hit.severity}, not {own}"
        with self._verify_lock:
            self.unchecked += 1
        event(log, "rule_unchecked", level=logging.WARNING, station=v.result.station.id,
              rule=v.rule.id, why=words, unverified=v.rule.unverified, severity=hit.severity,
              tries=v.tries)  # fmt: skip
        self._on_hits(v.result, [hit])

    def status(self) -> dict[str, Any]:
        with self._lock:
            waiting = {sid: len(p.results) for sid, p in self._pending.items() if p.results}
        with self._verify_lock:
            # Checks of keyword hits since the start: those the LLM answered, the tries it
            # didn't answer by why (a held hit counts once for each), and the hits held now.
            verify = {**self._verdicts, "not_run": dict(self._not_run), "held": len(self._held)}
        return {"checks": self.checks, "matches": self.matches, "error": self.error,
                "last_ms": self.last_ms, "waiting": waiting, "unchecked": self.unchecked,
                "verify": verify, "interval_s": self.interval_s}  # fmt: skip


_OBJECT = re.compile(r"\{[^{}]*\}")


def parse_matches(reply: str) -> list[dict[str, Any]]:
    """The JSON objects in a reply: a list, one object, or objects in prose around them."""
    text = reply.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    for candidate in (text, text[text.find("[") : text.rfind("]") + 1]):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
    out = []
    for m in _OBJECT.finditer(text):
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out
