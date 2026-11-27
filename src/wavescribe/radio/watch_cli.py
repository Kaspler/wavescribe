# SPDX-License-Identifier: Apache-2.0
"""``wavescribe radio watch``, ``radio catches`` and ``radio note``: watching a band for radio
that only talks now and then, the log of what was caught, and notes on frequencies."""

from __future__ import annotations

import argparse
import math
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

from wavescribe.radio.bandplan import POLICIES, lookup, mhz_text
from wavescribe.radio.watch import Catch, Hearing, on_raster, with_notes

if TYPE_CHECKING:
    from wavescribe.sources.sdr import RtlSdrDevice


def _window(spec: str) -> str | tuple[float, float]:
    """A band id, or START:STOP in MHz."""
    if ":" not in spec:
        return spec
    lo, _, hi = spec.partition(":")
    try:
        return float(lo), float(hi)
    except ValueError:
        raise ValueError(f"{spec!r}: a range is START:STOP in MHz, e.g. 446.0:446.2") from None


class CatchPrinter:
    """One line per catch as it ends: `[HH:MM:SS] MHz  band, channel  name  seconds  voice`;
    on stderr, one when the watch goes deaf and one when it hears again (``hearing``)."""

    def __init__(self, names: dict[int, dict[str, Any]] | None = None) -> None:
        self._lock = threading.Lock()
        self._names = names or {}
        self.count = 0
        self.spoken = 0

    def __call__(self, catch: Catch) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(catch.t_start))
        name = (self._names.get(catch.freq_hz) or {}).get("name")
        heard = {True: "voice", False: "no voice", None: "not checked"}[catch.voice]
        if catch.reason == "carrier":
            heard = "carrier, learnt as noise"
        parts = [f"[{stamp}] {mhz_text(catch.freq_hz / 1e6):<10}", catch.label or "?"]
        parts += [f'"{name}"'] if name else []
        parts += [f"{catch.duration:.1f} s", f"{catch.snr_db:.0f} dB"] + ([heard] if heard else [])
        with self._lock:
            self.count += 1
            self.spoken += bool(catch.voice)
            print("  ".join(parts), flush=True)

    def hearing(self, h: Hearing) -> None:
        """A line when the watch goes deaf and one when it hears again: the command line has
        no status to show it in."""
        stamp = time.strftime("%H:%M:%S", time.localtime(h.t))
        said = h.warning or f"hearing again after {h.deaf_s:.0f} s"
        if h.warning is None and h.learnt:
            said += ", with that noise as the new floor"
        with self._lock:
            print(f"# [{stamp}] {said}", file=sys.stderr, flush=True)


class Receiving:
    """Whether the receiver sends, while a watch runs from the command line: a line when it
    stops and one when it is back (it is tried again as under serve), and at the end how long
    the band wasn't heard. A watch that didn't hear the band all along says so, and fails."""

    def __init__(self, device: RtlSdrDevice, name: str) -> None:
        self.device, self.name = device, name
        self.down_since: float | None = None  # when it stopped sending, while it doesn't
        self.lost_s = 0.0  # time it didn't send, before it was back
        self._reconnects = 0
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="watch-receiving")

    def __enter__(self) -> Receiving:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._done.set()
        self._thread.join(2)
        self.check()  # a drop since the last look (stopping on purpose isn't one)

    def _run(self) -> None:
        while not self._done.wait(0.25):
            self.check()

    def check(self) -> None:
        d, now = self.device, time.time()
        if d.reconnects > self._reconnects:  # a run of rtl_sdr ended on its own
            self._reconnects = d.reconnects
            if self.down_since is None and d.heard_at is not None:
                self.down_since = now
                why = next((w for w in reversed(d.said) if _trouble(w)), "")
                why = f" ({_masked(why)})" if why else ""
                _say(f"{self.name} stopped sending{why}; trying again", now)
        if self.down_since is not None and d.connected:
            gap, self.down_since = now - self.down_since, None
            self.lost_s += gap
            _say(f"{self.name} is sending again, after {gap:.0f} s", now)

    def summary(self, printer: CatchPrinter) -> tuple[str, bool]:
        """The last line, and whether the band was heard all along."""
        line = f"# {printer.count} caught, {printer.spoken} with speech"
        if self.down_since is not None:
            stamp = time.strftime("%H:%M:%S", time.localtime(self.down_since))
            line += f"; {self.name} stopped sending at {stamp} and wasn't heard after that"
            return line, False
        if self.lost_s:
            return f"{line}; {self.name} wasn't heard for {self.lost_s:.0f} s", False
        return line, True


def _say(text: str, t: float) -> None:
    print(f"# [{time.strftime('%H:%M:%S', time.localtime(t))}] {text}", file=sys.stderr, flush=True)


def _trouble(line: str) -> bool:
    low = line.lower()
    return any(w in low for w in ("error", "failed", "no matching", "lost", "usb_"))


def _masked(text: str) -> str:
    """rtl_sdr's words with any serial in them cut to its last digits (radio/words.py)."""
    from wavescribe.log import _SN
    from wavescribe.radio.words import masked_serial

    return _SN.sub(lambda m: m[1] + masked_serial(m[2]), text)


def check_models(station: Any, args: argparse.Namespace) -> None:
    """What `listen` will refuse for want of a model (the speech detector, a speech model for
    the language, a translator asked for), said before the receiver is opened."""
    from wavescribe.pipeline import NO_VAD, plan_station
    from wavescribe.registry import ModelError, ModelRegistry

    registry = ModelRegistry.load()
    try:
        registry.resolve("vad")
    except ModelError:
        raise ModelError(NO_VAD) from None
    plan_station(registry, station, args.asr)
    if station.translation_targets and not args.no_translate:
        if args.translator:
            registry.installed(args.translator)
        else:
            registry.resolve("translate")


def minutes(text: str) -> float:
    """An argparse type: how long a watch runs, 0 for until Ctrl-C."""
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"give minutes from 0 up (0: until Ctrl-C), not {text!r}")
    return value


def cmd_watch(args: argparse.Namespace) -> int:
    """Watch every channel of a band (or a range) and transcribe whatever is said on any."""
    from wavescribe.cli import cmd_listen
    from wavescribe.config import DEFAULT_SAMPLE_RATE, build_device, build_station, radio_policy
    from wavescribe.radio.catalog import Catalog
    from wavescribe.radio.cli import _config, _old_spurs, _region, busy, holder, resolve_receiver
    from wavescribe.radio.scan import ScanError
    from wavescribe.radio.watch import plan_centre, plan_window, subscribe, subscribe_hearing
    from wavescribe.sources.sdr import claimed, forget_device, shared_device

    config = _config(args)
    region = _region(args, config)
    rx = resolve_receiver(args.receiver, config)
    spec = _window(args.window)
    rate = DEFAULT_SAMPLE_RATE
    watch, mode, width = plan_window(
        spec, region, rate=rate, mode=args.mode, threshold_db=args.threshold
    )
    centre = plan_centre(watch, width, rate)
    # The acknowledgement as this command takes it, before the station checks it again.
    strictest = watch.strictest_in(region, width)
    radio_policy(
        strictest, mode, region, args.acknowledge, "radio watch", width, "Pass --acknowledge"
    )
    dev_id = rx.id or "cli"
    device = build_device(
        {
            "id": dev_id,
            "serial": rx.serial,
            "center_mhz": centre / 1e6,
            "gain_db": args.gain if args.gain is not None else rx.gain_db,
            "sample_rate": rate,
            "ppm": args.ppm if args.ppm is not None else rx.ppm,
        },
        rx.name,
    )
    defaults = dict(config.defaults) if config is not None else {}
    d: dict[str, Any] = {
        "id": "watch",
        "name": watch.label,
        "language": args.language,
        "languages": args.languages or [],
        "translate_to": args.translate
        if args.translate is not None
        else list(defaults.get("translate_to", ())),
        "alert_languages": list(defaults.get("alert_languages", ())),
        "device": dev_id,
        "watch": list(spec) if isinstance(spec, tuple) else spec,
        "mode": mode,
        "threshold_db": args.threshold,
        "acknowledge": args.acknowledge,
    }
    station = build_station(d, "radio watch", {dev_id: device}, region)
    check_models(station, args)
    held = holder(rx)  # read without opening it, so a held receiver is refused at once
    if held is not None:
        raise busy(rx, None, held)
    policy = POLICIES[station.privacy]
    if policy.text_days == 0:
        args.no_store = True
    kept = (
        ""
        if policy.text_days is None
        else ": shown as it's caught, nothing written down"
        if policy.text_days == 0
        else f": catches and text kept {policy.text_days:g} days, no audio"
    )
    print(
        f"# watching {watch.label}: {len(watch.channels)} channels, "
        f"{mhz_text(watch.lo_hz / 1e6)}-{mhz_text(watch.hi_hz / 1e6)} MHz {mode}, on "
        f"{rx.name}; {station.privacy} radio{kept}",
        file=sys.stderr,
    )
    catalog = Catalog()
    _old_spurs(catalog, rx, args.receiver)  # a watch doesn't use such a list
    printer = CatchPrinter(catalog.notes())
    args.stations, args.source, args.tuned_stations = ["watch"], None, [station]
    args.realtime = False
    args.duration = args.minutes * 60 if args.minutes else None
    # The watch's source finds this device and its settings: a receiver that gives nothing
    # when first started ends the watch at once, instead of being tried again and again.
    sdr = shared_device(device, command_line=True)
    stop_printing = subscribe(printer)
    stop_hearing = subscribe_hearing(printer.hearing)
    receiving = Receiving(sdr, rx.name)
    try:
        with receiving:
            cmd_listen(args)
    except KeyboardInterrupt:
        if sdr.heard_at is not None:
            print(receiving.summary(printer)[0], file=sys.stderr)
        raise
    finally:
        stop_printing()
        stop_hearing()
        forget_device(rx.serial)
    if sdr.unopened is not None:
        if claimed(sdr.unopened):
            raise busy(rx, None)
        said = _masked(sdr.unopened.splitlines()[-1])
        raise ScanError(f"{rx.name} sent nothing, so nothing was watched: {said}")
    if sdr.heard_at is None:
        print(f"# nothing was watched: the time was up before {rx.name} sent anything",
              file=sys.stderr)  # fmt: skip
        return 1
    line, whole = receiving.summary(printer)
    print(line, file=sys.stderr)
    return 0 if whole else 1


def cmd_catches(args: argparse.Namespace) -> int:
    """What watches caught and kept, newest first, with the words heard in each."""
    from wavescribe.cli import DATA_DIR
    from wavescribe.radio.catalog import Catalog
    from wavescribe.store import Store

    out = sys.stderr if args.json else sys.stdout  # with --json, stdout is JSON only
    path = DATA_DIR / "wavescribe.db"
    rows: list[dict[str, Any]] = []
    older: list[dict[str, Any]] = []  # the newest before the window, when it has none
    if path.exists():
        store = Store(path)
        try:
            freq = round(args.mhz * 1e6) if args.mhz else None
            where = {"freq_hz": freq, "width_hz": args.width * 1e3}
            since = time.time() - args.hours * 3600 if args.hours else None
            rows = store.catches(**where, since=since, limit=args.limit)
            if not rows and since is not None:
                older = store.catches(**where, limit=1)
        finally:
            store.close()
    if older:
        newest = time.strftime("%Y-%m-%d %H:%M", time.localtime(older[0]["t_start"]))
        print(
            f"Caught and kept, but nothing in the last {args.hours:g} hours: the newest is from "
            f"{newest}. --hours 0 shows them all.",
            file=out,
        )
        return 0
    if not rows:
        print(
            "Nothing caught and kept. A watch's catches are kept as long as text of its class: "
            "by default nothing of private radio (walkie-talkies, 433 MHz devices) is written "
            "down, only shown while `radio watch` runs (docs/RADIO.md).",
            file=out,
        )
        return 0
    with_notes(rows, Catalog().notes())
    if args.json:
        from wavescribe.events import to_json

        for r in rows:
            print(to_json(r))
        return 0
    for r in reversed(rows):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["t_start"]))
        heard = {True: "voice", False: "no voice", None: "not checked"}[r["voice"]]
        name = f'  "{r["name"]}"' if r.get("name") else ""
        print(
            f"{stamp}  {mhz_text(r['mhz']):<10} {r['label'] or '?'}{name}  "
            f"{r['duration']:.1f} s  {r['snr_db']:.0f} dB  {heard}".rstrip()
        )
        for u in r["utterances"]:
            print(f"    {u['language']:<6} {u['text']}")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    """Name a frequency, note something about it, tag it; or show what was noted."""
    from wavescribe.radio.catalog import Catalog
    from wavescribe.radio.cli import _config, _region

    region = _region(args, _config(args))
    hz = on_raster(args.mhz * 1e6, region)
    band = lookup(hz, region)
    catalog = Catalog()
    tags = [] if args.clear else args.tag
    if args.clear:
        args.name, args.note = "", ""
    if args.name is None and args.note is None and tags is None:
        noted = catalog.note_for(hz)
    else:
        noted = catalog.note(hz, name=args.name, note=args.note, tags=tags)
    where = band.label(hz) if band is not None else "no band plan entry"
    print(f"{mhz_text(hz / 1e6)} MHz ({where})")
    if not noted or set(noted) <= {"freq_hz", "t"}:
        print("  nothing noted")
        return 0
    for key in ("name", "note"):
        if noted.get(key):
            print(f"  {key}: {noted[key]}")
    if noted.get("tags"):
        print(f"  tags: {', '.join(noted['tags'])}")
    return 0


def add_parsers(rsub: Any, add_store_args: Any, add_alert_args: Any, common: Any, ppm: Any) -> None:
    from wavescribe.cli import at_least_one  # loaded already: it builds these parsers

    p = rsub.add_parser(
        "watch", help="watch a whole band and transcribe whatever is said on any channel"
    )
    p.add_argument("receiver", help="receiver id from the config (e.g. a) or its serial")
    p.add_argument(
        "window", help="a band id (see `radio bands`), or START:STOP in MHz, about 2.25 MHz at most"
    )
    p.add_argument(
        "--language",
        default="auto",
        help="what's spoken, as BCP-47, or auto (default): each utterance in its own",
    )
    p.add_argument(
        "--languages", nargs="*", help="with auto: the languages it may be (e.g. pt-PT es en)"
    )
    p.add_argument("--translate", nargs="*", help="translate into these languages")
    p.add_argument("--mode", choices=("nfm", "am"), help="default: from the band plan")
    p.add_argument(
        "--threshold",
        type=float,
        default=8.0,
        help="dB over a channel's noise floor that counts as a transmission (default 8)",
    )
    p.add_argument("--gain", type=float, help="tuner gain in dB (default: the receiver's)")
    ppm(p)
    p.add_argument(
        "--acknowledge",
        action="store_true",
        help="confirm you may listen to this radio here (docs/RADIO.md)",
    )
    p.add_argument("--minutes", type=minutes, help="stop after this long (default, or 0: until "
                   "Ctrl-C)")  # fmt: skip
    p.add_argument("-v", "--verbose", action="store_true", help="log pipeline events to stderr")
    p.add_argument("--no-translate", action="store_true", help="transcribe only")
    p.add_argument("--no-gate", action="store_true", help="transcribe music too")
    p.add_argument("--asr", metavar="MODEL", help="ASR model id (default: first in the chain)")
    p.add_argument("--translator", metavar="MODEL", help="translation model id")
    add_store_args(p)
    add_alert_args(p)
    common(p)
    p.set_defaults(func=cmd_watch)

    p = rsub.add_parser("catches", help="what watches caught and kept, with what was said")
    p.add_argument("--mhz", type=float, help="one channel only")
    p.add_argument("--width", type=float, default=0.0, help="with --mhz: kHz either side")
    p.add_argument("--hours", type=float, default=24.0, help="this far back (0: all)")
    p.add_argument("--limit", type=at_least_one, default=100, help="at most this many (100)")
    p.add_argument("--json", action="store_true", help="one JSON object per line (experimental)")
    p.set_defaults(func=cmd_catches)

    p = rsub.add_parser("note", help="name a frequency, note something about it, tag it")
    p.add_argument("mhz", type=float, help="the frequency in MHz, e.g. 446.00625")
    p.add_argument("--name", help="what to call it (also its name in `radio heard`)")
    p.add_argument("--note", help="anything worth remembering about it")
    p.add_argument("--tag", nargs="*", help="tags, e.g. family hiking")
    p.add_argument("--clear", action="store_true", help="forget what was noted")
    common(p)
    p.set_defaults(func=cmd_note)
