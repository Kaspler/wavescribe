# SPDX-License-Identifier: Apache-2.0
import shutil
from pathlib import Path

import pytest

from wavescribe.alerts import load_alerts
from wavescribe.rules import RuleSet
from wavescribe.rules.text import Matcher, fold


def spans(term: str, text: str, language: str = "pt-PT") -> list[str]:
    return [text[a:b] for a, b in Matcher(language).find(term, text)]


def test_matching_ignores_case_and_accents_and_matches_plurals() -> None:
    text = "INUNDAÇÕES em vários bairros; a protecao civil pede calma."
    assert spans("inundação", text) == ["INUNDAÇÕES"]
    assert spans("Proteção Civil", text) == ["protecao civil"]
    assert fold("Proteção Civil") == "protecao civil"


def test_matching_respects_word_boundaries() -> None:
    # bandwatch's lesson: substring matching made "freq" match inside "centerfreq".
    assert spans("água", "Vamos aguardar pela decisão.") == []
    assert spans("ataque", "O contra-ataque falhou.") == ["ataque"]
    assert spans("cheia", "A sala estava cheia de gente; cheias no Tejo.") == ["cheia", "cheias"]


def test_acronyms_match_exactly() -> None:
    text = "A REN confirmou. A renda subiu. Ren é um nome."
    assert spans("REN", text) == ["REN"]


def test_phrases_match_word_by_word() -> None:
    text = "Há falta de água na cidade, e falta  de  águas noutra."
    assert spans("falta de água", text) == ["falta de água", "falta  de  águas"]
    assert spans("falta de água", "falta de luz e água") == []


def test_languages_without_a_stemmer_match_whole_folded_words() -> None:
    assert Matcher("xx")._stem is None
    assert spans("Pëtrol", "petrol shortage", "xx") == ["petrol"]
    assert spans("petrol", "petrols", "xx") == []


def test_scripts_without_spaces_match_substrings() -> None:
    assert spans("停電", "東京で停電が発生", "ja") == ["停電"]


def test_marks_that_are_part_of_a_word_are_kept() -> None:
    # A Japanese voicing mark: gas is not dregs. Half-width kana still match.
    assert spans("ガス", "ｶﾞｽ漏れ、ガス漏れ。カスではない", "ja") == ["ｶﾞｽ", "ガス"]
    # A Thai tone mark: น้ำ (water) is not นำ (to lead). ำ folds into two characters, and
    # the spans still land on the words as written.
    text = "ประกาศด่วน น้ำท่วมสูง น้ำท่วมถนน"
    assert spans("น้ำท่วม", text, "th") == ["น้ำท่วม", "น้ำท่วม"]
    assert spans("นำ", text, "th") == []
    # Devanagari and Bengali vowel signs belong to their word: वन (forest) is not inside
    # चेतावनी (warning), কম (less) not inside ভূমিকম্প (earthquake), and highlights are whole.
    assert spans("वन", "बाढ़ की चेतावनी", "hi") == []
    assert spans("चेतावनी", "बाढ़ की चेतावनी", "hi") == ["चेतावनी"]
    assert spans("কম", "ভূমিকম্পের সতর্কতা", "bn") == []
    # Where marks are optional they still fold away: Arabic vowel points, written or not.
    assert spans("مدرسة", "هذه مَدْرَسَة قريبة", "ar") == ["مَدْرَسَة"]


def pack(tmp_path: Path) -> Path:
    d = tmp_path / "keywords"
    d.mkdir()
    (d / "pt.toml").write_text(
        'language = "pt"\n[topics]\nwater = ["corte de água", "sem água"]\n'
        'conflict = ["sabotagem"]\n[places]\nlocal = ["Vila Nova"]\n'
    )
    return d


RULES = {
    "rule": [
        {"id": "water", "topics": ["water"], "near": ["local"], "severity": "high"},
        {"id": "conflict", "topics": ["conflict"], "severity": "medium"},
    ]
}


def test_near_a_local_place_raises_the_severity(tmp_path: Path) -> None:
    rules = RuleSet.load(RULES, pack(tmp_path))
    [far] = rules.evaluate("Corte de água em Lisboa amanhã.", "pt-PT")
    [near] = rules.evaluate("Corte de água em Vila Nova amanhã.", "pt-PT")
    assert (far.rule_id, far.severity, far.places) == ("water", "high", [])
    assert (near.severity, near.places) == ("critical", ["Vila Nova"])
    text = "Corte de água em Vila Nova amanhã."
    assert [text[a:b] for a, b in near.spans] == ["Corte de água", "Vila Nova"]


def test_text_heard_as_another_language_never_alarms(tmp_path: Path) -> None:
    # Parakeet sometimes answers in English, and "sabotage" stems like "sabotagem".
    rules = RuleSet.load(RULES, pack(tmp_path))
    [hit] = rules.evaluate("Nuclear sabotagem.", "pt-PT", flags=("lang:en",))
    assert (hit.severity, hit.capped) == ("low", "lang:en")


def test_stations_without_a_pack_get_no_keyword_hits(tmp_path: Path) -> None:
    rules = RuleSet.load(RULES, pack(tmp_path))
    assert rules.evaluate("Corte de água", "es-ES") == []
    assert rules.evaluate("Corte de água", "pt-BR")  # packs are per primary language


def test_real_pack_wins_over_its_example(tmp_path: Path) -> None:
    d = pack(tmp_path)
    (d / "pt.example.toml").write_text('language = "pt"\n[topics]\nwater = ["chuva"]\n')
    rules = RuleSet.load(RULES, d)
    assert rules.evaluate("Corte de água", "pt-PT") and not rules.evaluate("chuva", "pt-PT")


def test_exact_terms_skip_stemming() -> None:
    # Why "=" exists: Snowball stems Portuguese "tornado" like "torna" ("becomes").
    assert spans("tornado", "A situação torna-se difícil.") == ["torna"]
    text = "O tornado levou telhados. A crise tinha tornado tudo difícil."
    assert spans("=o tornado", text) == ["O tornado"]
    assert spans("=água", "As águias ganharam. ÁGUA da torneira, águas.") == ["ÁGUA"]
    assert spans("=agua", "Sem água.") == ["água"]  # case and accents still don't matter


def tracked_example(tmp_path: Path):
    """The tracked example rules and pack (not a private pack that may sit next to them)."""
    kw = tmp_path / "tracked"
    kw.mkdir()
    shutil.copy("config/keywords/pt.example.toml", kw / "pt.example.toml")
    return load_alerts(Path("config/alerts.example.toml"), kw)


def test_tracked_examples_load_and_match(tmp_path: Path) -> None:
    config = tracked_example(tmp_path)
    hits = config.rules.evaluate("Foi declarada situação de calamidade em todo o país.", "pt-PT")
    assert [(h.rule_id, h.severity) for h in hits] == [("civil-protection", "critical")]
    # Named in everyday news: stored, and raised only near home.
    hits = config.rules.evaluate("A Proteção Civil pede a evacuação das casas.", "pt-PT")
    assert [(h.rule_id, h.severity) for h in hits] == [("civil-protection-mention", "low")]
    hits = config.rules.evaluate("A Proteção Civil evacua casas na Vila Exemplo.", "pt-PT")
    assert [(h.rule_id, h.severity) for h in hits] == [("civil-protection-mention", "medium")]
    assert config.languages_for("critical", "pt-PT") == ["en"]
    assert config.languages_for("low", "pt-PT") == []
    assert all(r.id != "station-silent" for r in config.rules.rules)  # not implemented: skipped


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        # A bare string would be read letter by letter: "o" matches most Portuguese sentences.
        ({"terms": {"pt": "fogo"}}, 'rule fire: terms.pt must be a list of strings, e.g. ["fogo"]'),
        ({"terms": ["fogo"]}, "rule fire: terms must be a table of lists, e.g. { pt = "),
        ({"terms": {"pt": ["fogo", 7]}}, "rule fire: terms.pt must be a list of strings"),
        ({"topics": "water"}, 'rule fire: topics must be a list of strings, e.g. ["water"]'),
        ({"topics": ["water"], "near": "local"}, "rule fire: near must be a list of strings"),
        ({"topics": ["water"], "stations": "tsf"}, "rule fire: stations must be a list of strings"),
        ({"topics": ["water"], "min_mentions": "two"}, "rule fire: min_mentions must be a whole"),
        ({"topics": ["water"], "min_mentions": True}, "rule fire: min_mentions must be a whole"),
        ({"topics": ["water"], "verify": "no"}, "rule fire: verify must be true or false"),
        ({"kind": "llm", "prompt": ["leave the area"]}, "rule fire: prompt must be text"),
    ],
)
def test_rule_values_of_the_wrong_shape_are_refused(tmp_path: Path, rule: dict, message: str):
    from wavescribe.alerts import parse_alerts
    from wavescribe.config import ConfigError

    path = Path("config/alerts.toml")
    with pytest.raises(ConfigError) as error:
        parse_alerts({"rule": [{"id": "fire", **rule}]}, path, pack(tmp_path))
    assert str(error.value).startswith(f"{path}: {message}")


def test_a_keyword_pack_of_the_wrong_shape_names_its_file(tmp_path: Path) -> None:
    d = pack(tmp_path)
    (d / "es.toml").write_text('language = "es"\n[topics]\nwater = "corte de agua"\n')
    with pytest.raises(ValueError, match=r"es\.toml: topics\.water must be a list of strings"):
        RuleSet.load(RULES, d)
    (d / "es.toml").write_text('[topics]\nwater = ["corte de agua"]\n')
    with pytest.raises(ValueError, match=r"es\.toml: needs its language, e.g. language = \"pt\""):
        RuleSet.load(RULES, d)
