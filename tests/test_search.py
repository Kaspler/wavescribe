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


def test_percent_and_underscore_are_themselves_and_apostrophes_match_each_other(
    tmp_path,
) -> None:
    from tests.test_store import result
    from wavescribe.store import Store

    store = Store(tmp_path / "w.db")
    said = ["Desconto de 5% hoje", "Subida de 5 por cento", "a_b c", "axb c", "it's raining",
            "it\u2019s sunny", "п'ять хвилин", "п\u02bcять годин"]  # fmt: skip
    for i, text in enumerate(said):
        store.add_transcript(result("a", text, 1000.0 + i))

    def found(query: str) -> list[str]:
        return sorted(h["text"] for h in store.search(query))

    assert found("%") == ["Desconto de 5% hoje"]  # no wildcard for everything
    assert found("5%") == ["Desconto de 5% hoje"]
    assert found("*a_") == ["a_b c"]  # not "axb"
    assert found("it's") == found("it\u2019s") == ["it's raining", "it\u2019s sunny"]
    assert found("it's rain") == ["it's raining"]
    assert found("п\u2019ять") == ["п'ять хвилин", "п\u02bcять годин"]
    assert spans("it\u2019s", "it's raining") == ["it's"]
    store.close()
