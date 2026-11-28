# SPDX-License-Identifier: Apache-2.0
"""``wavescribe serve`` as one object: the pipeline, store, alerts, outputs and page, and every
change the page makes to them while they run.

Each change follows one path: validate it against a draft of the settings, save it to
``config/ui.toml`` (``settings.py``), then apply it to the running parts. Nothing the page does
needs a restart, and the hand-edited TOML files are never rewritten.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import secrets
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from wavescribe.config import ConfigError, Station, StationsConfig, confirmed, transcribed_languages
from wavescribe.events import NODE_ID
from wavescribe.invocation import typed
from wavescribe.languages import language_name
from wavescribe.log import event
from wavescribe.outputs import OutputConfig, OutputError, Outputs
from wavescribe.pipeline import (
    NO_VAD,
    Pipeline,
    Plan,
    Result,
    StationStopping,
    fan_out,
    plan_available,
    plan_station,
)
from wavescribe.refusal import Refusal, said
from wavescribe.registry import ModelEntry, ModelError, ModelRegistry
from wavescribe.rules import RULE_KEYS, VIEW_KEYS, LlmRule, parse_rule
from wavescribe.settings import (
    OVERRIDE_KEYS,
    Settings,
    apply_radio_policies,
    effective_config,
    station_dict,
)
from wavescribe.translate import unsupported_targets

if TYPE_CHECKING:
    from wavescribe.alerts import Alerter
    from wavescribe.ask import Asker
    from wavescribe.remote import Heard, RemoteFollower
    from wavescribe.rules.llm import LlmRuleChecker
    from wavescribe.store import Store
    from wavescribe.web.server import WebHub

log = logging.getLogger(__name__)

TOKEN_SCOPES = ("read", "push")
TOKEN_NOTE = ("Send it as Authorization: Bearer <token>. Where a header can't be set (a browser's "
              "EventSource), get a one-time ticket from POST /feed/v1/ticket and use ?ticket=: "
              "a token in a URL (?token=) works, but ends up in logs (docs/FEED.md).")  # fmt: skip
# A place runs a handful of stations and a household collects a few places. Past these, a push
# token is filling ui.toml and the page with stations, by mistake or on purpose.
MAX_PUSH_STATIONS_PER_NODE = 25
MAX_PUSH_STATIONS = 100
MAX_BATCH_EVENTS = 500  # a batch holds a few seconds of utterances; the rest is dropped
PUSH_ID_CHARS = 40  # a pushed station's id here: <node>-<station>
PUSH_NODE_CHARS = 16  # what is kept of a long node name in it
LAN_URL_TTL_S = 600.0  # the LAN address in links is looked up again after this long
# Overrides kept even when the station comes out the same without them: what the user confirmed.
KEPT_OVERRIDES = {"acknowledge", "privacy_floor"}


def push_id(node: str, remote_id: str) -> str:
    """The id here of a station another machine pushes: ``<node>-<station>``. When that is too
    long, the node part is shortened first and a short hash of both names is added, so two
    long names never come out the same."""
    sid = f"{node}-{remote_id}"
    if len(sid) <= PUSH_ID_CHARS:
        return sid
    tag = hashlib.sha256(f"{node}\n{remote_id}".encode()).hexdigest()[:6]
    head = _push_node(node)
    return f"{head}-{remote_id[: PUSH_ID_CHARS - len(head) - len(tag) - 2].rstrip('-')}-{tag}"


def _push_node(node: str) -> str:
    return node[:PUSH_NODE_CHARS].rstrip("-")


# Why a station that is switched off doesn't run (``App._mark_unavailable``).
SWITCHED_OFF = "switched off on the page"
UNCONFIRMED = "switched off until you confirm you may receive it under the region chosen"
NO_SOURCE = "no SDR device or stream URL configured"


def _plays_meanwhile(reason: str) -> str:
    """Why a station that should be written down only plays: its models aren't downloaded."""
    if reason == NO_VAD:
        return ("Plays without text until the speech models are downloaded: Settings → Languages "
                f"and models, or `{typed('models fetch')}`.")  # fmt: skip
    return f"Plays without text until its speech model is downloaded: {reason}"


def _untranslated(mt_id: str | None, missing: list[str]) -> str:
    """Why some of a station's translations are missing. It still runs and alerts on its own
    words, and the other languages are translated."""
    names = ", ".join(_lang_name(t) for t in missing)
    return (f"Not translated into {names}: {mt_id} can't translate into "
            f"{'it' if len(missing) == 1 else 'them'}. Choose another translator for this "
            "station, or other translation languages.")  # fmt: skip


def _differing(over: dict[str, Any], s: Station, file: Station | None) -> list[str]:
    """The overridden keys that make the station differ from stations.toml. One saved before
    unchanged overrides were dropped, or one the file now says too, isn't a change; a key that
    isn't a plain station field (a device, a confirmation) always counts."""
    if file is None:
        return sorted(over)
    plain = {f.name for f in fields(Station)}
    return sorted(k for k in over if k not in plain or getattr(s, k) != getattr(file, k))


class ChangeError(Refusal):
    """A change from the page that can't be applied; the message is shown to the user, and a
    technical refusal has a ``code`` beside it for the page (wavescribe/refusal.py)."""


class NotFound(ChangeError):
    """What the change is about doesn't exist (a 404 on the page's routes)."""


# Digests on a schedule are delivered as alerts are: without alerts, there are none.
ALERTS_OFF = (
    "alerts are off for this run (started with --no-alerts), and digests on a schedule come "
    "with them"
)


def slug(text: str) -> str:
    """An id from a name: "Rádio Sol" -> "radio-sol"; names with no Latin letters -> "station"."""
    from wavescribe.rules.text import fold

    s = re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-")
    return s[:40].strip("-") or "station"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class App:
    def __init__(
        self,
        base: StationsConfig,
        settings: Settings,
        registry: ModelRegistry,
        *,
        translate: bool = True,
        asr_model: str | None = None,
        mt_model: str | None = None,
        use_gate: bool = True,
        store: Store | None = None,
        alerts_path: Path | None = None,
        keywords_dir: Path | None = None,
        alerts_enabled: bool = True,
        host: str = "127.0.0.1",
        port: int = 8765,
        start: list[str] | None = None,
        pipeline_kwargs: dict[str, Any] | None = None,
        outputs_transport: Any = None,
    ) -> None:
        from wavescribe.alerts import DEFAULT_KEYWORDS_DIR

        self.base = base
        self.settings = settings
        self.registry = registry
        self.translate = translate
        self.asr_model = asr_model
        self.mt_model = mt_model
        self.use_gate = use_gate
        self.store = store
        self.host, self.port = host, port
        self.alerts_path = alerts_path
        self.keywords_dir = keywords_dir or DEFAULT_KEYWORDS_DIR
        self._lock = threading.RLock()
        self.started_at = time.time()
        self._node_id = self._make_node_id()  # written once, at the first start

        for cap, ids in (settings.get("models") or {}).items():
            try:
                registry.set_chain(cap, [i for i in ids if _known(registry, i)])
            except ModelError as e:
                log.warning("ignoring the page's %s model order: %s", cap, e)
        self.config, self.config_errors = effective_config(base, settings.snapshot())
        self._pin_acknowledgements()
        apply_radio_policies(base, settings.snapshot())
        self.plan, missing = plan_available(
            registry, list(self.config.stations), translate,
            asr_model=asr_model, mt_model=mt_model, use_gate=use_gate,
        )  # fmt: skip
        # Stations that can't start, with the reason (no source, switched off).
        self.unavailable: dict[str, str] = {}
        # Stations to be written down whose models aren't downloaded yet: they play without
        # text meanwhile, and the reason says what to download. A new install starts so.
        self.listen_only: dict[str, str] = {sid: _plays_meanwhile(r) for sid, r in missing.items()}
        # Stations started listen-only for this run (the page's start with listen_only): the
        # saved mode is untouched, and the next start, or the next serve, goes back to it.
        self.listen_only_run: set[str] = set()
        # Translation languages a station's translator lacks (MADLAD has a fixed list): it
        # still runs and alerts, translated into the rest, and its reason names them.
        self.untranslated: dict[str, list[str]] = {}
        self._target_checks: dict[tuple[object, ...], list[str]] = {}
        self._check_translations(quiet=True)  # plan_available has logged them
        self._mark_unavailable()
        if store is not None:
            self._apply_retention(settings.get("retention") or {})
            for s in self.config.stations:
                store.add_station(s)
            # Only now is it known what is kept and for how long (the policies, the retention):
            # from here on the store prunes by itself, also while no station says anything.
            store.adopt_classes(self.config.stations)
            store.keep_pruned()
            from wavescribe.radio.watch import subscribe

            self._keep_catches = subscribe(store.add_catch)  # kept like the words (watch.py)

        from wavescribe.web.server import WebHub

        self.hub: WebHub = WebHub(self.config, self.plan, self.unavailable, store=store)
        self.hub.app = self
        self.outputs = Outputs(
            self._output_configs(),
            store=store,
            stations=lambda: {s.id: s for s in self.config.stations},
            node=self.node,
            via=self.via,
            **({"transport": outputs_transport} if outputs_transport else {}),
        )
        self.hub.final_hooks.append(self.outputs.on_utterance)

        from wavescribe.alerts.actions import platform_actions
        from wavescribe.digest.speech import Speech

        self.speech = Speech(platform_actions())  # alerts and digests take turns to speak
        self.alerter: Alerter | None = None
        if alerts_enabled:
            self.alerter = self._make_alerter()
        self.llm_rules: LlmRuleChecker | None = None

        starting = start if start is not None else self._autostart()
        starting = [s for s in starting if self._startable(s)]
        self.pipeline = Pipeline(
            [self._runtime(self.config.get(s)) for s in starting if not self.config.get(s).remote],
            self.plan,
            # The store runs first: it assigns the segment id the page's payloads carry. The
            # alerter runs next, so the page gets the utterance with its keyword highlights.
            fan_out(store and store.add_translations, self.hub.on_result),
            on_transcript=fan_out(
                # The store gives the segment id; the hub notes it in flight in the same step,
                # which the feed's resume points rely on (feed.Cursor).
                store and self.hub.store_transcript,
                self.alerter and self.alerter.on_transcript,
                self._llm_rules_transcript,
                self.hub.on_transcript,
            ),
            on_filtered=self.alerter and self.alerter.on_filtered,
            # A flagged line Whisper heard again (asr/recheck.py): the store and page take its
            # final text, and the rules read it now (they skipped it while it was rechecking).
            on_rechecked=fan_out(
                store and store.update_transcript,
                self.alerter and self.alerter.on_transcript,
                self._llm_rules_transcript,
                self.hub.on_transcript,
            ),
            on_audio=self.hub.on_audio,
            **(pipeline_kwargs or {}),
        )
        self.hub.attach(self.pipeline)
        if self.alerter is not None:
            from wavescribe.rules.llm import LlmRuleChecker

            self.alerter.attach(self.pipeline.request_translation)
            self.alerter.watch(self.pipeline.status, lambda: self.config.stations)
            self.alerter.on_alert = self.hub.on_alert
            self.alerter.on_delivered = self.outputs.on_alert
            self.llm_rules = LlmRuleChecker(
                self._llm_rule_list,
                self.pipeline.request_llm,
                self.alerter.on_hits,
                reason_language=self._primary_language(),
                has_llm=lambda: self.plan.mt is not None,
            )
            self.alerter.verify = self.llm_rules.verify
        self.asker: Asker | None = None
        if store is not None:
            from wavescribe.ask import Asker
            from wavescribe.digest import Digester, model_of
            from wavescribe.digest.speech import Speaker

            self.asker = Asker(store, list(self.config.stations), self.pipeline.request_llm)
            self.hub.asker = self.asker
            self.hub.digester = Digester(
                store,
                lambda: self.config.stations,
                running=self.running,
                started=self.started_at,
                music=self.pipeline.music_heard,
                **model_of(self.pipeline),
            )
            self.hub.speaker = Speaker(self.speech, self._alert_voices, self.hub._closed)
        self._digests_lock = threading.Lock()  # the page replaces the scheduler
        self._digests_running = False  # run() started them: a replacement starts at once
        self.digest_error: str | None = None  # why the page's schedule wasn't used at start
        try:
            self.digests = self._digest_schedule()
        except ConfigError as e:
            if not self._page_digest():
                raise  # alerts.toml's: it stops the start, as a broken file does
            # The page's, written by the program: a station it names may have gone since.
            log.warning("the page's digest schedule is not used: %s", e)
            self.digests, self.digest_error = None, said(e)
        from wavescribe.downloads import Downloads

        self.downloads = Downloads(registry, on_done=self.models_changed)
        self.followers: dict[str, RemoteFollower] = {}
        self._pushed: dict[str, float] = {}  # pushed station -> when it last sent something
        # A station from another machine -> the machines its last line passed through, so what
        # is passed on says so and never comes back (``via``). In memory: a restart learns it
        # again from the next line.
        self._upstream: dict[str, tuple[str, ...]] = {}
        self._lan_url: str | None = None
        self._lan_url_at = 0.0  # when to look it up again
        self._monitor: Any = None
        self._resources: deque[dict[str, Any]] = deque(maxlen=240)  # an hour at 15 s
        self._last_snapshot: dict[str, Any] | None = None
        self._starting_remote = [s for s in starting if self.config.get(s).remote]

    # -- lifecycle ----------------------------------------------------------------------------

    def run(self) -> None:
        """Run until Ctrl-C (the pipeline's loop)."""
        for sid in self._starting_remote:
            self._start_follower(self.config.get(sid))
        with self._digests_lock:
            self._digests_running = True
            if self.digests is not None:
                self.digests.start()
        try:
            self.pipeline.run(until_idle=False)
        finally:
            self.close()

    def close(self) -> None:
        self.downloads.close()  # a download runs as a child process: it mustn't outlive serve
        if getattr(self, "_live", None) is not None:
            self._live.close_all()
        if getattr(self, "_keep_catches", None) is not None:
            self._keep_catches()
        for f in list(self.followers.values()):
            f.close()
        if self.llm_rules is not None:
            self.llm_rules.close()
        self.outputs.close()
        with self._digests_lock:
            self._digests_running = False
            if self.digests is not None:
                self.digests.stop()  # a digest being made is dropped
        self.hub.close()
        self.speech.close()  # a digest being read aloud: its `say` mustn't outlive the app

    # -- node --------------------------------------------------------------------------------

    def node(self) -> dict[str, Any]:
        """This machine as other programs see it: its name and the URL links point at."""
        n = self.settings.get("node") or {}
        # Not the hostname: it often holds the owner's name, and events carry this to others.
        name = n.get("name") or "wavescribe"
        url = n.get("public_url")
        if not url:
            local = self.host in ("127.0.0.1", "::1", "localhost")
            url = f"http://localhost:{self.port}" if local else self._lan_address()
        return {"name": name, "url": url.rstrip("/") if url else None, "id": self.node_id()}

    def _lan_address(self) -> str | None:
        """Under ``serve --lan``, this machine's private address for the links in what it
        sends: not its hostname, which often holds the owner's name and goes wherever the
        events go. None when it has none; the public URL, when set, is used instead."""
        now = time.monotonic()
        if now >= self._lan_url_at:
            from wavescribe.web import server

            found = [u for u in server.lan_urls(self.port)
                     if not (urlsplit(u).hostname or "").endswith(".local")]  # fmt: skip
            self._lan_url = found[0].rstrip("/") if found else None
            self._lan_url_at = now + LAN_URL_TTL_S
        return self._lan_url

    def node_id(self) -> str:
        """This machine's id in the events it sends: random, made once and kept in ui.toml.
        Machines that pass lines on list the ids they passed through (``via``), so a line
        never comes back to one it passed through."""
        return self._node_id

    def _make_node_id(self) -> str:
        nid = (self.settings.get("node") or {}).get("id")
        if isinstance(nid, str) and NODE_ID.fullmatch(nid):
            return nid
        made = secrets.token_hex(6)
        self.settings.update(lambda d: d.setdefault("node", {}).update(id=made))
        return made

    def via(self, station_id: str) -> list[str]:
        """The machines a station's lines passed through before this one, the first that heard
        them first; empty for this machine's own stations."""
        station = next((s for s in self.config.stations if s.id == station_id), None)
        if station is None or not station.remote:
            return []
        follower = self.followers.get(station_id) if station.remote != "push" else None
        if follower is not None:
            return list(follower.upstream)
        return list(self._upstream.get(station_id, ()))

    def set_node(self, name: str | None, public_url: str | None) -> dict[str, Any]:
        if public_url and not re.match(r"^https?://[^\s/]+", public_url):
            raise ChangeError("the public URL starts with http:// or https://")
        self.settings.update(lambda d: d.setdefault("node", {}).update(
            name=(name or "").strip()[:60] or None, public_url=(public_url or "").strip() or None
        ))  # fmt: skip
        return self.node()

    def _primary_language(self) -> str:
        counts: dict[str, int] = {}
        for s in self.config.stations:
            for t in s.translate_to[:1]:
                counts[t] = counts.get(t, 0) + 1
        return max(counts, key=counts.get) if counts else "en"  # type: ignore[arg-type]

    # -- stations ----------------------------------------------------------------------------

    def _autostart(self) -> list[str]:
        run = self.settings.get("run") or {}
        return [s for s in run.get("stations", []) if any(x.id == s for x in self.config.stations)]

    def _startable(self, sid: str) -> bool:
        try:
            s = self.config.get(sid)
        except (KeyError, ValueError, ConfigError):
            return False
        return not s.disabled and sid not in self.unavailable

    def _runtime(self, s: Station) -> Station:
        """The station as the pipeline runs it: without text while its models are missing, or
        while it was started listen-only for this run."""
        off = s.id in self.listen_only or s.id in self.listen_only_run
        return replace(s, transcribe=False) if off else s

    def _mark_unavailable(self) -> None:
        """Why each station that can't start can't, worked out afresh from the config."""
        page = {s.get("id"): s for s in self.settings.get("station") or []}
        overrides = self.settings.get("overrides") or {}
        for s in self.config.stations:
            reason = None
            if s.disabled:
                # Off on the page, or (settings._build) by a region whose class wants a
                # confirmation; the error says which, and what to do.
                by_page = (page.get(s.id) or overrides.get(s.id) or {}).get("disabled")
                reason = SWITCHED_OFF if by_page else UNCONFIRMED
            elif s.out_of_reach:  # its receiver is tuned where it doesn't hear it
                reason = s.out_of_reach
            elif not (s.remote or s.sdr or s.stream or s.stream_hls):
                reason = NO_SOURCE
            if reason:
                self.unavailable[s.id] = reason
            else:
                self.unavailable.pop(s.id, None)
        for sid, err in self.config_errors.items():
            if any(s.id == sid for s in self.config.stations):
                log.warning("page change to %s not applied: %s", sid, err)

    @property
    def radio(self) -> Any:
        """Find → On the air and Listen → On the air here: scans, the heard catalogue, tuning
        (radio/service.py)."""
        if getattr(self, "_radio", None) is None:
            from wavescribe.radio.service import RadioService

            self._radio = RadioService(self)
        return self._radio

    @property
    def live(self) -> Any:
        """Listen → Tune by hand: receivers tuned by hand, never transcribed (radio/live.py)."""
        if getattr(self, "_live", None) is None:
            from wavescribe.radio.live import LiveService

            self._live = LiveService(self)
        return self._live

    def running(self) -> set[str]:
        ids = set(self.pipeline.status()["stations"])
        return ids | {sid for sid, f in self.followers.items() if not f.closed}

    def stations_view(self) -> list[dict[str, Any]]:
        from wavescribe.radio.service import reach_center_mhz

        page_ids = {s.get("id") for s in self.settings.get("station") or []}
        overrides = self.settings.get("overrides") or {}
        file = {s.id: s for s in self.base.stations}
        running = self.running()
        out = []
        for s in self.config.stations:
            planned = self.plan.asr.get(s.id)
            out.append({
                **station_dict(s),
                "languages": transcribed_languages(s),  # as /api/state says them
                "remote_token": bool(s.remote_token),
                "origin": "page" if s.id in page_ids else "file",
                # Changed here: what the page set that differs from stations.toml.
                "overridden": _differing(overrides.get(s.id, {}), s, file.get(s.id)),
                "running": s.id in running,
                "disabled": s.disabled,
                "available": s.id not in self.unavailable,
                "reason": self.unavailable.get(s.id) or self.listen_only.get(s.id) or (
                    _untranslated(self._translator_id(s), self.untranslated[s.id])
                    if s.id in self.untranslated else None),
                "listen_only": s.id in self.listen_only,
                "listen_only_run": s.id in self.listen_only_run,
                # Translation languages its translator lacks; it runs and translates the rest.
                "translate_to_missing": self.untranslated.get(s.id, []),
                "privacy": s.privacy,
                # Kept stricter after a change of region, until confirmed (radio/service.py).
                "privacy_region": s.privacy_region,
                # As /api/state says them: why its receiver doesn't hear it, and where it would.
                "out_of_reach": None if s.disabled else s.out_of_reach,
                "reach_center_mhz": reach_center_mhz(s, self.config.stations, running),
                "error": self.config_errors.get(s.id),
                "asr_model": planned[0].id if planned else None,
                "asr_choice": s.asr_model,
                "transcribe": s.transcribe,
                "translate": s.translate,
                "mt_model": self._translator_id(s),
                "mt_choice": s.mt_model,
                "language_name": _lang_name(s.language),
                "kind": "remote" if s.remote else "sdr" if s.sdr else "stream",
            })  # fmt: skip
        return out

    def defaults_view(self) -> dict[str, Any]:
        """The stations file's ``[defaults]`` an add form fills a new station from: its language
        (None when the file gives none) and translation languages."""
        return {"language": self.base.defaults.get("language"),
                "translate_to": list(self.base.defaults.get("translate_to", ()))}  # fmt: skip

    def _translator_id(self, s: Station) -> str | None:
        mt = self.plan.translator_for(s)
        return mt[0].id if mt else None

    def _check_translations(self, *, quiet: bool = False) -> None:
        """Find the translation languages each station's translator lacks, after anything that
        may change them (the station, its translator, the model order, a download). Never
        fatal: alerts run on the station's own words, and the rest is translated."""
        found: dict[str, list[str]] = {}
        for s in self.config.stations:
            mt = self.plan.translator_for(s) if s.translation_targets else None
            if mt is None:
                continue
            key = (mt[0].id, str(mt[1]), s.translation_targets)
            if key not in self._target_checks:  # MADLAD's list is read from its vocabulary
                self._target_checks[key] = unsupported_targets(*mt, s.translation_targets)
            if self._target_checks[key]:
                found[s.id] = self._target_checks[key]
                if not quiet and self.untranslated.get(s.id) != found[s.id]:
                    log.warning("station %s: %s", s.id, _untranslated(mt[0].id, found[s.id]))
        self.untranslated = found

    def add_station(self, raw: dict[str, Any], *, start: bool = False) -> dict[str, Any]:
        """A station from the page: a stream URL, a feed of another Wavescribe, or a device.
        One its receiver doesn't hear where that is tuned is refused (``_check_draft``): the
        page's route moves the receiver first (``RadioService.add_station``)."""
        raw = self.station_entry(raw)
        sid = str(raw["id"])
        self._check_draft(lambda d: d.setdefault("station", []).append(raw), sid)
        self.settings.update(lambda d: d.setdefault("station", []).append(raw))
        self._reload()
        if start:
            self.start_station(sid)
        return next(v for v in self.stations_view() if v["id"] == sid)

    def station_entry(self, raw: dict[str, Any]) -> dict[str, Any]:
        """A station from the page as it is saved: without its empty fields, with an id from its
        name and a name from its id where it lacks one; refused when either is taken."""
        raw = {k: v for k, v in raw.items() if v not in (None, "", [])}
        raw.setdefault("id", self._free_id(slug(str(raw.get("name") or raw.get("id") or ""))))
        sid = str(raw["id"])
        if any(s.id == sid for s in self.config.stations):
            raise ChangeError(f"there is already a station called {sid!r}")
        raw.setdefault("name", sid)
        self._check_name_free(str(raw["name"]))
        return raw

    def update_station(self, sid: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Change a station: page stations are edited in place, stations.toml ones get an
        override (``[overrides.<id>]``) and the file stays as it is."""
        self.config.get(sid)
        page = any(s.get("id") == sid for s in self.settings.get("station") or [])
        unknown = set(changes) - OVERRIDE_KEYS - ({"remote", "remote_station", "remote_token",
                                                    "mw_khz"} if page else set())  # fmt: skip
        if unknown:
            raise ChangeError(f"can't change {', '.join(sorted(unknown))} here")
        if changes.get("name"):
            self._check_name_free(str(changes["name"]), sid)
        if changes.get("acknowledge"):  # confirmed where it is: a class kept stricter goes too
            changes = {**changes, "privacy_floor": None}

        def mutate(d: dict[str, Any]) -> None:
            if page:
                entry = next(s for s in d["station"] if s.get("id") == sid)
                for k, v in changes.items():
                    if v is None:
                        entry.pop(k, None)
                    else:
                        entry[k] = v
            else:
                over = d.setdefault("overrides", {}).setdefault(sid, {})
                for k, v in changes.items():
                    if v is None:
                        over.pop(k, None)  # back to what stations.toml says
                    else:
                        over[k] = v
                self._drop_unchanged(d, sid, changes)

        heard = self._check_draft(mutate, sid)
        if changes.get("acknowledge") is True:  # saved as the classes confirmed, this one too
            changes["acknowledge"] = confirmed(self._acknowledgement(sid), heard.privacy)
        self._check_models(mutate, sid, changes)
        self.settings.update(mutate)
        self._reload()
        return next(v for v in self.stations_view() if v["id"] == sid)

    def _drop_unchanged(self, d: dict[str, Any], sid: str, keys: Iterable[str]) -> None:
        """Forget the overrides among ``keys`` that leave the station as stations.toml has it
        (a language added on the page and removed again, a form that sends every field): only
        what really differs marks it "changed here". Confirmations are always kept, and a draft
        the station refuses is left for ``_check_draft`` to report."""
        overrides = d.get("overrides", {})
        over = overrides.get(sid)
        if not over:
            return
        config, errors = effective_config(self.base, d)
        if sid in errors:
            return
        wanted = config.get(sid)
        for k in [k for k in keys if k in over and k not in KEPT_OVERRIDES]:
            without = {kk: vv for kk, vv in over.items() if kk != k}
            trial = {**d, "overrides": {**overrides, sid: without}}
            if effective_config(self.base, trial)[0].get(sid) == wanted:
                over.pop(k)
        if not over:
            overrides.pop(sid)

    def _check_models(
        self, mutate: Callable[[dict[str, Any]], None], sid: str, changes: dict[str, Any]
    ) -> None:
        """Refuse model choices that can't run, before they are saved: transcribing without a
        speech model for the language, or a translator that isn't downloaded."""
        if not {"transcribe", "asr_model", "mt_model"} & set(changes):
            return
        draft = self.settings.snapshot()
        mutate(draft)
        s = effective_config(self.base, draft)[0].get(sid)
        if s.remote:
            return
        if s.transcribe and ("transcribe" in changes or changes.get("asr_model")):
            if self.plan.vad is None:
                raise ChangeError(NO_VAD)
            try:
                plan_station(self.registry, s, self.asr_model)
            except ModelError as e:  # a model that can't tell languages: code cannot_detect
                raise ChangeError(f"{s.name} can't be transcribed yet: {e}",
                                  getattr(e, "code", None)) from e  # fmt: skip
        if changes.get("mt_model"):
            try:
                entry, _ = self.registry.installed(str(changes["mt_model"]))
            except ModelError as e:
                raise ChangeError(str(e)) from e
            if entry.capability != "translate":
                raise ChangeError(f"{entry.id} is not a translation model")

    def delete_station(self, sid: str) -> None:
        if not any(s.get("id") == sid for s in self.settings.get("station") or []):
            if not any(s.id == sid for s in self.config.stations):
                raise NotFound(f"no station {sid!r}", "not_found")
            raise ChangeError(f"{sid} is in stations.toml: switch it off here, or edit that file")
        self.stop_station(sid)

        def mutate(d: dict[str, Any]) -> None:
            d["station"] = [s for s in d.get("station", []) if s.get("id") != sid]
            d.get("overrides", {}).pop(sid, None)
            d.get("remote_privacy", {}).pop(sid, None)
            run = d.get("run", {})
            run["stations"] = [s for s in run.get("stations", []) if s != sid]

        self.settings.update(mutate)
        self.pipeline.forget_station(sid)
        self._reload()

    def start_station(self, sid: str, *, listen_only: bool = False) -> None:
        """Start a station in its saved mode, or with ``listen_only`` just for this run: it
        plays and isn't written down until it is stopped or started again without it."""
        station = self.config.get(sid)
        if sid in self.unavailable:
            why = self.unavailable[sid]
            code = {SWITCHED_OFF: "switched_off", UNCONFIRMED: "unconfirmed"}.get(why)
            raise ChangeError(why, code)  # the page offers to switch it on, or to confirm
        held = station.sdr and getattr(self, "_live", None) and self._live.holds(
            station.sdr.device.id)  # fmt: skip
        if held:
            raise ChangeError(held)
        if station.remote:
            if listen_only:
                raise ChangeError(f"{station.name} is text from another Wavescribe: no audio")
            self._start_follower(station)
        else:
            (self.listen_only_run.add if listen_only else self.listen_only_run.discard)(sid)
            on = self.pipeline.status()["stations"].get(sid)
            if on is not None and not on["stopping"]:  # on already: switch it to the mode asked
                self.pipeline.update_station(self._runtime(station))
            else:  # off, or still stopping: add_station waits for the stop, or says why not
                try:
                    self.pipeline.add_station(self._runtime(station))
                except StationStopping as e:
                    raise ChangeError(str(e)) from e
        self._remember_running(sid, True)

    def stop_station(self, sid: str) -> None:
        set_up = {s.id for s in self.config.stations}
        set_up |= {s.get("id") for s in self.settings.get("station") or []}  # one that broke
        if sid not in set_up and sid not in self.running():
            self.config.get(sid)  # no station by that id: says so, as start does
        self.listen_only_run.discard(sid)
        follower = self.followers.pop(sid, None)
        if follower is not None:
            follower.close()
        self.hub.stop_station(sid)
        self._remember_running(sid, False)

    def _remember_running(self, sid: str, on: bool) -> None:
        def mutate(d: dict[str, Any]) -> None:
            run = d.setdefault("run", {})
            ids = [s for s in run.get("stations", []) if s != sid]
            run["stations"] = [*ids, sid] if on else ids

        self.settings.update(mutate)

    def _check_name_free(self, name: str, sid: str | None = None) -> None:
        """Refuse a name another station has (ignoring case and spaces at its ends): alerts, the
        page and the agent tools tell stations apart by name."""
        key = name.strip().casefold()
        if any(s.name.strip().casefold() == key for s in self.config.stations if s.id != sid):
            raise ChangeError(f"there is already a station called {name.strip()!r}: give this "
                              "one another name")  # fmt: skip

    def _free_id(self, base: str) -> str:
        ids = {s.id for s in self.config.stations}
        if base not in ids:
            return base
        n = 2
        while f"{base}-{n}" in ids:
            n += 1
        return f"{base}-{n}"

    def _pin_acknowledgements(self) -> None:
        """A reception confirmation saved as plain ``acknowledge = true`` (sent that way, or
        saved before the class was kept) becomes the class the station is in now, so moving
        it to another class later (a frequency, a region) asks again."""
        heard = {s.id: s.privacy for s in self.config.stations
                 if s.sdr is not None and s.id not in self.config_errors}  # fmt: skip

        def entries(d: dict[str, Any]) -> list[tuple[Any, dict[str, Any]]]:
            return [(e.get("id"), e) for e in d.get("station", [])] + list(
                d.get("overrides", {}).items())  # fmt: skip

        if any(e.get("acknowledge") is True and sid in heard
               for sid, e in entries(self.settings.snapshot())):  # fmt: skip

            def mutate(d: dict[str, Any]) -> None:
                for sid, e in entries(d):
                    if e.get("acknowledge") is True and sid in heard:
                        e["acknowledge"] = heard[sid]

            self.settings.update(mutate)

    def _check_draft(self, mutate: Callable[[dict[str, Any]], None], sid: str, *,
                     reach: bool = True) -> Station:  # fmt: skip
        """The station as the change would leave it, or why the change can't be made. A change
        that tunes it where its receiver doesn't hear it is refused with what to do (unless
        ``reach`` is false: the caller moves the receiver): a station its receiver was moved
        away from takes other changes (a name, a language) as before."""
        draft = self.settings.snapshot()
        mutate(draft)
        config, errors = effective_config(self.base, draft)
        if sid in errors:
            raise ChangeError.of(errors[sid])
        bad = {k: v for k, v in errors.items() if k.startswith("device:")}
        if bad:
            raise ChangeError(next(iter(bad.values())))
        station = config.get(sid)
        prev = next((s for s in self.config.stations if s.id == sid), None)
        moved = prev is None or not prev.out_of_reach or prev.sdr != station.sdr
        if reach and station.out_of_reach and moved and not station.disabled:
            raise ChangeError(station.out_of_reach, "out_of_reach")
        return station

    def _acknowledgement(self, sid: str) -> object:
        """What a station's saved confirmation says: its page entry, its override, the file."""
        for e in self.settings.get("station") or []:
            if e.get("id") == sid:
                return e.get("acknowledge", False)
        over = (self.settings.get("overrides") or {}).get(sid, {})
        if "acknowledge" in over:
            return over["acknowledge"]
        base = next((s for s in self.base.stations if s.id == sid), None)
        return station_dict(base).get("acknowledge", False) if base else False

    def _replan(self, s: Station, prev: Station | None) -> tuple[ModelEntry, Path] | None:
        """Plan one station's models after a change; returns its ASR model, or None when it
        isn't transcribed here (remote, listen only) or has no usable model yet (then it plays
        without text and ``self.listen_only`` says why)."""
        sid = s.id
        if s.remote or not s.transcribe:
            self.plan.asr.pop(sid, None)
            self.plan.mt_station.pop(sid, None)
            self.listen_only.pop(sid, None)
            if self.unavailable.get(sid) and not s.disabled:
                del self.unavailable[sid]  # _mark_unavailable puts back what still applies
            return None
        planned = self.plan.asr.get(sid)
        if (
            planned is None
            or prev is None
            # languages: the model for language "auto" must know each of them
            or (prev.language, prev.languages, prev.asr_model)
            != (s.language, s.languages, s.asr_model)
        ):
            planned = None
            if self.plan.vad is None:
                self.listen_only[sid] = _plays_meanwhile(NO_VAD)
            else:
                try:
                    planned = plan_station(self.registry, s, self.asr_model)
                    self.listen_only.pop(sid, None)
                except ModelError as e:
                    self.listen_only[sid] = _plays_meanwhile(str(e))
            if planned is None:
                self.plan.asr.pop(sid, None)
            else:
                self.plan.asr[sid] = planned
        if s.mt_model and self.translate and not self.mt_model:
            if prev is None or prev.mt_model != s.mt_model or sid not in self.plan.mt_station:
                try:
                    self.plan.mt_station[sid] = self.registry.installed(s.mt_model)
                except ModelError as e:
                    log.warning("station %s keeps the shared translator: %s", sid, e)
                    self.plan.mt_station.pop(sid, None)
        else:
            self.plan.mt_station.pop(sid, None)
        return planned

    def models_changed(self, model_id: str | None = None) -> None:
        """A model was downloaded: use it where it was missing (the speech detector, the
        music gate, the translator, stations waiting for a model for their language)."""
        with self._lock:
            if self.plan.vad is None:
                with contextlib.suppress(ModelError):
                    self.plan.vad = self.registry.resolve("vad")
            if self.plan.gate is None and self.use_gate:
                with contextlib.suppress(ModelError):
                    self.plan.gate = self.registry.resolve("gate")
            if self.plan.mt is None and self.translate:
                with contextlib.suppress(ModelError):
                    mt = (self.registry.installed(self.mt_model) if self.mt_model
                          else self.registry.resolve("translate"))  # fmt: skip
                    self.pipeline.set_translator(mt)
            for s in self.config.stations:
                if s.disabled or (s.id not in self.unavailable and s.id not in self.listen_only):
                    continue
                waiting = s.id in self.listen_only
                self.plan.asr.pop(s.id, None)  # plan it afresh
                planned = self._replan(s, None)
                if waiting and planned is not None and s.id in self.running():
                    # It plays already: now with text (unless it was started listen-only).
                    self.pipeline.update_station(self._runtime(s), planned)
            self._check_translations()
            self._mark_unavailable()
        event(log, "models_changed", model=model_id, waiting=len(self.listen_only))

    def _reload(self) -> None:
        """Lay the settings over stations.toml again and apply the difference."""
        with self._lock:
            old = {s.id: s for s in self.config.stations}
            self.config, self.config_errors = effective_config(self.base, self.settings.snapshot())
            self._pin_acknowledgements()
            apply_radio_policies(self.base, self.settings.snapshot())
            new = {s.id: s for s in self.config.stations}
            for sid, s in new.items():
                prev = old.get(sid)
                planned = self._replan(s, prev)
                if prev is not None and prev != s:
                    if s.disabled or s.out_of_reach:  # off, or its receiver no longer hears it
                        self.stop_station(sid) if sid in self.running() else None
                    elif not s.remote:  # without a model for it, it goes on without text
                        self.pipeline.update_station(self._runtime(s), planned)
                    else:
                        follower = self.followers.get(sid)
                        if follower is not None:
                            follower.station = s
            for sid in set(old) - set(new):
                self.plan.asr.pop(sid, None)
                self.plan.mt_station.pop(sid, None)
                self.unavailable.pop(sid, None)
                self.listen_only.pop(sid, None)
            self._check_translations()
            self._mark_unavailable()
            self.hub.config = self.config
            self.hub.unavailable = self.unavailable
            if self.asker is not None:
                self.asker.stations = {s.id: s for s in self.config.stations}
            if self.alerter is not None:
                self.alerter.station_names = {s.id: s.name for s in self.config.stations}
            if self.store is not None:
                for sid in set(new) - set(old):
                    self.store.add_station(new[sid])

    # -- remote stations ----------------------------------------------------------------------

    def _start_follower(self, station: Station) -> None:
        from wavescribe.remote import MAX_EVENT_AGE_S, SENDER_CLOCK_DRIFT_S, RemoteFollower

        if station.remote == "push":
            return  # pushed to us: nothing to connect to
        if station.id in self.followers and not self.followers[station.id].closed:
            return
        # Catch up from just after the last utterance stored for it, or the last hour when it
        # is new here; never from further back than an utterance may be and still be taken.
        now = time.time()
        since, stored = now - 3600, []
        if self.store is not None:
            last = self.store.last_heard().get(station.id)
            if last is not None:
                since = min(max(last + 0.01, now - MAX_EVENT_AGE_S), now)
                # Times stored from a sender whose clock runs fast are ours, up to this far
                # behind its own: what it replays of these is skipped (RemoteFollower).
                recent = self.store.between(last - SENDER_CLOCK_DRIFT_S, last,
                                            station_ids=[station.id])  # fmt: skip
                stored = [(u["text"], u["duration"]) for u in recent]
        self.followers[station.id] = RemoteFollower(
            station, self._take_followed, since=since, stored=stored, node_id=self.node_id()
        )

    def _take_followed(self, result: Result) -> None:
        # The class first: outputs and viewers on other machines ask the station for it.
        self._note_classes({result.station.id: result.station.privacy})
        self.pipeline.ingest(result)

    def _note_classes(self, said: dict[str, str], new: Iterable[dict[str, Any]] = ()) -> None:
        """Keep the privacy class another machine gave its stations (``said``, by our id),
        where it isn't the one they have, and add the stations a push brought (``new``): one
        write of ui.toml for both. Everything that asks whether a station's text may be
        shown to other machines or sent on reads the class from the station."""
        from wavescribe.remote import held_as

        new = list(new)
        with self._lock:
            have = {s.id: s for s in self.config.stations}
            noted = self.settings.get("remote_privacy") or {}
            # Kept when it changes how the station is handled, or what its sender says (a
            # stricter class in the station's config may cover that, until the config changes).
            changed = {sid: cls for sid, cls in said.items() if sid not in have
                       or have[sid].privacy != held_as(cls, have[sid])
                       or noted.get(sid, cls) != cls}  # fmt: skip
            if not changed and not new:
                return

            def mutate(d: dict[str, Any]) -> None:
                d.setdefault("station", []).extend(new)
                d.setdefault("remote_privacy", {}).update(changed)

            self.settings.update(mutate)
            self._reload()

    def remote_status(self) -> dict[str, dict[str, Any]]:
        out = {sid: f.status() for sid, f in list(self.followers.items()) if not f.closed}
        now = time.time()
        for sid, t in list(self._pushed.items()):  # ingest adds to it from other threads
            out[sid] = {"connected": now - t < 120, "reconnects": 0, "pushed": True,
                        "last_data_age": round(now - t, 1), "in_speech": False,
                        "music": False, "quiet_s": round(now - t), "level": 0,
                        "stopping": False}  # fmt: skip
        return out

    def ingest(self, batch: dict[str, Any]) -> dict[str, int]:
        """Events pushed by another Wavescribe (its output pointed at our /feed/v1/ingest):
        each of its stations becomes a station here, created on first sight.

        A push token reaches only the stations pushing made. An event whose id here belongs to
        any other station is refused, as is one that would take the pushed stations past their
        caps, or whose values don't check out (``read_utterance``). An event marked ``test``
        (the sender's Test button) is checked the same way and never kept: no station, no
        text. Returns how many events were taken and how many were refused, and how many
        tests passed when there were any."""
        from wavescribe.remote import MAX_NAME_CHARS, BadEvent, read_utterance, to_result

        if not isinstance(batch, dict):
            raise ChangeError("expected a JSON event or batch")
        events = batch.get("events") if batch.get("type") == "batch" else [batch]
        if not isinstance(events, list):
            raise ChangeError("a batch carries an events list")
        sender = batch.get("node")
        name = sender.get("name") if isinstance(sender, dict) else None
        node = slug(name[:MAX_NAME_CHARS] if isinstance(name, str) and name else "remote")
        refused: Counter[str] = Counter()
        me = self.node_id()
        if len(events) > MAX_BATCH_EVENTS:
            refused["too many events in one batch"] = len(events) - MAX_BATCH_EVENTS
        wanted: list[tuple[Heard, str]] = []
        tests = [ev for ev in events[:MAX_BATCH_EVENTS] if isinstance(ev, dict) and ev.get("test")]
        tested = 0
        with self._lock:  # one batch at a time decides which stations exist
            made = {s.id: s.remote_station for s in self.config.stations if s.remote == "push"}
            saved = {s.get("id") for s in self.settings.get("station") or []}
            others = ({s.id for s in self.config.stations} | saved) - set(made)
            from_node = sum(sid.startswith((f"{node}-", f"{_push_node(node)}-")) for sid in made)
            new: list[dict[str, Any]] = []
            said: dict[str, str] = {}  # the class each station's latest event gave it
            for ev in events[:MAX_BATCH_EVENTS]:
                if not isinstance(ev, dict) or ev.get("type") != "utterance":
                    continue
                if not ev.get("text"):
                    continue  # nothing was heard
                try:
                    heard = read_utterance(ev)
                except BadEvent as e:
                    refused[str(e)] += 1
                    continue
                if why := heard.refused_by(me):
                    refused[why] += 1  # an echo of what this machine passed on
                    continue
                if ev.get("test"):
                    tested += 1  # it would have been taken; that is all a test asks
                    continue
                remote_id = slug(heard.station_id)
                sid = push_id(node, remote_id)
                legacy = f"{node}-{remote_id}"[:PUSH_ID_CHARS]  # cut short, before the hash
                if made.get(legacy) == remote_id:
                    sid = legacy  # made that way earlier: its text stays in one station
                if sid in others or made.get(sid, remote_id) != remote_id:
                    refused["the id belongs to another station here"] += 1
                    continue
                if sid not in made:
                    if from_node >= MAX_PUSH_STATIONS_PER_NODE or len(made) >= MAX_PUSH_STATIONS:
                        refused["too many pushed stations"] += 1
                        continue
                    new.append({
                        "id": sid, "name": f"{heard.station_name or remote_id} ({node})",
                        "language": heard.station_language or heard.language or "und",
                        "remote": "push", "remote_station": remote_id,
                    })  # fmt: skip
                    made[sid] = remote_id
                    from_node += 1
                if heard.privacy is not None:
                    said[sid] = heard.privacy
                self._upstream[sid] = heard.path
                wanted.append((heard, sid))
            unmade = self._unmakeable(new) if new else set()
            if unmade:
                refused["its station can't be made here"] = sum(s in unmade for _, s in wanted)
            self._note_classes(
                {sid: cls for sid, cls in said.items() if sid not in unmade},
                [e for e in new if e["id"] not in unmade],
            )
            known = {s.id: s for s in self.config.stations}
        taken = 0
        for heard, sid in wanted:
            station = known.get(sid)
            if station is None or station.disabled:
                continue
            self._pushed[sid] = time.time()
            self.pipeline.ingest(to_result(heard, station))
            taken += 1
        if refused:
            event(log, "ingest_refused", level=logging.WARNING, node=node, taken=taken,
                  refused=dict(refused))  # fmt: skip
        counts = {"accepted": taken, "rejected": sum(refused.values())}
        return {**counts, "test": tested} if tests else counts

    def _unmakeable(self, entries: list[dict[str, Any]]) -> set[str]:
        """The ids of the stations a push brought that the config refuses. They are built
        the way the page's stations are, and one that doesn't build must not reach ui.toml."""
        draft = self.settings.snapshot()
        draft.setdefault("station", []).extend(entries)
        _, errors = effective_config(self.base, draft)
        return {e["id"] for e in entries if e["id"] in errors}

    # -- retention ---------------------------------------------------------------------------

    def _apply_retention(self, r: dict[str, Any]) -> None:
        if self.store is None:
            return
        self.store.set_retention(
            audio_days=r.get("audio_days"),
            audio_max_gb=r.get("audio_max_gb"),
            text_days=r.get("text_days"),
        )

    def set_retention(self, changes: dict[str, Any]) -> dict[str, Any]:
        if self.store is None:
            raise ChangeError("this run keeps nothing (started with --no-store)")
        clean: dict[str, float] = {}
        limits = {"audio_days": 3650, "audio_max_gb": 10000, "text_days": 36500}
        for key, top in limits.items():
            if key in changes and changes[key] is not None:
                v = float(changes[key])
                if not 0 <= v <= top:
                    raise ChangeError(f"{key} is between 0 and {top}")
                clean[key] = v
        if self.store.archive is None and clean.get("audio_days"):
            raise ChangeError("audio recording is off for this run (started with --audio-days 0)")
        self.settings.update(lambda d: d.setdefault("retention", {}).update(clean))
        self._apply_retention(clean)
        return self.store.retention()

    # -- models ------------------------------------------------------------------------------

    def set_chain(self, capability: str, ids: list[str]) -> None:
        if not ids:
            raise ChangeError(f"keep at least one {capability} model in the order")
        try:
            self.registry.set_chain(capability, ids)
        except ModelError as e:
            raise ChangeError(str(e)) from e
        self.settings.update(lambda d: d.setdefault("models", {}).update({capability: ids}))
        if capability == "asr":
            for s in self.config.stations:
                if s.remote or s.asr_model or self.asr_model:
                    continue
                if not s.transcribe:
                    continue
                try:
                    planned = plan_station(self.registry, s)
                except ModelError as e:
                    self.listen_only[s.id] = _plays_meanwhile(str(e))
                    self.plan.asr.pop(s.id, None)
                    self.pipeline.update_station(self._runtime(s))  # goes on without text
                    continue
                self.listen_only.pop(s.id, None)
                if self.plan.asr.get(s.id, (None,))[0] != planned[0]:
                    self.plan.asr[s.id] = planned
                    self.pipeline.update_station(self._runtime(s), planned)
            self._mark_unavailable()
        elif capability == "translate" and self.translate and not self.mt_model:
            try:
                mt = self.registry.resolve("translate")
            except ModelError as e:
                raise ChangeError(str(e)) from e
            if self.plan.mt is None or mt[0] != self.plan.mt[0]:
                self.pipeline.set_translator(mt)
                self._check_translations()

    def plan_view(self) -> Plan:
        return self.plan

    # -- alerts ------------------------------------------------------------------------------

    def _alert_raw(self) -> tuple[dict[str, Any], Path | None]:
        from wavescribe.alerts import read_alerts, resolve_alerts_path

        path = resolve_alerts_path(self.alerts_path)
        return (read_alerts(path) if path else {}), path

    def _alert_config(self, page: dict[str, Any] | None = None):  # -> AlertConfig
        from wavescribe.alerts import merge_page, parse_alerts

        raw, path = self._alert_raw()
        merged, rules, disabled = merge_page(raw, page if page is not None else
                                             (self.settings.get("alerts") or {}))  # fmt: skip
        return parse_alerts(merged, path, self.keywords_dir, rules, disabled)

    def _make_alerter(self):  # -> Alerter
        from wavescribe.alerts import Alerter

        config = self._alert_config()
        return Alerter(config, store=self.store, actions=self.speech,
                       station_names={s.id: s.name for s in self.config.stations})  # fmt: skip

    def _alert_voices(self) -> dict[str, Any]:
        return dict(self.alerter.config.voices) if self.alerter is not None else {}

    def _digest_schedule(self, page: dict[str, Any] | None = None):  # -> Scheduler | None
        """``[digest]`` in alerts.toml, or the page's in its place (``[alerts.digest]`` in
        config/ui.toml): digests on a schedule, spoken in the alerts' voices. Not started.
        ``page``: the page's alert settings to use rather than those saved."""
        from wavescribe.digest.schedule import scheduler
        from wavescribe.web.digest import unavailable

        table, where = self._digest_table(page)
        if self.alerter is None and table is not None:
            log.warning("[digest] in %s: no scheduled digests while alerts are off", where)
        if self.alerter is None or self.hub.digester is None or self.hub.speaker is None:
            return None
        return scheduler(
            table,
            where,
            digester=self.hub.digester,
            speaker=self.hub.speaker,
            voices=self.alerter.config.voices,
            language=self.alerter.config.notify_language,
            exports=self.hub.exports,
            closed=threading.Event(),  # its own: stop() sets it, and close() calls that
            unavailable=lambda: unavailable(self.hub),
        )

    def _digest_table(self, page: dict[str, Any] | None = None) -> tuple[Any, str]:
        """The ``[digest]`` table in effect (None: no digests on a schedule) and the file it
        comes from: the page's in place of alerts.toml's, when the page set one."""
        from wavescribe.alerts import merge_page

        raw, path = self._alert_raw()
        page = page if page is not None else (self.settings.get("alerts") or {})
        merged, _rules, _disabled = merge_page(raw, page)
        where = self.settings.path if isinstance(page.get("digest"), dict) else path
        return merged.get("digest"), str(where or "alerts.toml")

    def _page_digest(self) -> dict[str, Any] | None:
        """The page's digest schedule as saved (``off``: switched off), None when it set none."""
        table = (self.settings.get("alerts") or {}).get("digest")
        return table if isinstance(table, dict) else None

    def _check_digest_schedule(self, table: dict[str, Any]):  # -> Schedule
        """A digest schedule from the page, checked as ``[digest]`` is; refusals are coded."""
        from wavescribe.digest.schedule import known, parse_schedule

        assert self.alerter is not None
        schedule = parse_schedule(
            table, None, voices=self.alerter.config.voices, language=self._digest_language()
        )
        assert schedule is not None
        known(schedule.stations, (s.id for s in self.config.stations), None)
        return schedule

    def _digest_language(self) -> str:
        """A scheduled digest's language when its table gives none."""
        config = self.alerter.config if self.alerter is not None else None
        if config is not None and config.notify_language:
            return config.notify_language
        return self.hub.digester.language() if self.hub.digester is not None else "en"

    def digest_schedule_view(self) -> dict[str, Any]:
        """The digest schedule for the page: what is set and where, whether it runs, when
        the next digest comes, and what it may be set to."""
        from wavescribe.digest.schedule import EVERY
        from wavescribe.web.digest import digest_fields, unavailable

        raw, path = self._alert_raw()
        page = self._page_digest()
        table = page if page is not None else raw.get("digest")
        shown = {k: v for k, v in table.items() if k != "off"} if isinstance(table, dict) else None
        error = self.digest_error
        schedule = self.digests.schedule if self.digests is not None else None
        if schedule is None and shown is not None and self.alerter is not None:
            try:
                schedule = self._check_digest_schedule(shown)
            except ConfigError as e:
                error = error or said(e)
        source = "page" if page is not None else "file" if table is not None else None
        voices = sorted(self.alerter.config.voices) if self.alerter is not None else []
        return {
            "enabled": shown is not None and not (page or {}).get("off"),
            "source": source,
            "file": str(path) if path else None,
            # Whether the alerts file has a [digest] of its own, also while the page's is used:
            # what "use the file's again" would bring back.
            "file_has_schedule": raw.get("digest") is not None,
            "schedule": schedule.table() if schedule is not None else None,
            "running": self.digests is not None,
            "next_at": (
                round(self.digests.schedule.next_delivery(time.time()))
                if self.digests is not None
                else None
            ),
            "alerts": self.alerter is not None,
            **digest_fields(unavailable(self.hub)),
            "error": str(error) if error else None,
            "error_code": getattr(error, "code", None),
            "language": self._digest_language(),
            "voices": voices,
            "every_minutes_choices": list(EVERY),
        }

    def set_digest_schedule(self, body: dict[str, Any]) -> dict[str, Any]:
        """The page's digest schedule (``every_minutes``, ``stations``, ``language``,
        ``speak``, ``quiet_hours``), checked as ``[digest]`` is, saved in place of alerts.toml's,
        and running at once. ``enabled: false`` switches digests on a schedule off; the rest is
        kept, for the page to show."""
        if self.alerter is None:
            raise ChangeError(ALERTS_OFF, "alerts_off")
        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ChangeError("enabled must be true or false", "bad_enabled", field="enabled")
        table = {k: v for k, v in body.items() if k != "enabled" and v is not None}
        schedule = self._check_digest_schedule(table)
        # every_minutes always: an empty table would be left out of ui.toml, and the file's
        # schedule would come back in its place.
        stored = {**table, "every_minutes": schedule.every_minutes}
        if not enabled:
            stored["off"] = True
        page = self.settings.get("alerts") or {}
        page["digest"] = stored
        self._reschedule(page)
        return self.digest_schedule_view()

    def reset_digest_schedule(self) -> dict[str, Any]:
        """Forget the page's digest schedule: alerts.toml's ``[digest]``, if any, runs again."""
        if self.alerter is None:
            raise ChangeError(ALERTS_OFF, "alerts_off")
        page = self.settings.get("alerts") or {}
        page.pop("digest", None)
        self._reschedule(page)
        return self.digest_schedule_view()

    def _reschedule(self, page: dict[str, Any]) -> None:
        """Save the page's alert settings ``page`` and run the digest schedule they make in
        place of the one running; nothing is saved or replaced when it doesn't load."""
        with self._digests_lock:
            new = self._digest_schedule(page)
            self.settings.update(lambda d: d.__setitem__("alerts", page))
            old, self.digests, self.digest_error = self.digests, new, None
            if old is not None:
                old.stop()
            if new is not None and self._digests_running:
                new.start()

    def _llm_rule_list(self) -> list[LlmRule]:
        return list(self.alerter.config.rules.llm_rules) if self.alerter else []

    def _llm_rules_transcript(self, result: Result) -> None:
        if self.llm_rules is not None:
            self.llm_rules.on_transcript(result)

    def change_alerts(self, mutate: Callable[[dict[str, Any]], None]) -> None:
        """Change the page's alert settings (``[alerts]``): validated, saved, applied."""
        if self.alerter is None:
            raise ChangeError("alerts are off for this run (started with --no-alerts)")
        draft = self.settings.get("alerts") or {}
        mutate(draft)
        try:
            config = self._alert_config(draft)
        except (ConfigError, ValueError) as e:
            raise ChangeError(str(e)) from e
        self.settings.update(lambda d: d.__setitem__("alerts", draft))
        self.alerter.reconfigure(config)

    def save_rule(self, raw: dict[str, Any]) -> None:
        # What rules_view added for the page isn't the rule's: a rule sent back whole is kept
        # without it. Any other key the rule doesn't take is refused, as in alerts.toml.
        raw = {k: v for k, v in raw.items() if k not in VIEW_KEYS}
        try:
            parse_rule(raw, "page")
        except ValueError as e:
            raise ChangeError(str(e)) from e

        def mutate(a: dict[str, Any]) -> None:
            rules = [r for r in a.get("rule", []) if r.get("id") != raw["id"]]
            a["rule"] = [*rules, raw]

        self.change_alerts(mutate)

    def delete_rule(self, rule_id: str) -> None:
        self.change_alerts(lambda a: a.__setitem__(
            "rule", [r for r in a.get("rule", []) if r.get("id") != rule_id]))  # fmt: skip

    def enable_rule(self, rule_id: str, on: bool) -> None:
        def mutate(a: dict[str, Any]) -> None:
            off = [r for r in a.get("disabled_rules", []) if r != rule_id]
            a["disabled_rules"] = off if on else [*off, rule_id]

        self.change_alerts(mutate)

    def rules_view(self) -> dict[str, Any]:
        raw, path = self._alert_raw()
        page = self.settings.get("alerts") or {}
        disabled = set(page.get("disabled_rules", []))
        rules = []
        page_ids = {r.get("id") for r in page.get("rule", [])}
        for r in raw.get("rule", []):
            if r.get("id") not in page_ids:
                rules.append({**r, "origin": "file", "enabled": r.get("id") not in disabled,
                              "kind": r.get("kind", "keyword")})  # fmt: skip
        for r in page.get("rule", []):
            rules.append({**r, "origin": "page", "enabled": r.get("id") not in disabled,
                          "kind": r.get("kind", "keyword")})  # fmt: skip
        config = self.alerter.config if self.alerter else None
        packs = {}
        if config is not None:
            for lang, p in config.rules.packs.items():
                packs[lang] = {"topics": {k: list(v) for k, v in p.topics.items()},
                               "places": sorted(p.places)}  # fmt: skip
        return {
            "enabled": self.alerter is not None,
            "file": str(path) if path else None,
            "rules": rules,
            # The keys each kind of rule takes, so that the page saves an edited rule without
            # one alerts.toml has that this version doesn't (save_rule refuses those).
            "rule_keys": {kind: sorted(keys) for kind, keys in RULE_KEYS.items()},
            "packs": packs,
            "delivery": {
                "notify_language": config.notify_language if config else None,
                "translation_wait_s": config.translation_wait_s if config else None,
                "cooldown_minutes": round(config.cooldown_s / 60, 1) if config else None,
                "remind_minutes": round(config.remind_s / 60, 1) if config else None,
            },
            "actions": {k: list(v) for k, v in (config.actions if config else {}).items()},
            "voices": {
                k: {"voice": v.voice, "intro": v.intro, "fallback": v.fallback}
                for k, v in (config.voices if config else {}).items()
            },
            "installed_voices": sorted(self.alerter._voices or []) if self.alerter else [],
            "llm": self.llm_rules.status() if self.llm_rules else None,
        }

    def try_rules(self, text: str, language: str, station: str | None = None) -> list[dict]:
        """Which rules a sentence would trip (keyword rules only; no alert is raised)."""
        if self.alerter is None:
            return []
        hits = self.alerter.config.rules.evaluate(text, language, (), station)
        return [{"rule": h.rule_id, "severity": h.severity, "terms": h.terms,
                 "places": h.places, "spans": h.spans} for h in hits]  # fmt: skip

    # -- outputs -----------------------------------------------------------------------------

    def _output_configs(self) -> list[OutputConfig]:
        out = []
        for raw in self.settings.get("output") or []:
            try:
                out.append(OutputConfig.from_dict(raw))
            except OutputError as e:
                log.warning("output %s skipped: %s", raw.get("id"), e)
        return out

    def outputs_view(self) -> dict[str, Any]:
        status = self.outputs.status()
        return {
            "outputs": [{**c.public(), "status": status.get(c.id)} for c in self._output_configs()],
            "deliveries": self.outputs.recent()[:50],
            "node": self.node(),
        }

    def save_output(self, raw: dict[str, Any]) -> dict[str, Any]:
        raw = dict(raw)
        new = not raw.get("id")  # the page sends the id only when it edits one
        raw["id"] = raw.get("id") or slug(str(raw.get("name") or "output"))
        existing = next((o for o in self.settings.get("output") or []
                         if o.get("id") == raw["id"]), None)  # fmt: skip
        if new and existing is not None:  # it would replace that one, and lose its secrets
            name = existing.get("name") or raw["id"]
            raise ChangeError(f"there is already an output called {name!r}: give this one "
                              "another name, or edit that one")  # fmt: skip
        # The page never sees secrets: a masked or missing value keeps the stored one.
        if existing is not None:
            if raw.get("secret") in (None, True, "•••"):
                raw["secret"] = existing.get("secret", "")
            headers = raw.get("headers") or {}
            raw["headers"] = {k: (existing.get("headers", {}).get(k, "") if v == "•••" else v)
                              for k, v in headers.items()}  # fmt: skip
            # The page's form has no field for these: what the file says stays. A timeout and a
            # program are a kind's own (a webhook waits seconds for an answer, a mesh node
            # minutes for an acknowledgement): they stay only while the kind does.
            same = existing.get("kind", "webhook") == raw.get("kind", "webhook")
            for key in ("timeout_s", "alert_max_age_hours", "command"):
                if key not in raw and key in existing and (same or key == "alert_max_age_hours"):
                    raw[key] = existing[key]
        try:
            config = OutputConfig.from_dict(raw)
        except (OutputError, TypeError, ValueError) as e:
            raise ChangeError(str(e)) from e
        stored = {k: v for k, v in config.to_dict().items() if v not in ("", [], {}, None)}

        def mutate(d: dict[str, Any]) -> None:
            outs = [o for o in d.get("output", []) if o.get("id") != config.id]
            d["output"] = [*outs, stored]

        self.settings.update(mutate)
        self.outputs.configure(self._output_configs())
        return config.public()

    def delete_output(self, output_id: str) -> None:
        self.settings.update(lambda d: d.__setitem__(
            "output", [o for o in d.get("output", []) if o.get("id") != output_id]))  # fmt: skip
        self.outputs.configure(self._output_configs())

    def test_output(self, output_id: str) -> dict[str, Any]:
        config = next((c for c in self._output_configs() if c.id == output_id), None)
        if config is None:
            raise ChangeError(f"no output called {output_id!r}")
        return self.outputs.test(config)

    # -- feed tokens -------------------------------------------------------------------------

    def tokens_view(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in t.items() if k != "hash"}
                for t in self.settings.get("feed_token") or []]  # fmt: skip

    def create_token(self, label: str, scopes: list[str]) -> dict[str, Any]:
        scopes = [s for s in scopes if s in TOKEN_SCOPES] or ["read"]
        token = "wst_" + secrets.token_urlsafe(24)
        entry = {"id": secrets.token_hex(4), "label": label.strip()[:60] or "token",
                 "scopes": scopes, "created": round(time.time()), "hash": _hash(token)}  # fmt: skip
        self.settings.update(lambda d: d.setdefault("feed_token", []).append(entry))
        return {**{k: v for k, v in entry.items() if k != "hash"}, "token": token,
                "note": TOKEN_NOTE}  # fmt: skip

    def delete_token(self, token_id: str) -> None:
        def mutate(d: dict[str, Any]) -> None:
            d["feed_token"] = [t for t in d.get("feed_token", []) if t.get("id") != token_id]

        self.settings.update(mutate)
        self.hub.revoke(token_id)  # its streams end now, and its unused tickets with them

    def tokens_required(self) -> bool:
        return bool(self.settings.get("feed_token"))

    def check_token(self, token: str | None, scope: str) -> bool:
        return self.token_id(token, scope) is not None

    def token_id(self, token: str | None, scope: str) -> str | None:
        """The id of the feed token ``token`` is, if it has ``scope``; None otherwise."""
        if not token:
            return None
        digest = _hash(token)
        for t in self.settings.get("feed_token") or []:
            if secrets.compare_digest(t.get("hash", ""), digest) and scope in t.get("scopes", []):
                return str(t.get("id") or digest[:8])
        return None

    # -- status ------------------------------------------------------------------------------

    def start_sampling(self, interval_s: float = 15.0) -> None:
        """Sample resources in the background (~45 ms of CPU each; 0.3% of a core at 15 s),
        so the status panel can show the last hour, e.g. the power draw on battery."""

        def loop() -> None:
            while not self.hub._closed.wait(interval_s):
                self.sample()
                if self.config.devices:
                    try:
                        self.radio.adopt_rds_names()
                    except Exception:
                        log.exception("naming FM stations from RDS")

        threading.Thread(target=loop, daemon=True, name="resources").start()

    def sample(self) -> dict[str, Any]:
        from wavescribe.system import ResourceMonitor

        with self._lock:
            if self._monitor is None:
                self._monitor = ResourceMonitor(_data_dir(self.store))
            monitor = self._monitor
        try:
            snap = monitor.snapshot(self.pipeline.child_pids())
        except Exception as e:  # a sensor must never take the panel down
            return {"error": str(e)[:200]}
        self._last_snapshot = snap
        self._resources.append({
            "t": snap.get("time"),
            "cpu": (snap.get("cpu") or {}).get("total_percent"),
            "process_cpu": (snap.get("process") or {}).get("cpu_percent"),
            "footprint_mb": (snap.get("process") or {}).get("footprint_mb"),
            "memory_used_gb": (snap.get("memory") or {}).get("used_gb"),
            "gpu": (snap.get("gpu") or {}).get("utilization_percent"),
            "watts": (snap.get("power") or {}).get("system_watts"),
            "disk_free_gb": (snap.get("disk") or {}).get("free_gb"),
            "rx_kbps": (snap.get("network") or {}).get("rx_kbps"),
            "tx_kbps": (snap.get("network") or {}).get("tx_kbps"),
        })  # fmt: skip
        return snap

    def status_view(self) -> dict[str, Any]:
        snap = self._last_snapshot
        if snap is None or time.time() - (snap.get("time") or 0) > 3:
            snap = self.sample()
        out: dict[str, Any] = {
            "uptime_s": round(time.time() - self.started_at),
            "node": self.node(),
            "pipeline": self.pipeline.status(),
            "outputs": self.outputs.status(),
            "llm_rules": self.llm_rules.status() if self.llm_rules else None,
            "remote": self.remote_status(),
            "system": snap,
            "history": list(self._resources),
        }
        if self.store is not None:
            out["store"] = {**self.store.stats(), "retention": self.store.retention()}
        return out


def _known(registry: ModelRegistry, model_id: str) -> bool:
    try:
        registry.get(model_id)
    except ModelError:
        return False
    return True


def _lang_name(tag: str) -> str:
    try:
        return language_name(tag)
    except Exception:
        return tag


def _data_dir(store: Store | None) -> Path:
    return store.path.parent if store is not None else Path("data")


__all__ = ["App", "ChangeError", "replace"]
