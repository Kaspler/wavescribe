# SPDX-License-Identifier: Apache-2.0
"""Alert words suggested in each station language: one question per language, what is readable
of a cut reply is kept, and the answer says what came back and what didn't."""

import json
import threading
from types import SimpleNamespace

import pytest

from wavescribe.rules import suggest
from wavescribe.rules.suggest import budget, in_script, read_words
from wavescribe.web import api

REPLIES = {
    "de": '["Regierung", "Bundesregierung", "Kabinett"]',
    "fr": '```json\n["gouvernement", "exécutif"]\n```',
    "he": '["ממשלה", "הממשלה", "קבינט"]',
    "uk": 'Ось слова:\n["уряд", "кабінет міністрів"]',
    "ja": '["政府", "内閣", "せいふ", "キャビネット"]',
    "es": '{"es": ["gobierno", "ejecutivo"]}',
}


class Llm:
    """A fake LLM: answers each language's question from a script, and notes what it was
    asked and with what budget."""

    model_id = "fake-llm"

    def __init__(self, replies: dict[str, str], cut: tuple[str, ...] = ()) -> None:
        self.replies, self.cut = replies, cut
        self.asked: list[tuple[str, int]] = []
        self.last_finish_reason: str | None = None

    def complete(self, messages, *, max_tokens=512, on_text=None) -> str:
        [message] = messages
        lang = next(x for x in self.replies if f"({x})" in message["content"])
        self.asked.append((lang, max_tokens))
        self.last_finish_reason = "length" if lang in self.cut else "stop"
        return self.replies[lang]


def at_once(llm) -> suggest.Submit:
    def submit(run) -> bool:
        run(llm)
        return True

    return submit


def test_each_language_is_asked_on_its_own_with_a_budget_for_the_words() -> None:
    llm = Llm(REPLIES)
    out = suggest.suggest(at_once(llm), ["government"], list(REPLIES))
    assert out["terms"] == {
        "de": ["Regierung", "Bundesregierung", "Kabinett"],
        "fr": ["gouvernement", "exécutif"],
        "he": ["ממשלה", "הממשלה", "קבינט"],
        "uk": ["уряд", "кабінет міністрів"],
        "ja": ["政府", "内閣", "せいふ", "キャビネット"],
        "es": ["gobierno", "ejecutivo"],
    }
    assert out["count"] == 16 and out["empty"] == []
    assert all(e["why"] is None and e["message"] is None for e in out["languages"].values())
    assert "proposals" in out["message"] and "check each one" in out["message"]
    assert [lang for lang, _ in llm.asked] == list(REPLIES)
    assert {tokens for _, tokens in llm.asked} == {budget(1)[1]}
    # More words to translate, more room to answer; never without bound.
    assert budget(1)[1] < budget(5)[1] < budget(30)[1] == budget(300)[1]


def test_a_cut_reply_costs_one_language_and_keeps_its_whole_words() -> None:
    replies = {**REPLIES, "he": '["ממשלה", "הממשלה", "קבי', "de": '["Regier', "fr": "["}
    llm = Llm(replies, cut=("he", "de", "fr"))
    out = suggest.suggest(at_once(llm), ["government"], list(replies))
    assert out["terms"]["he"] == ["ממשלה", "הממשלה"]  # the word that was cut is left out
    assert out["languages"]["he"]["why"] == "cut"
    assert "may have been more" in out["languages"]["he"]["message"]
    assert out["terms"]["de"] == [] and out["languages"]["de"]["why"] == "cut"
    assert out["terms"]["fr"] == [] and out["languages"]["fr"]["why"] == "cut"
    assert out["terms"]["uk"] == ["уряд", "кабінет міністрів"]  # the others are whole
    assert out["empty"] == ["de", "fr"] and out["count"] == 10
    assert "None for German (the model's reply was cut short), French" in out["message"]


def test_a_reply_cut_without_the_model_saying_so_is_still_known_as_cut() -> None:
    # A backend that doesn't say how a reply ended: the list was never closed.
    silent = SimpleNamespace(model_id="x", complete=lambda m, max_tokens=0: '["rząd", "gabinet')
    out = suggest.suggest(at_once(silent), ["government"], ["pl"])
    assert out["languages"]["pl"] == {
        "words": ["rząd"], "why": "cut", "dropped": 0,
        "message": "the model's reply was cut short: there may have been more",
    }  # fmt: skip


def test_a_reply_that_is_no_list_says_so() -> None:
    llm = Llm({"de": "Gern! Hier sind einige Wörter für Sie.", "fr": "[]", "it": '{"a": 1}'})
    out = suggest.suggest(at_once(llm), ["government"], ["de", "fr", "it"])
    assert out["terms"] == {"de": [], "fr": [], "it": []} and out["count"] == 0
    assert [out["languages"][x]["why"] for x in ("de", "fr", "it")] == [
        "unreadable", "none", "unreadable",
    ]  # fmt: skip
    assert out["message"].startswith("No suggestions came back. None for German (the model's")


def test_words_in_another_script_than_the_languages_are_left_out() -> None:
    llm = Llm({
        "he": '["government", "ממשלה", "memshala", "ממשלת NATO"]',
        "uk": '["government", "uriad"]',
        "ja": '["政府", "seifu"]',
        "de": '["Regierung", "правительство", "112"]',
        "tlh": '["nugh"]',  # no CLDR data for it: nothing to check against
    })  # fmt: skip
    out = suggest.suggest(at_once(llm), ["government"], ["he", "uk", "ja", "de", "tlh"])
    assert out["terms"] == {
        "he": ["ממשלה", "ממשלת NATO"], "uk": [], "ja": ["政府"], "de": ["Regierung", "112"],
        "tlh": ["nugh"],
    }  # fmt: skip
    assert out["languages"]["he"]["dropped"] == 2 and out["languages"]["he"]["why"] is None
    assert out["languages"]["uk"]["why"] == "script" and out["languages"]["uk"]["dropped"] == 2
    # A language CLDR has in two scripts takes either, unless the tag names one.
    assert in_script("уряд", "sr") and in_script("vlada", "sr") and in_script("vlada", "sr-Latn")
    assert not in_script("влада", "sr-Latn") and not in_script("vlada", "uk")
    assert in_script("정부", "ko") and in_script("حكومة", "fa") and in_script("ממשלה", "he")


def test_explanations_repeats_and_too_many_words_are_left_out() -> None:
    many = [f"Wort{i}" for i in range(30)]
    reply = json.dumps(["Regierung", " Regierung ", "", 7, "x" * 61, *many])
    out = suggest.suggest(at_once(Llm({"de": reply})), ["government"], ["de"])
    assert out["terms"]["de"] == ["Regierung", *many[:19]]


def test_a_full_queue_and_a_request_out_of_time_say_so_and_ask_no_further() -> None:
    llm = Llm(REPLIES)
    turns = iter([True, False])

    def one_then_full(run) -> bool:
        ok = next(turns)
        if ok:
            run(llm)
        return ok

    out = suggest.suggest(one_then_full, ["government"], ["de", "fr", "he"])
    assert [out["languages"][x]["why"] for x in ("de", "fr", "he")] == [None, "busy", "busy"]
    assert out["terms"]["de"] and len(llm.asked) == 1

    # The second language's job never runs: its wait ends at the time limit, by a clock of the
    # test's own, and the job does nothing if the model gets to it afterwards.
    now = [0.0]
    queued: list = []
    llm = Llm(REPLIES)

    def submit(run) -> bool:
        if not queued:
            run(llm)
        queued.append(run)
        return True

    def wait(done: threading.Event, seconds: float) -> bool:
        if not done.is_set():
            now[0] += seconds
        return done.is_set()

    out = suggest.suggest(submit, ["government"], ["de", "fr", "he"], total_s=120.0,
                          clock=lambda: now[0], wait=wait)  # fmt: skip
    assert [out["languages"][x]["why"] for x in ("de", "fr", "he")] == [None, "timeout", "timeout"]
    assert len(queued) == 2 and now[0] == 120.0
    queued[1](llm)  # the model gets to it after everyone left
    assert [lang for lang, _ in llm.asked] == ["de"]


def test_a_translator_that_is_no_llm_and_one_that_raises() -> None:
    plain = SimpleNamespace(model_id="madlad")
    out = suggest.suggest(at_once(plain), ["government"], ["de", "fr"])
    assert [e["why"] for e in out["languages"].values()] == ["no_llm", "no_llm"]

    def broken(messages, max_tokens=0):
        raise RuntimeError("out of memory")

    out = suggest.suggest(at_once(SimpleNamespace(model_id="x", complete=broken)),
                          ["government"], ["de"])  # fmt: skip
    assert out["languages"]["de"]["why"] == "unreadable" and out["count"] == 0


@pytest.mark.parametrize(
    ("reply", "words", "whole"),
    [
        ('["a", "b"]', ["a", "b"], True),
        ('Sure:\n["a", "b"]\nHope it helps.', ["a", "b"], True),
        ('["a", "b", "c', ["a", "b"], False),
        ('["a \\"quoted\\" b", "c"', ['a "quoted" b', "c"], False),
        ('{"xx": ["a"], "yy": ["b"]}', ["a"], True),
        ('{"xx": ["a", "b', ["a"], False),
        ("no list here", None, False),
        ("", None, False),
        ("[]", [], True),
        # Quotation marks of other scripts, as the model wrote Japanese words once.
        ("[「道路閉鎖」,「停電」]", ["道路閉鎖", "停電"], True),
        ("[«coupure», “inondation”, 『避難", ["coupure", "inondation"], False),
        ("[a, b, c]", None, True),  # a list, but not of quoted words
    ],
)
def test_reading_a_reply(reply: str, words: list[str] | None, whole: bool) -> None:
    assert read_words(reply, "xx") == (words, whole)


def fake_app(llm, backend: str = "mlx-lm", accept: bool = True) -> SimpleNamespace:
    def request_llm(run, name="llm", priority=0.5, **_kw) -> bool:
        assert (name, priority) == ("suggest", 0.4)  # where the questions' jobs go
        if accept:
            run(llm)
        return accept

    mt = (SimpleNamespace(id="some-model", backend=backend), None) if llm else None
    return SimpleNamespace(pipeline=SimpleNamespace(request_llm=request_llm),
                           plan=SimpleNamespace(mt=mt))  # fmt: skip


def suggest_over_the_api(app, languages: list[str]):
    return api.dispatch(app, "POST", "/api/manage/rules/suggest", {},
                        {"terms": ["government", " "], "languages": languages})  # fmt: skip


def test_the_page_is_told_what_came_back_in_each_language() -> None:
    llm = Llm({**REPLIES, "he": '["ממשלה", "הממ'}, cut=("he",))
    status, out = suggest_over_the_api(fake_app(llm), ["de", "he", "de"])
    assert status == 200
    assert sorted(out) == ["at", "count", "empty", "languages", "message", "note", "terms"]
    assert out["terms"] == {"de": ["Regierung", "Bundesregierung", "Kabinett"], "he": ["ממשלה"]}
    assert out["note"] == '["government"]' and out["count"] == 4 and out["empty"] == []
    assert out["languages"]["he"]["why"] == "cut"


def test_no_word_at_all_is_a_refusal_that_says_why_and_never_a_success() -> None:
    # Every reply cut before its first word: what the page once took for suggestions added.
    langs = ("de", "fr", "es", "he", "ar", "ja", "pl", "it")
    cut = Llm(dict.fromkeys(langs, '{"d'), cut=langs)
    status, out = suggest_over_the_api(fake_app(cut), list(cut.replies))
    assert status == 400 and out["code"] == "no_suggestions" and out["count"] == 0
    assert out["error"].startswith("No suggestions came back. None for German (the model's")
    assert not any(out["terms"].values()) and len(out["empty"]) == 8
    assert {e["why"] for e in out["languages"].values()} == {"cut"}

    status, out = suggest_over_the_api(fake_app(Llm(REPLIES), accept=False), ["de", "fr"])
    assert status == 400 and out["code"] == "no_suggestions"
    assert {e["why"] for e in out["languages"].values()} == {"busy"}

    for app in (fake_app(None), fake_app(Llm(REPLIES), backend="ctranslate2-madlad")):
        status, out = suggest_over_the_api(app, ["de"])
        assert status == 400 and out["code"] == "no_llm" and "need an LLM" in out["error"]
    status, out = suggest_over_the_api(fake_app(Llm(REPLIES)), [])
    assert status == 400 and "at least one language" in out["error"]
