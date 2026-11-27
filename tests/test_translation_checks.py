# SPDX-License-Identifier: Apache-2.0
"""A translator's reply is kept only when it translates the line: what the model says about the
text instead (a remark, a note in brackets, its fixed "can't translate" reply) is not stored as
what the station said. Every sentence here is invented."""

import threading
from pathlib import Path

import numpy as np
import pytest

from tests.test_pipeline import (
    PLAN,
    EnergyVad,
    FakeTranscriber,
    FakeTranslator,
    run_with,
    station,
)
from tests.test_segmenter import silence, tone
from tests.test_store import result
from wavescribe.pipeline import Pipeline
from wavescribe.store import Store
from wavescribe.translate.checks import (
    DECLINED,
    EMPTY,
    REMARK,
    TOO_LONG,
    UNTRANSLATABLE,
    check_reply,
    is_left_out,
    left_out_flag,
    remark_languages,
)
from wavescribe.translate.llm import SYSTEM_PROMPT, messages

# Recognition gone wrong, as a poor signal gives it: words that are no sentence.
GARBLED = {
    "pt-PT": "A ponte dos trem o sim ontem que verde falar muito do quando.",
    "de": "Der Brücke haben gestern grün wenn sprechen viel das wann nicht ob.",
    "ja": "橋のきのうは緑がとき話す多くいつのをでも",
}
# What a model answers about such text instead of translating it.
REMARKS = {
    "en": "The meaning of the text is unclear because of recognition errors. A coherent "
    "translation cannot be provided.",
    "de": "Der Text ist unverständlich und kann nicht übersetzt werden.",
    "es": "El texto es incomprensible: no se puede traducir.",
    "fr": "Le texte est incompréhensible, il est impossible de traduire cette phrase.",
    "pt": "O texto é incompreensível e não pode ser traduzido.",
    "uk": "Текст незрозумілий через помилки розпізнавання, перекласти його неможливо.",
}
GOOD = {
    "pt-PT": "The bridge was closed yesterday and the trains ran late all morning.",
    "de": "The bridge has been closed since yesterday, nobody knows until when.",
    "ja": "The bridge was green yesterday, and many spoke about when.",
}


@pytest.mark.parametrize("source", sorted(GARBLED))
def test_the_models_fixed_reply_is_no_translation(source: str) -> None:
    for reply in (UNTRANSLATABLE, UNTRANSLATABLE.upper(), f" {UNTRANSLATABLE}.\n",
                  "[untranslatable]", f"{UNTRANSLATABLE} The words make no sentence."):  # fmt: skip
        checked = check_reply(reply, GARBLED[source])
        assert (checked.text, checked.why) == (None, DECLINED)
    # The word itself, said on air, is translated as any other.
    assert check_reply("Untranslatable!", "Intraduisible !").text == "Untranslatable!"


@pytest.mark.parametrize("language", sorted(REMARKS))
def test_a_remark_about_the_text_is_no_translation(language: str) -> None:
    assert language in remark_languages()
    for source in GARBLED.values():
        checked = check_reply(REMARKS[language], source)
        assert (checked.text, checked.why) == (None, REMARK)


@pytest.mark.parametrize("source", sorted(GARBLED))
def test_a_translation_is_kept_as_it_came(source: str) -> None:
    checked = check_reply(f"  {GOOD[source]}\n", GARBLED[source])
    assert (checked.text, checked.why, checked.notes) == (GOOD[source], None, 0)


def test_a_note_in_brackets_is_cut_out_and_the_translation_kept() -> None:
    source = "O presidente da junta falou do orçamento da Flurbe para a ponte."
    for reply, kept in (
        ("The council chair spoke about the Flurbe (a term that may be a misrecognition) budget "
         "for the bridge.", "The council chair spoke about the Flurbe budget for the bridge."),
        ("The council chair spoke about the budget for the bridge (the name is unclear).",
         "The council chair spoke about the budget for the bridge."),
        ("[Note: one word could not be made out] The council chair spoke about the budget.",
         "The council chair spoke about the budget."),
        ("The council chair spoke with [missing name] about the budget.",
         "The council chair spoke with about the budget."),
        ("Le président a parlé du budget du pont [mot manquant].",
         "Le président a parlé du budget du pont."),
        ("Der Vorsitzende sprach über den Haushalt der Flurbe (unverständlich) für die Brücke.",
         "Der Vorsitzende sprach über den Haushalt der Flurbe für die Brücke."),
    ):  # fmt: skip
        checked = check_reply(reply, source)
        assert (checked.text, checked.why, checked.notes) == (kept, None, 1)


def test_other_brackets_stay_they_are_mostly_words_that_were_said() -> None:
    source = "O presidente da junta falou do PRR e disse, ai meu Deus, que não chega."
    for reply in (
        "The council chair spoke about the Recovery Plan (RRP) and said it is not enough.",
        "The council chair spoke about the plan and said (oh dear) that it is not enough.",
        "The council chair spoke about the plan and said that [the money] is not enough.",
        "市长谈到了复苏计划（PRR），说钱不够。",  # noqa: RUF001
    ):
        checked = check_reply(reply, source)
        assert (checked.text, checked.why, checked.notes) == (reply, None, 0)


def test_a_reply_that_is_only_a_note_is_no_translation() -> None:
    full_width = "\uff08聞き取れません\uff09"
    for reply in ("(unintelligible)", "[inaudible]", "[[niet te vertalen]]", full_width):
        checked = check_reply(reply, GARBLED["de"])
        assert (checked.text, checked.why) == (None, REMARK)
    # A line that is in brackets itself (a model's "[music]") is translated in them.
    assert check_reply("[music]", "[música]").text == "[music]"


def test_brackets_the_original_has_are_kept_unless_they_are_a_note() -> None:
    source = "A taxa (de dois por cento) sobe amanhã."
    kept = check_reply("The rate (of two per cent) goes up tomorrow.", source)
    assert (kept.text, kept.notes) == ("The rate (of two per cent) goes up tomorrow.", 0)
    cut = check_reply(
        "The rate (of two per cent) goes up tomorrow (the source text may hold a recognition "
        "error).",
        source,
    )
    assert (cut.text, cut.notes) == ("The rate (of two per cent) goes up tomorrow.", 1)


def test_a_reply_of_several_lines_keeps_the_one_that_translates() -> None:
    source = "A ponte fechou ontem e os comboios andaram atrasados toda a manhã."
    good = GOOD["pt-PT"]
    for reply, notes in (
        (f"Here is the translation:\n\n{good}", 1),
        (f"Hier folgt der Satz auf Englisch:\n{good}", 1),  # a heading in any language
        (f'Based on the transcript:\n\n"{good}"\n\n*(Note: one name may be a transcription '
         'error for another (a guess).)*', 2),
        (f"{good}\n\n(the last word is unclear)", 1),
        (f"{good}\n\nNota do tradutor: há um erro de reconhecimento no nome.", 1),
    ):  # fmt: skip
        checked = check_reply(reply, source)
        assert (checked.text, checked.why, checked.notes) == (good, None, notes)
    # Two lines that could both be the translation: nobody can say which.
    two = check_reply(f"{good}\nOr: the bridge closed and the trains were late.", source)
    assert (two.text, two.why) == (None, REMARK)
    # A heading and nothing after it.
    assert check_reply("Here is what I understood:\n\n(nothing)", source).why == REMARK


def test_a_reply_far_longer_than_the_line_is_no_translation() -> None:
    # No language of remarks.toml: only its length gives this remark (in Dutch) away.
    remark = (
        "De betekenis van deze woorden is niet duidelijk, want het lijkt erop dat de spraak "
        "verkeerd is verstaan, en daarom volgt hier geen vertaling."
    )
    assert check_reply(remark, "Verde ponte ob.").why == TOO_LONG
    assert check_reply("", GARBLED["de"]).why == EMPTY
    # Languages grow at different rates: a few characters of Chinese, a long German compound.
    chinese = "谢谢\uff0c明天早上再见。"
    assert check_reply("Thank you, and see you again tomorrow morning.", chinese).text
    assert check_reply(
        "The captain of the Danube steamship company spoke.",
        "Der Donaudampfschifffahrtsgesellschaftskapitän sprach.",
    ).text
    assert check_reply("Yes.", "Sim.").text == "Yes."
    # Hebrew and Arabic write no vowels: their translations run to about twice their length.
    assert check_reply(
        "The bridge has been closed since yesterday and the trains ran late all morning.",
        "הגשר נסגר אתמול והרכבות איחרו כל הבוקר.",
    ).text


def test_a_programme_about_translating_keeps_its_translation() -> None:
    # The station itself talks about recognition errors: its translation says so too.
    source = "O programa de legendas fez vários erros de reconhecimento na transmissão de ontem."
    reply = "The subtitling program made several recognition errors in yesterday's broadcast."
    assert check_reply(reply, source).text == reply
    # Words a programme says are no remark: "the original text" of a law, for one.
    said = "The original text of the law was changed, and nobody could translate the minister."
    source = "O texto original da lei foi mudado e ninguém conseguiu traduzir a ministra."
    assert check_reply(said, source).text == said


def test_the_prompt_names_the_languages_and_gives_the_fixed_reply() -> None:
    assert UNTRANSLATABLE in SYSTEM_PROMPT
    for source, target, names in (
        ("pt-PT", "es", ("Portuguese (Portugal) (pt-PT)", "Spanish (es)")),
        ("de", "uk", ("German (de)", "Ukrainian (uk)")),
        ("ja", "fr", ("Japanese (ja)", "French (fr)")),
    ):
        system, user = messages(GARBLED[source], source, target)
        assert system == {"role": "system", "content": SYSTEM_PROMPT}
        assert all(name in user["content"] for name in names)
        assert user["content"].endswith(GARBLED[source])


class ScriptedLlm:
    """A fake LLM translator: it gets the prompt the real one gets and answers what its script
    says for the line (a remark, the fixed reply, a translation)."""

    model_id = "fake-llm"

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.asked: list[list[dict[str, str]]] = []

    def translate(self, text: str, source: str, target: str) -> str:
        self.asked.append(messages(text, source, target))
        return self.answers[text]


@pytest.mark.parametrize("source", sorted(GARBLED))
def test_garbled_lines_get_a_translation_or_none_and_never_a_remark(source: str) -> None:
    lines = [f"{GARBLED[source]} {i}" for i in range(4)]
    llm = ScriptedLlm({
        lines[0]: REMARKS["en"],
        lines[1]: UNTRANSLATABLE,
        lines[2]: GOOD[source],
        lines[3]: f"{GOOD[source]} (some words may be misrecognised)",
    })  # fmt: skip
    kept = [check_reply(llm.translate(line, source, "en"), line) for line in lines]
    assert [(k.text, k.why) for k in kept] == [
        (None, REMARK), (None, DECLINED), (GOOD[source], None), (GOOD[source], None),
    ]  # fmt: skip
    assert all(UNTRANSLATABLE in asked[0]["content"] for asked in llm.asked)


class RemarksOnLong(FakeTranslator):
    """Fake translator: a remark for the 3 s line, the fixed reply for the 2 s one into French."""

    def translate(self, text: str, source: str, target: str) -> str:
        if text == "3s":
            return REMARKS["en"]
        if target == "fr":
            return UNTRANSLATABLE
        return super().translate(text, source, target)


def test_a_line_whose_translation_was_a_remark_keeps_its_words_and_says_why() -> None:
    audio = np.concatenate([silence(0.5), tone(2), silence(1), tone(3), silence(1)])
    results, status = run_with(
        audio, [station("a", translate_to=("en", "fr"))],
        translator_factory=lambda _e, _p: RemarksOnLong(),
    )  # fmt: skip
    assert [r.transcript.text for r in results] == ["2s", "3s"]
    assert [r.translations for r in results] == [{"en": "en:2s"}, {}]
    assert [r.flags for r in results] == [
        ("untranslated:fr:declined",),
        ("untranslated:en:remark", "untranslated:fr:remark"),
    ]
    # Counted with the lines left untranslated, and not as a machine that can't keep up.
    assert status["dropped"]["mt"] == 3 and status["load"]["state"] == "ok"


def test_an_alerts_translation_that_was_a_remark_is_left_out() -> None:
    pipeline = Pipeline(
        [], PLAN, lambda r: None,
        vad_factory=lambda _path: EnergyVad(),
        transcriber_factory=lambda _e, _p: FakeTranscriber(),
        translator_factory=lambda _e, _p: RemarksOnLong(),
    )  # fmt: skip
    runner = threading.Thread(target=lambda: pipeline.run(until_idle=False), daemon=True)
    runner.start()
    got: list = []
    done = threading.Event()

    def callback(translations: dict, model: str | None) -> None:
        got.append(translations)
        if len(got) == 2:
            done.set()

    pipeline.request_translation("3s", "pt-PT", ["en", "fr"], callback)
    pipeline.request_translation("Olá", "pt-PT", ["en", "fr"], callback)
    assert done.wait(10)
    pipeline.stop()
    assert got == [{}, {"en": "en:Olá"}]


def test_the_store_keeps_why_a_translation_was_left_out(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    r = result("a", GARBLED["pt-PT"], 1000.0)
    r.flags = ("lang:es",)
    store.add_transcript(r)
    r.translations["fr"] = "Le pont était fermé hier."
    r.translations_model = "fake-mt"
    r.flags = (*r.flags, left_out_flag("en", REMARK))
    store.add_translations(r)
    [u] = store.recent("a")
    assert u["translations"] == {"fr": "Le pont était fermé hier."}
    assert u["flags"] == ["lang:es", "untranslated:en:remark"] and is_left_out(u["flags"])

    none = result("a", GARBLED["de"], 1001.0, lang="de")
    store.add_transcript(none)
    none.flags = (left_out_flag("en", DECLINED),)
    store.add_translations(none)  # no translation at all: the flag is stored all the same
    assert store.recent("a")[-1]["flags"] == ["untranslated:en:declined"]
    assert [h["text"] for h in store.search("ponte")] == [GARBLED["pt-PT"]]  # still found


def test_a_following_machine_takes_a_left_out_flag_for_every_translation() -> None:
    from tests.test_remote import EVENT, NOW
    from wavescribe.remote import read_utterance

    tags = [f"x{i:02}-Latn-AQ" for i in range(32)]
    flags = ["lang:es", *(left_out_flag(t, TOO_LONG) for t in tags)]
    heard = read_utterance({**EVENT, "translations": {}, "flags": flags}, now=NOW)
    assert heard.flags == tuple(flags) and is_left_out(heard.flags)
