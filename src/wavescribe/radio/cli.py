# SPDX-License-Identifier: Apache-2.0
"""``wavescribe radio``: band plans, scans, what was heard, live RDS, and tuning a frequency."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

from wavescribe.radio.bandplan import (
    AUTO,
    POLICIES,
    REGIONS,
    default_region,
    mhz_text,
    region_band,
    region_bands,
)
from wavescribe.radio.words import masked_serial, receiver_name


def _region(args: argparse.Namespace, config: Any = None) -> str:
    return args.region or (config.radio_region if config is not None else None) or AUTO


def _config(args: argparse.Namespace) -> Any:
    from wavescribe.config import ConfigError, load_stations, resolve_stations_path
    from wavescribe.settings import Settings, effective_config

    try:
        base = load_stations(resolve_stations_path(args.config))
    except ConfigError:
        return None
    from wavescribe.settings import apply_radio_policies

    data = Settings().data
    try:
        config, _ = effective_config(base, data)
    except Exception:
        config = base
    apply_radio_policies(base, data)
    return config


class Receiver(NamedTuple):
    serial: str
    gain_db: float
    ppm: int
    name: str  # how output names it: "receiver A", never by its serial (people paste output)
    id: str = ""  # its id in the config; "" for a dongle no receiver uses


def resolve_receiver(name: str, config: Any) -> Receiver:
    """A receiver by its id in the config (``a``) or by serial."""
    if config is not None:
        for d in config.devices:
            if name in (d.id, d.serial):
                return Receiver(d.serial, d.gain_db, d.ppm, f"receiver {receiver_name(d.id)}",
                                d.id)  # fmt: skip
    from wavescribe.sources.sdr import list_dongles

    serials = [d["serial"] for d in list_dongles()]
    if name in serials:
        return Receiver(name, 30.0, 0, f"the dongle {masked_serial(name)}")
    known = ", ".join([d.id for d in config.devices] if config else []) or "none in the config"
    plugged = ", ".join(masked_serial(s) for s in serials) or "none"
    where = "; Settings → Receivers and `wavescribe doctor` show their serials" if serials else ""
    raise ValueError(f"no receiver {name!r} (configured: {known}; plugged in: {plugged}"
                     f"{where})")  # fmt: skip


def holder(rx: Receiver) -> dict[str, Any] | None:
    """What holds ``rx``'s dongle, from macOS's USB registry without opening it
    (radio/holders.py); None when nothing does, and wherever that can't be told."""
    from wavescribe.radio import holders

    return holders.held_elsewhere().get(rx.serial)


def busy(rx: Receiver, on_the_page: str | None, held: dict[str, Any] | None = None) -> Exception:
    """Why ``rx`` can't be opened, in words to act on. It names what holds the dongle when the
    USB registry says (``held``, else read now), and nothing when that can't be known for sure:
    another serve on this machine may hold another dongle. The holder is most often
    Wavescribe's own server, which a scan from the page pauses for as long as it takes
    (``on_the_page`` says where; None where the page can't do it)."""
    from wavescribe.radio.scan import ScanError

    held = held or holder(rx)
    if held is None:
        serve = "; if it is serve, " + (
            f"do it from the page instead: {on_the_page}" if on_the_page else "stop it"
        )
        return ScanError(f"{rx.name} is busy: another program has it open (`wavescribe serve`, "
                         "an SDR program, or another rtl_ tool). Close it, or use another "
                         f"receiver{serve}")  # fmt: skip
    if held.get("serve"):
        instead = f"Do it from the page instead: {on_the_page}. Or stop serve" if on_the_page else (
            "Stop serve")  # fmt: skip
        return ScanError(f"{rx.name} is busy: Wavescribe itself has it open (`wavescribe serve`, "
                         f"process {held['wavescribe_pid']}). {instead}, or use another "
                         "receiver")  # fmt: skip
    if held.get("other_wavescribe"):
        return ScanError(f"{rx.name} is busy: another Wavescribe command has it open (process "
                         f"{held['wavescribe_pid']}). Wait for it to end or stop it, or use "
                         "another receiver")  # fmt: skip
    return ScanError(f"{rx.name} is busy: {held['program']} (process {held['pid']}) has it open. "
                     "Close it, or use another receiver")  # fmt: skip


def cmd_bands(args: argparse.Namespace) -> int:
    region = _region(args, _config(args))
    if region == AUTO:
        print(
            f"# band plan for region {default_region()}, from this computer's locale; until "
            "a region is set, each frequency takes the strictest class any region gives it"
        )
        print(f"# set one with --region ({', '.join(REGIONS)}) or [radio] region")
    else:
        print(f"# band plan for region {region} (change with --region: {', '.join(REGIONS)})")
    for b in region_bands(region):
        chans = f", {len(b.channels)} channels" if b.channels else ""
        step = f", {b.step_hz / 1e3:g} kHz raster" if b.step_hz else ""
        how = b.mode or "digital: no decoder"
        out = "" if b.to_dict()["tunable"] else " (outside the receiver's range)"
        print(f"{b.id:18} {b.start_hz / 1e6:9.4f}-{b.stop_hz / 1e6:<9.4f} {how:15} "
              f"{b.privacy:9} {b.name}{step}{chans}{out}")  # fmt: skip
    return 0


def _parse_range(spec: str, region: str) -> tuple[int, int, str]:
    from wavescribe.radio.scan import SWEEP_LIMITS_HZ, band_range

    if spec in ("all", "everything"):
        return SWEEP_LIMITS_HZ[0], SWEEP_LIMITS_HZ[1], "the whole tuner range"
    if ":" in spec:  # labelled as the page labels it, since the page shows saved scans too
        lo, hi = (float(x) for x in spec.split(":", 1))
        return round(lo * 1e6), round(hi * 1e6), f"{lo:g} to {hi:g} MHz"
    lo_hz, hi_hz = band_range(spec, region)
    band = region_band(spec, region)
    return lo_hz, hi_hz, band.name if band is not None else spec


FIND = "Find → On the air, which pauses the receiver's stations for the scan"


def cmd_scan(args: argparse.Namespace) -> int:
    from wavescribe.radio.catalog import Catalog
    from wavescribe.radio.scan import Busy, Progress, fit_range, scan

    config = _config(args)
    region = _region(args, config)
    rx = resolve_receiver(args.receiver, config)
    serial, gain, ppm = rx.serial, rx.gain_db, rx.ppm
    if args.gain is not None:
        gain = args.gain
    if args.ppm is not None:
        ppm = args.ppm
    start, stop, what = _parse_range(args.range, region)
    start, stop, cut = fit_range(start, stop)  # a range partly out of reach is said, not hidden
    if cut:
        print(f"# {cut}", file=sys.stderr)
    print(f"# scanning {what} on {rx.name} at {gain:g} dB gain, region {region}", file=sys.stderr)
    t0 = time.time()
    last = [""]

    def progress(p: Progress) -> None:
        line = f"\r  {p.phase} {p.done}/{p.total}   "
        if line != last[0] and sys.stderr.isatty():
            sys.stderr.write(line)
            sys.stderr.flush()
            last[0] = line

    catalog = Catalog()
    _old_spurs(catalog, rx, args.receiver)
    try:
        sw, signals = scan(serial, start, stop, region, gain_db=gain, ppm=ppm,
                           rds=not args.no_rds, rds_seconds=args.rds_seconds,
                           min_snr_db=args.min_snr, watch_s=args.watch * 60,
                           spurs=catalog.spurs(serial), on_progress=progress)  # fmt: skip
    except Busy:
        raise busy(rx, FIND) from None
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 40 + "\r")
    device = next((d.id for d in config.devices if d.serial == serial), None) if config else None
    catalog.record(signals, serial=serial, start_hz=start, stop_hz=stop, sweep=sw,
                   device=device, what=what)  # fmt: skip
    _warn(sw)
    print(f"# {_found(signals, time.time() - t0)}; kept in data/radio/heard.json",
          file=sys.stderr)  # fmt: skip
    for s in signals:
        print(_signal_line(s.to_dict()))
    return 0


def _found(signals: list[Any], took_s: float) -> str:
    """How many signals a scan found, with lines marked as a receiver's own (another
    receiver's clock, this one's spurs) counted apart: they are listed, but aren't signals."""
    clocks = sum(1 for s in signals if s.clock_hz)
    spurs = sum(1 for s in signals if s.spur and not s.clock_hz)
    real = len(signals) - clocks - spurs
    said = f"{real} signal{'' if real == 1 else 's'} in {took_s:.0f} s"
    apart = []
    if clocks:
        apart.append(f"{clocks} line{' that is' if clocks == 1 else 's that are'} probably "
                     "another receiver's clock")  # fmt: skip
    if spurs:
        apart.append(f"{spurs} of the receiver's own spurs")
    return said + (f", and {' and '.join(apart)}" if apart else "")


def _warn(sw: Any) -> None:
    """What may be wrong with a sweep (the tuner didn't lock, a range had no reading)."""
    for w in getattr(sw, "warnings", None) or []:
        print(f"# warning: {w}", file=sys.stderr)


def _old_spurs(catalog: Any, rx: Receiver, receiver: str) -> None:
    """Spurs learnt by an earlier version, at about 87 kHz bins, miss narrow lines."""
    info = catalog.spur_info().get(rx.serial)
    if info and info.get("count") and not info.get("bin_hz"):
        print(f"# {rx.name}'s spurs were learnt by an earlier version, which missed narrow "
              f"ones: learn them again with `wavescribe radio spurs {receiver} --learn`",
              file=sys.stderr)  # fmt: skip


def _signal_line(s: dict[str, Any]) -> str:
    rds = s.get("rds") or {}
    name = s.get("user_name") or rds.get("ps") or ""
    extra = []
    if rds.get("pty_name") and rds.get("pty"):
        extra.append(rds["pty_name"])
    if rds.get("rt"):
        extra.append(f"“{rds['rt']}”")
    if s.get("clock_hz"):
        extra.append("probably another receiver's clock, not a transmission")
    elif s.get("spur"):
        extra.append("probably the receiver's own spur")
    if (s.get("passes") or 1) > 1:
        extra.append(f"heard in {s.get('seen', 1)} of {s['passes']} sweeps")
    how = s.get("mode") or "—"
    line = (f"{s['mhz']:10.5f} MHz  {s.get('snr_db', 0):5.1f} dB  {how:4} "
            f"{s.get('privacy', ''):9} {name:10} {s.get('label') or 'unlisted'}")  # fmt: skip
    return line + (f"  · {' · '.join(extra)}" if extra else "")


LEARN = "Settings → Receivers, Learn under the receiver's own signals"


def cmd_spurs(args: argparse.Namespace) -> int:
    from wavescribe.radio.catalog import Catalog
    from wavescribe.radio.scan import Busy, Progress, fit_range, learn_spurs

    config = _config(args)
    rx = resolve_receiver(args.receiver, config)
    serial, gain, ppm = rx.serial, rx.gain_db, rx.ppm
    if args.ppm is not None:
        ppm = args.ppm
    catalog = Catalog()
    if args.forget:
        catalog.forget_spurs(serial)
        print(f"Forgot {rx.name}'s spurs.")
        return 0
    if not args.learn:
        freqs = catalog.spurs(serial)
        if not freqs:
            print(f"No spurs learnt for {rx.name}. Disconnect its antenna, then run "
                  f"`wavescribe radio spurs {args.receiver} --learn`.")  # fmt: skip
        else:
            _old_spurs(catalog, rx, args.receiver)
        for f in freqs:
            print(f"{f / 1e6:10.5f} MHz")
        return 0
    start, stop, what = _parse_range(args.range, _region(args, config))
    start, stop, cut = fit_range(start, stop)
    if cut:
        print(f"# {cut}", file=sys.stderr)
    print(f"# learning {rx.name}'s own signals over {what}: is its antenna disconnected?",
          file=sys.stderr)  # fmt: skip

    def progress(p: Progress) -> None:
        if sys.stderr.isatty():
            sys.stderr.write(f"\r  sweep {p.done}/{p.total}   ")
            sys.stderr.flush()

    try:
        sw, freqs = learn_spurs(serial, start, stop, gain_db=args.gain or gain, ppm=ppm,
                                on_progress=progress)  # fmt: skip
    except Busy:
        raise busy(rx, LEARN) from None
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 30 + "\r")
    _warn(sw)
    catalog.save_spurs(serial, freqs, start, stop)
    print(f"# {len(freqs)} spurs kept; scans with {rx.name} will flag them", file=sys.stderr)
    for f in freqs:
        print(f"{f / 1e6:10.5f} MHz")
    return 0


def cmd_heard(args: argparse.Namespace) -> int:
    from wavescribe.radio.catalog import Catalog

    entries = Catalog().entries()
    if not entries:
        where = sys.stderr if args.json else sys.stdout  # with --json, stdout is JSON only
        config = _config(args)
        ids = [d.id for d in config.devices] if config is not None else []
        if ids:
            print(f"Nothing heard yet: scan first, e.g. `wavescribe radio scan {ids[0]} fm`.",
                  file=where)  # fmt: skip
        else:
            print("Nothing heard yet: scan first, e.g. `wavescribe radio scan <receiver> fm`, "
                  "where <receiver> is a dongle's serial (Settings → Receivers) or the "
                  "id of a [[device]] in the stations file.", file=where)  # fmt: skip
        return 0
    if args.json:
        from wavescribe.events import to_json

        config = _config(args)
        ids = {d.serial: d.id for d in config.devices} if config is not None else {}
        for e in entries:
            print(to_json(shareable(e, ids)))
        return 0
    for e in entries:
        ago = (time.time() - e["last_heard"]) / 3600
        mark = "  (not heard in the last scan)" if e.get("missing") else ""
        print(_signal_line(e) + f"  · last heard {ago:.1f} h ago{mark}")
    return 0


def shareable(entry: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """A heard entry for output: the receiver that heard it by its id from the config (by a
    masked serial when no receiver uses that dongle any more), and no serial."""
    out = {k: v for k, v in entry.items() if k != "serial"}
    serial = entry.get("serial")
    if serial and not out.get("device"):
        out["device"] = ids.get(serial) or masked_serial(serial)
    return out


def cmd_rds(args: argparse.Namespace) -> int:
    import os
    import select
    import shutil
    import subprocess
    import threading

    import numpy as np

    from wavescribe.dsp import OFF_CENTRE_HZ
    from wavescribe.dsp.rds import RdsDecoder
    from wavescribe.dsp.wfm import WfmDemodulator, iq_from_u8
    from wavescribe.radio.scan import ScanError
    from wavescribe.sources.sdr import claimed, device_arg, ppm_args

    config = _config(args)
    rx = resolve_receiver(args.receiver, config)
    serial, gain, ppm = rx.serial, rx.gain_db, rx.ppm
    if args.ppm is not None:
        ppm = args.ppm
    rate, freq = 2_400_000, round(args.mhz * 1e6)
    centre = freq - OFF_CENTRE_HZ["wfm"]
    if shutil.which("rtl_sdr") is None:
        raise ScanError("rtl_sdr isn't installed (brew install librtlsdr)")
    proc = subprocess.Popen(["rtl_sdr", "-d", device_arg(serial), "-f", str(centre), "-s",
                             str(rate), "-g", f"{args.gain or gain:g}", *ppm_args(ppm), "-n",
                             str(int(args.seconds * rate)), "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # fmt: skip
    assert proc.stdout is not None and proc.stderr is not None
    # What rtl_sdr says is the only account of a dongle that gave nothing. Read as it comes,
    # so that a full pipe can't stall it.
    said, its_stderr = bytearray(), proc.stderr
    listener = threading.Thread(target=lambda: said.extend(its_stderr.read()), daemon=True)
    listener.start()
    demod, dec = WfmDemodulator(rate, freq - centre), RdsDecoder()
    shown = None
    fd, buf, got = proc.stdout.fileno(), bytearray(), 0
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 5)
            data = os.read(fd, 1 << 20) if ready else b""
            if not data:
                break
            buf += data
            got += len(data)
            while len(buf) >= rate // 2:
                dec(demod.mpx(iq_from_u8(np.frombuffer(bytes(buf[: rate // 2]), np.uint8))))
                del buf[: rate // 2]
            now = dec.info.to_dict()
            key = (now["pi"], now["ps"], now["rt"], now["ta"])
            if key != shown and (now["pi"] or now["ps"]):
                shown = key
                print(f"{time.strftime('%H:%M:%S')}  PI {now['pi'] or '—'}  "
                      f"{now['ps'] or '(name not yet)':10} {now['pty_name'] or ''}"
                      f"{'  TA' if now['ta'] else ''}  {now['rt'] or ''}", flush=True)  # fmt: skip
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    if not got:
        listener.join(2)
        text = said.decode(errors="replace").strip()
        if claimed(text):
            raise busy(rx, "Listen → Tune by hand shows its RDS")
        raise ScanError(f"rtl_sdr gave no samples: {text[-300:] or 'and no reason'}")
    info = dec.info
    print(f"# {info.groups} groups, {info.bad_blocks} bad blocks, {info.corrected} corrected; "
          f"other frequencies: {', '.join(f'{f:g}' for f in sorted(info.af)) or 'none sent'}",
          file=sys.stderr)  # fmt: skip
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    """Listen to and transcribe one frequency: the listen pipeline on a station made here."""
    from wavescribe.cli import cmd_listen
    from wavescribe.config import Station, parse_sdr_source, radio_policy

    config = _config(args)
    region = _region(args, config)
    rx = resolve_receiver(args.receiver, config)
    serial, gain, ppm = rx.serial, rx.gain_db, rx.ppm
    if args.ppm is not None:
        ppm = args.ppm
    mode = args.mode
    if mode is None:
        from wavescribe.radio.bandplan import lookup

        band = lookup(args.mhz * 1e6, region)
        mode = band.mode if band and band.mode else "nfm"
    q = f"gain={args.gain or gain:g}&ppm={ppm}&mode={mode}"
    q += "&ack=1" if args.acknowledge else ""
    if args.squelch is not None:
        q += f"&squelch={args.squelch:g}"
    spec = f"sdr:{serial}/{mhz_text(args.mhz)}?{q}"
    tuning = parse_sdr_source(spec, region)
    privacy, band_id = radio_policy(tuning.freq_hz, mode, region, args.acknowledge, spec)
    defaults = dict(config.defaults) if config is not None else {}
    station = Station(
        id="tuned", name=f"{mhz_text(args.mhz)} MHz", language=args.language,
        translate_to=tuple(args.translate or defaults.get("translate_to", ())),
        alert_languages=tuple(defaults.get("alert_languages", ())), sdr=tuning,
        mhz=(args.mhz,), privacy=privacy, band=band_id,
    )  # fmt: skip
    policy = POLICIES[privacy]
    if policy.text_days == 0:
        args.no_store = True
    print(f"# {mhz_text(args.mhz)} MHz {mode} on {rx.name}: {privacy} radio"
          + ("" if policy.text_days is None else
             " (live only, nothing stored)" if policy.text_days == 0 else
             f" (text kept {policy.text_days:g} days, no audio)"), file=sys.stderr)  # fmt: skip
    args.stations, args.source, args.tuned_stations = ["tuned"], spec, [station]
    args.realtime = False
    return cmd_listen(args)


def add_parser(sub: Any, add_store_args: Any, add_alert_args: Any) -> None:
    radio = sub.add_parser("radio", help="scan bands, see what was heard, tune a frequency")
    rsub = radio.add_subparsers(dest="radio_command", metavar="ACTION", required=True)

    def ppm(p: argparse.ArgumentParser) -> None:
        p.add_argument("--ppm", type=int, help="the dongle's crystal error in parts per million "
                       "(default: the receiver's, else 0)")  # fmt: skip

    def receiver(p: argparse.ArgumentParser) -> None:
        p.add_argument("receiver", help="receiver id from the config (e.g. a) or its serial")

    def gain(p: argparse.ArgumentParser) -> None:
        p.add_argument("--gain", type=float, help="tuner gain in dB (default: the receiver's)")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", type=Path, help="stations TOML (default: config/stations.toml)")
        p.add_argument("--region", choices=REGIONS, help="band plan region (default: from the "
                       "config's [radio] region, else this machine's locale)")  # fmt: skip

    p = rsub.add_parser("bands", help="the band plan: what is where, and its privacy class")
    common(p)
    p.set_defaults(func=cmd_bands)

    p = rsub.add_parser("scan", help="find signals in a band (FM stations named by RDS)")
    receiver(p)
    p.add_argument("range", nargs="?", default="fm",
                   help="a band id (see `radio bands`), START:STOP in MHz, or all")  # fmt: skip
    gain(p)
    ppm(p)
    p.add_argument("--no-rds", action="store_true", help="don't decode FM station names")
    p.add_argument("--rds-seconds", type=float, default=12.0,
                   help="longest listen per 2 MHz of FM for station names")  # fmt: skip
    p.add_argument("--min-snr", type=float, default=8.0, help="dB above the noise to count")
    p.add_argument("--watch", type=float, default=0.0, metavar="MINUTES",
                   help="sweep again and again for this long, to catch radio that talks "
                        "now and then (walkie-talkies, aircraft, amateurs)")  # fmt: skip
    common(p)
    p.set_defaults(func=cmd_scan)

    p = rsub.add_parser("spurs", help="the receiver's own signals, learnt with no antenna")
    receiver(p)
    p.add_argument("range", nargs="?", default="all", help="where to look (default: all)")
    p.add_argument("--learn", action="store_true",
                   help="sweep now (antenna disconnected) and keep what shows up")  # fmt: skip
    p.add_argument("--forget", action="store_true", help="forget the spurs learnt for it")
    gain(p)
    ppm(p)
    common(p)
    p.set_defaults(func=cmd_spurs)

    p = rsub.add_parser("heard", help="everything scans have found here")
    p.add_argument("--config", type=Path, help="stations TOML (default: config/stations.toml)")
    p.add_argument("--json", action="store_true", help="one JSON object per line (experimental)")
    p.set_defaults(func=cmd_heard)

    p = rsub.add_parser("rds", help="live RDS from one FM station: name, type, radiotext")
    receiver(p)
    p.add_argument("mhz", type=float, help="the station's frequency in MHz, e.g. 98.6")
    p.add_argument("--seconds", type=float, default=30.0, help="how long to listen (30)")
    gain(p)
    ppm(p)
    common(p)
    p.set_defaults(func=cmd_rds)

    p = rsub.add_parser(
        "tune", help="listen to and transcribe one frequency",
        description="Listen to one frequency on a receiver and print what is said, as `listen` "
        "does for a station; nothing is saved as a station (Find on the page keeps one). "
        "Radio that isn't broadcast or amateur asks for --acknowledge. See docs/RADIO.md.",
    )  # fmt: skip
    receiver(p)
    p.add_argument("mhz", type=float, help="the frequency in MHz, e.g. 446.00625")
    p.add_argument("--language", required=True, help="what's spoken, as BCP-47 (e.g. es-ES)")
    p.add_argument("--translate", nargs="*", help="translate into these languages")
    p.add_argument("--mode", choices=("wfm", "nfm", "am"), help="default: from the band plan")
    p.add_argument("--squelch", type=float, help="fixed squelch level in dBFS (default: auto)")
    gain(p)
    ppm(p)
    p.add_argument("--acknowledge", action="store_true",
                   help="confirm you may listen to this radio here (docs/RADIO.md)")  # fmt: skip
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    p.add_argument("-v", "--verbose", action="store_true", help="log pipeline events to stderr")
    p.add_argument("--no-translate", action="store_true", help="transcribe only")
    p.add_argument("--no-gate", action="store_true", help="transcribe songs too")
    p.add_argument("--asr", metavar="MODEL", help="ASR model id (default: first in the chain)")
    p.add_argument("--translator", metavar="MODEL", help="translation model id")
    add_store_args(p)
    add_alert_args(p)
    common(p)
    p.set_defaults(func=cmd_tune)

    from wavescribe.radio.watch_cli import add_parsers  # watch, catches, note

    add_parsers(rsub, add_store_args, add_alert_args, common, ppm)
