# SPDX-License-Identifier: Apache-2.0
"""Ask the transcripts: a question in any language, answered from what the stations said.

    question ─► LLM: which stations, which time range, which search words
             ─► full-text search over transcripts and their translations (the store's FTS5)
             ─► LLM: a short answer in the question's language, citing each excerpt as [n]

Search words come in the translation languages as well as the stations' own: every utterance
of a translated station is stored in those languages too, so one set of words reaches stations
in any language. Questions with no topic ("what happened in the last hour?") take an evenly
spread sample of the time range instead of a search.

The LLM is the translation model, already loaded by the pipeline. A question runs on its thread
(``Pipeline.request_llm``): after alert translations, ahead of the continuous backlog, which
waits for the few seconds an answer takes. Alerts never wait for it: they don't use translation.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from wavescribe.events import local_time
from wavescribe.languages import language_name
from wavescribe.log import event

if TYPE_CHECKING:
    from wavescribe.config import Station
    from wavescribe.radio.bandplan import Exports
    from wavescribe.store import Store

log = logging.getLogger(__name__)

DEFAULT_HOURS = 24.0  # when the question names no time
MAX_EXCERPTS = 30  # best matches kept for a topic
OVERVIEW_EXCERPTS = 40  # spread over the range for a question with no topic
EXCERPT_CHARS = 360  # per utterance: one is rarely longer
# All excerpts together, about 3k tokens. Reading the prompt is most of an answer's time
# (about 400 tokens/s for Qwen3.5-9B on an M1 Max), so this is the main speed lever.
PROMPT_CHARS = 12_000
MAX_TERMS = 12
PLAN_TOKENS = 200  # one line of JSON; a model that rambles is cut off and parsed anyway
HISTORY_TURNS = 2  # earlier questions and answers kept for follow-ups ("and on TSF?")
TIME_FORMAT = "%Y-%m-%d %H:%M"

PLAN_SYSTEM = (
    "You turn a question about radio broadcasts into a search over their transcripts. "
    "Reply with one JSON object and nothing else."
)
ANSWER_SYSTEM = (
    "You answer questions about what radio stations broadcast, using only the numbered "
    "excerpts you are given. They come from automatic speech recognition and machine "
    "translation, so they may contain errors: say so when something is unclear. Answer in the "
    "same language as the question. Say when and on which station something was said, and cite "
    "the excerpts you use like [3] or [3, 7]. If the excerpts don't answer the question, say so "
    "in one sentence. Be concise: a few sentences or a short list."
)


class Llm(Protocol):
    model_id: str
    last_stats: dict[str, float]

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 512,
        on_text: Callable[[str], None] | None = None,
    ) -> str: ...


Emit = Callable[[str, dict[str, Any]], None]


@dataclass
class SearchPlan:
    stations: list[str]  # empty: every station
    since: float
    until: float
    terms: list[str] = field(default_factory=list)
    overview: bool = False
    widened: bool = False  # nothing in the range asked about, so the last DEFAULT_HOURS instead
    sampled: bool = False  # nothing matched the terms, so a sample of everything said instead

    def payload(self) -> dict[str, Any]:
        return {
            "stations": self.stations,
            "since": round(self.since),
            "until": round(self.until),
            "terms": self.terms,
            "overview": self.overview,
            "widened": self.widened,
            "sampled": self.sampled,
        }


class Asker:
    """Answers questions from the store with the pipeline's LLM."""

    def __init__(
        self,
        store: Store,
        stations: list[Station] | tuple[Station, ...],
        submit: Callable[[Callable[[Any], None]], bool],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.stations = {s.id: s for s in stations}
        self._submit = submit
        self._clock = clock

    def ask(self, question: str, history: list[tuple[str, str]], emit: Emit, *,
            exports: Exports | None = None) -> bool:  # fmt: skip
        """Queue a question. ``emit(event, data)`` is then called from the LLM's thread with
        "plan", "sources", "text" (pieces of the answer as they're written), and finally "done"
        or "error". False if the LLM can't take it (none loaded, or its queue is full).
        ``exports`` limits the answer to what may leave this machine (a viewer on another
        one): the stations it lists, and of what they said only what it lets go."""
        question = question.strip()[:1000]
        return self._submit(
            lambda llm: self._run(llm, question, history[-HISTORY_TURNS:], emit, exports)
        )

    # -- on the LLM's thread --------------------------------------------------------------------

    def _run(self, llm: Any, question: str, history: list[tuple[str, str]], emit: Emit,
             exports: Exports | None = None) -> None:  # fmt: skip
        if not hasattr(llm, "complete"):
            emit(
                "error", {"error": f"asking needs an LLM; this run translates with {llm.model_id}"}
            )
            return
        only = None if exports is None else set(exports.stations)
        try:
            t0 = time.perf_counter()
            plan = self.plan(llm, question, history, only)
            if only is not None:
                if not only:
                    emit("error", {"error": "nothing here can be asked about from another machine"})
                    return
                kept = [s for s in plan.stations if s in only]
                plan = replace(plan, stations=kept or sorted(only))
            t_plan = time.perf_counter()
            plan, excerpts = self.search(plan, exports)
            emit("plan", plan.payload())
            t_search = time.perf_counter()
            emit("sources", {"sources": [self._source(i + 1, u) for i, u in enumerate(excerpts)]})
            messages = self.answer_messages(question, history, excerpts, plan=plan)
            answer = llm.complete(
                messages, max_tokens=700, on_text=lambda text: emit("text", {"text": text})
            )
            t_end = time.perf_counter()
            timings = {
                "plan_ms": round((t_plan - t0) * 1000),
                "search_ms": round((t_search - t_plan) * 1000),
                "answer_ms": round((t_end - t_search) * 1000),
                "total_ms": round((t_end - t0) * 1000),
            }
            stats = dict(getattr(llm, "last_stats", {}) or {})
            event(
                log,
                "ask",
                excerpts=len(excerpts),
                terms=len(plan.terms),
                overview=plan.overview,
                answer_chars=len(answer),
                **timings,
                **stats,
            )
            emit("done", {"model": llm.model_id, **timings, **stats})
        except Exception as e:
            log.exception("ask failed")
            # The exception's text may name folders of this machine's user: only a question
            # asked here is told it; another machine is pointed at the log.
            why = f": {e}" if exports is None else "; the log on the Wavescribe machine says why"
            emit("error", {"error": f"the question couldn't be answered{why}"})

    def plan(self, llm: Llm, question: str, history: list[tuple[str, str]],
             only: set[str] | None = None) -> SearchPlan:  # fmt: skip
        now = self._clock()
        messages = self.plan_messages(question, history, now, only)
        reply = llm.complete(messages, max_tokens=PLAN_TOKENS)
        return self.parse_plan(reply, now)

    def plan_messages(
        self, question: str, history: list[tuple[str, str]], now: float,
        only: set[str] | None = None,
    ) -> list[dict[str, str]]:  # fmt: skip
        span = self.store.span()
        covered = f" Transcripts cover {_local(span[0])} to {_local(span[1])}." if span else ""
        heard = self.store.last_heard()
        shown = [s for s in self.stations.values() if only is None or s.id in only]
        lines = [
            f"- {s.id}: {s.name}, {language_name(s.language)}"
            + (f", last heard {_local(heard[s.id])}" if s.id in heard else ", nothing stored")
            for s in shown
        ]
        targets = sorted({t for s in shown for t in s.translation_targets})
        names = " and ".join(language_name(t) for t in targets)
        translated = f"Every transcript is also translated into {names}.\n" if names else ""
        in_langs = f"in {names}, plus the same words " if names else ""
        earlier = "".join(f"Earlier question: {q}\nEarlier answer: {a[:400]}\n" for q, a in history)
        weekday = datetime.fromtimestamp(now).strftime("%A")
        content = (
            f"Now: {weekday} {_local(now)} (local time).{covered}\n{_time_anchors(now)}\n"
            "Stations (id: name, language):\n" + "\n".join(lines) + "\n"
            f"{translated}\n{earlier}Question: {question}\n\n"
            'Reply with one line of JSON. Example, for "Did station x say anything about road '
            'closures this morning?" asked on 2026-05-02 at 11:00, station x being in Spanish:\n'
            '{"terms": ["road", "closure", "traffic", "carretera", "cortada", "tráfico"], '
            '"stations": ["x"], "from": "2026-05-02 06:00", "to": "2026-05-02 11:00", '
            '"overview": false}\n'
            "Rules:\n"
            "- stations: [] unless the question names stations, or a country or language (then "
            "its stations).\n"
            "- from, to: the time the question asks about. Yesterday is the whole previous day, "
            f"today is since 00:00. No time named: the last {DEFAULT_HOURS:g} hours.\n"
            f"- terms: 3 to 8 short search words for the topic, {in_langs}in the languages of "
            "the stations named. No generic words.\n"
            '- overview: true only when the question has no topic ("what was said?").'
        )
        return [{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": content}]

    def parse_plan(self, reply: str, now: float) -> SearchPlan:
        """The LLM's JSON, checked: unknown stations dropped, times clamped, sane defaults. A
        reply cut off mid-way still yields whatever fields it completed."""
        default = SearchPlan([], now - DEFAULT_HOURS * 3600, now + 60, overview=True)
        raw = _loose_json(reply)
        if not raw:
            log.info("ask: unreadable plan %r", reply[:200])
            return default
        stations = [s for s in _strings(raw.get("stations")) if s in self.stations]
        if len(stations) >= max(3, 0.8 * len(self.stations)):
            stations = []  # it listed (nearly) everything: that means all
        since = _parse_time(raw.get("from")) or default.since
        until = _parse_time(raw.get("to")) or now
        until = min(max(until, since), now) + 60  # "to now" includes the last minute
        if until - since < 60:
            since = until - 3600
        terms = list(dict.fromkeys(_strings(raw.get("terms"))))[:MAX_TERMS]
        overview = raw.get("overview") is True or not terms
        return SearchPlan(stations, since, until, terms, overview)

    def search(
        self, plan: SearchPlan, exports: Exports | None = None
    ) -> tuple[SearchPlan, list[dict[str, Any]]]:
        """The plan as asked, then fallbacks until something turns up: the same words over the
        last DEFAULT_HOURS (when the range was shorter), then a sample of everything said in the
        range, so the answer can say "nothing about X; meanwhile they talked about Y"."""
        tries = [plan]
        if plan.until - plan.since < DEFAULT_HOURS * 3600:
            tries.append(replace(plan, since=plan.until - DEFAULT_HOURS * 3600, widened=True))
        if not plan.overview:
            tries += [replace(p, overview=True, sampled=True) for p in list(tries)]
        for attempt in tries:
            if excerpts := self.retrieve(attempt, exports):
                return attempt, excerpts
        return plan, []

    def retrieve(self, plan: SearchPlan, exports: Exports | None = None) -> list[dict[str, Any]]:
        """Excerpts for the answer, oldest first, within the prompt budget."""
        kw = {
            "station_ids": plan.stations or None,
            "since": plan.since,
            "until": plan.until,
            "exports": exports,
        }
        if plan.overview:
            found = self.store.window(**kw, limit=OVERVIEW_EXCERPTS)
        else:
            found = self.store.find(plan.terms, **kw, limit=MAX_EXCERPTS)
        kept, used = [], 0
        for u in found:  # best first (find) or evenly spread (window): keep until the budget
            size = len(self._excerpt_text(u))
            if used + size > PROMPT_CHARS:
                break
            kept.append(u)
            used += size
        return sorted(kept, key=lambda u: u["t_start"])

    def answer_messages(
        self,
        question: str,
        history: list[tuple[str, str]],
        excerpts: list[dict[str, Any]],
        *,
        plan: SearchPlan | None = None,
    ) -> list[dict[str, str]]:
        blocks = [
            f"[{i + 1}] {self._name(u['station'])}, {_when(u['t_start'])}\n{self._excerpt_text(u)}"
            for i, u in enumerate(excerpts)
        ]
        body = "\n\n".join(blocks) if blocks else "(no excerpts matched)"
        notes = []
        if plan is not None and blocks:
            if plan.widened:
                notes.append(
                    "Nothing was found in the time the question asks about; these excerpts are "
                    f"from the last {DEFAULT_HOURS:g} hours. Say so."
                )
            if plan.sampled:
                notes.append(
                    f"None of these excerpts contain the search words ({', '.join(plan.terms)}): "
                    "they are a sample of everything said in that time. Say that nothing "
                    "specific was found, then answer from the sample if it helps."
                )
        if notes:
            body = "(" + " ".join(notes) + ")\n\n" + body
        messages = [{"role": "system", "content": ANSWER_SYSTEM}]
        for q, a in history:
            messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a[:600]}]
        messages.append({"role": "user", "content": f"Excerpts:\n\n{body}\n\nQuestion: {question}"})
        return messages

    def _excerpt_text(self, u: dict[str, Any]) -> str:
        # The translation alone when there is one: half the prompt, so twice as fast. Each
        # citation shows the original (and its audio) on the page.
        translation = next(iter(u["translations"].values()), None)
        if translation:
            return f"(translated from {language_name(u['language'])}) {_clip(translation)}"
        return _clip(u["text"])

    def _name(self, station_id: str) -> str:
        s = self.stations.get(station_id)
        return s.name if s else station_id

    def _source(self, n: int, u: dict[str, Any]) -> dict[str, Any]:
        return {"n": n, **u, "name": self._name(u["station"])}


def _loose_json(reply: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", reply, re.S)
    if m:
        try:
            raw = json.loads(m.group(0))
            if isinstance(raw, dict):
                return raw
        except json.JSONDecodeError:
            pass
    out: dict[str, Any] = {}  # field by field, for a reply that was cut off
    for key in ("stations", "terms"):
        if lm := re.search(rf'"{key}"\s*:\s*\[([^\]]*)', reply):
            out[key] = re.findall(r'"([^"]+)"', lm.group(1))
    for key in ("from", "to"):
        if sm := re.search(rf'"{key}"\s*:\s*"([^"]+)"', reply):
            out[key] = sm.group(1)
    if bm := re.search(r'"overview"\s*:\s*(true|false)', reply):
        out["overview"] = bm.group(1) == "true"
    return out


def _strings(value: object) -> list[str]:
    return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []


def _time_anchors(now: float) -> str:
    """Ready-made times for the model: small LLMs get clock arithmetic wrong ("the last half
    hour" became one minute)."""
    ago = ", ".join(
        f"{label} ago {_local(now - s)}"
        for label, s in (("15 min", 900), ("30 min", 1800), ("1 h", 3600), ("3 h", 10800),
                         ("6 h", 21600), ("12 h", 43200))
    )  # fmt: skip
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = datetime.fromtimestamp(today.timestamp() - 43200).replace(hour=0, minute=0)
    return (
        f"Times: {ago}. Today since {today.strftime(TIME_FORMAT)}. "
        f"Yesterday {yesterday.strftime(TIME_FORMAT)} to {today.strftime('%Y-%m-%d')} 00:00."
    )


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EXCERPT_CHARS else text[: EXCERPT_CHARS - 1] + "…"


def _local(t: float) -> str:
    return local_time(t, TIME_FORMAT)


def _when(t: float) -> str:
    return local_time(t, "%a %d %b %H:%M")


def _parse_time(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    for fmt in (TIME_FORMAT, "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip()[: len(fmt) + 2], fmt).timestamp()
        except ValueError:
            continue
    return None
