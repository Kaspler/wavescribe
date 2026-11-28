# SPDX-License-Identifier: Apache-2.0
"""Where to look around (radio/scope.py, the band plan's groups): what is offered in a region,
what a first look covers, and that a group needing a confirmation is never tuned without it."""

from __future__ import annotations

import pytest

from wavescribe.radio import bandplan
from wavescribe.radio.scope import default_groups, offered, refused, scope


def test_every_band_has_one_of_the_groups() -> None:
    ids = [g.id for g in bandplan.groups()]
    assert ids == ["broadcast", "aircraft", "ships", "amateur", "weather", "walkie-talkies",
                   "other"]  # fmt: skip
    assert {b.group for b in bandplan.bands()} == set(ids)
    assert bandplan.find("pmr446").to_dict()["group"] == "walkie-talkies"


def test_a_group_with_nothing_a_receiver_tunes_in_the_region_isnt_offered() -> None:
    cept = {o.group.id: o for o in offered("cept")}
    assert "weather" not in cept  # a weather band only the Americas have
    assert [b.id for b in cept["broadcast"].bands] == ["fm"]  # medium wave is below the tuner
    assert "weather" in {o.group.id for o in offered("2")}


def test_a_first_look_covers_only_groups_that_need_no_confirmation() -> None:
    assert default_groups("cept") == ["broadcast", "amateur"]
    assert default_groups("2") == ["broadcast", "amateur", "weather"]


def test_a_group_needing_a_confirmation_is_refused_without_it_and_never_tuned() -> None:
    assert refused(["broadcast", "aircraft"], "cept") == {"aircraft": ("utility",)}
    got = scope(["broadcast", "aircraft"], "cept")
    assert [b.id for b in got.bands] == ["fm"]
    assert got.looked == ("broadcast",)
    assert got.not_looked == ("aircraft", "ships", "amateur", "walkie-talkies", "other")


def test_once_its_class_is_confirmed_a_group_is_visited() -> None:
    assert refused(["aircraft", "ships"], "cept", ["utility"]) == {}
    got = scope(["aircraft", "ships"], "cept", ["utility"])
    assert [b.id for b in got.bands] == ["airband", "marine"]  # lowest first
    assert all(b.privacy == "utility" for b in got.bands)


def test_a_group_of_several_classes_needs_each_confirmed() -> None:
    assert refused(["other"], "cept", ["private"]) == {"other": ("digital",)}
    got = scope(["other"], "cept", ["private"])
    assert [b.id for b in got.bands] == ["srd433"]  # the digital bands stay out
    everything = scope(["other"], "cept", ["private", "digital"])
    assert {b.privacy for b in everything.bands} == {"private", "digital"}


def test_a_group_the_region_doesnt_offer_is_an_error() -> None:
    with pytest.raises(ValueError, match="weather"):
        scope(["weather"], "cept")
