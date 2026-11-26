# SPDX-License-Identifier: Apache-2.0
import pytest

from wavescribe.languages import language_name, same_language, translation_targets


@pytest.mark.parametrize(
    ("tag", "name"),
    [
        ("uk", "Ukrainian"),
        ("pt-PT", "Portuguese (Portugal)"),
        ("en", "English"),
        ("zz-QQ", "zz-QQ"),
    ],
)
def test_language_name(tag: str, name: str) -> None:
    pytest.importorskip("babel")
    assert language_name(tag) == name


@pytest.mark.parametrize(
    ("source", "targets", "expected"),
    [
        ("en-GB", ("en",), ()),  # an English station needs no English translation
        ("EN-us", ("en-GB",), ()),  # case and region don't matter
        ("pt-PT", ("en", "pt-BR", "uk"), ("en", "uk")),
        ("he", ("en", "uk"), ("en", "uk")),
        ("de", (), ()),
    ],
)
def test_translation_targets(
    source: str, targets: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    assert translation_targets(source, targets) == expected


def test_same_language() -> None:
    assert same_language("fr-CA", "fr")
    assert not same_language("uk", "en-GB")
