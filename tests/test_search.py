# SPDX-License-Identifier: Apache-2.0
from wavescribe.rules.text import Matcher
from wavescribe.search import match, parse, snippet


def spans(query: str, text: str) -> list[str] | None:
    found = match(parse(query), text)
    return None if found is None else [text[a:b] for a, b in found]


def test_a_word_matches_at_the_start_of_a_word_ignoring_case_and_accents() -> None:
    assert spans("eau", "Le niveau de l'eau monte") == ["eau"]
    assert spans("eau", "Le niveau monte") is None
    assert spans("agua tamega", "As ÁGUAS subiram no rio Tâmega") == ["ÁGUA", "Tâmega"]
    assert spans("agua", "A\u0301gua fria") == ["A\u0301gua"]  # an accent written apart
    assert spans("agua tamega", "Água fria") is None  # every word must be there


def test_marks_count_where_the_keyword_rules_count_them() -> None:
    # Hindi: the nukta makes ज़रा (a little) another word than जरा (old age).
    zara, jara = "ज़रा", "जरा"
    assert spans(zara, f"{jara} आई") is None
    assert spans(zara, f"{zara} रुको") == [zara]
    assert Matcher("hi").find(zara, f"{jara} आई") == []  # what a rule says too
    assert spans("agua", "Água fria") == ["Água"]  # where accents are optional, still folded


def test_a_leading_star_matches_inside_words_and_a_trailing_one_changes_nothing() -> None:
    assert spans("*eau", "Le niveau monte") == ["eau"]
    assert spans("evacu*", "Evacuação imediata") == ["Evacu"]
    assert parse("* ** *eau*") == parse("*eau")


def test_scripts_without_spaces_match_anywhere() -> None:
    assert spans("電が発", "東京都で停電が発生しました") == ["電が発"]
    assert spans("ไฟดับ", "เกิดไฟดับทั้งเมือง") == ["ไฟดับ"]


def test_a_snippet_marks_matches_around_the_first_one() -> None:
    text = "Boa tarde. " * 6 + "As águas do rio subiram durante a noite. " + "Fim. " * 8
    cut = snippet(text, match(parse("agua"), text) or [], ("[", "]"))
    assert cut.startswith("…") and cut.endswith("…") and "As [água]s do rio" in cut
    assert len(cut) <= 82 and " " not in (cut[1], cut[-2])  # cut at spaces
    assert snippet("Água fria", [(0, 4)], ("[", "]")) == "[Água] fria"
