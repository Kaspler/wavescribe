# SPDX-License-Identifier: Apache-2.0
import sqlite3
import time
from pathlib import Path

import numpy as np
import pytest

from tests.helpers import requires_ffmpeg
from tests.test_pipeline import station
from wavescribe.asr import Transcript
from wavescribe.pipeline import Result
from wavescribe.segment import Utterance
from wavescribe.store import HIT_END, HIT_START, AudioArchive, Store


def result(sid: str, text: str, t: float, lang: str = "pt-PT", **translations: str) -> Result:
    st = station(sid, language=lang)
    samples = np.zeros(16000, dtype=np.float32)
    u = Utterance(sid, 1, samples, t_start=t, t_end=t + 1, ready_at=t + 1.5)
    r = Result(st, u, Transcript(text, lang, "fake-asr"), timings={"asr_ms": 12.0})
    if translations:
        r.translations.update(translations)
        r.translations_model = "fake-mt"
    return r


def test_stores_transcripts_and_translations_and_reads_them_back(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    r = result("a", "Corte de água amanhã no centro", 1000.0)
    sid = store.add_transcript(r)
    assert sid is not None and r.segment_id == sid
    r.translations["en"] = "Water cut tomorrow in the centre"
    r.translations_model = "fake-mt"
    store.add_translations(r)
    assert store.add_transcript(result("a", "", 1001.0)) is None  # nothing heard: not stored

    [u] = store.recent("a")
    assert u["text"] == "Corte de água amanhã no centro"
    assert u["translations"] == {"en": "Water cut tomorrow in the centre"}
    assert u["key"] == f"a:{sid}" and u["final"] and u["audio"] is False
    store.close()

    reopened = Store(tmp_path / "w.db")  # history survives a restart
    assert [x["id"] for x in reopened.recent("a")] == [sid]


def test_recent_pages_backwards(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    for i in range(5):
        store.add_transcript(result("a", f"frase {i}", 1000.0 + i))
    store.add_transcript(result("b", "outra estação", 1002.5))
    assert [u["text"] for u in store.recent("a", limit=2)] == ["frase 3", "frase 4"]
    oldest = store.recent("a", limit=2)[0]["id"]
    assert [u["text"] for u in store.recent("a", limit=2, before_id=oldest)] == [
        "frase 1",
        "frase 2",
    ]


def test_around_gives_a_window_on_both_sides_of_one_utterance(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    ids = []
    for i in range(10):
        ids.append(store.add_transcript(result("a", f"frase {i}", 1000.0 + i)))
        store.add_transcript(result("b", f"outra {i}", 1000.5 + i))  # interleaved ids

    def texts(segment_id: int, limit: int) -> list[str]:
        return [u["text"] for u in store.around("a", segment_id, limit=limit)]

    assert texts(ids[5], 4) == ["frase 4", "frase 5", "frase 6", "frase 7"]
    assert texts(ids[5], 5) == ["frase 3", "frase 4", "frase 5", "frase 6", "frase 7"]
    assert texts(ids[0], 4) == ["frase 0", "frase 1", "frase 2", "frase 3"]  # after fills in
    assert texts(ids[9], 4) == ["frase 6", "frase 7", "frase 8", "frase 9"]  # before fills in
    assert texts(ids[9], 1) == ["frase 9"]
    assert len(texts(ids[5], 300)) == 10
    # Another station's id, or a pruned one, still places the window where it was said.
    assert texts(ids[5] + 1, 2) == ["frase 5", "frase 6"]
    assert store.around("c", ids[5]) == []


def test_search_ignores_accents_and_case_and_finds_word_starts(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    store.add_transcript(result("a", "As ÁGUAS subiram no rio Tâmega", 1000.0))
    store.add_transcript(result("b", "Trânsito normal na A3", 1001.0))
    [hit] = store.search("agua tamega")
    assert hit["station"] == "a" and hit["field"] == "source"
    assert f"{HIT_START}ÁGUA{HIT_END}" in hit["snippet"]
    assert store.search("agua", station_id="b") == []
    assert [h["station"] for h in store.search("A3")] == ["b"]  # short word: LIKE fallback
    assert store.search('"quoted AND NOT ( odd') == []  # FTS syntax in a query is just text


def test_search_matches_whole_words_or_their_starts_unless_asked_for_a_fragment(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "w.db")
    store.add_transcript(result("f", "Coupure d'eau demain", 1000.0, lang="fr"))
    for i in range(5):  # newer, and each has "eau" inside a word
        store.add_transcript(result("f", f"Le niveau monte {i}", 1001.0 + i, lang="fr"))
    store.add_transcript(result("f", "Il va pleuvoir", 1010.0, lang="fr"))
    store.add_transcript(result("f", "Le cheval court", 1011.0, lang="fr"))

    [hit] = store.search("eau", limit=1)  # inside-word hits don't crowd out the limit
    assert hit["text"] == "Coupure d'eau demain" and f"d'{HIT_START}eau{HIT_END}" in hit["snippet"]
    assert len(store.search("*eau")) == 6  # a leading * asks for a fragment
    assert f"niv{HIT_START}eau{HIT_END}" in store.search("*eau")[0]["snippet"]
    assert [h["text"] for h in store.search("coup*")] == ["Coupure d'eau demain"]  # a start
    assert [h["text"] for h in store.search("va")] == ["Il va pleuvoir"]  # short: the same
    assert [h["text"] for h in store.search("*va")] == ["Le cheval court", "Il va pleuvoir"]
    store.close()


def test_search_finds_translations_and_any_script(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    r = result("a", "Corte de energia", 1000.0, en="Power cut", uk="Відключення світла")
    seg = store.add_transcript(r)
    store.add_translations(r)
    store.add_transcript(result("j", "東京都で停電が発生しました", 1001.0, lang="ja"))

    [hit] = store.search("power")
    assert hit["id"] == seg and hit["field"] == "translation" and hit["field_lang"] == "en"
    assert store.search("світла")[0]["id"] == seg
    assert store.search("停電が")[0]["station"] == "j"  # no spaces between words: still found
    assert store.search("電が発")[0]["station"] == "j"  # and anywhere: there are no word starts


def test_find_reaches_two_character_words_where_words_are_that_short(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    store.add_transcript(result("j", "東京都で停電が発生しました", 1000.0, lang="ja"))
    store.add_transcript(result("j", "明日は晴れでしょう", 1001.0, lang="ja"))
    r = result("a", "Corte de energia em toda a cidade", 1002.0, ja="市内全域で停電")
    store.add_transcript(r)
    store.add_translations(r)
    found = store.find(["停電"], since=0.0)  # too short for the index, as most such words are
    assert [u["id"] for u in found] == [3, 1]  # the translation counts; the newest first
    assert [u["id"] for u in store.find(["停電", "東京"], since=0.0)] == [1, 3]  # both terms first
    assert [u["id"] for u in store.find(["停電", "cidade"], since=0.0)] == [3, 1]  # indexed first
    assert [u["id"] for u in store.find(["停電"], since=0.0, until=1001.0)] == [1]
    assert [u["id"] for u in store.find(["停電"], since=0.0, station_ids=["a"])] == [3]
    assert store.find(["de"], since=0.0) == []  # in nearly every Portuguese sentence
    store.close()


@requires_ffmpeg
def test_audio_archive_saves_replays_and_prunes(tmp_path: Path) -> None:
    archive = AudioArchive(tmp_path / "audio", max_bytes=10**9, max_age_days=7, prune_every_s=0)
    store = Store(tmp_path / "w.db", archive=archive)
    now = time.time()
    ids = []
    for i in range(3):
        r = result("a", f"frase {i}", now - 10 + i)
        r.utterance.samples[:] = 0.1 * np.sin(np.arange(16000) / 5)
        ids.append(store.add_transcript(r))
    deadline = time.time() + 20
    while store.counts()["audio_segments"] < 3 and time.time() < deadline:
        time.sleep(0.1)
    assert store.counts()["audio_segments"] == 3
    path = store.audio_file(ids[0])
    assert path is not None and path.suffix == ".ogg"
    pcm = archive.decode(path)
    assert abs(len(pcm) / 2 / 16000 - 1.0) < 0.1  # one second back out

    # Over the size cap: the oldest audio goes first, and the text stays.
    size = store.counts()["audio_bytes"]
    assert store.prune_audio(max_bytes=size - 1, max_age_s=86400) == 1
    assert store.audio_file(ids[0]) is None and not path.exists()
    assert store.audio_file(ids[2]) is not None
    assert len(store.recent("a")) == 3
    # Too old: gone regardless of size.
    assert store.prune_audio(max_bytes=10**9, max_age_s=1) == 2
    store.close()


def test_old_text_is_pruned_with_its_index_entries(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    now = time.time()
    for i in range(30):
        store.add_transcript(result("a", f"frase{i:02d} notícias", now - 86400 * (30 - i)))
    r = result("a", "frase99 notícias", now, en="news")
    store.add_transcript(r)
    store.add_translations(r)
    assert store.prune_text(max_age_s=86400 * 10.5, batch=4) == 20  # several batches
    kept = store.recent("a", limit=100)
    assert [u["text"][:7] for u in kept][:2] == ["frase20", "frase21"] and len(kept) == 11
    assert store.search("frase05") == []  # gone from the index too
    assert [h["text"][:7] for h in store.search("noticias")][-1] == "frase20"
    assert store.search("news")[0]["text"].startswith("frase99")
    assert store.prune_text(max_age_s=86400 * 10.5) == 0


def test_opens_a_version_1_database_and_adds_flags(tmp_path: Path) -> None:
    Store(tmp_path / "w.db").close()
    old = sqlite3.connect(tmp_path / "w.db")  # make it look like a schema-1 database
    old.execute("ALTER TABLE transcripts DROP COLUMN flags")
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()
    store = Store(tmp_path / "w.db")
    r = result("a", "No quiero saber nada de lo que pasó ayer.", 1000.0)
    r.flags = ("lang:es",)
    store.add_transcript(r)
    assert store.recent("a")[0]["flags"] == ["lang:es"]


def test_a_commit_that_fails_does_not_block_later_writes(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    with pytest.raises(sqlite3.IntegrityError), store._tx() as db:
        db.execute("PRAGMA defer_foreign_keys = ON")  # checked at COMMIT, which then fails
        db.execute(
            "INSERT INTO translations (segment_id, lang, text, model) VALUES (999, 'en', 'x', 'm')"
        )
    assert store.add_transcript(result("a", "depois da falha", 1000.0)) is not None
    assert [u["text"] for u in store.recent("a")] == ["depois da falha"]
    store.close()


@requires_ffmpeg
def test_the_audio_archive_outlives_a_directory_it_cannot_create(tmp_path: Path, caplog) -> None:
    root = tmp_path / "audio"
    root.write_text("in the way")  # a file where the archive's directory belongs
    store = Store(tmp_path / "w.db", archive=AudioArchive(root))
    lost = store.add_transcript(result("a", "sem áudio", time.time() - 2))
    deadline = time.time() + 20
    while "could not archive audio" not in caplog.text and time.time() < deadline:
        time.sleep(0.05)
    assert "could not archive audio" in caplog.text
    root.unlink()
    kept = store.add_transcript(result("a", "com áudio", time.time() - 1))
    while store.audio_file(kept) is None and time.time() < deadline:
        time.sleep(0.05)
    assert store.audio_file(kept) is not None and store.audio_file(lost) is None
    store.close()


def test_opens_a_version_5_database_and_adds_the_alerts_class(tmp_path: Path) -> None:
    from wavescribe.alerts import Alert

    Store(tmp_path / "w.db").close()
    old = sqlite3.connect(tmp_path / "w.db")  # make it look like a schema-5 database
    old.execute("DROP INDEX segments_class")
    old.execute("ALTER TABLE alerts RENAME COLUMN privacy TO spare")  # schema 5 had no class
    old.execute(
        "CREATE INDEX segments_privacy ON segments (privacy, t_start) WHERE privacy IS NOT NULL"
    )
    old.execute("INSERT INTO segments (station_id, t_start, t_end) VALUES ('a', 1000, 1001)")
    old.execute(
        "INSERT INTO transcripts (segment_id, lang, text, model) "
        "VALUES (1, 'pt-PT', 'de antes', 'fake-asr')"
    )
    old.execute(
        "INSERT INTO alerts (rule_id, severity, first_at, last_at, stations, language, "
        "quote, terms) VALUES ('water', 'high', 1000, 1000, 'a', 'pt-PT', 'de antes', "
        "'[]')"
    )
    old.execute("PRAGMA user_version = 5")
    old.commit()
    old.close()
    store = Store(tmp_path / "w.db")
    assert store.recent("a")[0]["privacy"] is None  # nobody recorded what it was heard under
    assert store.alerts()[0]["privacy"] is None
    store.add_transcript(result("a", "de agora", 2000.0))
    alert = Alert(
        "fire", "high", ["a"], "pt-PT", "de agora", [], 2000.0, 2000.0, privacy=["broadcast"]
    )
    store.add_alert(alert)
    assert [u["privacy"] for u in store.recent("a")] == [None, "broadcast"]
    assert store.alert(alert.id)["privacy"] == ["broadcast"]
    store.close()


def test_opens_a_version_8_database_and_adds_whether_an_alert_was_checked(tmp_path: Path) -> None:
    from wavescribe.alerts import Alert
    from wavescribe.rules import Hit

    Store(tmp_path / "w.db").close()
    old = sqlite3.connect(tmp_path / "w.db")  # make it look like a schema-8 database
    old.execute("ALTER TABLE alerts DROP COLUMN checked")
    old.execute(
        "INSERT INTO alerts (rule_id, severity, first_at, last_at, stations, language, "
        "quote, terms, privacy) VALUES ('flood', 'high', 1000, 1000, 'a', 'nl-NL', "
        "'van eerder', '[]', 'broadcast')"
    )
    old.execute("PRAGMA user_version = 8")
    old.commit()
    old.close()
    store = Store(tmp_path / "w.db")
    assert store.alerts()[0]["checked"] is None  # from before it was kept: nothing is claimed
    alert = Alert("flood", "medium", ["a"], "nl-NL", "van nu", [], 2000.0, 2000.0, checked=False)
    store.add_alert(alert)
    assert store.alert(alert.id)["checked"] is False
    alert.checked, alert.severity = True, "high"  # a later mention the model confirmed
    store.add_mention(alert, None, Hit("flood", "high", [], [], [], checked=True))
    assert (store.alert(alert.id)["checked"], store.alert(alert.id)["severity"]) == (True, "high")
    plain = Alert("frost", "low", ["a"], "nl-NL", "van nu", [], 2000.0, 2000.0)
    store.add_alert(plain)
    assert store.alert(plain.id)["checked"] is None
    store.close()
    db = sqlite3.connect(tmp_path / "w.db")
    assert db.execute("PRAGMA user_version").fetchone()[0] == 9
    assert db.execute("SELECT checked FROM alerts ORDER BY id").fetchall() == [
        (None,), (1,), (None,),
    ]  # fmt: skip
    db.close()


def test_the_store_prunes_by_itself_while_nothing_is_written(tmp_path: Path) -> None:
    def wait_for(cond) -> None:
        deadline = time.time() + 10
        while not cond() and time.time() < deadline:
            time.sleep(0.02)
        assert cond()

    now = time.time()
    store = Store(tmp_path / "w.db")  # an earlier run that kept everything
    older = store.add_transcript(result("a", "há dois meses", now - 60 * 86400))
    newer = store.add_transcript(result("a", "há dez dias", now - 10 * 86400))
    store.close()

    archive = AudioArchive(tmp_path / "audio", max_age_days=7)
    archive.enabled = False  # recording is switched off: what was recorded still ages out
    archive.root.mkdir()
    store = Store(tmp_path / "w.db", archive=archive, max_text_age_days=30)
    with store._tx() as db:
        for seg in (older, newer):
            (archive.root / f"{seg}.ogg").write_bytes(b"OggS")
            db.execute(
                "UPDATE segments SET audio_path = ?, audio_bytes = 4 WHERE id = ?",
                (f"{seg}.ogg", seg),
            )
    store.keep_pruned()  # at the start of a run, with no station saying anything
    wait_for(lambda: [u["text"] for u in store.recent("a")] == ["há dez dias"])
    wait_for(lambda: store.counts()["audio_segments"] == 0)
    assert list(archive.root.iterdir()) == []
    store.set_retention(text_days=5)  # a shorter time, and still nothing is written
    wait_for(lambda: store.recent("a") == [])
    store.close()


def test_audio_goes_with_its_text_in_a_run_that_records_none(tmp_path: Path) -> None:
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "old.ogg").write_bytes(b"OggS")
    store = Store(tmp_path / "w.db", audio_dir=audio)  # as started with --audio-days 0
    seg = store.add_transcript(result("a", "gravado noutra execução", time.time() - 40 * 86400))
    with store._tx() as db:
        db.execute(
            "UPDATE segments SET audio_path = 'old.ogg', audio_bytes = 4 WHERE id = ?", (seg,)
        )
    assert store.prune_text(max_age_s=30 * 86400) == 1
    assert not (audio / "old.ogg").exists()
    store.close()


def test_audio_encoded_after_its_text_was_pruned_does_not_stay(tmp_path: Path) -> None:
    archive = AudioArchive(tmp_path / "audio")
    archive.enabled = False  # no ffmpeg here: the files are written by hand
    store = Store(tmp_path / "w.db", archive=archive)
    old = store.add_transcript(result("a", "já apagado", time.time() - 40 * 86400))
    new = store.add_transcript(result("a", "ainda aqui", time.time()))
    assert store.prune_text(max_age_s=30 * 86400) == 1
    for name in ("old.ogg", "new.ogg"):
        (archive.root / name).parent.mkdir(parents=True, exist_ok=True)
        (archive.root / name).write_bytes(b"OggS")
    store._saved(old, "old.ogg", 4)  # the encoder finished after the prune
    store._saved(new, "new.ogg", 4)
    assert not (archive.root / "old.ogg").exists()
    assert store.audio_file(new) == archive.root / "new.ogg"
    store.close()


def test_searches_do_not_wait_for_a_write_in_progress(tmp_path: Path) -> None:
    import threading

    store = Store(tmp_path / "w.db")
    store.add_transcript(result("a", "Trânsito normal na A3", 1000.0))
    found: dict[str, list] = {}

    def search() -> None:
        found["short"] = store.search("A3")  # too short for the index: a scan
        found["terms"] = store.find(["trânsito"])
        found["window"] = store.window(since=0.0)

    with store._tx() as db:  # a write that takes its time
        db.execute("INSERT INTO segments (station_id, t_start, t_end) VALUES ('a', 2000, 2001)")
        db.execute(
            "INSERT INTO transcripts (segment_id, lang, text, model) "
            "VALUES (2, 'pt-PT', 'Acidente na A3', 'fake-asr')"
        )
        reader = threading.Thread(target=search)
        reader.start()
        reader.join(timeout=5)
        assert not reader.is_alive()
    for hits in found.values():  # what was committed when they ran
        assert [u["text"] for u in hits] == ["Trânsito normal na A3"]
    assert len(store.search("A3")) == 2
    store.close()


def test_a_row_dated_in_the_future_does_not_hold_back_pruning(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    store._next_text_prune = float("inf")
    now = time.time()
    store.add_transcript(result("a", "from a clock that was wrong", now + 86400 * 365))
    for i in range(5):
        store.add_transcript(result("a", f"old {i}", now - 86400 * 30))
    assert store.prune_text(max_age_s=86400 * 7, batch=2) == 5
    assert [u["text"] for u in store.recent("a")] == ["from a clock that was wrong"]
    store.close()


def test_an_alert_on_an_utterance_pruned_already_is_still_recorded(tmp_path: Path) -> None:
    from wavescribe.alerts import Alert
    from wavescribe.rules import Hit

    store = Store(tmp_path / "w.db")
    store._next_text_prune = float("inf")
    then = time.time() - 30 * 86400  # caught up after a pause, older than the text kept
    gone = store.add_transcript(result("a", "corte de água amanhã", then))
    assert store.prune_text(max_age_s=7 * 86400) == 1
    alert = Alert("water", "high", ["a"], "pt-PT", "corte de água amanhã", ["água"], then, then,
                  segment_ids=[gone])  # fmt: skip
    store.add_alert(alert)
    store.add_mention(alert, gone, Hit("water", "high", ["água"], [], [(9, 13)]))
    kept = store.alert(alert.id)
    assert kept["rule"] == "water" and kept["segment_id"] is None
    store.close()
