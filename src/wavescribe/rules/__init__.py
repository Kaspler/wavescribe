# SPDX-License-Identifier: Apache-2.0
"""Alert rules: evaluated on source-language text the moment ASR finishes, never on translations.

Keyword rules match topics from per-language packs (``config/keywords/<lang>.toml``), so a rule
like "water" works for every station whose language has a pack, or their own terms per language
(rules made on the page), or regular expressions. Adding a language is a pack file or a list of
terms, never code.

LLM rules are a plain-language description ("they tell people to leave an area"); the LLM reads
each station's recent source-language text against them (``rules/llm.py``).

Station rules are about a station rather than what it says: it went silent (``SilenceRule``), or
an FM station raised its RDS traffic-announcement flag (``TrafficRule``). They read the
pipeline's station status, in ``alerts/watch.py``.
"""

from __future__ import annotations

import json
import logging
import re
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from wavescribe.languages import primary_subtag
from wavescribe.rules.text import Matcher, compile_pattern

log = logging.getLogger(__name__)

SEVERITIES = ("low", "medium", "high", "critical")
# Python's re has no timeout, so what a pattern can cost is bounded instead: its length, how
# many a rule has, nested repeats refused at load, and only one utterance's text read at a time.
MAX_REGEX_CHARS = 200
MAX_REGEXES = 20  # per rule
REGEX_TEXT_CHARS = 2000  # more than any utterance the ASR checks keep (35 chars/s, ~30 s)
SLOW_REGEX_MS = 50  # a pattern this slow on one utterance is named in the log, once
STATION_KINDS = ("silence", "traffic")  # rules about a station, not about what it says


def raise_severity(severity: str, steps: int = 1) -> str:
    i = min(len(SEVERITIES) - 1, max(0, SEVERITIES.index(severity) + steps))
    return SEVERITIES[i]


def config_file(path: Path) -> Path | None:
    """The real config file, else its tracked ``.example`` twin, else None."""
    if path.exists():
        return path
    example = path.with_name(path.stem + ".example" + path.suffix)
    return example if example.exists() else None


def _strings(value: Any, what: str) -> tuple[str, ...]:
    """A list of strings from a rule or a keyword pack. A bare string is refused rather than
    read letter by letter: ``terms = { pt = "fogo" }`` would match "o", which is in most
    Portuguese sentences, and ``stations = "tsf"`` would match no station at all."""
    if value is None:
        return ()
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) for v in value):
        example = [value] if isinstance(value, str) else ["one", "another"]
        raise ValueError(
            f"{what} must be a list of strings, e.g. {json.dumps(example, ensure_ascii=False)}"
        )
    return tuple(value)


def _groups(value: Any, what: str, example: str) -> dict[str, tuple[str, ...]]:
    """A table of named lists of strings: a pack's topics or places, a rule's terms."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a table of lists, e.g. {example}")
    return {str(k): _strings(v, f"{what}.{k}") for k, v in value.items()}


@dataclass(frozen=True)
class KeywordPack:
    language: str
    topics: dict[str, tuple[str, ...]]
    places: dict[str, tuple[str, ...]]

    @classmethod
    def load(cls, path: Path) -> KeywordPack:
        raw = tomllib.loads(path.read_text())
        if not isinstance(raw.get("language"), str):
            raise ValueError(f'{path}: needs its language, e.g. language = "pt"')
        return cls(
            language=raw["language"],
            topics=_groups(raw.get("topics"), f"{path}: topics", 'water = ["corte de água"]'),
            places=_groups(raw.get("places"), f"{path}: places", 'local = ["Vila Nova"]'),
        )


@dataclass(frozen=True)
class KeywordRule:
    id: str
    topics: tuple[str, ...]
    severity: str
    near: tuple[str, ...] = ()  # place groups that raise the severity one level
    # Below this many mentions within the cooldown the alert stays low (stored, not announced),
    # unless a place from `near` is named: a real event is repeated, a passing reference isn't.
    min_mentions: int = 1
    terms: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # language -> own terms
    stations: tuple[str, ...] = ()  # only these stations; empty means all
    verify: bool = False  # the LLM confirms a hit before it is announced above low
    origin: str = "file"  # "file" (alerts.toml) or "page"
    # Regular expressions, compiled at load, matched on the source text ignoring case, whatever
    # the station's language (`stations` narrows them down).
    regex: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True)
class LlmRule:
    """A rule in plain language, checked by the LLM on source-language text."""

    id: str
    prompt: str
    severity: str
    stations: tuple[str, ...] = ()
    min_mentions: int = 1
    origin: str = "page"


@dataclass(frozen=True)
class SilenceRule:
    """A running station delivered no audio, or only dead air, for ``after_s`` seconds."""

    id: str
    severity: str
    stations: tuple[str, ...] = ()
    after_s: float = 300.0
    level_db: float = -50.0  # dead air: every audio level read stays below this (dBFS)
    origin: str = "file"
    kind: ClassVar[str] = "silence"


@dataclass(frozen=True)
class TrafficRule:
    """An FM station's RDS says a traffic announcement is on (TP and TA set)."""

    id: str
    severity: str
    stations: tuple[str, ...] = ()
    origin: str = "file"
    kind: ClassVar[str] = "traffic"


StationRule = SilenceRule | TrafficRule


@dataclass
class Hit:
    """One rule matching one utterance; the evidence travels with the verdict."""

    rule_id: str
    severity: str
    terms: list[str]  # pack terms that matched, as written in the pack; a regex's matches
    places: list[str]
    spans: list[tuple[int, int]]  # character spans in the source text
    capped: str | None = None  # why the severity was capped, e.g. "lang:en", "unverified"
    kind: str = "keyword"  # or "llm", "silence", "traffic"
    reason: str | None = None  # the LLM's one-line reason (LLM rules and verified hits)
    again: bool = False  # the LLM confirmed an earlier hit on the same utterance

    def payload(self) -> dict[str, Any]:
        return {"rule": self.rule_id, "severity": self.severity, "spans": self.spans}


_RULE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def _regexes(value: Any, rid: str) -> tuple[re.Pattern[str], ...]:
    """A rule's ``regex`` list, compiled now so a bad pattern fails at load, naming the rule."""
    patterns = [p for p in _strings(value, f"rule {rid}: regex") if p.strip()]
    if len(patterns) > MAX_REGEXES:
        raise ValueError(f"rule {rid}: at most {MAX_REGEXES} regular expressions in one rule")
    compiled = []
    for p in patterns:
        try:
            compiled.append(compile_pattern(p, MAX_REGEX_CHARS))
        except ValueError as e:
            shown = p if len(p) <= 40 else p[:40] + "…"
            raise ValueError(f"rule {rid}: regex {shown!r} {e}") from e
    return tuple(compiled)


def _number(raw: Mapping[str, Any], key: str, default: float, rid: str, low: float,
            high: float) -> float:  # fmt: skip
    value = raw.get(key, default)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"rule {rid}: {key} must be a number, e.g. {default:g}")
    if not low <= value <= high:
        raise ValueError(f"rule {rid}: {key} must be from {low:g} to {high:g}")
    return float(value)


def parse_rule(
    raw: Mapping[str, Any], origin: str = "file"
) -> KeywordRule | LlmRule | SilenceRule | TrafficRule:
    """One ``[[rule]]`` table, from alerts.toml or the page. Raises ValueError."""
    rid = str(raw.get("id", ""))
    if not _RULE_ID.fullmatch(rid):
        raise ValueError(f"rule {rid!r}: ids are lowercase letters, digits, - or _")
    severity = raw.get("severity", "medium")
    if severity not in SEVERITIES:
        raise ValueError(f"rule {rid}: severity must be one of {SEVERITIES}")
    min_mentions = raw.get("min_mentions", 1)
    if not isinstance(min_mentions, int) or isinstance(min_mentions, bool):
        raise ValueError(f"rule {rid}: min_mentions must be a whole number, e.g. 2")
    if min_mentions < 1:
        raise ValueError(f"rule {rid}: min_mentions must be at least 1")
    stations = _strings(raw.get("stations"), f"rule {rid}: stations")
    kind = raw.get("kind", "keyword")
    if kind == "llm":
        prompt = raw.get("prompt", "")
        if not isinstance(prompt, str):
            raise ValueError(f'rule {rid}: prompt must be text, e.g. "they tell people to leave"')
        if not prompt.strip():
            raise ValueError(f"rule {rid}: an LLM rule needs a prompt (what to listen for)")
        return LlmRule(rid, prompt.strip()[:500], severity, stations, min_mentions, origin)
    if kind == "silence":
        after = _number(raw, "after_minutes", 5, rid, 0.5, 24 * 60)
        level = _number(raw, "level_db", -50, rid, -120, 0)
        return SilenceRule(rid, severity, stations, after * 60, level, origin)
    if kind == "traffic":
        return TrafficRule(rid, severity, stations, origin)
    if kind != "keyword":
        raise ValueError(f"rule {rid}: kind is keyword, llm, silence or traffic")
    own = _groups(raw.get("terms") or {}, f"rule {rid}: terms", '{ pt = ["fogo", "incêndio"] }')
    terms: dict[str, tuple[str, ...]] = {}
    for lang, words in own.items():
        clean = tuple(w.strip() for w in words if w.strip())
        if clean:
            terms[primary_subtag(lang)] = clean
    topics = _strings(raw.get("topics"), f"rule {rid}: topics")
    regex = _regexes(raw.get("regex"), rid)
    if not topics and not terms and not regex:
        raise ValueError(f"rule {rid}: needs topics (from a keyword pack), terms or regex")
    verify = raw.get("verify", False)
    if not isinstance(verify, bool):
        raise ValueError(f"rule {rid}: verify must be true or false")
    near = _strings(raw.get("near"), f"rule {rid}: near")
    return KeywordRule(
        rid, topics, severity, near, min_mentions, terms, stations, verify, origin, regex
    )


@dataclass
class RuleSet:
    rules: list[KeywordRule]
    packs: dict[str, KeywordPack]  # primary subtag -> pack
    llm_rules: list[LlmRule] = field(default_factory=list)
    station_rules: list[StationRule] = field(default_factory=list)
    _matchers: dict[str, Matcher] = field(default_factory=dict)
    _slow: set[tuple[str, str]] = field(default_factory=set)  # (rule, pattern) named as slow

    @classmethod
    def load(
        cls,
        alerts: dict[str, Any],
        keywords_dir: Path,
        page_rules: list[Mapping[str, Any]] = (),  # type: ignore[assignment]
        disabled: tuple[str, ...] = (),
    ) -> RuleSet:
        rules: list[KeywordRule] = []
        llm: list[LlmRule] = []
        station: list[StationRule] = []
        page_ids = {str(r.get("id")) for r in page_rules}
        tables = [(r, "file") for r in alerts.get("rule", []) if r.get("id") not in page_ids]
        for raw, origin in [*tables, *((r, "page") for r in page_rules)]:
            if raw.get("kind", "keyword") not in ("keyword", "llm", *STATION_KINDS):
                log.info("rule %s: kind %r isn't implemented yet; skipped", raw.get("id"),
                         raw.get("kind"))  # fmt: skip
                continue
            rule = parse_rule(raw, origin)
            if rule.id in disabled:
                continue
            if isinstance(rule, LlmRule):
                llm.append(rule)
            elif isinstance(rule, KeywordRule):
                rules.append(rule)
            else:
                station.append(rule)
        packs = {}
        if keywords_dir.is_dir():
            for path in sorted(keywords_dir.glob("*.toml")):
                if path.name.endswith(".example.toml") and (
                    path.with_name(path.name.replace(".example", "")).exists()
                ):
                    continue  # the real pack wins over its example
                pack = KeywordPack.load(path)
                packs[primary_subtag(pack.language)] = pack
        return cls(rules, packs, llm, station)

    def pack_for(self, language: str) -> KeywordPack | None:
        return self.packs.get(primary_subtag(language))

    def get(self, rule_id: str) -> KeywordRule | LlmRule | StationRule | None:
        every = [*self.rules, *self.llm_rules, *self.station_rules]
        return next((r for r in every if r.id == rule_id), None)

    def evaluate(
        self, text: str, language: str, flags: tuple[str, ...] = (), station: str | None = None
    ) -> list[Hit]:
        pack = self.pack_for(language)
        own = primary_subtag(language)
        if not text or (pack is None and not any(own in r.terms or r.regex for r in self.rules)):
            return []
        topics = pack.topics if pack is not None else {}
        places = pack.places if pack is not None else {}
        matcher = self._matchers.setdefault(language, Matcher(language))
        tokens = matcher.tokens(text)
        cache: dict[str, list[tuple[int, int]]] = {}

        def find(term: str) -> list[tuple[int, int]]:
            if term not in cache:
                cache[term] = matcher.find(term, text, tokens)
            return cache[term]

        hits = []
        for rule in self.rules:
            if rule.stations and station is not None and station not in rule.stations:
                continue
            terms, spans = [], []
            candidates = [t for topic in rule.topics for t in topics.get(topic, ())]
            for term in [*candidates, *rule.terms.get(own, ())]:
                found = find(term)
                if found and term not in terms:
                    terms.append(term)
                    spans.extend(found)
            for pattern in rule.regex:
                for a, b in self._search(rule.id, pattern, text):
                    spans.append((a, b))
                    if text[a:b] not in terms:
                        terms.append(text[a:b])
            if not terms:
                continue
            named = [p for group in rule.near for p in places.get(group, ()) if find(p)]
            for p in named:
                spans.extend(find(p))
            severity = raise_severity(rule.severity) if named else rule.severity
            hit = Hit(rule.id, severity, terms, named, sorted(set(spans)))
            # Text heard as another language: Snowball stems Portuguese "sabotagem" and English
            # "sabotage" alike, and Parakeet sometimes answers in English. Log it, don't alarm.
            other = next((f for f in flags if f.startswith("lang:")), None)
            if other and SEVERITIES.index(hit.severity) > 0:
                hit.severity, hit.capped = "low", other
            hits.append(hit)
        return hits

    def _search(self, rule_id: str, pattern: re.Pattern[str], text: str) -> list[tuple[int, int]]:
        """Spans where a rule's regex matches one utterance (its first REGEX_TEXT_CHARS)."""
        t0 = time.perf_counter()
        matches = pattern.finditer(text, 0, REGEX_TEXT_CHARS)
        found = [m.span() for m in matches if m.end() > m.start()]  # zero-width says nothing
        ms = (time.perf_counter() - t0) * 1000
        if ms > SLOW_REGEX_MS and (rule_id, pattern.pattern) not in self._slow:
            self._slow.add((rule_id, pattern.pattern))
            log.warning("rule %s: regex %r took %.0f ms on one utterance; it runs on every "
                        "utterance, so make it simpler", rule_id, pattern.pattern, ms)  # fmt: skip
        return found
