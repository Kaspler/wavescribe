# SPDX-License-Identifier: Apache-2.0
"""``wavescribe digest``: the request it sends and the digest as a terminal shows it."""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from wavescribe.digest import reading
from wavescribe.events import local_time
from wavescribe.languages import same_language

TIME_OF_DAY = re.compile(r"(\d{1,2}):(\d{2})")


def since(text: str, *, now: datetime | None = None) -> float:
    """``--since``: seconds since 1970, or a time of day, today or, when that is still to come,
    yesterday (at 00:15, 23:30 is three quarters of an hour ago; at 02:43, 02:50 is nearly a
    day ago, and the digest's first line says "yesterday")."""
    try:
        return float(text)
    except ValueError:
        pass
    m = TIME_OF_DAY.fullmatch(text.strip())
    if m is None or int(m[1]) > 23 or int(m[2]) > 59:
        raise argparse.ArgumentTypeError("give a time of day like 21:30, or seconds since 1970")
    now = now or datetime.now()
    t = now.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
    return (t - timedelta(days=1) if t > now else t).timestamp()


def body(args: argparse.Namespace) -> dict[str, Any]:
    """The request ``POST /api/digest`` takes, from the command's options."""
    out: dict[str, Any] = {"kind": "now" if args.now else "digest"}
    if args.stations:
        out["stations"] = [s.strip() for s in args.stations.split(",") if s.strip()]
    if args.since is not None:
        out["since"] = args.since
    elif args.minutes is not None:
        out["minutes"] = args.minutes
    if getattr(args, "until", None) is not None:
        out["until"] = args.until
    if args.language:
        out["language"] = args.language
    if args.speak:
        out["speak"] = True
    return out


def span(window: Mapping[str, Any], *, dated: bool = False) -> str:
    """A digest's window as its first line shows it: "14:30-16:00", or "yesterday 02:50 to
    today 02:43" when it starts the day before (a time of day still to come, given to
    ``--since``, is yesterday's). ``dated`` (it ends before now, ``--until``): with its days,
    "Tue 03 Mar 14:30-16:00"."""
    start, end = (datetime.fromtimestamp(window[k]) for k in ("since", "until"))
    if dated:
        if start.date() == end.date():
            return f"{end:%a %d %b} {start:%H:%M}-{end:%H:%M}"
        return f"{start:%a %d %b %H:%M} to {end:%a %d %b %H:%M}"
    if start.date() == end.date():
        return f"{start:%H:%M}-{end:%H:%M}"
    return f"yesterday {start:%H:%M} to today {end:%H:%M}"


def render(out: Mapping[str, Any], *, dated: bool = False) -> str:
    """A digest for a terminal: what it covers and who wrote it from how much, the alerts of the
    window, the points, then the moments they rest on. ``dated``: its window ends before now."""
    names = out.get("names") or {}
    shown = ", ".join(names.get(s, s) for s in out["stations"])
    head = f"Now on {shown}" if out["kind"] == "now" else shown
    window = span(out["window"], dated=dated)
    lines = [f"{head}, {window}", *([out["notice"]] if out.get("notice") else [])]
    if out["outcome"] == "points" and out["unread"]:
        unread = ", ".join(f"{names.get(s, s)} ({why})" for s, why in out["unread"].items())
        lines.append(f"Not read: {unread}.")
    lines.append("")
    for a in out["alerts"]:
        summaries = a.get("summaries") or {}
        said = next(
            (t for lang, t in summaries.items() if same_language(lang, out["language"])),
            a.get("quote") or "",
        )
        where = ", ".join(a.get("names") or a["stations"])
        lines.append(f"! {a['severity']} {local_time(a['announced_at'], '%H:%M')} {where}: {said}")
    if out["alerts"]:
        lines.append("")
    lines.append(out["text"])
    if out["sources"]:
        lines.append("")
    for u in out["sources"]:
        when = local_time(u["t_start"], "%H:%M")
        lines.append(
            f"[{u['n']}] {u.get('name') or u['station']} {when}  {reading(u, out['language'])}"
        )
    return "\n".join(lines)
