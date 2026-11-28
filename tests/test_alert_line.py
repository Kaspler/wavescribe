# SPDX-License-Identifier: Apache-2.0
"""An alert as one short line, for channels that take small messages: what it says, and a cut
that never breaks a character, in any script."""

import unicodedata

import pytest

from tests.test_outputs import ALERT, NODE, STATIONS, Recorder, outputs, wait_for
from wavescribe.events import alert_event, as_line, fit_bytes
from wavescribe.outputs import OutputConfig, OutputError

# Invented announcements, long enough to be cut.
SCRIPTS = {
    "latin": "Évacuação preventiva às 15h: ruas fechadas perto da ponte, sigam as indicações "
             "da proteção civil e não atravessem a zona ribeirinha até nova informação.",
    "cyrillic": "Увага: перекриття дороги біля мосту з 15:00, просимо обирати об'їзд через "  # noqa: RUF001
                "центр міста та стежити за оголошеннями служби порятунку до нового повідомлення.",
    "cjk": "下午三点起桥附近道路封闭请绕行市中心并留意救援部门的最新通知谢谢合作" * 3,
    "emoji": "Pont fermé 🚧🚧 détour 👩‍👩‍👧‍👦 familles 👍🏽 bienvenues 🇫🇷🇵🇹 " * 6,
    "marks": "Café fermé à 15h, réouverture prévue demain " * 6,
}  # fmt: skip


def splits_a_character(text: str, i: int) -> bool:
    """Whether something that belongs to the character before ``i`` starts at ``i``: an accent,
    a joiner or what it joins, a skin tone, the second half of a flag."""
    c = text[i]
    if unicodedata.category(c) in ("Mn", "Mc", "Me") or "‍" in (c, text[i - 1]):
        return True
    if 0x1F3FB <= ord(c) <= 0x1F3FF:
        return True
    regional = 0
    while regional < i and 0x1F1E6 <= ord(text[i - regional - 1]) <= 0x1F1FF:
        regional += 1
    return 0x1F1E6 <= ord(c) <= 0x1F1FF and regional % 2 == 1


@pytest.mark.parametrize("script", sorted(SCRIPTS))
@pytest.mark.parametrize("limit", [40, 41, 42, 43, 57, 100, 137, 200])
def test_a_cut_line_never_breaks_a_character(script: str, limit: int) -> None:
    ev = alert_event({**ALERT, "quote": SCRIPTS[script]}, STATIONS, NODE)
    whole = as_line(ev, 100_000)
    line = as_line(ev, limit)
    data = line.encode("utf-8")
    assert len(data) <= limit and data.decode("utf-8") == line  # whole characters only
    assert "\n" not in line
    if len(whole.encode("utf-8")) <= limit:
        assert line == whole
        return
    assert line.endswith("…")
    kept = line.removesuffix("…")
    assert whole.startswith(kept) and not splits_a_character(whole, len(kept))
    # Little is lost to the cut: at most one character of up to 4 bytes, a joined emoji, and
    # the space or comma before the "…".
    assert len(data) >= limit - 3 * 11


def test_a_cut_backs_off_a_whole_emoji_sequence_and_a_flag() -> None:
    family = "👩‍👩‍👧‍👦"  # 25 bytes: four people held together by joiners
    text = "ab " + family + " cd"
    for limit in range(4, 31):  # it all fits in 31
        kept = fit_bytes(text, limit).removesuffix("…")
        assert text.startswith(kept) and not splits_a_character(text, len(kept))
        assert family[:2] not in kept or family in kept
    flags = "🇵🇹🇫🇷🇩🇪"  # three flags, two letters each
    for limit in range(4, 25):  # it all fits in 25
        kept = fit_bytes(flags + "!", limit).removesuffix("…")
        assert len(kept) % 2 == 0 and flags.startswith(kept)


def test_the_line_says_severity_check_rule_station_then_the_words_as_heard() -> None:
    ev = alert_event(ALERT, STATIONS, NODE, text="Corte de água amanhã\nna zona norte.",
                     translations={"en": "Water cut tomorrow"})  # fmt: skip
    assert as_line(ev) == "[HIGH] water · a: Corte de água amanhã na zona norte."
    unchecked = alert_event({**ALERT, "checked": False, "change": "ended"}, STATIONS, NODE)
    assert as_line(unchecked) == "[HIGH, not checked] water (over) · a: Corte de água"
    quiet = alert_event({**ALERT, "stations": []}, STATIONS, NODE, text="")
    assert as_line(quiet) == "[HIGH] water"  # no station, no words: nothing made up
    assert as_line(ev, 40) == "[HIGH] water · a: Corte de água ama…"  # cut between letters
    assert len(as_line(ev, 40).encode()) == 40


def test_a_one_line_webhook_sends_alerts_as_text_within_its_byte_limit() -> None:
    rec = Recorder()
    outs = outputs([{"id": "pager", "url": "http://h/sms", "format": "line", "max_bytes": 60}], rec)
    long = "Corte de água amanhã entre as nove e as dezassete horas em toda a zona norte."
    outs.on_alert(ALERT, "high", long, {"en": "Water cut tomorrow"})
    wait_for(lambda: rec.requests)
    _url, body, headers = rec.requests[0]
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert body.decode() == "[HIGH] water · a: Corte de água amanhã entre as nove e…"
    assert len(body) <= 60 and "Water" not in body.decode()  # never the translation
    assert outs.configs()[0].to_dict()["max_bytes"] == 60
    assert "max_bytes" not in OutputConfig.from_dict({"id": "h", "url": "http://h"}).to_dict()
    assert OutputConfig.from_dict({"id": "h", "url": "http://h", "format": "line"}).max_bytes == 200
    for bad in ({"max_bytes": 39}, {"max_bytes": 4097}, {"max_bytes": 80.5}, {"max_bytes": "80"},
                {"events": ["alert", "utterance"]}):  # fmt: skip
        with pytest.raises(OutputError):
            OutputConfig.from_dict({"id": "h", "url": "http://h", "format": "line", **bad})
    outs.close()
