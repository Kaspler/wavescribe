# SPDX-License-Identifier: Apache-2.0
"""Alerts: rule hits grouped into alerts, delivered as notifications and speech, kept as CAP.

Detection runs on source-language text the moment ASR finishes (``Alerter.on_transcript``).
Translation is only for the people reading or hearing the alert: delivery asks for it ahead of
the station backlog and waits at most ``translation_wait_s``, then goes out with whatever it has.
A translation that comes back in the wrong language is discarded, and a spoken alert without a
checked translation says the language's fixed fallback phrase instead, so nothing invented is
ever spoken and no alert is silent.

Alert fatigue: an alert is one incident. Repeats of its rule (on any station) fold into it as
mentions and are not announced again, unless the severity rises; once nobody has mentioned it for
``cooldown_minutes`` the incident is over, and the next mention is a new alert. While it goes on,
``remind_minutes`` (60 by default, 0 for never) after it was last announced a mention brings one
"still being mentioned" reminder: notified, logged and sent to webhooks, never spoken. One
utterance that trips several rules is announced once, by its most severe alert; the others are
marked ``covered``. Every event handed on (``on_alert``, ``on_delivered``) says what it is in
``change``: ``new``, ``escalated``, ``reminder`` and ``ended`` were announced; ``update`` is
anything else (a mention, a translation, a delivery outcome).

Delivery state follows bandwatch's lesson: an alert is marked delivered only once an action
confirmed it, and each action's outcome is stored next to it.

Delivery never depends on the database: every write to the store is best effort (``_keep``),
and an alert whose delivery could not run is marked failed rather than left pending, so the
rule's next mention announces it. One whose actions ran but reached nobody (notifications
switched off, a missing voice) is tried again at a mention ``RETRY_S`` later, then twice as
long each time, without sending its webhooks again: retrying at every mention announced one
story dozens of times.

Station alerts (a station gone silent, a traffic announcement; ``alerts/watch.py``) take the
same path with text worded by Wavescribe instead of words heard (``on_station``); a silence
says once when it is over (``station_back``).
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wavescribe.alerts.actions import ActionError, Actions, platform_actions
from wavescribe.asr.checks import wrong_language
from wavescribe.config import ConfigError
from wavescribe.languages import same_language
from wavescribe.log import event
from wavescribe.rules import SEVERITIES, STATION_KINDS, Hit, RuleSet

log = logging.getLogger(__name__)

DEFAULT_ALERTS_PATH = Path("config/alerts.toml")
EXAMPLE_ALERTS_PATH = Path("config/alerts.example.toml")
DEFAULT_KEYWORDS_DIR = Path("config/keywords")
DELIVERY_QUEUE_MAX = 50
# A delivery whose actions ran but reached nobody is tried again at a mention this long after,
# then twice as long each time, up to RETRY_MAX_S.
RETRY_S = 300.0
RETRY_MAX_S = 3600.0
# What an event about an alert is (``change``): announced (the first four) or not.
ANNOUNCED = ("new", "escalated", "reminder", "ended")
# Station alerts are worded by Wavescribe, in the language of its page; delivery translates
# them like a quote, for the notification and the voices.
STATION_TEXT_LANGUAGE = "en"

# translate(text, source_language, targets, callback(translations, model_id))
TranslateFn = Callable[[str, str, list[str], Callable[[dict[str, str], str | None], None]], None]


@dataclass(frozen=True)
class Voice:
    voice: str
    fallback: str  # spoken when no checked translation is available
    intro: str = ""  # spoken before a translation, e.g. "Attention. The radio reports:"
    summary: str = ""  # what a digest read aloud opens with (digest/speech.py)
    earlier: str = ""  # how it names the alerts of its window


@dataclass(frozen=True)
class AlertConfig:
    rules: RuleSet
    actions: dict[str, tuple[str, ...]]  # severity -> actions
    voices: dict[str, Voice]  # language -> voice
    notify_language: str | None = None
    notify_sound: str | None = None  # "" for silent notifications; None: the platform's sound
    translation_wait_s: float = 10.0
    cooldown_s: float = 900.0  # a quiet period this long ends an incident
    remind_s: float = 3600.0  # "still being mentioned" at most this often; 0: never
    path: Path | None = None

    def languages_for(self, severity: str, source: str, spoken: bool = True) -> list[str]:
        """Languages to translate an alert into before delivering it (``spoken``: with the
        voices' languages)."""
        actions = self.actions.get(severity, ())
        langs = [self.notify_language] if "notify" in actions and self.notify_language else []
        if spoken:
            langs += [a.split(":", 1)[1] for a in actions if a.startswith("speak:")]
        out: list[str] = []
        for lang in langs:
            if not same_language(lang, source) and lang not in out:
                out.append(lang)
        return out


def resolve_alerts_path(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit
    for path in (DEFAULT_ALERTS_PATH, EXAMPLE_ALERTS_PATH):
        if path.exists():
            return path
    return None


def read_alerts(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"{path}: not found") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e


def load_alerts(path: Path, keywords_dir: Path = DEFAULT_KEYWORDS_DIR) -> AlertConfig:
    return parse_alerts(read_alerts(path), path, keywords_dir)


def merge_page(raw: dict[str, Any], page: dict[str, Any]) -> tuple[dict[str, Any], list, tuple]:
    """alerts.toml with the page's changes (``[alerts]`` in config/ui.toml) laid over it: its
    delivery settings, actions and voices per key, its own rules, and file rules it switched
    off. Returns (raw, page rules, disabled rule ids)."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in raw.items()}
    for key in ("delivery", "actions", "speak"):
        if page.get(key):
            out[key] = {**out.get(key, {}), **page[key]}
    return out, list(page.get("rule", [])), tuple(page.get("disabled_rules", ()))


def parse_alerts(
    raw: dict[str, Any],
    path: Path | None,
    keywords_dir: Path,
    page_rules: list[dict[str, Any]] | None = None,
    disabled: tuple[str, ...] = (),
) -> AlertConfig:
    where = str(path or "alerts")
    unknown = set(raw) - {"delivery", "speak", "rule", "actions", "digest"}  # digest/schedule.py
    if unknown:
        raise ConfigError(f"{where}: unknown top-level keys {sorted(unknown)}")
    rule = raw.get("rule", [])
    if not isinstance(rule, list) or not all(isinstance(r, dict) for r in rule):
        raise ConfigError(f"{where}: rules are written as [[rule]], with double brackets, "
                          "one block for each")  # fmt: skip
    for key in ("delivery", "speak", "actions"):
        if not isinstance(raw.get(key, {}), dict):
            raise ConfigError(f"{where}: [{key}] must be a table")
    try:
        rules = RuleSet.load(raw, keywords_dir, page_rules or [], disabled)
    except (KeyError, ValueError) as e:
        raise ConfigError(f"{where}: {e}") from e
    voices = {}
    for lang, v in raw.get("speak", {}).items():
        if not isinstance(v, dict) or "voice" not in v or "fallback" not in v:
            raise ConfigError(f"{where} [speak.{lang}]: needs 'voice' and 'fallback'")
        voices[lang] = Voice(
            v["voice"],
            v["fallback"],
            v.get("intro", ""),
            v.get("summary", ""),
            v.get("earlier", ""),
        )
    actions: dict[str, tuple[str, ...]] = {}
    for severity, names in raw.get("actions", {}).items():
        if severity not in SEVERITIES:
            raise ConfigError(f"{where} [actions]: unknown severity {severity!r}")
        if not isinstance(names, list | tuple) or not all(isinstance(n, str) for n in names):
            raise ConfigError(f"{where} [actions]: {severity} must be a list of actions, "
                              'e.g. ["notify", "speak:en", "log"]')  # fmt: skip
        for name in names:
            kind, _, lang = name.partition(":")
            if kind == "speak" and lang not in voices:
                raise ConfigError(f"{where} [actions]: {name!r} needs a [speak.{lang}] voice")
            if kind not in ("notify", "speak", "log") or (kind == "speak") != bool(lang):
                raise ConfigError(f"{where} [actions]: unknown action {name!r}")
        actions[severity] = tuple(names)
    delivery = raw.get("delivery", {})
    sound = delivery.get("notify_sound")
    if sound is not None and not isinstance(sound, str):
        raise ConfigError(f"{where} [delivery]: notify_sound must be a sound's name, e.g. "
                          '"Glass", or "" for silent notifications')  # fmt: skip
    for r in rules.rules:
        if r.origin == "page":
            continue
        for topic in r.topics:
            if not any(topic in p.topics for p in rules.packs.values()):
                log.warning("rule %s: topic %r is in no keyword pack", r.id, topic)
    return AlertConfig(
        rules=rules,
        actions=actions,
        voices=voices,
        notify_language=delivery.get("notify_language"),
        notify_sound=sound,
        translation_wait_s=_amount(delivery, "translation_wait_s", 10, where),
        cooldown_s=_amount(delivery, "cooldown_minutes", 15, where) * 60,
        remind_s=_amount(delivery, "remind_minutes", 60, where) * 60,
        path=path,
    )


def _amount(delivery: dict[str, Any], key: str, default: float, where: str) -> float:
    """A [delivery] setting that is a length of time: a number, zero or more."""
    value = delivery.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value < 1e6:
        raise ConfigError(f"{where} [delivery]: {key} must be a number, e.g. {default}")
    return float(value)


@dataclass
class Alert:
    rule_id: str
    severity: str
    station_ids: list[str]
    language: str
    quote: str
    terms: list[str]
    first_at: float
    last_at: float
    segment_ids: list[int] = field(default_factory=list)
    id: int | None = None
    mentions: int = 1
    status: str = "pending"
    delivered_severity: str | None = None
    delivered_at: float | None = None
    actions: dict[str, Any] = field(default_factory=dict)  # action -> "ok"/failure; covered_by
    summaries: dict[str, str] = field(default_factory=dict)  # translations of the quote
    summaries_model: str | None = None
    last_text: str = ""  # the latest mention's text (announced when the severity rises)
    covered_severity: str | None = None  # announced by another alert of the same sentence
    short_lived: bool = False  # from radio whose class keeps text for days: not in the log
    # The privacy classes its stations had when they were heard. Kept with the alert: its words
    # are judged by them later, whatever those station ids mean by then (radio/bandplan.py).
    privacy: list[str] = field(default_factory=list)
    # min_mentions: the severity the hits ask for, and whether enough of them confirmed it
    wanted: str = "low"
    heard: int = 0  # mentions that count towards confirmation (not heard as another language)
    confirmed: bool = True
    kind: str = "keyword"  # or "llm", or a station rule's kind ("silence", "traffic")
    reason: str | None = None  # the LLM's reason, when an LLM rule or check was involved
    ended: bool = False  # a station alert whose cause is over (the station is back)
    languages: list[str] = field(default_factory=list)  # its stations' alert_languages
    # Deliveries (kept in memory only): the highest severity one ran at, whether or not it
    # reached anybody, and when; how many in a row reached nobody; and when it was last
    # announced, for reminders.
    tried_severity: str | None = None
    tried_at: float = 0.0
    unreached: int = 0
    announced_at: float | None = None

    def payload(self, change: str = "update") -> dict[str, Any]:
        """The alert as observers get it; ``change`` says what this event is (``ANNOUNCED``,
        or ``update``)."""
        return {
            "change": change,
            "id": self.id,
            "rule": self.rule_id,
            "severity": self.severity,
            "first_at": round(self.first_at, 2),
            "last_at": round(self.last_at, 2),
            "stations": list(self.station_ids),
            "language": self.language,
            "privacy": list(self.privacy),
            "quote": self.quote,
            "terms": list(self.terms),
            "mentions": self.mentions,
            "segment_id": self.segment_ids[0] if self.segment_ids else None,
            "status": self.status,
            "delivered_at": self.delivered_at,
            "delivered_severity": self.delivered_severity,
            "actions": dict(self.actions),
            "summaries": dict(self.summaries),
            "summaries_model": self.summaries_model,
            "kind": self.kind,
            "reason": self.reason,
        }


def _said(result: Any) -> str:
    """The words an alert may carry: none from private radio (docs/RADIO.md)."""
    from wavescribe.radio.bandplan import UNQUOTED, quotable

    return result.transcript.text if quotable(result.station) else UNQUOTED


def _reason(hit: Hit, station: Any) -> str | None:
    """The LLM's reason sums up what was said, so it goes only where a quote may."""
    from wavescribe.radio.bandplan import quotable

    return hit.reason if quotable(station) else None


def _short_lived(station: Any) -> bool:
    from wavescribe.radio.bandplan import policy_of

    policy = policy_of(station)
    return policy is not None and policy.text_days is not None


def _add_languages(alert: Alert, station: Any) -> None:
    """The languages a station wants its alerts in (``alert_languages``), added to an alert it
    is part of."""
    for lang in getattr(station, "alert_languages", ()):
        if lang not in alert.languages:
            alert.languages.append(lang)


def _rank(severity: str | None) -> int:
    return -1 if severity is None else SEVERITIES.index(severity)


class Alerter:
    """Pipeline observer: evaluates rules on each transcript and delivers alerts."""

    def __init__(
        self,
        config: AlertConfig,
        *,
        store: Any = None,  # wavescribe.store.Store
        actions: Actions | None = None,
        station_names: dict[str, str] | None = None,
        on_alert: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.store = store
        self.actions = actions if actions is not None else platform_actions()
        self.station_names = station_names or {}
        self.on_alert = on_alert
        self.translate: TranslateFn | None = None
        # Called once an alert went out, with (payload, severity, text, checked translations):
        # webhooks and feed pushes (wavescribe.outputs).
        self.on_delivered: Callable[[dict[str, Any], str, str, dict[str, str]], None] | None = None
        # verify(result, hit, rule): ask the LLM whether a keyword hit is real; it answers by
        # calling on_hits with a confirmed copy (wavescribe.rules.llm).
        self.verify: Callable[[Any, Hit, Any], None] | None = None
        self._clock = clock
        self._lock = threading.Lock()
        self._open: dict[str, Alert] = {}  # rule id -> its alert while inside the cooldown
        self._min_mentions = {
            r.id: r.min_mentions for r in [*config.rules.rules, *config.rules.llm_rules]
        }
        self._deliveries: queue.Queue[tuple | None] = queue.Queue(maxsize=DELIVERY_QUEUE_MAX)
        self._watch: Any = None  # alerts/watch.py, once ``watch`` starts it
        self._voices = self.actions.voices()
        for lang, v in config.voices.items():
            if self._voices is not None and v.voice not in self._voices:
                log.warning("voice %r for %s is not installed; spoken %s alerts will fail "
                            "(System Settings > Accessibility > Spoken Content)", v.voice, lang,
                            lang)  # fmt: skip
        self._worker = threading.Thread(target=self._deliver_loop, daemon=True, name="alerts")
        self._worker.start()

    def reconfigure(self, config: AlertConfig) -> None:
        """New rules or delivery settings (from the page) from the next utterance on."""
        with self._lock:
            self.config = config
            every = [*config.rules.rules, *config.rules.llm_rules]
            self._min_mentions = {r.id: r.min_mentions for r in every}

    def attach(self, translate: TranslateFn | None) -> None:
        """Give the alerter a way to translate (``Pipeline.request_translation``)."""
        self.translate = translate

    def watch(
        self,
        status: Callable[[], dict[str, Any]],
        stations: Callable[[], Any],
        interval_s: float | None = None,
    ) -> Any:  # -> StationWatch
        """Check the station rules every few seconds on ``status()``, the pipeline's status
        snapshot (``Pipeline.status``); ``stations()`` lists the configured stations."""
        from wavescribe.alerts.watch import INTERVAL_S, StationWatch

        self._watch = StationWatch(self, status, stations, interval_s=interval_s or INTERVAL_S,
                                   clock=self._clock)  # fmt: skip
        self._watch.start()
        return self._watch

    def close(self, timeout: float = 5.0) -> None:
        if self._watch is not None:
            self._watch.close()
        try:
            self._deliveries.put(None, timeout=timeout)
        except queue.Full:
            return
        self._worker.join(timeout)

    # -- detection (ASR thread) ---------------------------------------------------------------

    def on_transcript(self, result: Any) -> None:  # wavescribe.pipeline.Result
        text = result.transcript.text
        # A line heard in another language that Whisper is about to hear again: the rules read
        # it once, when it comes back through on_rechecked (asr/recheck.py). Until then it
        # could raise only a low alert, which is never announced.
        if not text or getattr(result, "rechecking", False):
            return
        rules = self.config.rules
        hits = rules.evaluate(text, result.station.language, result.flags, result.station.id)
        result.hits = hits
        to_verify = []
        for hit in hits:
            rule = rules.get(hit.rule_id)
            verify = getattr(rule, "verify", False) and self.verify is not None
            if verify and hit.capped is None and _rank(hit.severity) > 0:
                to_verify.append((dataclasses.replace(hit, again=True), rule))
                hit.severity, hit.capped = "low", "unverified"
        self._handle(result, hits, _said(result))
        for confirmed, rule in to_verify:
            try:
                self.verify(result, confirmed, rule)  # type: ignore[misc]
            except Exception:
                log.exception("verification request failed")

    def on_filtered(self, result: Any, reason: str) -> None:
        """An utterance the ASR checks dropped; it is not stored, shown or translated.

        Only a repetition loop is read here: that drop is a judgement, and a loop of a rule's
        own word ("evacuate, evacuate, evacuate, evacuate") is what a decoder invents over
        music as well as what a loudspeaker van says. Its keyword hits are kept as a low
        alert: on record with the words heard, never announced, and never counted towards
        ``min_mentions``. A real warning is repeated in a sentence that isn't a loop.
        """
        if reason != "repetition" or not result.transcript.text:
            return
        station = result.station
        hits = self.config.rules.evaluate(result.transcript.text, station.language, (), station.id)
        for hit in hits:
            hit.severity, hit.capped = "low", reason
        result.hits = hits
        self._handle(result, hits, _said(result))

    def on_hits(self, result: Any, hits: list[Hit]) -> None:
        """Hits found after the fact: LLM rules, or keyword hits the LLM confirmed."""
        if hits:
            result.hits = [*result.hits, *hits]
            self._handle(result, hits, _said(result))

    def _handle(self, result: Any, hits: list[Hit], text: str) -> None:
        from wavescribe.radio.bandplan import logged_text

        now: list[Alert] = []  # to announce
        later: list[Alert] = []  # got more serious while a delivery is in flight
        remind: list[Alert] = []  # still being mentioned, long after it was announced
        for hit in hits:
            event(log, "rule_hit", station=result.station.id, rule=hit.rule_id,
                  severity=hit.severity, terms=",".join(hit.terms), capped=hit.capped,
                  text=logged_text(result.station, text))  # fmt: skip
            alert, wants = self._add(hit, result)
            {"now": now, "later": later, "remind": remind}.get(wants, []).append(alert)
        if not now and not later:
            # One reminder for a sentence, by its most severe alert; with an announcement, none.
            if remind:
                lead = max(remind, key=lambda a: _rank(a.severity))
                self._dispatch(lead, lead.severity, text, kind="reminder")
            return
        # One sentence, one announcement: the most severe alert speaks for the others.
        lead = max(now + later, key=lambda a: _rank(a.severity))
        covered_by = lead.id if lead.id is not None else lead.rule_id  # the lead's id, if stored
        for other in now + later:
            if other is lead:
                continue
            with self._lock:
                if other in later:  # its in-flight delivery must not announce it again
                    other.covered_severity = other.severity
                    continue
                other.status = "covered"
                other.delivered_severity = other.severity
                other.announced_at = self._clock()
                other.actions = {"covered_by": covered_by}
            self._keep("update_delivery", other)
            self._publish(other)
        if lead in now:  # else its in-flight delivery announces it when it finishes
            self._dispatch(lead, lead.severity, text, [o.rule_id for o in now if o is not lead])

    def _add(self, hit: Hit, result: Any) -> tuple[Alert, str | None]:
        """Fold a hit into its rule's open alert or open a new one.

        Returns the alert and whether it wants announcing: "now", "later" (it got more serious
        while a delivery is in flight, which re-announces it when done), "remind" (still being
        mentioned ``remind_s`` after it was announced), or None.
        """
        from wavescribe.radio.bandplan import class_of

        now = self._clock()
        station = result.station
        deliver = False
        wants = None
        with self._lock:
            alert = self._open.get(hit.rule_id)
            if alert is not None and now - alert.last_at <= self.config.cooldown_s:
                if not hit.again:  # a confirmation of an utterance already counted
                    alert.mentions += 1
                alert.last_at = now
                alert.last_text = _said(result)
                # The reason is shown beside the quote, so it stays the first one given. An
                # alert of a rule with `verify` opens before the LLM has answered and gets
                # its reason here, with the confirmation.
                if alert.reason is None:
                    alert.reason = _reason(hit, station)
                if station.id not in alert.station_ids:
                    alert.station_ids.append(station.id)
                if class_of(station) not in alert.privacy:
                    alert.privacy.append(class_of(station))
                # Words from radio kept only for days stay out of the log, whichever station
                # opened the alert.
                alert.short_lived = alert.short_lived or _short_lived(station)
                _add_languages(alert, station)
                alert.terms += [t for t in hit.terms if t not in alert.terms]
                if result.segment_id is not None:
                    alert.segment_ids.append(result.segment_id)
                self._confirm(alert, hit)
                target = alert.wanted if alert.confirmed else "low"
                if _rank(target) > _rank(alert.severity):
                    alert.severity = target
                # Announce again only when it got more serious than what was announced, or
                # when nobody was reached and it is time to try again.
                if _rank(alert.severity) > _rank(alert.delivered_severity):
                    if alert.status == "pending":
                        wants = "later"
                    elif self._may_try(alert, now):
                        deliver = True
                elif self._reminder_due(alert, now):
                    alert.announced_at = now  # one reminder, whatever else is said meanwhile
                    wants = "remind"
                self._keep("add_mention", alert, result.segment_id, hit)
            else:
                alert = Alert(
                    rule_id=hit.rule_id,
                    severity="low",
                    station_ids=[station.id],
                    language=station.language,
                    quote=_said(result),
                    terms=list(hit.terms),
                    first_at=now,
                    last_at=now,
                    segment_ids=[result.segment_id] if result.segment_id is not None else [],
                    confirmed=False,
                    kind=hit.kind,
                    reason=_reason(hit, station),
                    short_lived=_short_lived(station),
                    privacy=[class_of(station)],
                )
                _add_languages(alert, station)
                self._confirm(alert, hit)
                alert.severity = alert.wanted if alert.confirmed else "low"
                self._open[hit.rule_id] = alert
                self._keep("add_mention", alert, result.segment_id, hit)
                deliver = True
            if deliver:
                alert.status = "pending"
                wants = "now"
        self._publish(alert)
        return alert, wants

    def _may_try(self, alert: Alert, now: float) -> bool:
        """Whether a mention may announce an alert nobody was told about at its severity (caller
        holds the lock). At once when no delivery ran at that severity (it couldn't run, or the
        severity rose); after a delivery that reached nobody, only ``RETRY_S`` later, then twice
        as long each time: a notification that times out must not make every mention a new
        announcement."""
        if _rank(alert.severity) > _rank(alert.tried_severity):
            return True
        wait = min(RETRY_MAX_S, RETRY_S * 2 ** max(0, alert.unreached - 1))
        return now - alert.tried_at >= wait

    def _reminder_due(self, alert: Alert, now: float) -> bool:
        """An announced alert (above low) still being mentioned ``remind_s`` after it was last
        announced (caller holds the lock)."""
        return (
            self.config.remind_s > 0
            and alert.status != "pending"
            and alert.announced_at is not None
            and _rank(alert.delivered_severity) > 0
            and now - alert.announced_at >= self.config.remind_s
        )

    # -- station alerts (alerts/watch.py) -----------------------------------------------------

    def on_station(self, rule: Any, station: Any, text: str) -> Alert:
        """A station rule fired (``rules.StationRule``): one alert through the usual path, with
        ``text`` worded by Wavescribe and no words heard. While the rule's alert is open and
        within the cooldown, another station firing it folds in unannounced: when the network
        goes down every stream goes quiet at once, and that is one alert."""
        from wavescribe.radio.bandplan import class_of

        now = self._clock()
        hit = Hit(rule.id, rule.severity, [], [], [], kind=rule.kind)
        with self._lock:
            alert = self._open.get(rule.id)
            fold = (
                alert is not None
                and alert.kind == rule.kind
                and not alert.ended
                and now - alert.last_at <= self.config.cooldown_s
            )
            if fold:
                assert alert is not None
                alert.mentions += 1
                alert.last_at = now
                alert.last_text = text
                if station.id not in alert.station_ids:
                    alert.station_ids.append(station.id)
                if class_of(station) not in alert.privacy:
                    alert.privacy.append(class_of(station))
                _add_languages(alert, station)
            else:
                alert = Alert(
                    rule_id=rule.id,
                    severity=rule.severity,
                    station_ids=[station.id],
                    language=STATION_TEXT_LANGUAGE,
                    quote=text,
                    terms=[],
                    first_at=now,
                    last_at=now,
                    kind=rule.kind,
                    privacy=[class_of(station)],
                    wanted=rule.severity,
                    heard=1,
                )
                _add_languages(alert, station)
                self._open[rule.id] = alert
                alert.status = "pending"
            self._keep("add_mention", alert, None, hit)
        event(log, "station_alert", rule=rule.id, kind=rule.kind, station=station.id,
              folded=fold, text=text)  # fmt: skip
        self._publish(alert)
        if not fold:
            self._dispatch(alert, alert.severity, text)
        return alert

    def station_back(self, alert: Alert, text: str | None) -> None:
        """A station alert's cause is over. With ``text`` ("audio is back on …") it is said
        once, through the notification, log and webhooks but never spoken: nobody needs to stop
        what they are doing for it. Without, it ends quietly (the station was stopped). Either
        way the alert closes, so the next time is a new alert."""
        hit = Hit(alert.rule_id, alert.severity, [], [], [], kind=alert.kind)
        with self._lock:
            if alert.ended:
                return
            alert.ended = True
            if self._open.get(alert.rule_id) is alert:
                del self._open[alert.rule_id]
            if text is None:
                return
            alert.last_at = self._clock()
            alert.reason = text
            alert.status = "pending"
            self._keep("add_mention", alert, None, hit)
        event(log, "station_back", rule=alert.rule_id, alert=alert.id, text=text)
        self._publish(alert)
        self._dispatch(alert, alert.severity, text, kind="back")

    def _keep(self, write: str, alert: Alert, *args: Any) -> None:
        """Call the store's ``write(alert, *args)``, best effort. A full disk or a locked
        database costs the record, never the announcement. An alert the store hasn't taken yet
        (its first write failed) is added first, so it is on record once the store recovers."""
        if self.store is None:
            return
        try:
            if alert.id is None:
                self.store.add_alert(alert)
            getattr(self.store, write)(alert, *args)
        except Exception:
            log.exception("alert %s (%s): the store's %s failed", alert.id, alert.rule_id, write)

    def _confirm(self, alert: Alert, hit: Hit) -> None:
        """Count a hit towards its rule's min_mentions (caller holds the lock)."""
        if _rank(hit.severity) > _rank(alert.wanted):
            alert.wanted = hit.severity
        if hit.capped is not None:  # another language, a loop, unverified: never confirms
            return
        alert.heard += 1
        if alert.heard >= self._min_mentions.get(alert.rule_id, 1) or hit.places:
            alert.confirmed = True

    # -- delivery ---------------------------------------------------------------------------

    def _dispatch(
        self,
        alert: Alert,
        severity: str,
        text: str,
        covers: list[str] | None = None,
        kind: str = "announce",
    ) -> None:
        """Ask for translations of ``text``, then queue delivery when they arrive or the wait
        runs out. ``kind``: ``announce``; ``back``, a station alert saying it is over; or
        ``reminder``, still being mentioned (it says what was translated already, at once)."""
        back = kind == "back"
        langs = [] if kind == "reminder" else self.config.languages_for(
            severity, alert.language, spoken=not back)  # fmt: skip
        fired = threading.Event()
        lock = threading.Lock()

        def finish(translations: dict[str, str], model: str | None) -> None:
            with lock:
                late = fired.is_set()
                fired.set()
            if late:
                if translations and text == alert.quote:  # too late to announce; still shown
                    self._keep_summaries(alert, translations, model)
                return
            timer.cancel()
            try:
                item = (alert, severity, text, covers or [], translations, model, kind)
                self._deliveries.put_nowait(item)
            except queue.Full:
                event(log, "alert_dropped", level=logging.WARNING, alert=alert.id)
                if kind == "announce":
                    self._undelivered(alert)

        timer = threading.Timer(self.config.translation_wait_s, finish, args=({}, None))
        timer.daemon = True
        if not langs or self.translate is None:
            finish({}, None)
        else:
            timer.start()
            try:
                self.translate(text, alert.language, langs, finish)
            except Exception:
                log.exception("alert translation request failed")
                finish({}, None)
        if kind == "announce":  # queued behind the delivery's own translations
            self._translate_quote(alert, severity, skip=langs if text == alert.quote else [])

    def _translate_quote(self, alert: Alert, severity: str, skip: list[str]) -> None:
        """The quote in its stations' ``alert_languages``, once the alert is above low: for the
        page, CAP and webhooks, whoever is notified. Asked for after the delivery's own
        translations (``skip``), and never waited for."""
        from wavescribe.radio.bandplan import UNQUOTED

        if _rank(severity) < 1 or self.translate is None or alert.quote == UNQUOTED:
            return
        langs = [
            lang
            for lang in alert.languages
            if lang not in skip
            and lang not in alert.summaries
            and not same_language(lang, alert.language)
        ]
        if not langs:
            return

        def keep(translations: dict[str, str], model: str | None) -> None:
            if translations:
                self._keep_summaries(alert, translations, model)

        try:
            self.translate(alert.quote, alert.language, langs, keep)
        except Exception:
            log.exception("alert translation request failed")

    def _keep_summaries(
        self, alert: Alert, translations: dict[str, str], model: str | None, keep: bool = True
    ) -> dict[str, str]:
        """The translations that are really in their target language; kept as the alert's
        summaries when they translate its quote."""
        checked = {}
        for lang, text in translations.items():
            other = wrong_language(text, lang) if text.strip() else "empty"
            if other:
                event(log, "alert_translation_rejected", level=logging.WARNING, alert=alert.id,
                      lang=lang, heard_as=other,
                      text="(not logged)" if alert.short_lived else text[:120])  # fmt: skip
                continue
            checked[lang] = text.strip()
        if checked and keep:
            alert.summaries.update(checked)
            alert.summaries_model = model
            self._keep("add_alert_summaries", alert, checked, model or "unknown")
            self._publish(alert)
        return checked

    def _deliver_loop(self) -> None:
        while True:
            item = self._deliveries.get()
            if item is None:
                self._deliveries.task_done()
                return
            try:
                self._deliver(*item)
            except Exception:
                log.exception("alert delivery failed")
                self._undelivered(item[0])
            finally:
                self._deliveries.task_done()

    def _undelivered(self, alert: Alert) -> None:
        """A delivery that could not run. Nothing else would ever move the alert out of
        "pending", and a pending alert holds its rule's later mentions back; marked failed,
        the next mention announces it."""
        with self._lock:
            if alert.status != "pending":
                return
            alert.status = "failed"
        self._keep("update_delivery", alert)
        self._publish(alert)

    def drain(self) -> None:
        """Wait until every queued delivery is done (replays and tests)."""
        self._deliveries.join()

    def _deliver(
        self,
        alert: Alert,
        severity: str,
        text: str,
        covers: list[str],
        translations: dict[str, str],
        model: str | None,
        what: str = "announce",
    ) -> None:
        if what == "reminder":
            self._remind(alert, severity)
            return
        back = what == "back"
        with self._lock:
            if back:
                change = "ended"
            elif _rank(severity) > _rank(alert.tried_severity):
                change = "new" if alert.tried_severity is None else "escalated"
            else:
                change = "update"  # tried again: the last delivery reached nobody
        checked = self._keep_summaries(alert, translations, model, keep=text == alert.quote)
        names = [self.station_names.get(s, s) for s in alert.station_ids]
        outcomes: dict[str, str] = {}
        for action in self.config.actions.get(severity, ()):
            kind, _, lang = action.partition(":")
            if back and kind == "speak":
                continue
            try:
                if kind == "notify":
                    lang_ = self.config.notify_language
                    body = checked.get(lang_, "") if lang_ else ""
                    rules = alert.rule_id + (f" + {', '.join(covers)}" if covers else "")
                    label = "BACK" if back else severity.upper()
                    title = f"{label} · {rules} · {', '.join(names)}"
                    self.actions.notify(title, body or text, sound=self.config.notify_sound)
                elif kind == "speak":
                    self._speak(lang, alert, text, checked)
                elif kind == "log":
                    # The log is kept longer than such radio's text: its words stay out.
                    words = "(not logged: kept only for days)" if alert.short_lived else text
                    event(log, "alert", alert=alert.id,
                          rule=alert.rule_id, severity=severity,
                          covers=",".join(covers), stations=",".join(alert.station_ids),
                          text=words[:200])  # fmt: skip
                outcomes[action] = "ok"
            except Exception as e:  # ActionError, OSError, subprocess timeouts, ...
                outcomes[action] = f"failed: {e}"[:200]
                event(log, "alert_action_failed", level=logging.ERROR, alert=alert.id,
                      action=action, error=str(e)[:200])  # fmt: skip
        ok = [a for a, o in outcomes.items() if o == "ok"]
        reaching = [a for a in ok if a != "log"]  # log alone reaches nobody
        with self._lock:
            alert.actions.update(outcomes)
            if not outcomes or set(outcomes) == {"log"}:
                alert.status = "logged"
            elif len(ok) == len(outcomes):
                alert.status = "delivered"
            elif reaching:
                alert.status = "partial"
            else:
                alert.status = "failed"
            now = self._clock()
            if reaching:
                alert.delivered_at = now
                alert.delivered_severity = severity
            elif alert.status == "logged":
                alert.delivered_severity = severity
            if not back:
                if _rank(severity) > _rank(alert.tried_severity):
                    alert.tried_severity = severity
                alert.tried_at = now
                told = bool(reaching) or alert.status == "logged"
                alert.unreached = 0 if told else alert.unreached + 1
                if told:
                    alert.announced_at = now
        self._keep("update_delivery", alert)
        event(log, "alert_delivered", alert=alert.id, rule=alert.rule_id, severity=severity,
              status=alert.status, change=change, translated=",".join(checked))  # fmt: skip
        self._publish(alert, change)
        # Webhooks get each announcement once: not a try again of one that reached nobody here.
        if self.on_delivered is not None and change != "update":
            try:
                self.on_delivered(alert.payload(change), severity, text, checked)
            except Exception:
                log.exception("alert outputs failed")
        with self._lock:  # it got more serious while this delivery was in flight
            done = max(_rank(severity), _rank(alert.covered_severity))
            again = not back and _rank(alert.severity) > done
            if again:
                alert.status = "pending"
        if again:
            self._dispatch(alert, alert.severity, alert.last_text or alert.quote)

    def _remind(self, alert: Alert, severity: str) -> None:
        """Still being mentioned, long after it was announced: one notification, a log line
        and the webhooks, saying how often and since when, with what was translated already.
        Never spoken, and the alert's delivery state stays as its announcement left it."""
        names = [self.station_names.get(s, s) for s in alert.station_ids]
        lang = self.config.notify_language
        said = (alert.summaries.get(lang) if lang else None) or alert.quote
        since = time.strftime("%H:%M", time.localtime(alert.first_at))
        body = f"Mentioned {alert.mentions} times since {since}: {said}"
        failed = []
        for action in self.config.actions.get(severity, ()):
            try:
                if action == "notify":
                    title = f"STILL {severity.upper()} · {alert.rule_id} · {', '.join(names)}"
                    self.actions.notify(title, body, sound=self.config.notify_sound)
                elif action == "log":
                    words = "(not logged: kept only for days)" if alert.short_lived else said
                    event(log, "alert", alert=alert.id,
                          rule=alert.rule_id, severity=severity, change="reminder",
                          mentions=alert.mentions, stations=",".join(alert.station_ids),
                          text=words[:200])  # fmt: skip
            except Exception as e:  # as for an announcement: a failed action is logged
                failed.append(action)
                event(log, "alert_action_failed", level=logging.ERROR, alert=alert.id,
                      action=action, error=str(e)[:200])  # fmt: skip
        event(log, "alert_reminded", alert=alert.id, rule=alert.rule_id, severity=severity,
              mentions=alert.mentions, failed=",".join(failed))  # fmt: skip
        self._publish(alert, "reminder")
        if self.on_delivered is not None:
            try:
                self.on_delivered(alert.payload("reminder"), severity, alert.quote,
                                  dict(alert.summaries))  # fmt: skip
            except Exception:
                log.exception("alert outputs failed")

    def _speak(self, lang: str, alert: Alert, text: str, checked: dict[str, str]) -> None:
        voice = self.config.voices[lang]
        if self._voices is not None and voice.voice not in self._voices:
            raise ActionError(f"voice {voice.voice!r} is not installed")
        if same_language(lang, alert.language):
            said = text
        elif lang in checked and alert.kind in STATION_KINDS:
            said = checked[lang]  # the intro ("the radio reports:") is for words heard
        elif lang in checked:
            said = f"{voice.intro} {checked[lang]}".strip()
        else:
            said = voice.fallback
        self.actions.speak(voice.voice, said)

    def _publish(self, alert: Alert, change: str = "update") -> None:
        if self.on_alert is not None:
            try:
                self.on_alert(alert.payload(change))
            except Exception:
                log.exception("alert observer failed")
