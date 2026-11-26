# SPDX-License-Identifier: Apache-2.0
"""LLM rules: "alert me when they tell people to leave an area", checked by the LLM.

Each station's new source-language utterances are collected and read together, about once a
``interval_s`` or when enough text piles up, against every plain-language rule that applies to
the station. One request covers all of a station's rules, so the cost grows with the talk on
the air, not with the number of rules. The LLM sees the source text (never a translation, so
translation stays off the alert path), and its one-line reason travels with the alert.

The same model confirms keyword hits from rules marked ``verify``: a keyword alone can't tell a
water cut from a football team called "Água"; the LLM, reading the sentence, usually can.

Both run as jobs on the translation thread, ahead of station translations and questions.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from wavescribe.languages import language_name
from wavescribe.log import event
from wavescribe.radio.bandplan import loggable, logged_text
from wavescribe.rules import Hit, KeywordRule, LlmRule

log = logging.getLogger(__name__)

RULES_PRIORITY = 0.3  # alert translations are 0, questions 0.5, stations from 1
CHECK_TOKENS = 300
VERIFY_TOKENS = 120
# A keyword hit the LLM hasn't judged by then is announced unchecked, at its own severity.
VERIFY_TIMEOUT_S = 90.0

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


Submit = Callable[[Callable[[Any], None], str, float], bool]  # (run, name, priority) -> queued


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
        self.unchecked = 0  # keyword hits announced without the LLM's verdict
        self.verify_timeout_s = VERIFY_TIMEOUT_S
        self.last_ms: float | None = None
        self._closed = threading.Event()
        if timer:
            threading.Thread(target=self._tick_loop, daemon=True, name="llm-rules").start()

    def close(self) -> None:
        self._closed.set()

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

        if not self._submit(run, "llm-rules", RULES_PRIORITY):
            event(log, "drop", level=logging.WARNING, stage="llm-rules", station=station_id)

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
        """Ask whether a keyword hit is real; on yes, hand back the hit at its own severity.

        A rule must never go silent because its check couldn't run: with no LLM, a full queue,
        a failure, an unreadable reply or no verdict within ``verify_timeout_s``, the hit is
        announced anyway at its own severity, its reason saying it wasn't checked."""
        once = threading.Lock()  # the verdict, a failure and the timeout race: the first wins

        def settle(real: bool | None, reason: str | None) -> None:
            if not once.acquire(blocking=False):
                return
            timer.cancel()
            if real is False:
                return
            if real is None:
                self.unchecked += 1
                event(log, "rule_unchecked", level=logging.WARNING, station=result.station.id,
                      rule=rule.id, why=reason)  # fmt: skip
                reason = f"not checked by the LLM: {reason}"
            hit.reason = reason
            self._on_hits(result, [hit])

        timer = threading.Timer(self.verify_timeout_s, settle, (None, "it was busy"))
        timer.daemon = True

        def run(llm: Any) -> None:
            try:
                judge(llm)
            except Exception as e:
                log.exception("verifying a keyword hit failed")
                settle(None, f"the check failed ({type(e).__name__})")

        def judge(llm: Any) -> None:
            if not hasattr(llm, "complete"):
                self.error = f"LLM checks need an LLM; this run translates with {llm.model_id}"
                settle(None, f"{llm.model_id} can't judge")
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
                settle(None, "its answer couldn't be read")
                return
            event(log, "rule_verified", station=result.station.id, rule=rule.id, real=real,
                  reason=str(verdict.get("reason", ""))[:120])  # fmt: skip
            settle(real, str(verdict.get("reason") or "")[:200] or None)

        timer.start()
        if not self._submit(run, "verify", RULES_PRIORITY):
            settle(None, "no LLM is loaded, or it is too busy")

    def status(self) -> dict[str, Any]:
        with self._lock:
            waiting = {sid: len(p.results) for sid, p in self._pending.items() if p.results}
        return {"checks": self.checks, "matches": self.matches, "error": self.error,
                "last_ms": self.last_ms, "waiting": waiting, "unchecked": self.unchecked,
                "interval_s": self.interval_s}  # fmt: skip


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
