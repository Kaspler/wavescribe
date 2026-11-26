# SPDX-License-Identifier: Apache-2.0
"""Alerts as CAP 1.2 (OASIS Common Alerting Protocol) documents.

CAP is the standard emergency-alert format, so any CAP consumer (an alert hub,
a dashboard, a mesh gateway) can take Wavescribe's alerts. One ``<info>`` block carries the
source-language text; each checked translation gets its own ``<info>`` in its language, marked as
machine translation.

Honesty about what we know: a keyword heard on the radio says nothing about how sure or how
urgent the underlying event is, so certainty and urgency are always "Unknown". The CAP severity
maps from the rule's configured severity. A station alert (a station gone silent, an RDS traffic
announcement) was seen on the station's status rather than heard, and its note says so.
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from typing import Any

NS = "urn:oasis:names:tc:emergency:cap:1.2"
SEVERITY = {"critical": "Extreme", "high": "Severe", "medium": "Moderate", "low": "Minor"}
CATEGORY = {"silence": "Infra", "traffic": "Transport"}  # station alerts; words are "Other"


def _seconds(epoch: float) -> int:
    """Whole seconds; 0 (the epoch, as in events.iso) for a time the calendar can't hold: a
    wrong clock, in a database from before times were checked."""
    try:
        dt.datetime.fromtimestamp(int(epoch), dt.UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return 0
    return int(epoch)


def _time(epoch: float) -> str:
    # CAP wants an explicit offset ("+00:00"), never "Z".
    return dt.datetime.fromtimestamp(_seconds(epoch), dt.UTC).isoformat()


def to_cap(alert: dict[str, Any], *, station_names: dict[str, str] | None = None,
           sender: str = "wavescribe@localhost") -> str:  # fmt: skip
    """A CAP 1.2 XML document for one alert (a ``Store.alert()`` payload)."""
    names = station_names or {}
    stations = [names.get(s, s) for s in alert["stations"]]
    root = ET.Element("alert", xmlns=NS)

    def sub(parent: ET.Element, tag: str, text: str) -> ET.Element:
        el = ET.SubElement(parent, tag)
        el.text = text
        return el

    sub(root, "identifier", f"wavescribe-{alert['id']}-{_seconds(alert['first_at'])}")
    sub(root, "sender", sender)
    sub(root, "sent", _time(alert["last_at"]))
    sub(root, "status", "Actual")
    sub(root, "msgType", "Alert")
    sub(root, "scope", "Public")  # derived from public broadcasts
    kind = alert.get("kind") or "keyword"
    if kind in CATEGORY:
        note = f"Seen by Wavescribe on the station's status ({kind}), not in what was said."
    else:
        note = (
            f"Heard on the radio by Wavescribe; {alert['mentions']} mention(s). "
            "Keyword match on speech recognition output: verify before acting."
        )
    sub(root, "note", note)

    def info(lang: str, description: str, machine_translation: str | None) -> None:
        el = ET.SubElement(root, "info")
        sub(el, "language", lang)
        sub(el, "category", CATEGORY.get(kind, "Other"))
        sub(el, "event", alert["rule"])
        sub(el, "urgency", "Unknown")
        sub(el, "severity", SEVERITY.get(alert["severity"], "Unknown"))
        sub(el, "certainty", "Unknown")
        sub(el, "effective", _time(alert["first_at"]))
        sub(el, "senderName", f"Wavescribe: {', '.join(stations)}")
        sub(el, "headline", f"{alert['rule']}: {', '.join(stations)}"[:160])
        sub(el, "description", description)
        for name, value in (
            ("terms", ", ".join(alert["terms"])),
            ("stations", ", ".join(alert["stations"])),
            ("mentions", str(alert["mentions"])),
            ("machine_translation", machine_translation),
        ):
            if value:
                p = ET.SubElement(el, "parameter")
                sub(p, "valueName", name)
                sub(p, "value", value)

    info(alert["language"], alert["quote"], None)
    for lang, text in alert.get("summaries", {}).items():
        info(lang, text, alert.get("summaries_model") or "unknown")
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
