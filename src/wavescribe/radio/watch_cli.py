# SPDX-License-Identifier: Apache-2.0
"""``wavescribe radio watch``, ``radio catches`` and ``radio note``: watching a band for radio
that only talks now and then, the log of what was caught, and notes on frequencies."""

from __future__ import annotations

import argparse
import sys
import threading
import time
from typing import Any

from wavescribe.radio.bandplan import POLICIES, lookup, mhz_text
from wavescribe.radio.watch import Catch, on_raster, with_notes


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
    """One line per catch as it ends: `[HH:MM:SS] MHz  band, channel  name  seconds  voice`."""

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


def cmd_watch(args: argparse.Namespace) -> int:
    """Watch every channel of a band (or a range) and transcribe whatever is said on any."""
    from wavescribe.cli import cmd_listen
    from wavescribe.config import DEFAULT_SAMPLE_RATE, build_device, build_station
    from wavescribe.radio.catalog import Catalog
    from wavescribe.radio.cli import _config, _region, resolve_receiver
    from wavescribe.radio.watch import plan_centre, plan_window, subscribe

    config = _config(args)
    region = _region(args, config)
    serial, gain, ppm = resolve_receiver(args.receiver, config)
    spec = _window(args.window)
    rate = DEFAULT_SAMPLE_RATE
    watch, mode, width = plan_window(
        spec, region, rate=rate, mode=args.mode, threshold_db=args.threshold
    )
    centre = plan_centre(watch, width, rate)
    device = build_device(
        {
            "id": "cli",
            "serial": serial,
            "center_mhz": centre / 1e6,
            "gain_db": args.gain if args.gain is not None else gain,
            "sample_rate": rate,
            "ppm": args.ppm if args.ppm is not None else ppm,
        },
        f"receiver {args.receiver}",
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
        "device": "cli",
        "watch": list(spec) if isinstance(spec, tuple) else spec,
        "mode": mode,
        "threshold_db": args.threshold,
        "acknowledge": args.acknowledge,
    }
    station = build_station(d, "radio watch", {"cli": device}, region)
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
        f"{mhz_text(watch.lo_hz / 1e6)}-{mhz_text(watch.hi_hz / 1e6)} MHz {mode}, on receiver "
        f"{args.receiver}; {station.privacy} radio{kept}",
        file=sys.stderr,
    )
    printer = CatchPrinter(Catalog().notes())
    stop_printing = subscribe(printer)
    args.stations, args.source, args.tuned_stations = ["watch"], None, [station]
    args.realtime = False
    args.duration = args.minutes * 60 if args.minutes else None
    try:
        return cmd_listen(args)
    finally:
        stop_printing()
        print(f"# {printer.count} caught, {printer.spoken} with speech", file=sys.stderr)


def cmd_catches(args: argparse.Namespace) -> int:
    """What watches caught and kept, newest first, with the words heard in each."""
    from wavescribe.cli import DATA_DIR
    from wavescribe.radio.catalog import Catalog
    from wavescribe.store import Store

    out = sys.stderr if args.json else sys.stdout  # with --json, stdout is JSON only
    path = DATA_DIR / "wavescribe.db"
    rows: list[dict[str, Any]] = []
    if path.exists():
        store = Store(path)
        try:
            rows = store.catches(
                freq_hz=round(args.mhz * 1e6) if args.mhz else None,
                width_hz=args.width * 1e3,
                since=time.time() - args.hours * 3600 if args.hours else None,
                limit=args.limit,
            )
        finally:
            store.close()
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
    p = rsub.add_parser(
        "watch", help="watch a whole band and transcribe whatever is said on any channel"
    )
    p.add_argument("receiver", help="receiver id from the config (e.g. a) or its serial")
    p.add_argument(
        "window", help="a band id (see `radio bands`), or START:STOP in MHz, about 2 MHz at most"
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
    p.add_argument("--minutes", type=float, help="stop after this long (default: Ctrl-C)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--no-translate", action="store_true")
    p.add_argument("--no-gate", action="store_true", help="transcribe music too")
    p.add_argument("--asr")
    p.add_argument("--translator")
    add_store_args(p)
    add_alert_args(p)
    common(p)
    p.set_defaults(func=cmd_watch)

    p = rsub.add_parser("catches", help="what watches caught and kept, with what was said")
    p.add_argument("--mhz", type=float, help="one channel only")
    p.add_argument("--width", type=float, default=0.0, help="with --mhz: kHz either side")
    p.add_argument("--hours", type=float, default=24.0, help="this far back (0: all)")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--json", action="store_true", help="one JSON object per line (experimental)")
    p.set_defaults(func=cmd_catches)

    p = rsub.add_parser("note", help="name a frequency, note something about it, tag it")
    p.add_argument("mhz", type=float)
    p.add_argument("--name", help="what to call it (also its name in `radio heard`)")
    p.add_argument("--note", help="anything worth remembering about it")
    p.add_argument("--tag", nargs="*", help="tags, e.g. family hiking")
    p.add_argument("--clear", action="store_true", help="forget what was noted")
    common(p)
    p.set_defaults(func=cmd_note)
