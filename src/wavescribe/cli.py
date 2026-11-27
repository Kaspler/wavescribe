# SPDX-License-Identifier: Apache-2.0
"""The `wavescribe` command line."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wavescribe import __version__
from wavescribe.events import local_time  # standard library only: cheap at start

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from wavescribe.alerts import Alerter
    from wavescribe.pipeline import Plan, Result
    from wavescribe.registry import ModelEntry, ModelRegistry
    from wavescribe.store import Store

DATA_DIR = Path("data")
DEFAULT_SETTINGS = Path("config/ui.toml")  # settings.DEFAULT_SETTINGS_PATH, without its imports
NC_HELP = "acknowledge that a restricted model is for non-commercial use only"


def _size(gb: float | None) -> str:
    if not gb:
        return "-"
    return f"{gb:.1f} GB" if gb >= 0.1 else f"{max(1, round(gb * 1024))} MB"


_CAPABILITY = {"vad": "speech detection", "gate": "music filter", "asr": "speech to text",
               "translate": "translation"}  # fmt: skip


def _licence_note(registry: ModelRegistry, e: ModelEntry) -> str:
    if e.tier == "byo":
        unknown = e.license.lower() in ("", "unknown", "none")
        return "your own" + (": licence unknown" if unknown else "")
    if e.tier == "default":
        return ""
    state = "accepted" if registry.licenses.accepted(e) else "needs your acceptance"
    return ("non-commercial use only, " if e.tier == "restricted" else "") + state


def cmd_models_list(args: argparse.Namespace) -> int:
    from wavescribe.registry import ModelRegistry

    registry = ModelRegistry.load()
    if args.json:
        from wavescribe.events import to_json

        keys = ("id", "capability", "license", "license_url", "tier", "size_gb", "languages")
        for e in registry.entries():
            row = {k: getattr(e, k) for k in keys}
            row["accepted"] = registry.licenses.accepted(e)
            row["installed"] = registry.local_path(e) is not None
            print(to_json(row))
        return 0
    rows = [("ID", "FOR", "LANGUAGES", "SIZE", "LICENCE", "INSTALLED", "NOTE")]
    for e in registry.entries():
        rows.append((
            e.id,
            _CAPABILITY.get(e.capability, e.capability),
            "any" if "*" in e.languages else str(len(e.languages)),
            _size(e.size_gb),
            e.license,
            "yes" if registry.local_path(e) is not None else "-",
            _licence_note(registry, e),  # only for licences that need your agreement
        ))  # fmt: skip
    if not any(r[-1] for r in rows[1:]):
        rows = [r[:-1] for r in rows]  # every licence is open: no column of blanks
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip())
    return 0


def cmd_models_fetch(args: argparse.Namespace) -> int:
    from wavescribe.registry import LicenseError, ModelRegistry

    registry = ModelRegistry.load()
    ids = args.ids or [
        e.id for cap in ("vad", "gate", "asr", "translate") for e in registry.chain(cap)[:1]
    ]
    entries = [registry.get(i) for i in ids]
    # Settle every licence before downloading anything.
    for entry in entries:
        if registry.licenses.accepted(entry):
            continue
        print(f"{entry.id} is tier {entry.tier} under {entry.license}: {entry.license_url}")
        if not args.accept_license:
            nc = " --non-commercial" if entry.tier == "restricted" else ""
            hint = f"--accept-license '{entry.license}'{nc}"
            print(f"  read the licence, then run again with {hint}", file=sys.stderr)
            return 2
        try:
            registry.licenses.accept(entry, args.accept_license, non_commercial=args.non_commercial)
        except LicenseError as e:
            print(f"wavescribe: {e}", file=sys.stderr)
            return 2
        print(f"  accepted (recorded in {registry.licenses.path})")
    for entry in entries:
        print(f"{entry.id}: {entry.license} ({entry.license_url})")
        print(f"  from {entry.source}" + (f" @ {entry.revision}" if entry.revision else ""))
        path = registry.fetch(entry)
        print(f"  -> {path}")
    out = DATA_DIR / "THIRD_PARTY_MODELS.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(credits_markdown(registry), encoding="utf-8")
    print(f"credits for the installed models: {out}")
    return 0


def cmd_models_accept(args: argparse.Namespace) -> int:
    from wavescribe.registry import LicenseError, ModelRegistry

    registry = ModelRegistry.load()
    entry = registry.get(args.id)
    if entry.tier in ("default", "byo"):
        print(f"{entry.id} is tier {entry.tier}: nothing to accept")
        return 0
    print(f"{entry.id}: {entry.license} ({entry.license_url})")
    try:
        registry.licenses.accept(entry, args.license, non_commercial=args.non_commercial)
    except LicenseError as e:
        print(f"wavescribe: {e}", file=sys.stderr)
        return 2
    print(f"accepted (recorded in {registry.licenses.path})")
    return 0


def credits_markdown(registry: ModelRegistry) -> str:
    """Attribution for every installed model (CC-BY and friends require it)."""
    lines = ["# Third-party models", "",
             "Models installed by `wavescribe models fetch`, downloaded from their rights holders.",
             ""]  # fmt: skip
    for e in registry.entries():
        if registry.local_path(e) is None:
            continue
        lines.append(f"- **{e.id}** ({e.capability}): {e.attribution or e.upstream}  ")
        lines.append(f"  Licence: {e.license}" + (f" <{e.license_url}>" if e.license_url else "")
                     + (f"; source {e.upstream}" if e.upstream else ""))  # fmt: skip
    return "\n".join(lines) + "\n"


def cmd_models_credits(args: argparse.Namespace) -> int:
    from wavescribe.registry import ModelRegistry

    print(credits_markdown(ModelRegistry.load()), end="")
    return 0


def warn_noncommercial(plan: Plan) -> list[str]:
    """Restricted (non-commercial) models in use: say so every time (LICENSING.md)."""
    nc = plan.noncommercial()
    if nc:
        print(f"# ⚠ non-commercial models active: {', '.join(nc)}", file=sys.stderr)
    return nc


def cmd_listen(args: argparse.Namespace) -> int:
    from wavescribe.config import load_stations, resolve_stations_path
    from wavescribe.log import setup_logging
    from wavescribe.pipeline import Pipeline, fan_out, plan_models
    from wavescribe.registry import ModelRegistry
    from wavescribe.sources.stream import StreamSource

    setup_logging(
        DATA_DIR / "logs", console_level=logging.INFO if args.verbose else logging.WARNING
    )
    config = load_stations(resolve_stations_path(args.config))
    from wavescribe.settings import Settings, apply_radio_policies

    apply_radio_policies(config, Settings().data)  # what is kept of radio that isn't broadcast
    # `wavescribe radio tune` passes a station it made for one frequency.
    stations = getattr(args, "tuned_stations", None) or [config.get(sid) for sid in args.stations]
    if args.source and len(stations) != 1:
        raise ValueError("--source needs exactly one station")
    for s in stations:  # `listen` prints what is transcribed here, and these never are
        if s.remote:
            raise ValueError(
                f"station {s.id!r} is transcribed by another Wavescribe, not on this machine; "
                "read it on the page (wavescribe serve)"
            )
        if not s.transcribe:
            raise ValueError(
                f"station {s.id!r} is only listened to (transcribe = false), so there is no "
                "text to print; hear it on the page (wavescribe serve) or set transcribe = true"
            )
    tuning = None
    if args.source and args.source.startswith("sdr:"):
        from dataclasses import replace

        from wavescribe.config import parse_sdr_source, radio_policy

        # The region chosen on the page counts here too, over the stations file's.
        region = (Settings().data.get("radio") or {}).get("region") or config.radio_region
        tuning = parse_sdr_source(args.source, region)
        # The frequency's class, not the configured station's, decides what is kept.
        privacy, band = radio_policy(tuning.freq_hz, tuning.mode, region, True, args.source)
        stations = [replace(stations[0], sdr=tuning, privacy=privacy, band=band)]

    # Before the models load, which takes a while: the tools the sources run.
    import shutil

    if args.source:
        sdr, stream = tuning is not None, tuning is None
    else:
        sdr, stream = any(s.sdr for s in stations), any(not s.sdr for s in stations)
    needed = [("rtl_sdr", "brew install librtlsdr")] if sdr else []
    needed += [("ffmpeg", "brew install ffmpeg")] if stream else []
    for tool, how in needed:
        if shutil.which(tool) is None:
            raise ValueError(f"{tool} isn't installed, and the stations need it ({how})")

    plan = plan_models(
        ModelRegistry.load(),
        stations,
        translate=not args.no_translate,
        asr_model=args.asr,
        mt_model=args.translator,
        use_gate=not args.no_gate,
    )
    warn_noncommercial(plan)
    for s in stations:
        entry = plan.asr[s.id][0]
        targets = (",".join(s.translation_targets) if plan.mt else "") or "none"
        print(f"# {s.id}: {s.language} -> asr {entry.id} -> {targets}", file=sys.stderr)
    print("# loading models...", file=sys.stderr)

    factory = None
    if tuning is not None:
        from wavescribe.sources.sdr import SdrChannelSource

        factory = lambda st: SdrChannelSource(st.id, tuning)  # noqa: E731
    elif args.source:
        factory = lambda st: StreamSource(st.id, args.source, realtime=args.realtime)  # noqa: E731

    store = open_store(args)
    printer = ResultPrinter()
    if store is not None:
        for s in stations:
            store.add_station(s)
        store.keep_pruned()  # as under serve: what is due goes while nothing is said, too
    names = {s.id: s.name for s in config.stations}
    alerter = open_alerter(args, names, store, on_alert=printer.on_alert)
    pipeline = Pipeline(
        stations,
        plan,
        fan_out(store and store.add_translations, printer),
        # The store runs first: it assigns the segment id that alert mentions point to.
        on_transcript=fan_out(store and store.add_transcript, alerter and alerter.on_transcript),
        on_filtered=alerter and alerter.on_filtered,
        on_rechecked=fan_out(store and store.update_transcript, alerter and alerter.on_transcript),
        source_factory=factory,
    )
    if alerter is not None:
        alerter.attach(pipeline.request_translation)
        alerter.watch(pipeline.status, lambda: stations)
    from wavescribe.radio.watch import subscribe

    keep_catches = subscribe(store.add_catch) if store is not None else None  # a watch's
    try:
        pipeline.run(duration=args.duration)
    finally:
        if alerter is not None:
            alerter.close()
        if keep_catches is not None:
            keep_catches()
        if store is not None:
            store.close()
    return 0


def port_number(value: str) -> int:
    """A TCP port for ``--port``: 1 to 65535."""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} isn't a port number") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{port} isn't a port: ports are 1 to 65535")
    return port


def cmd_serve(args: argparse.Namespace) -> int:
    import webbrowser

    from wavescribe.alerts import resolve_alerts_path
    from wavescribe.app import App
    from wavescribe.config import ConfigError, load_stations, resolve_stations_path
    from wavescribe.log import setup_logging
    from wavescribe.registry import ModelRegistry
    from wavescribe.settings import Settings
    from wavescribe.web.server import PortInUseError, access_line, check_port, lan_urls, serve

    setup_logging(
        DATA_DIR / "logs", console_level=logging.INFO if args.verbose else logging.WARNING
    )
    base = load_stations(resolve_stations_path(args.config))
    try:
        settings = Settings(args.settings)
    except ConfigError as e:
        print(f"wavescribe: {e}", file=sys.stderr)
        return 1
    host = "0.0.0.0" if args.lan else args.host
    try:
        check_port(host, args.port)  # before loading anything
    except PortInUseError as e:
        print(f"wavescribe: {e}", file=sys.stderr)
        return 1
    store = open_store(args)
    app = App(
        base, settings, ModelRegistry.load(),
        translate=not args.no_translate, asr_model=args.asr, mt_model=args.translator,
        use_gate=not args.no_gate, store=store, alerts_path=args.alerts,
        alerts_enabled=not args.no_alerts and resolve_alerts_path(args.alerts) is not None,
        host=host, port=args.port, start=args.stations or None,
    )  # fmt: skip
    for sid in args.stations:
        app.config.get(sid)  # fail early on a typo
    for sid, reason in app.unavailable.items():
        print(f"# {sid}: unavailable ({reason})", file=sys.stderr)
    for sid, reason in app.config_errors.items():
        print(f"# page change to {sid} not applied: {reason}", file=sys.stderr)
    warn_noncommercial(app.plan)
    if app.alerter is not None:
        print(f"# alerts: {rule_counts(app.alerter.config.rules)}", file=sys.stderr)

    def ready(url: str) -> None:
        print(f"Wavescribe is running at {url}  (Ctrl-C to stop)", file=sys.stderr)
        if host not in ("127.0.0.1", "::1", "localhost"):
            for lan in lan_urls(args.port):
                print(f"  on the LAN (read-only): {lan}", file=sys.stderr)
        print(access_line(host, behind_proxy=args.behind_proxy, public_read=args.public_read,
                          remote_ask=args.allow_remote_ask), file=sys.stderr)  # fmt: skip
        if args.open:
            webbrowser.open(url)

    from wavescribe.control import load_key

    try:
        serve(app, host=host, port=args.port, on_ready=ready, behind_proxy=args.behind_proxy,
              public_read=args.public_read, remote_ask=args.allow_remote_ask,
              control_key=load_key(DATA_DIR, create=True))  # fmt: skip
    finally:
        if app.alerter is not None:
            app.alerter.close()
        if store is not None:
            store.close()
    return 0


def open_alerter(
    args: argparse.Namespace,
    station_names: dict[str, str],
    store: Store | None,
    on_alert: Callable[[dict[str, Any]], None] | None = None,
) -> Alerter | None:
    """Keyword alerts from config/alerts.toml (or its example) unless ``--no-alerts``."""
    if args.no_alerts:
        return None
    from wavescribe.alerts import Alerter

    config, path = _alert_config(args.alerts)
    if config is None:
        print("# alerts: off (no config/alerts.toml and no rules made on the page)",
              file=sys.stderr)  # fmt: skip
        return None
    lists = ", ".join(sorted(config.rules.packs)) or "none"
    # Only serve runs the LLM that reads the text for LLM rules.
    unchecked = "; LLM rules are checked under serve only" if config.rules.llm_rules else ""
    print(f"# alerts: {rule_counts(config.rules)} ({path or 'the page'}, with the page's "
          f"changes){unchecked}; topic word lists: {lists}", file=sys.stderr)  # fmt: skip
    return Alerter(config, store=store, station_names=station_names, on_alert=on_alert)


def rule_counts(rules: Any) -> str:
    """How many rules there are of each kind, as serve and listen say when they start."""
    return (f"{len(rules.rules)} keyword, {len(rules.llm_rules)} LLM and "
            f"{len(rules.station_rules)} station rules")  # fmt: skip


def _alerts_replay(args: argparse.Namespace, db: Path) -> int:
    from wavescribe.alerts.replay import replay
    from wavescribe.store import Store

    config, path = _alert_config(args.alerts, args.settings)
    if config is None:
        print("wavescribe: no alert rules (config/alerts.toml, or rules made on the page)",
              file=sys.stderr)  # fmt: skip
        return 1
    logging.getLogger("wavescribe.alerts").setLevel(logging.ERROR)  # no "alert" log lines
    store = Store(db)
    try:
        since = time.time() - args.hours * 3600 if args.hours else None
        report = replay(config, store.transcripts(since=since))
    finally:
        store.close()
    print(
        f"{report.utterances} utterances over {report.hours:.1f} h, rules from {path or 'the page'}"
    )
    per_day = 24 / report.hours if report.hours else 0.0
    sev = report.by_severity()
    print("alerts: " + (", ".join(f"{n} {s}" for s, n in sev.items() if n) or "none"))
    if per_day:
        print(_announced_per_day(config.actions, sev, per_day))
    for (s, rule), n in sorted(report.announced.items(), key=lambda kv: -kv[1]):
        terms = report.terms.get(rule, {})
        top = ", ".join(f"{t} x{c}" for t, c in terms.most_common(5))
        print(f"  {n:4} {s:<8} {rule:<22} hits {report.hits[rule]:<4} {top}")
    if args.examples:
        print()
        for t, s, rule, terms, quote in report.examples[-args.examples :]:
            stamp = local_time(t, "%m-%d %H:%M")
            print(f"{stamp} {s:<8} {rule:<22} {','.join(terms)}: {quote[:110]}")
    return 0


def _announced_per_day(
    actions: dict[str, tuple[str, ...]], by_severity: dict[str, int], per_day: float
) -> str:
    """How many alerts a day would reach someone (a notification, a voice, a webhook), as the
    rules' severities are set to deliver them; the rest are only logged."""
    loud = [s for s, n in by_severity.items() if n and set(actions.get(s, ())) - {"log"}]
    quiet = [s for s, n in by_severity.items() if n and s not in loud]
    out = f"announced: {sum(by_severity[s] for s in loud) * per_day:.1f} alerts a day"
    if loud:
        out += f" ({', '.join(loud)})"
    if quiet:
        out += f"; only logged: {', '.join(quiet)}"
    return out


def _alert_config(
    explicit: Path | None, settings: Path = DEFAULT_SETTINGS
) -> tuple[Any, Path | None]:
    """The rules `wavescribe serve` runs: alerts.toml (or its example) with the page's changes
    from ``settings`` (config/ui.toml) laid over it (its own rules, file rules it switched off,
    its delivery settings). (None, None) when there is neither."""
    from wavescribe.alerts import (
        DEFAULT_KEYWORDS_DIR,
        merge_page,
        parse_alerts,
        read_alerts,
        resolve_alerts_path,
    )
    from wavescribe.settings import Settings

    path = resolve_alerts_path(explicit)
    page = Settings(settings).get("alerts") or {}
    if path is None and not page.get("rule"):
        return None, None
    merged, rules, disabled = merge_page(read_alerts(path) if path else {}, page)
    return parse_alerts(merged, path, DEFAULT_KEYWORDS_DIR, rules, disabled), path


def cmd_alerts(args: argparse.Namespace) -> int:
    from wavescribe.store import Store

    if args.alerts_command == "check":
        config, _ = _alert_config(args.alerts, args.settings)
        if config is None:
            print("wavescribe: no alert rules (config/alerts.toml, or rules made on the page)",
                  file=sys.stderr)  # fmt: skip
            return 1
        text = " ".join(args.text)
        hits = config.rules.evaluate(text, args.language)
        for h in hits:
            marked = text
            for a, b in sorted(h.spans, reverse=True):
                marked = f"{marked[:a]}[{marked[a:b]}]{marked[b:]}"
            print(f"{h.severity:<8} {h.rule_id:<20} {marked}")
            print(f"{'':<29} actions: {', '.join(config.actions.get(h.severity, ())) or 'none'}")
        if not hits:
            print("no rule matches", file=sys.stderr)
            if config.rules.pack_for(args.language) is None:
                from wavescribe.languages import language_name, primary_subtag

                code = primary_subtag(args.language)
                print(f"(rules that use topics have no word list in {language_name(code)}, "
                      f"config/keywords/{code}.toml; rules with their own words still apply)",
                      file=sys.stderr)  # fmt: skip
        return 0 if hits else 1

    if args.alerts_command == "test":
        from wavescribe.alerts.actions import platform_actions

        config, _ = _alert_config(args.alerts, args.settings)  # with the page's voices
        actions = platform_actions()
        installed = actions.voices()
        actions.notify("Wavescribe test", "Alerts can reach this screen.")
        print("notification sent (check the top right of the screen)")
        for lang, voice in config.voices.items() if config else []:
            if args.language and lang != args.language:
                continue
            if installed is not None and voice.voice not in installed:
                where = "System Settings > Accessibility > Spoken Content > Manage voices"
                print(f"{lang}: voice {voice.voice!r} is NOT installed ({where})")
                continue
            print(f"{lang}: speaking with {voice.voice}: {voice.fallback}")
            actions.speak(voice.voice, voice.fallback)
        return 0

    path = DATA_DIR / "wavescribe.db"
    if not path.exists():
        print(f"wavescribe: nothing stored yet ({path} doesn't exist)", file=sys.stderr)
        return 1
    if args.alerts_command == "replay":
        return _alerts_replay(args, path)
    store = Store(path)
    try:
        if args.alerts_command == "cap":
            from wavescribe.alerts.cap import to_cap

            alert = store.alert(args.id)
            if alert is None:
                print(f"wavescribe: no alert {args.id}", file=sys.stderr)
                return 1
            print(to_cap(alert))
            return 0
        alerts = store.alerts(limit=args.limit)
        if args.json:
            from wavescribe.events import alert_event

            print_events(reversed(alerts), alert_event)
            return 0
        for a in reversed(alerts):
            stamp = local_time(a["first_at"], "%Y-%m-%d %H:%M")
            more = f" (+{a['mentions'] - 1})" if a["mentions"] > 1 else ""
            where = ",".join(a["stations"])
            print(
                f"#{a['id']:<5} [{stamp}] {a['severity']:<8} {a['rule']:<18} {where:<12} "
                f"{a['status']:<9} {a['quote'][:70]}{more}"
            )
        if not alerts:
            print("no alerts yet", file=sys.stderr)
        return 0
    finally:
        store.close()


def open_store(args: argparse.Namespace) -> Store | None:
    """The SQLite store (and audio archive) unless ``--no-store``."""
    if args.no_store:
        return None
    from wavescribe.store import AudioArchive, Store

    archive = None
    if args.audio_days > 0 and args.audio_max_gb > 0:
        archive = AudioArchive(
            DATA_DIR / "audio",
            max_bytes=int(args.audio_max_gb * 1024**3),
            max_age_days=args.audio_days,
        )
    text_days = args.text_days if args.text_days > 0 else None
    return Store(
        DATA_DIR / "wavescribe.db",
        archive=archive,
        audio_dir=DATA_DIR / "audio",
        max_text_age_days=text_days,
    )


def print_events(rows: Iterable[dict[str, Any]], event: Callable[..., dict[str, Any]]) -> None:
    """Stored utterances or alerts (``--json``) as the feed sends them, one JSON object a line.

    Stations are named as a running server names them: the stations file with the page's
    changes. There is no URL, so no audio links: nothing here knows the address a server would
    be reached at."""
    from wavescribe.config import ConfigError, load_stations, resolve_stations_path
    from wavescribe.events import to_json
    from wavescribe.settings import Settings, effective_config

    stations: dict[str, Any] = {}
    node = {"name": "wavescribe", "url": None}
    try:
        settings = Settings()
        config, _ = effective_config(load_stations(resolve_stations_path()), settings.data)
    except ConfigError:
        pass  # each station is then named by its id, as the feed names one it doesn't know
    else:
        stations = {s.id: s for s in config.stations}
        node["name"] = (settings.get("node") or {}).get("name") or node["name"]
    for row in rows:
        print(to_json(event(row, stations, node)))


def cmd_search(args: argparse.Namespace) -> int:
    from wavescribe.store import HIT_END, HIT_START, Store

    path = DATA_DIR / "wavescribe.db"
    if not path.exists():
        print(f"wavescribe: nothing stored yet ({path} doesn't exist)", file=sys.stderr)
        return 1
    store = Store(path)
    hits = store.search(" ".join(args.query), station_id=args.station, limit=args.limit)
    if args.json:
        from wavescribe.events import utterance_event

        print_events(reversed(hits), utterance_event)
        store.close()
        return 0 if hits else 1
    bold, plain = ("\033[1m", "\033[0m") if sys.stdout.isatty() else ("[", "]")
    for h in reversed(hits):  # oldest first, so the newest ends up next to the prompt
        stamp = local_time(h["t_start"], "%Y-%m-%d %H:%M")
        snippet = h["snippet"].replace(HIT_START, bold).replace(HIT_END, plain)
        print(f"[{stamp}] {h['station']:<10} {_lang(h['field_lang'])}  {snippet}")
    if not hits:
        print("no matches", file=sys.stderr)
    store.close()
    return 0 if hits else 1


def cmd_record(args: argparse.Namespace) -> int:
    import wave

    import numpy as np

    from wavescribe.audio import SAMPLE_RATE
    from wavescribe.config import load_stations, resolve_stations_path
    from wavescribe.sources.stream import StreamSource

    station = load_stations(resolve_stations_path(args.config)).get(args.station)
    url = station.stream or station.stream_hls
    if not url:
        raise ValueError(f"station {station.id!r} has no stream URL")
    stamp = time.strftime("%Y%m%d-%H%M")
    out = args.out or DATA_DIR / "bakeoff" / f"{station.id}-{stamp}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    target = int(args.minutes * 60 * SAMPLE_RATE)
    written = 0
    print(f"recording {station.id} for {args.minutes} min -> {out} (local only, never commit)")
    source = StreamSource(station.id, url)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        for chunk in source:
            part = chunk.samples[: target - written]
            w.writeframes((np.clip(part, -1, 1) * 32767).astype("<i2").tobytes())
            written += len(part)
            if written >= target:
                source.close()
    return 0


def cmd_bakeoff(args: argparse.Namespace) -> int:
    from wavescribe.bakeoff import bakeoff
    from wavescribe.registry import ModelRegistry

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    report = bakeoff(args.clips, models, args.language, registry=ModelRegistry.load())
    out = DATA_DIR / "bakeoff" / f"report-{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report + "\n", encoding="utf-8")
    print(report)
    print(f"\n(report saved to {out})")
    return 0


class ResultPrinter:
    """`[HH:MM:SS] station  lang  text`, with each translation on its own line underneath.

    Words that tripped an alert rule are marked (bold on a terminal, [brackets] otherwise), and
    each alert prints one `!!` line when it goes out (``on_alert``).
    """

    def __init__(self, tty: bool | None = None) -> None:
        self._lock = threading.Lock()
        tty = sys.stdout.isatty() if tty is None else tty
        self._on, self._off = ("\033[1;31m", "\033[0m") if tty else ("[", "]")
        self._announced: set[tuple] = set()

    def _marked(self, text: str, hits: list) -> str:
        spans = sorted({s for h in hits for s in h.spans})
        out, pos = [], 0
        for a, b in spans:
            if a < pos:
                continue
            out += [text[pos:a], self._on, text[a:b], self._off]
            pos = b
        return "".join(out) + text[pos:]

    def __call__(self, result: Result) -> None:
        text = result.transcript.text
        if not text:
            return
        stamp = time.strftime("%H:%M:%S", time.localtime(result.utterance.t_start))
        who = result.station.id
        if result.utterance.freq_hz:  # a watch's: the channel it was caught on
            from wavescribe.radio.bandplan import mhz_text

            who = mhz_text(result.utterance.freq_hz / 1e6)
        head = f"[{stamp}] {who:<10}"
        shown = self._marked(text, result.hits) if result.hits else text
        lines = [f"{head} {_lang(result.transcript.language)}  {shown}"]
        lines += [
            f"{'':<{len(head)}} {_lang(lang)}  {translated}"
            for lang, translated in result.translations.items()
        ]
        with self._lock:
            print("\n".join(lines), flush=True)

    def on_alert(self, alert: dict) -> None:
        """Alerter observer: one line per announcement (not per mention or translation)."""
        if alert["status"] in ("pending", "covered"):
            return
        key = (alert["rule"], alert["first_at"], alert["delivered_severity"])
        if key in self._announced:
            return
        self._announced.add(key)
        stamp = time.strftime("%H:%M:%S", time.localtime(alert["last_at"]))
        said = next(iter(alert["summaries"].values()), None) or alert["quote"]
        trouble = "" if alert["status"] in ("delivered", "logged") else f" ({alert['status']})"
        where = ",".join(alert["stations"]) + trouble
        line = f"[{stamp}] !! {alert['severity'].upper()} {alert['rule']} {where}: {said}"
        with self._lock:
            print(line, flush=True)


def _lang(tag: str) -> str:
    return tag.split("-")[0].upper().ljust(3)


def add_gate_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--no-gate", action="store_true", help="transcribe songs too (no speech/music gate)"
    )


def add_store_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--no-store", action="store_true", help="don't keep anything in data/wavescribe.db"
    )
    p.add_argument(
        "--audio-days", type=float, default=7.0, help="keep speech audio this long (0: never)"
    )
    p.add_argument(
        "--audio-max-gb", type=float, default=2.0, help="cap on the speech audio archive"
    )
    p.add_argument(
        "--text-days",
        type=float,
        default=365.0,
        help="keep text and translations this long (about 12 GB a year for 4 talk stations)",
    )


def add_json_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--json",
        action="store_true",
        help="one JSON object per line, for scripts and AI agents (experimental)",
    )


def add_settings_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--settings", type=Path, default=DEFAULT_SETTINGS,
        help=f"where the page keeps its changes, as serve's --settings (default: "
        f"{DEFAULT_SETTINGS})",
    )  # fmt: skip


def at_least_one(text: str) -> int:
    """An argparse type: a whole number from 1 up."""
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"give a whole number from 1 up, not {text!r}")
    return n


def add_alert_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--alerts", type=Path, help="alert rules TOML (default: config/alerts.toml)")
    p.add_argument("--no-alerts", action="store_true", help="no keyword alerts")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wavescribe", description=__doc__)
    parser.add_argument("--version", action="version", version=f"wavescribe {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    models = sub.add_parser("models", help="list and download models")
    models_sub = models.add_subparsers(dest="models_command", metavar="ACTION", required=True)
    p = models_sub.add_parser("list", help="show registered models, licences, install state")
    add_json_arg(p)
    p.set_defaults(func=cmd_models_list)
    p = models_sub.add_parser(
        "fetch", help="download models (the only command that downloads by itself)"
    )
    p.add_argument("ids", nargs="*", help="model ids (default: the first model of each chain)")
    p.add_argument(
        "--accept-license", metavar="LICENCE", help="accept an opt-in or restricted licence"
    )
    p.add_argument("--non-commercial", action="store_true", help=NC_HELP)
    p.set_defaults(func=cmd_models_fetch, network=True)
    p = models_sub.add_parser("accept", help="accept an opt-in or restricted model's licence")
    p.add_argument("id", help="the model's id, as `models list` shows it")
    p.add_argument("--license", required=True, help="the licence id, as `models list` shows it")
    p.add_argument("--non-commercial", action="store_true", help=NC_HELP)
    p.set_defaults(func=cmd_models_accept)
    p = models_sub.add_parser("credits", help="attribution for the installed models (Markdown)")
    p.set_defaults(func=cmd_models_credits)

    p = sub.add_parser("listen", help="live transcript (and translation) of one or more stations")
    p.add_argument("stations", nargs="+", metavar="STATION", help="station ids from the config")
    p.add_argument("--config", type=Path, help="stations TOML (default: config/stations.toml)")
    p.add_argument("--no-translate", action="store_true", help="transcribe only")
    p.add_argument("--asr", metavar="MODEL", help="ASR model id (default: first in the chain)")
    p.add_argument("--translator", metavar="MODEL", help="translation model id")
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    p.add_argument(
        "--source",
        help="override the station's source (one station only): a stream URL, a file, or "
        "sdr:SERIAL/MHZ[?gain=20&center=MHZ] for an RTL-SDR",
    )
    p.add_argument("--realtime", action="store_true", help="replay --source at its own pace")
    p.add_argument("-v", "--verbose", action="store_true", help="log pipeline events to stderr")
    add_gate_arg(p)
    add_store_args(p)
    add_alert_args(p)
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("serve", help="local web page: pick stations, read and listen live")
    p.add_argument("stations", nargs="*", metavar="STATION", help="stations to start right away")
    p.add_argument("--config", type=Path, help="stations TOML (default: config/stations.toml)")
    p.add_argument("--host", default="127.0.0.1", help="address to bind (default: this machine)")
    p.add_argument(
        "--lan", action="store_true", help="let other devices on the LAN read along (read-only)"
    )
    p.add_argument("--port", type=port_number, default=8765, help="port for the page (default "
                   "8765)")  # fmt: skip
    p.add_argument(
        "--behind-proxy", action="store_true",
        help="a reverse proxy or tunnel forwards to this server: treat every request as another "
        "machine's (read-only, feed tokens required)",
    )  # fmt: skip
    p.add_argument(
        "--public-read", action="store_true",
        help="through a proxy, let anyone read the page (what was said, alerts, audio) without a "
        "feed token",
    )  # fmt: skip
    p.add_argument(
        "--allow-remote-ask", action="store_true",
        help="let other machines ask the local model questions (it is busy meanwhile)",
    )  # fmt: skip
    p.add_argument("--open", action="store_true", help="open the page in the default browser")
    add_settings_arg(p)
    p.add_argument("--no-translate", action="store_true", help="transcribe only")
    p.add_argument("--asr", metavar="MODEL", help="ASR model id (default: first in the chain)")
    p.add_argument("--translator", metavar="MODEL", help="translation model id")
    p.add_argument("-v", "--verbose", action="store_true", help="log pipeline events to stderr")
    add_gate_arg(p)
    add_store_args(p)
    add_alert_args(p)
    p.set_defaults(func=cmd_serve)

    alerts = sub.add_parser(
        "alerts",
        help="recent alerts, CAP export, rule and voice checks",
        description="Recent alerts, CAP export, rule and voice checks. Alerts can be missed, "
        "late or wrong: they are a help, never the only warning where safety is at stake.",
    )
    alerts.set_defaults(func=cmd_alerts, alerts_command="list", limit=20, alerts=None,
                        settings=DEFAULT_SETTINGS)  # fmt: skip
    add_json_arg(alerts)
    alerts_sub = alerts.add_subparsers(dest="alerts_command", metavar="ACTION")
    p = alerts_sub.add_parser("list", help="the latest alerts (default)")
    p.add_argument("--limit", type=at_least_one, default=20, help="at most this many (20)")
    add_json_arg(p)
    p = alerts_sub.add_parser("cap", help="one alert as a CAP 1.2 XML document")
    p.add_argument("id", type=int, help="the alert's number, as `alerts list` shows it (#12)")
    p = alerts_sub.add_parser("check", help="which rules a sentence would trigger (dry run)")
    p.add_argument("text", nargs="+", help="the sentence, as it would be said on the radio")
    p.add_argument("--language", required=True, help="BCP-47 tag of the text, e.g. pt-PT")
    p.add_argument("--alerts", type=Path, help="alert rules TOML (default: config/alerts.toml)")
    add_settings_arg(p)
    p = alerts_sub.add_parser(
        "replay", help="run stored transcripts through the rules: what would have been announced"
    )
    p.add_argument("--alerts", type=Path, help="alert rules TOML (try a draft before using it)")
    add_settings_arg(p)
    p.add_argument("--hours", type=float, help="only the last HOURS (default: everything)")
    p.add_argument("--examples", type=int, default=0, help="also show the last N announcements")
    p = alerts_sub.add_parser("test", help="send a test notification and speak each voice")
    p.add_argument("--language", help="only this voice's language")
    p.add_argument("--alerts", type=Path, help="alert rules TOML (default: config/alerts.toml)")
    add_settings_arg(p)

    from wavescribe.radio.cli import add_parser as add_radio_parser

    add_radio_parser(sub, add_store_args, add_alert_args)

    # Both take serve's options, so they come after it.
    from wavescribe.doctor import add_parser as add_doctor_parser
    from wavescribe.service import add_parser as add_service_parser

    add_service_parser(sub)
    add_doctor_parser(sub)

    p = sub.add_parser("search", help="search everything heard so far, in any language")
    p.add_argument(
        "query", nargs="+", help="words to find, at the start of a word (*word: anywhere in one)"
    )
    p.add_argument("--station", help="only this station (its id)")
    p.add_argument("--limit", type=at_least_one, default=30, help="at most this many (30)")
    add_json_arg(p)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("record", help="save a station's stream to a local WAV clip")
    p.add_argument("station", help="station id from the config")
    p.add_argument("--minutes", type=float, default=15.0, help="how long to record (15)")
    p.add_argument("--out", type=Path, help="default: data/bakeoff/<station>-<time>.wav")
    p.add_argument("--config", type=Path, help="stations TOML (default: config/stations.toml)")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("bakeoff", help="compare ASR models on recorded clips")
    p.add_argument("clips", nargs="+", type=Path, help="16 kHz mono WAVs from `record`")
    p.add_argument("--models", required=True, help="comma-separated model ids")
    p.add_argument("--language", required=True, help="BCP-47 language of the clips, e.g. pt-PT")
    p.set_defaults(func=cmd_bakeoff)

    from wavescribe.agent.cli import add_parser as add_agent_parser
    from wavescribe.token_cli import add_parser as add_token_parser

    add_agent_parser(sub)
    add_token_parser(sub)
    _describe(parser)
    return parser


def _describe(parser: argparse.ArgumentParser) -> None:
    """Every command's own --help opens with what it does: its line in the list above it, where
    it has no longer description."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {a.dest: a.help for a in action._choices_actions}
            for name, child in action.choices.items():
                if not child.description and helps.get(name):
                    text = str(helps[name])
                    child.description = text[0].upper() + text[1:] + "."
                _describe(child)


def install_stop_signals() -> None:
    """Stop cleanly on SIGINT and SIGTERM: workers, ffmpeg and sockets close.

    A process started in the background by a non-interactive shell inherits SIGINT as ignored,
    and Python then installs no Ctrl-C handler. The 2026-11-22 soak ran 10 hours past its end
    that way, ignoring the sampler's SIGINT. So SIGINT is re-armed explicitly, and SIGTERM
    (launchd, kill) exits through the same cleanup.
    """
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    install_stop_signals()
    if not getattr(args, "network", False):
        # Offline at runtime: only explicit setup commands may reach the network.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, ValueError) as e:
        print(f"wavescribe: {e}", file=sys.stderr)
        return 1
