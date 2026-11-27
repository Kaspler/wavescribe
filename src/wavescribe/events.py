# SPDX-License-Identifier: Apache-2.0
"""The event objects Wavescribe hands to other programs: the same JSON over the pull feed
(``/feed/v1``) and in pushes to webhooks. Text and metadata only, never audio: an
``audio_url`` points back at this machine when it keeps the recording and its address is known.

Version 1 shapes (docs/FEED.md is the reference):

    utterance  {type, id, node, station{id,name,language,privacy}, time, t_start, duration,
                language, text, translations{lang: text}, flags[], rules[], audio_url?}
    alert      {type, id, node, rule, kind, severity, status, time, first_at, last_at, mentions,
                terms[], reason?, station{…}, stations[{…}], language, text, translations{…},
                context[{id, time, station, text, translations}], segment_id, audio_url?,
                cap_url?, change?}  change (live events and pushes): new, escalated,
                reminder or ended when it was announced; update for anything else
    batch      {type: "batch", node, events[…]}
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from typing import Any

from wavescribe.asr.language import AUTO

FEED_VERSION = 1
# A live feed's event id is a resume point: "<low>" or "<low>,<id>,<id>…". Every utterance up to
# segment id ``low`` that the stream may send was sent (or never will be), and the ids after it
# were sent too: they finished before an older one still waiting for its translation. At most
# this many follow; past it, a resume sends some again (docs/FEED.md, "Resuming").
MAX_AHEAD = 64
_RESUME = re.compile(rf"[0-9]{{1,18}}(?:,[0-9]{{1,18}}){{0,{MAX_AHEAD}}}")


def resume_point(text: str | None) -> tuple[int, frozenset[int]] | None:
    """A feed event id as a client sends it back (``Last-Event-ID``, ``since_id``): the low-water
    mark and the ids above it the client has. A plain segment id is one with nothing above it.
    None for anything else, which a client must never send back: a header the HTTP client
    refuses would fail every reconnect."""
    if not text or not _RESUME.fullmatch(text):
        return None
    low, *have = (int(part) for part in text.split(","))
    return low, frozenset(i for i in have if i > low)


def resume_id(low: int, have: Iterable[int]) -> str:
    """The event id for a resume point (``resume_point`` reads it back)."""
    return ",".join(str(i) for i in (low, *sorted(have)))


class Seen:
    """The utterance events a feed client took lately, by their own ``id`` and text: a resume
    can send a few again (docs/FEED.md, "Resuming"), and those are skipped. Remembers at most
    ``size``."""

    def __init__(self, size: int = 4096) -> None:
        self.size = size
        self._keys: OrderedDict[tuple[int, int], None] = OrderedDict()

    def first(self, ev: Mapping[str, Any]) -> bool:
        """Whether this event wasn't taken before; from now on it was. One without a segment
        id (a sender with no store) can't be told apart, and always counts as new."""
        seg = ev.get("id")
        if type(seg) is not int:
            return True
        key = (seg, hash(ev.get("text")))
        if key in self._keys:
            return False
        self._keys[key] = None
        if len(self._keys) > self.size:
            self._keys.popitem(last=False)
        return True


def iso(epoch: float) -> str:
    """UTC, second precision, with a Z. A value that isn't a time the calendar holds (not a
    number, not finite, far out of range) reads as the epoch: this formats stored rows for
    every feed consumer, and one bad row must not end the stream for all of them."""
    try:
        return dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return "1970-01-01T00:00:00Z"


def local_time(epoch: float, fmt: str) -> str:
    """A stored time as local text for people (the CLI, Ask's prompts); "?" for one the
    calendar can't hold, as in ``iso``: a row from a wrong clock in an old database must not
    end a search or an answer."""
    try:
        return dt.datetime.fromtimestamp(epoch).strftime(fmt)
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def to_json(data: Any) -> str:
    """JSON text for the page, the feed and anything else that leaves the server. Python
    writes a float that isn't finite as ``NaN`` or ``Infinity``, which is not JSON: a browser
    refuses the whole response, so one such number in a stored row would stop the page loading
    for everyone. Here they go out as null."""
    try:
        return json.dumps(data, ensure_ascii=False, allow_nan=False)
    except ValueError:
        return json.dumps(_finite(data), ensure_ascii=False, allow_nan=False)


def _finite(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_finite(v) for v in value]
    return value


def station_ref(sid: str, stations: Mapping[str, Any]) -> dict[str, Any]:
    """A station as events name it. Its privacy class goes with it (radio/bandplan.py), so a
    machine that takes the text in can keep, quote and pass it on by the same class."""
    s = stations.get(sid)
    if s is None:
        return {"id": sid, "name": sid, "language": None}
    # A watch's language is "auto": each of its utterances carries the one it was heard in.
    language = None if s.language == AUTO else s.language
    return {"id": s.id, "name": s.name, "language": language, "privacy": s.privacy}


def _langs(translations: Mapping[str, str], languages: Iterable[str]) -> dict[str, str]:
    wanted = list(languages)
    if not wanted:
        return dict(translations)
    return {k: v for k, v in translations.items() if k in wanted}


def utterance_event(
    u: Mapping[str, Any],
    stations: Mapping[str, Any],
    node: Mapping[str, Any],
    *,
    languages: Iterable[str] = (),
    via: Iterable[str] = (),
) -> dict[str, Any]:
    """An utterance (the page's payload shape, live or stored) as a feed event. ``via``: the
    ids of the machines a line from another machine passed through before this one, the one
    that heard it first; this machine's id is the node's (``node.id``)."""
    base = node.get("url")
    seg = u.get("id")
    sender = {"name": node.get("name"), "url": base}
    if node.get("id"):
        sender["id"] = node["id"]
    out: dict[str, Any] = {
        "type": "utterance",
        "version": FEED_VERSION,
        "id": seg,
        "node": sender,
        "station": station_ref(u["station"], stations),
        "time": iso(u["t_start"]),
        "t_start": u["t_start"],
        "duration": u.get("duration"),
        "language": u.get("language"),
        "text": u.get("text", ""),
        "translations": _langs(u.get("translations") or {}, languages),
        "flags": list(u.get("flags") or []),
        "rules": sorted({h["rule"] for h in u.get("hits") or [] if "rule" in h}),
    }
    if base and seg is not None and u.get("audio"):
        out["audio_url"] = f"{base}/api/segments/{seg}/audio.wav"
    if via := list(via):
        out["via"] = via
    return out


def alert_event(
    alert: Mapping[str, Any],
    stations: Mapping[str, Any],
    node: Mapping[str, Any],
    *,
    text: str | None = None,
    translations: Mapping[str, str] | None = None,
    context: Iterable[Mapping[str, Any]] = (),
    languages: Iterable[str] = (),
) -> dict[str, Any]:
    """An alert (``Alert.payload()`` or ``Store.alert()``) as a feed event. ``text`` is what
    was announced (the latest mention when the severity rose), else the first quote."""
    base = node.get("url")
    langs = list(languages)
    ids = list(alert.get("stations") or [])
    seg = alert.get("segment_id")
    out: dict[str, Any] = {
        "type": "alert",
        "version": FEED_VERSION,
        "id": alert.get("id"),
        "node": {"name": node.get("name"), "url": base},
        "rule": alert.get("rule"),
        "kind": alert.get("kind", "keyword"),
        "severity": alert.get("severity"),
        "status": alert.get("status"),
        "time": iso(alert.get("last_at") or alert.get("first_at") or 0),
        "first_at": alert.get("first_at"),
        "last_at": alert.get("last_at"),
        "mentions": alert.get("mentions", 1),
        "terms": list(alert.get("terms") or []),
        "reason": alert.get("reason"),
        "station": station_ref(ids[0], stations) if ids else None,
        "stations": [station_ref(s, stations) for s in ids],
        "language": alert.get("language"),
        "text": text if text is not None else alert.get("quote", ""),
        "translations": _langs(
            translations if translations is not None else alert.get("summaries") or {}, langs
        ),
        "context": [
            {
                "id": c.get("id"),
                "time": iso(c["t_start"]),
                "station": c.get("station"),
                "text": c.get("text", ""),
                "translations": _langs(c.get("translations") or {}, langs),
            }
            for c in context
        ],
        "segment_id": seg,
    }
    if base and seg is not None:
        out["audio_url"] = f"{base}/api/segments/{seg}/audio.wav"
    if base and alert.get("id") is not None:
        out["cap_url"] = f"{base}/api/alerts/{alert['id']}.cap"
    if "change" in alert:  # live events say what changed; stored rows don't
        out["change"] = alert["change"]
    return out


def as_text(ev: Mapping[str, Any], languages: Iterable[str] = ()) -> str:
    """One readable line or two, for chat-style webhooks (ntfy, a bot) that take plain text."""
    langs = list(languages)
    tr = ev.get("translations") or {}
    shown = next((tr[lang] for lang in langs if lang in tr), None) or next(iter(tr.values()), "")
    station = (ev.get("station") or {}).get("name") or "?"
    if ev["type"] == "alert":
        head = f"[{str(ev.get('severity', '')).upper()}] {ev.get('rule')} · {station}"
        head += {"escalated": " (more serious now)", "reminder": " (still being mentioned)",
                 "ended": " (over)"}.get(ev.get("change") or "", "")  # fmt: skip
        body = shown or ev.get("text", "")
        if shown and ev.get("text"):
            body += f"\n({ev['text']})"
        return f"{head}\n{body}"
    if ev["type"] == "batch":
        return "\n".join(as_text(e, langs) for e in ev.get("events", []))
    line = f"{station} {ev.get('time', '')}: {ev.get('text', '')}"
    return line + (f"\n  → {shown}" if shown else "")
