# SPDX-License-Identifier: Apache-2.0
"""The store: every utterance with its transcript and translations, searchable, in one SQLite file.

WAL mode and one connection behind a lock. Writes are a few rows per utterance, so contention is
negligible. Searches, which may scan, read through a second, read-only connection. Text is kept
for a configurable age (about 1 KB per utterance). Speech audio goes to a bounded
``AudioArchive`` so a timestamp can be replayed; music and silence are never stored.
Alerts, their mentions (for highlighting) and their translated summaries live here too.

Search uses FTS5 with the trigram tokenizer, which matches substrings in any script (languages
written without spaces included), ignoring case and accents: "agua" finds "águas". Queries with a
word shorter than three characters fall back to a LIKE scan (in a phrase, only when all its words
are that short). ``wavescribe.search`` then keeps the hits where each word starts a word, unless
it was written ``*word``, and where each phrase has its words in order.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from wavescribe.audio import SAMPLE_RATE
from wavescribe.config import Station
from wavescribe.log import event
from wavescribe.pipeline import Result
from wavescribe.search import lookups as search_lookups
from wavescribe.search import match as search_match
from wavescribe.search import parse as search_terms
from wavescribe.search import snippet as search_snippet
from wavescribe.search import spellings as search_spellings

if TYPE_CHECKING:
    from wavescribe.alerts import Alert
    from wavescribe.radio.bandplan import Exports
    from wavescribe.radio.watch import Catch
    from wavescribe.rules import Hit

log = logging.getLogger(__name__)

SCHEMA_VERSION = 7  # 2: transcripts.flags; 3: alerts; 4: deliveries, alerts.kind/reason;
# 5: segments.privacy; 6: the class on every row, alerts.privacy; 7: catches, segments.freq_hz
# Snippet markers around matches; the page turns them into <mark>.
HIT_START, HIT_END = "\x02", "\x03"


@dataclass(frozen=True)
class KeptStation:
    """A station as the store keeps it (``Store.stations``), read as ``events.station_ref``
    reads a configured one."""

    id: str
    name: str
    language: str
    privacy: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    language TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    station_id TEXT NOT NULL,
    t_start REAL NOT NULL,              -- epoch seconds
    t_end REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'speech',
    audio_path TEXT,                    -- relative to the audio archive; NULL once pruned
    audio_bytes INTEGER NOT NULL DEFAULT 0,
    privacy TEXT,                       -- the station's privacy class when this was heard;
                                        -- NULL in rows from before every row carried it
    freq_hz INTEGER                     -- where it was heard, for radio; NULL otherwise
);
CREATE INDEX IF NOT EXISTS segments_station_time ON segments (station_id, t_start);
CREATE INDEX IF NOT EXISTS segments_audio ON segments (t_start) WHERE audio_path IS NOT NULL;
CREATE TABLE IF NOT EXISTS transcripts (
    segment_id INTEGER PRIMARY KEY REFERENCES segments (id) ON DELETE CASCADE,
    lang TEXT NOT NULL,
    text TEXT NOT NULL,
    model TEXT NOT NULL,
    confidence REAL,
    latency_ms REAL,
    flags TEXT NOT NULL DEFAULT ''      -- comma-separated ASR check flags, e.g. 'lang:es'
);
CREATE TABLE IF NOT EXISTS translations (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL REFERENCES segments (id) ON DELETE CASCADE,
    lang TEXT NOT NULL,
    text TEXT NOT NULL,
    model TEXT NOT NULL,
    latency_ms REAL,
    UNIQUE (segment_id, lang)
);
-- Alerts: one row per rule firing, with repeats inside the cooldown folded in as mentions.
-- The quote keeps the source text after its transcript is pruned; alerts are small and rare
-- (at most one per rule per cooldown), so they are kept.
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL,             -- highest severity among its mentions
    first_at REAL NOT NULL,
    last_at REAL NOT NULL,
    stations TEXT NOT NULL,             -- comma-separated station ids, first heard first
    language TEXT NOT NULL,             -- of the quote
    quote TEXT NOT NULL,                -- source-language text of the first mention
    terms TEXT NOT NULL,                -- JSON list of the pack terms that matched
    mentions INTEGER NOT NULL DEFAULT 1,
    segment_id INTEGER REFERENCES segments (id) ON DELETE SET NULL,  -- first mention
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|delivered|partial|failed|logged|covered
    delivered_at REAL,                  -- set only once an action confirmed delivery
    delivered_severity TEXT,
    actions TEXT NOT NULL DEFAULT '{}', -- JSON: action -> "ok" or the failure
    kind TEXT NOT NULL DEFAULT 'keyword',  -- keyword|llm
    reason TEXT,                        -- the LLM's reason (LLM rules, verified keyword hits)
    privacy TEXT                        -- comma-separated classes its stations had when they
                                        -- were heard; NULL in rows from before it was kept
);
CREATE INDEX IF NOT EXISTS alerts_last ON alerts (last_at);
CREATE TABLE IF NOT EXISTS alert_mentions (
    alert_id INTEGER NOT NULL REFERENCES alerts (id) ON DELETE CASCADE,
    segment_id INTEGER NOT NULL REFERENCES segments (id) ON DELETE CASCADE,
    severity TEXT NOT NULL,
    terms TEXT NOT NULL,                -- JSON list
    spans TEXT NOT NULL,                -- JSON [[start, end], ...] character offsets in the text
    PRIMARY KEY (alert_id, segment_id)
);
CREATE INDEX IF NOT EXISTS alert_mentions_segment ON alert_mentions (segment_id);
-- What webhooks and feed pushes sent, kept so "did it go out?" survives a restart. Small:
-- one row per request; the newest 10,000 are kept.
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY,
    output_id TEXT NOT NULL,
    event TEXT NOT NULL,                -- alert|utterance|batch|test
    ref TEXT,                           -- alert id or segment ids
    t REAL NOT NULL,
    ok INTEGER NOT NULL,
    status INTEGER,                     -- HTTP status, if a response came back
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    ms REAL
);
CREATE INDEX IF NOT EXISTS deliveries_t ON deliveries (t);
CREATE TABLE IF NOT EXISTS alert_summaries (
    alert_id INTEGER NOT NULL REFERENCES alerts (id) ON DELETE CASCADE,
    lang TEXT NOT NULL,
    text TEXT NOT NULL,
    model TEXT NOT NULL,
    PRIMARY KEY (alert_id, lang)
);
-- What watches caught (radio/watch.py): one row per transmission, words or none. Its words are
-- the segments of the same station and frequency over the same moments.
CREATE TABLE IF NOT EXISTS catches (
    id INTEGER PRIMARY KEY,
    station_id TEXT NOT NULL,
    freq_hz INTEGER NOT NULL,
    t_start REAL NOT NULL,              -- the transmission's first moment above the noise
    t_end REAL NOT NULL,
    mode TEXT NOT NULL,
    band TEXT,
    label TEXT NOT NULL DEFAULT '',
    snr_db REAL,
    peak_db REAL,
    voice INTEGER,                      -- 1 someone spoke, 0 nobody did, NULL not checked
    reason TEXT,                        -- 'carrier': on for minutes, learnt into the floor
    privacy TEXT NOT NULL               -- the watch's class, which decides how long it stays
);
CREATE INDEX IF NOT EXISTS catches_time ON catches (t_start);
CREATE INDEX IF NOT EXISTS catches_freq ON catches (freq_hz, t_start);
CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5 (
    text, content='transcripts', content_rowid='segment_id',
    tokenize='trigram remove_diacritics 1'
);
CREATE VIRTUAL TABLE IF NOT EXISTS translations_fts USING fts5 (
    text, content='translations', content_rowid='id', tokenize='trigram remove_diacritics 1'
);
CREATE TRIGGER IF NOT EXISTS transcripts_ai AFTER INSERT ON transcripts BEGIN
    INSERT INTO transcripts_fts (rowid, text) VALUES (new.segment_id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS transcripts_ad AFTER DELETE ON transcripts BEGIN
    INSERT INTO transcripts_fts (transcripts_fts, rowid, text)
        VALUES ('delete', old.segment_id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS transcripts_au AFTER UPDATE ON transcripts BEGIN
    INSERT INTO transcripts_fts (transcripts_fts, rowid, text)
        VALUES ('delete', old.segment_id, old.text);
    INSERT INTO transcripts_fts (rowid, text) VALUES (new.segment_id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS translations_ai AFTER INSERT ON translations BEGIN
    INSERT INTO translations_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS translations_ad AFTER DELETE ON translations BEGIN
    INSERT INTO translations_fts (translations_fts, rowid, text)
        VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS translations_au AFTER UPDATE ON translations BEGIN
    INSERT INTO translations_fts (translations_fts, rowid, text)
        VALUES ('delete', old.id, old.text);
    INSERT INTO translations_fts (rowid, text) VALUES (new.id, new.text);
END;
"""


class Store:
    def __init__(
        self,
        path: Path,
        *,
        archive: AudioArchive | None = None,
        audio_dir: Path | None = None,
        max_text_age_days: float | None = None,
        prune_text_every_s: float = 600.0,
    ) -> None:
        """Open (or create) the store.

        ``max_text_age_days`` deletes older utterances. Speech can't arrive faster than real time,
        so an age limit is also a size limit: text costs about 5.5 bytes per character with its
        search index, and four talk stations with one translation add roughly 11-14 GB a year.
        (A byte cap was tried first, but FTS5 frees index pages only as it merges segments, so the
        database size lags behind deletions and a size-driven loop over-deletes.)

        ``audio_dir`` is where earlier runs archived audio, for a run that has no ``archive`` of
        its own: the files still go when their utterances do.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.archive = archive
        self._audio_dir = audio_dir
        self.max_text_age_s = None if max_text_age_days is None else max_text_age_days * 86400
        self._prune_text_every_s = prune_text_every_s
        self._next_text_prune = 0.0
        self._pruner: threading.Thread | None = None
        self._wake_pruner = threading.Event()
        self._closing = threading.Event()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")  # durable enough; much less disk I/O
        self._db.execute("PRAGMA foreign_keys = ON")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{path} was written by a newer Wavescribe (schema {version})")
        self._db.executescript(_SCHEMA)
        if version == 1:  # 1 -> 2: flags from the ASR checks
            self._db.execute("ALTER TABLE transcripts ADD COLUMN flags TEXT NOT NULL DEFAULT ''")
        if version == 3:  # 3 -> 4: LLM rules (the deliveries table is new, so CREATE made it)
            self._db.execute("ALTER TABLE alerts ADD COLUMN kind TEXT NOT NULL DEFAULT 'keyword'")
            self._db.execute("ALTER TABLE alerts ADD COLUMN reason TEXT")
        columns = {r[1] for r in self._db.execute("PRAGMA table_info(segments)")}
        if "privacy" not in columns:  # 4 -> 5: each row's class, so its retention outlives the run
            self._db.execute("ALTER TABLE segments ADD COLUMN privacy TEXT")
        if "freq_hz" not in columns:  # 6 -> 7: the frequency, for a watch's many (catches is new)
            self._db.execute("ALTER TABLE segments ADD COLUMN freq_hz INTEGER")
        columns = {r[1] for r in self._db.execute("PRAGMA table_info(alerts)")}
        if "privacy" not in columns:  # 5 -> 6: alerts carry their stations' classes too
            self._db.execute("ALTER TABLE alerts ADD COLUMN privacy TEXT")
        # Pruning by class looks rows up by (class, age). Broadcast is never pruned that way and
        # is most of the rows, so it stays out of the index. Schema 5 indexed every row with a
        # class, which was right while broadcast rows had none.
        self._db.execute("DROP INDEX IF EXISTS segments_privacy")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS segments_class ON segments (privacy, t_start) "
            f"WHERE {_PRUNED_BY_CLASS}"
        )
        for fts in ("transcripts_fts", "translations_fts"):
            # Remove deleted rows from the index in place. By default FTS5 writes tombstones and
            # reclaims space only at a later merge, so pruning wouldn't shrink anything.
            self._db.execute(f"INSERT INTO {fts} ({fts}, rank) VALUES ('secure-delete', 1)")
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        # Searches read through a connection of their own. In WAL mode it reads while the main
        # one writes, so a slow scan (a word too short for the index, a wide time range) holds
        # up other searches, never the utterance that is being stored.
        self._read_lock = threading.Lock()
        self._reader = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            check_same_thread=False,
            isolation_level=None,
        )
        self._reader.row_factory = sqlite3.Row

    def close(self) -> None:
        self._closing.set()
        self._wake_pruner.set()
        if self._pruner is not None:
            self._pruner.join(timeout=10)
        if self.archive is not None:
            self.archive.close()
        with self._read_lock:
            self._reader.close()
        with self._lock:
            self._db.close()

    def set_retention(
        self,
        *,
        audio_days: float | None = None,
        audio_max_gb: float | None = None,
        text_days: float | None = None,
    ) -> None:
        """Change how long things are kept, from the next prune on: at once under
        ``keep_pruned``, else at the next write. ``audio_days`` 0 stops recording (what is kept
        still ages out); ``text_days`` 0 keeps text forever."""
        if text_days is not None:
            self.max_text_age_s = text_days * 86400 if text_days > 0 else None
        if self.archive is not None:
            if audio_days is not None:
                self.archive.enabled = audio_days > 0
                if audio_days > 0:
                    self.archive.max_age_s = audio_days * 86400
            if audio_max_gb is not None and audio_max_gb > 0:
                self.archive.max_bytes = int(audio_max_gb * 1024**3)
        self.prune_soon()

    def prune_soon(self) -> None:
        """Prune at the next chance (at once under ``keep_pruned``): what is kept just changed,
        here or in a class of radio's policy."""
        if self.archive is not None:
            self.archive.prune_soon()
        self._next_text_prune = 0.0
        self._wake_pruner.set()

    def keep_pruned(self, check_every_s: float = 60.0) -> None:
        """Prune now, and from now on in the background as well as on writes.

        Without it nothing ages out while nothing is written: overnight on a quiet channel,
        with every station stopped, or after recording was switched off. Call it once what is
        kept has been decided (``set_retention``, the radio policies): a prune before that
        would apply the defaults.
        """
        if self._pruner is None:
            self._pruner = threading.Thread(
                target=self._prune_loop, args=(check_every_s,), daemon=True, name="store-prune"
            )
            self._pruner.start()

    def _prune_loop(self, check_every_s: float) -> None:
        while not self._closing.is_set():
            try:
                self._prune_due()
            except Exception:
                if self._closing.is_set():
                    return  # the database was closed while a prune was running
                log.exception("pruning the store failed")
            self._wake_pruner.wait(check_every_s)
            self._wake_pruner.clear()

    def adopt_classes(self, stations: Iterable[Station]) -> None:
        """Give what was stored without a privacy class the class its station has now, when
        that isn't broadcast.

        Schema 5 wrote the class on a row only while the class kept text for a number of days,
        and never on an alert. Such rows say nothing of what they were heard under, so the
        stricter reading is taken where there is one: text kept while a class was set to "keep"
        is reached by a shorter retention later, and doesn't leave the machine. This can't be
        right every time. Rows of a station that has since been deleted, or moved to a
        broadcast frequency, stay unclassified: they are kept like broadcast text, and those of
        the moved station can be read from other machines. And unclassified rows that an id
        stored as a broadcast station, before it moved to another class, take that class.
        """
        from wavescribe.radio.bandplan import class_of

        classes = {s.id: class_of(s) for s in stations}
        with self._tx() as db:
            for station_id, privacy in classes.items():
                if privacy != "broadcast":
                    db.execute(
                        "UPDATE segments SET privacy = ? WHERE station_id = ? AND privacy IS NULL",
                        (privacy, station_id),
                    )
            unclassed = db.execute("SELECT id, stations FROM alerts WHERE privacy IS NULL")
            for alert_id, heard_on in unclassed.fetchall():
                known = sorted({classes[s] for s in heard_on.split(",") if s in classes})
                if known not in ([], ["broadcast"]):
                    db.execute(
                        "UPDATE alerts SET privacy = ? WHERE id = ?", (",".join(known), alert_id)
                    )

    def retention(self) -> dict[str, float | bool | None]:
        a = self.archive
        return {
            "recording": bool(a and a.enabled),
            "audio_days": round(a.max_age_s / 86400, 2) if a else None,
            "audio_max_gb": round(a.max_bytes / 1024**3, 2) if a else None,
            "text_days": round(self.max_text_age_s / 86400, 2) if self.max_text_age_s else 0,
        }

    def stats(self) -> dict[str, Any]:
        """Sizes for the status panel: the database (with its WAL) and the audio archive."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*), MIN(t_start), COALESCE(SUM(audio_bytes), 0), "
                "COUNT(audio_path) FROM segments"
            ).fetchone()
        db_bytes = sum(
            p.stat().st_size
            for p in (self.path, self.path.with_name(self.path.name + "-wal"))
            if p.exists()
        )
        return {
            "utterances": row[0],
            "oldest": row[1],
            "db_bytes": db_bytes,
            "audio_bytes": row[2],
            "audio_files": row[3],
            "audio_skipped": self.archive.skipped if self.archive else 0,
        }

    def add_delivery(
        self,
        output_id: str,
        event: str,
        ref: str | None,
        ok: bool,
        status: int | None,
        error: str | None,
        attempts: int,
        ms: float | None,
    ) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO deliveries (output_id, event, ref, t, ok, status, error, attempts, "
                "ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (output_id, event, ref, time.time(), int(ok), status, error, attempts, ms),
            )
            # Bounded even when text is kept forever: the newest 10,000 requests are plenty.
            db.execute(
                "DELETE FROM deliveries WHERE id <= (SELECT MAX(id) FROM deliveries) - 10000"
            )

    def deliveries(self, output_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM deliveries", []
        if output_id:
            sql, args = sql + " WHERE output_id = ?", [output_id]
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, limit]).fetchall()
        return [{**dict(r), "ok": bool(r["ok"])} for r in rows]

    def after(
        self,
        segment_id: int,
        *,
        station_ids: list[str] | None = None,
        until: float | None = None,
        limit: int = 500,
        exports: Exports | None = None,
    ) -> list[dict[str, Any]]:
        """Utterances stored after ``segment_id`` (oldest first): what a feed client missed."""
        sql = "SELECT s.id FROM segments s JOIN transcripts t ON t.segment_id = s.id WHERE s.id > ?"
        args: list[Any] = [segment_id]
        if station_ids:
            sql += f" AND s.station_id IN ({','.join('?' * len(station_ids))})"
            args += station_ids
        if until is not None:
            sql += " AND s.t_start < ?"
            args.append(until)
        leaving, leaving_args = _exports_sql(exports)
        sql += leaving
        args += leaving_args
        with self._lock:
            ids = [r[0] for r in self._db.execute(sql + " ORDER BY s.id LIMIT ?", [*args, limit])]
            return self._payloads(ids)

    def between(
        self,
        since: float,
        until: float,
        *,
        station_ids: list[str] | None = None,
        limit: int = 10000,
        exports: Exports | None = None,
    ) -> list[dict[str, Any]]:
        """Utterances that started in [since, until], in the order they were stored (exports).

        By segment id, not start time, so a page cut at ``limit`` holds every utterance up to
        its last id, and ``after(that id)`` goes on with none skipped: a station a few seconds
        behind the others stores an utterance after later-starting ones of other stations."""
        where, args = _window_sql(station_ids, since, until, exports)
        with self._lock:
            ids = [
                r[0]
                for r in self._db.execute(
                    "SELECT s.id FROM segments s JOIN transcripts t ON t.segment_id = s.id "
                    f"WHERE 1 = 1{where} ORDER BY s.id LIMIT ?",
                    [*args, limit],
                )
            ]
            return self._payloads(ids)

    def last_id(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COALESCE(MAX(id), 0) FROM segments").fetchone()[0])

    def context(
        self,
        segment_id: int,
        *,
        before: int = 2,
        after: int = 1,
        exports: Exports | None = None,
    ) -> list[dict[str, Any]]:
        """An utterance with its neighbours on the same station (for webhook payloads)."""
        leaving, leaving_args = _exports_sql(exports)
        with self._lock:
            row = self._db.execute(
                "SELECT station_id FROM segments WHERE id = ?", (segment_id,)
            ).fetchone()
            if row is None:
                return []
            prev = self._db.execute(
                "SELECT s.id FROM segments s JOIN transcripts t ON t.segment_id = s.id "
                f"WHERE s.station_id = ? AND s.id < ?{leaving} ORDER BY s.id DESC LIMIT ?",
                (row[0], segment_id, *leaving_args, before),
            ).fetchall()
            nxt = self._db.execute(
                "SELECT s.id FROM segments s JOIN transcripts t ON t.segment_id = s.id "
                f"WHERE s.station_id = ? AND s.id >= ?{leaving} ORDER BY s.id LIMIT ?",
                (row[0], segment_id, *leaving_args, after + 1),
            ).fetchall()
            return self._payloads([r[0] for r in reversed(prev)] + [r[0] for r in nxt])

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                # A COMMIT that fails (a full disk, a deferred constraint) leaves the transaction
                # open, and every later BEGIN would fail inside it. Some errors end it themselves.
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    # -- writes -----------------------------------------------------------------------------

    def add_station(self, station: Station) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO stations (id, name, language) VALUES (?, ?, ?) ON CONFLICT (id) "
                "DO UPDATE SET name = excluded.name, language = excluded.language",
                (station.id, station.name, station.language),
            )

    def stations(self) -> dict[str, KeptStation]:
        """The stations as Wavescribe last started them here (``add_station``), each with the
        class its newest stored line was heard under (None before rows carried one, or with
        nothing stored): what names a station when no stations file has it."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, name, language, (SELECT s.privacy FROM segments s "
                "WHERE s.station_id = stations.id ORDER BY s.t_start DESC LIMIT 1) AS privacy "
                "FROM stations"
            ).fetchall()
        return {r["id"]: KeptStation(r["id"], r["name"], r["language"], r["privacy"]) for r in rows}

    def add_transcript(self, result: Result, *, prune: bool = True) -> int | None:
        """Store an utterance and its source-language text; sets ``result.segment_id``.

        Utterances the ASR heard nothing in are not stored. Pipeline observer: call it before any
        observer that needs the segment id (the web view does). ``prune=False`` leaves what is due
        to ``prune_due``, for a caller that holds a lock of its own around this write.
        """
        u, t = result.utterance, result.transcript
        if not t.text:
            return None
        from wavescribe.radio.bandplan import class_of, policy_of

        policy = policy_of(result.station)
        if policy is not None and policy.text_days == 0:
            if prune:
                self._prune_due()
            return None  # live only: shown on the page as it's heard, never written down
        with self._tx() as db:
            # The class goes on every row. How long the words are kept and whether they may
            # leave the machine is decided by the class they were heard under, whatever the
            # station's id means after a restart, a retune or its deletion.
            sdr = result.station.sdr
            freq = u.freq_hz or (sdr.freq_hz if sdr is not None and sdr.watch is None else None)
            cur = db.execute(
                "INSERT INTO segments (station_id, t_start, t_end, privacy, freq_hz) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    result.station.id,
                    u.t_start,
                    u.t_start + u.duration,
                    class_of(result.station),
                    freq,
                ),
            )
            segment_id = int(cur.lastrowid or 0)
            db.execute(
                "INSERT INTO transcripts "
                "(segment_id, lang, text, model, confidence, latency_ms, flags) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (segment_id, t.language, t.text, t.model, t.confidence,
                 result.timings.get("asr_ms"), ",".join(result.flags)),
            )  # fmt: skip
        result.segment_id = segment_id
        audio_ok = policy is None or policy.audio
        if self.archive is not None and self.archive.enabled and u.samples.size and audio_ok:
            self.archive.submit(segment_id, result.station.id, u.t_start, u.samples, self._saved)
        if prune:
            self._prune_due()
        return segment_id

    def prune_due(self) -> None:
        """What ``add_transcript`` prunes after a write, when it was told not to."""
        self._prune_due()

    def _prune_due(self) -> None:
        """Everything whose time is up, at most once per ``prune_text_every_s``: text past the
        store's retention, text and alert words past their class's, and archived audio past
        its age or size limit, whether or not this run still records any."""
        with self._lock:  # a write and the background pruner may both find it due
            if time.monotonic() < self._next_text_prune:
                return
            self._next_text_prune = time.monotonic() + self._prune_text_every_s
        if self.max_text_age_s is not None:
            self.prune_text(max_age_s=self.max_text_age_s)
        self.prune_short_lived()
        self.prune_catches()
        if self.archive is not None:
            self.archive.maybe_prune(self)

    def prune_short_lived(self, batch: int = 200) -> int:
        """Text from radio whose privacy class keeps it for days, not the store's year
        (radio/bandplan.py), older than its class allows now: the user may have shortened it.
        Alerts raised on such radio lose their words at the same age.

        Works in batches so writers aren't blocked for long. Returns how many utterances were
        deleted.
        """
        from wavescribe.radio.bandplan import POLICIES

        deleted = 0
        for privacy, policy in POLICIES.items():
            if policy.text_days is None:
                continue
            cutoff = time.time() - policy.text_days * 86400
            segments, more = 0, True
            while more and not self._closing.is_set():
                with self._lock:
                    rows = self._db.execute(
                        "SELECT id, audio_path FROM segments "
                        f"WHERE privacy = ? AND {_PRUNED_BY_CLASS} AND t_start < ? LIMIT ?",
                        (privacy, cutoff, batch),
                    ).fetchall()
                    more = len(rows) == batch
                    if not rows:
                        break
                    with self._tx() as db:
                        db.executemany(
                            "DELETE FROM segments WHERE id = ?", [(r["id"],) for r in rows]
                        )
                self._unlink_audio(r["audio_path"] for r in rows)
                segments += len(rows)
                time.sleep(0.01)  # let a waiting writer in: a big prune mustn't stop storing
            alerts = self._blank_alerts(privacy, cutoff)
            if segments or alerts:
                event(log, "text_pruned", privacy=privacy, segments=segments, alerts=alerts)
            deleted += segments
        return deleted

    def _blank_alerts(self, privacy: str, cutoff: float) -> int:
        """Take the words out of alerts first heard before ``cutoff`` on radio of one class:
        the quote, the matched terms, the LLM's reason and the translations. An alert outlives
        the utterance it quotes, so without this the words of a class kept for days would stay
        for good. The alert itself stays: which rule, when, on which stations, how severe."""
        heard = "instr(',' || privacy || ',', ?) > 0 AND first_at < ?"
        args = (f",{privacy},", cutoff)
        with self._tx() as db:
            db.execute(
                "DELETE FROM alert_summaries "
                f"WHERE alert_id IN (SELECT id FROM alerts WHERE {heard})",
                args,
            )
            blanked = db.execute(
                "UPDATE alerts SET quote = '', terms = '[]', reason = NULL "
                f"WHERE {heard} AND (quote <> '' OR terms <> '[]' OR reason IS NOT NULL)",
                args,
            )
            return blanked.rowcount

    def _unlink_audio(self, paths: Iterable[str | None]) -> None:
        """Remove archived files whose rows are gone or no longer point at them. The archive's
        directory is known even when this run records nothing, so files of earlier runs don't
        outlive their text."""
        root = self.archive.root if self.archive is not None else self._audio_dir
        if root is None:
            return
        for rel in paths:
            if rel:
                (root / rel).unlink(missing_ok=True)

    def update_transcript(self, result: Result) -> None:
        """Pipeline observer for a line Whisper listened to again (``on_rechecked``,
        asr/recheck.py): its text, model and flags as the second opinion left them."""
        if result.segment_id is None:
            return
        t = result.transcript
        with self._tx() as db:
            db.execute(
                "UPDATE transcripts SET lang = ?, text = ?, model = ?, confidence = ?, flags = ? "
                "WHERE segment_id = ?",
                (t.language, t.text, t.model, t.confidence, ",".join(result.flags),
                 result.segment_id),
            )  # fmt: skip

    def add_translations(self, result: Result) -> None:
        """Pipeline observer for finished results: store whatever translations they carry."""
        if result.segment_id is None or not result.translations:
            return
        model = result.translations_model or "?"
        with self._tx() as db:
            for lang, text in result.translations.items():
                db.execute(
                    "INSERT INTO translations (segment_id, lang, text, model, latency_ms) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (segment_id, lang) DO UPDATE SET "
                    "text = excluded.text, model = excluded.model, "
                    "latency_ms = excluded.latency_ms",
                    (result.segment_id, lang, text, model, result.timings.get("mt_ms")),
                )

    def add_alert(self, alert: Alert) -> int:
        """Insert a new alert with its first mention; sets ``alert.id``."""
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO alerts (rule_id, severity, first_at, last_at, stations, language, "
                "quote, terms, mentions, segment_id, status, kind, reason, privacy) "
                # An utterance older than the text kept (one caught up after a pause) may be
                # pruned already: the alert is kept, without its link to the gone segment.
                "VALUES (?,?,?,?,?,?,?,?,?,(SELECT id FROM segments WHERE id = ?),?,?,?,?)",
                (alert.rule_id, alert.severity, alert.first_at, alert.last_at,
                 ",".join(alert.station_ids), alert.language, alert.quote,
                 json.dumps(alert.terms, ensure_ascii=False), alert.mentions,
                 alert.segment_ids[0] if alert.segment_ids else None, alert.status,
                 alert.kind, alert.reason, ",".join(alert.privacy) or None),
            )  # fmt: skip
            alert.id = int(cur.lastrowid or 0)
        return alert.id

    def add_mention(self, alert: Alert, segment_id: int | None, hit: Hit) -> None:
        """Record one utterance's hit under an alert, and the alert's new totals."""
        with self._tx() as db:
            if segment_id is not None:
                db.execute(  # nothing to point at when its segment is pruned already
                    "INSERT OR REPLACE INTO alert_mentions "
                    "(alert_id, segment_id, severity, terms, spans) SELECT ?, ?, ?, ?, ? "
                    "WHERE EXISTS (SELECT 1 FROM segments WHERE id = ?)",
                    (alert.id, segment_id, hit.severity,
                     json.dumps(hit.terms, ensure_ascii=False), json.dumps(hit.spans),
                     segment_id),
                )  # fmt: skip
            db.execute(
                "UPDATE alerts SET severity = ?, last_at = ?, stations = ?, mentions = ?, "
                "terms = ?, reason = ?, privacy = ? WHERE id = ?",
                (alert.severity, alert.last_at, ",".join(alert.station_ids), alert.mentions,
                 json.dumps(alert.terms, ensure_ascii=False), alert.reason,
                 ",".join(alert.privacy) or None, alert.id),
            )  # fmt: skip

    def update_delivery(self, alert: Alert) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE alerts SET status = ?, delivered_at = ?, delivered_severity = ?, "
                "actions = ? WHERE id = ?",
                (alert.status, alert.delivered_at, alert.delivered_severity,
                 json.dumps(alert.actions, ensure_ascii=False), alert.id),
            )  # fmt: skip

    def add_alert_summaries(self, alert: Alert, summaries: dict[str, str], model: str) -> None:
        with self._tx() as db:
            for lang, text in summaries.items():
                db.execute(
                    "INSERT OR REPLACE INTO alert_summaries (alert_id, lang, text, model) "
                    "VALUES (?, ?, ?, ?)",
                    (alert.id, lang, text, model),
                )

    def _saved(self, segment_id: int, rel_path: str, size: int) -> None:
        with self._tx() as db:
            kept = db.execute(
                "UPDATE segments SET audio_path = ?, audio_bytes = ? WHERE id = ?",
                (rel_path, size, segment_id),
            ).rowcount
        if not kept:  # its utterance was pruned while the file was being encoded
            self._unlink_audio([rel_path])
        assert self.archive is not None
        self.archive.maybe_prune(self)

    # -- reads ------------------------------------------------------------------------------

    def recent(
        self,
        station_id: str,
        *,
        limit: int = 300,
        before_id: int | None = None,
        exports: Exports | None = None,
    ) -> list[dict[str, Any]]:
        """The latest utterances of a station (oldest first), optionally older than a segment.

        Segment ids grow in arrival order, so they page exactly (timestamps are rounded on the
        page).
        """
        leaving, leaving_args = _exports_sql(exports)
        with self._lock:
            rows = self._db.execute(
                f"SELECT s.id FROM segments s WHERE s.station_id = ? AND s.id < ?{leaving} "
                "ORDER BY s.id DESC LIMIT ?",
                (station_id, before_id if before_id is not None else 2**62, *leaving_args, limit),
            ).fetchall()
            return self._payloads([r["id"] for r in reversed(rows)])

    def around(
        self,
        station_id: str,
        segment_id: int,
        *,
        limit: int = 50,
        exports: Exports | None = None,
    ) -> list[dict[str, Any]]:
        """A station's utterances around one segment (oldest first): that segment if it is
        still kept, about half of ``limit`` before it and the rest after; near either end of
        what is kept, the other side fills the window."""
        leaving, leaving_args = _exports_sql(exports)
        args = (station_id, segment_id, *leaving_args, limit)
        with self._lock:
            before = [r["id"] for r in self._db.execute(
                f"SELECT s.id FROM segments s WHERE s.station_id = ? AND s.id <= ?{leaving} "
                "ORDER BY s.id DESC LIMIT ?", args)]  # fmt: skip
            after = [r["id"] for r in self._db.execute(
                f"SELECT s.id FROM segments s WHERE s.station_id = ? AND s.id > ?{leaving} "
                "ORDER BY s.id LIMIT ?", args)]  # fmt: skip
            n_before = min(len(before), max(limit - limit // 2, limit - len(after)))
            return self._payloads(before[:n_before][::-1] + after[: limit - n_before])

    def alerts(self, *, limit: int = 50, before_id: int | None = None) -> list[dict[str, Any]]:
        """The latest alerts, newest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM alerts WHERE id < ? ORDER BY id DESC LIMIT ?",
                (before_id if before_id is not None else 2**62, limit),
            ).fetchall()
            return self._alert_payloads(rows)

    def alert(self, alert_id: int) -> dict[str, Any] | None:
        with self._lock:
            rows = self._db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchall()
            found = self._alert_payloads(rows)
        return found[0] if found else None

    def _alert_payloads(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        summaries: dict[int, dict[str, str]] = {}
        models: dict[int, str] = {}
        for s in self._db.execute(
            f"SELECT alert_id, lang, text, model FROM alert_summaries WHERE alert_id IN ({marks})",
            ids,
        ):
            summaries.setdefault(s["alert_id"], {})[s["lang"]] = s["text"]
            models[s["alert_id"]] = s["model"]
        return [
            {
                "id": r["id"],
                "rule": r["rule_id"],
                "severity": r["severity"],
                "first_at": round(r["first_at"], 2),
                "last_at": round(r["last_at"], 2),
                "stations": r["stations"].split(","),
                "language": r["language"],
                "privacy": r["privacy"].split(",") if r["privacy"] else None,
                "quote": r["quote"],
                "terms": json.loads(r["terms"]),
                "mentions": r["mentions"],
                "segment_id": r["segment_id"],
                "status": r["status"],
                "delivered_at": r["delivered_at"],
                "delivered_severity": r["delivered_severity"],
                "actions": json.loads(r["actions"]),
                "kind": r["kind"],
                "reason": r["reason"],
                "summaries": summaries.get(r["id"], {}),
                "summaries_model": models.get(r["id"]),
            }
            for r in rows
        ]

    def search(
        self,
        query: str,
        *,
        station_id: str | None = None,
        limit: int = 50,
        exports: Exports | None = None,
    ) -> list[dict[str, Any]]:
        """Utterances whose source text or any translation has every word of ``query`` at the
        start of a word, or anywhere for a word written ``*word``, and every phrase in double
        quotes with its words in order (``wavescribe.search``).

        Newest first. Each hit carries ``snippet`` (the matching text with HIT_START/HIT_END
        around matches), ``field`` ("source" or "translation") and ``field_lang``.
        """
        terms = search_terms(query)
        if not terms:
            return []
        words = search_lookups(terms)
        where, where_args = _exports_sql(exports)
        if station_id:
            where += " AND s.station_id = ?"
            where_args.append(station_id)
        # Each word as the text may write it: an apostrophe typed one way finds the others.
        spelt = [search_spellings(w) for w in words]
        if all(len(w) >= 3 for w in words):
            match = " AND ".join(  # FTS5 ANDs bare phrases, but not groups in brackets
                "(" + " OR ".join('"' + v.replace('"', '""') + '"' for v in vs) + ")"
                for vs in spelt
            )
            sql = f"""
                SELECT s.id, 'source' AS field, t.lang AS lang, s.t_start, t.text AS text
                FROM transcripts_fts
                JOIN transcripts t ON t.segment_id = transcripts_fts.rowid
                JOIN segments s ON s.id = t.segment_id
                WHERE transcripts_fts MATCH ?{where}
                UNION ALL
                SELECT s.id, 'translation', tr.lang, s.t_start, tr.text
                FROM translations_fts
                JOIN translations tr ON tr.id = translations_fts.rowid
                JOIN segments s ON s.id = tr.segment_id
                WHERE translations_fts MATCH ?{where}
                ORDER BY t_start DESC"""
            args: list[Any] = [match, *where_args, match, *where_args]
        else:  # trigrams can't index words under 3 characters: scan instead
            # % and _ are LIKE's wildcards: escaped, they are only themselves.
            like = " AND ".join(
                "(" + " OR ".join(["text LIKE ? ESCAPE '\\'"] * len(vs)) + ")" for vs in spelt
            )
            pats = [f"%{_like_literal(v)}%" for vs in spelt for v in vs]
            sql = f"""
                SELECT s.id, 'source' AS field, t.lang AS lang, s.t_start, t.text AS text
                FROM transcripts t JOIN segments s ON s.id = t.segment_id
                WHERE {like}{where}
                UNION ALL
                SELECT s.id, 'translation', tr.lang, s.t_start, tr.text
                FROM translations tr JOIN segments s ON s.id = tr.segment_id
                WHERE {like.replace("text", "tr.text")}{where}
                ORDER BY t_start DESC"""
            args = [*pats, *where_args, *pats, *where_args]
        with self._read_lock:
            # The index finds every word as a substring; keep the rows where each starts where
            # it may, one per utterance, preferring the source text, until ``limit`` are found.
            best: dict[int, tuple[sqlite3.Row, list[tuple[int, int]]]] = {}
            oldest = float("inf")
            try:
                for h in self._reader.execute(sql, args):
                    if len(best) >= limit and h["t_start"] < oldest:
                        break
                    spans = search_match(terms, h["text"])
                    if spans is None:
                        continue
                    known = best.get(h["id"])
                    if known is None and len(best) < limit:
                        best[h["id"]], oldest = (h, spans), h["t_start"]
                    elif known is not None and h["field"] == "source" != known[0]["field"]:
                        best[h["id"]] = (h, spans)
            except sqlite3.OperationalError as e:  # a query FTS5 can't parse
                log.info("search %r failed: %s", query, e)
                return []
            out = []
            for payload in self._payloads(list(best), self._reader):
                h, spans = best[payload["id"]]
                payload.update(
                    snippet=search_snippet(h["text"], spans, (HIT_START, HIT_END)),
                    field=h["field"],
                    field_lang=h["lang"],
                )
                out.append(payload)
            return sorted(out, key=lambda p: -p["t_start"])

    def find(
        self,
        terms: list[str],
        *,
        station_ids: list[str] | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 40,
        exports: Exports | None = None,
    ) -> list[dict[str, Any]]:
        """Utterances whose source text or any translation contains any of ``terms``, best match
        first (BM25: rarer terms, and more of them, rank higher).

        The trigram index can't look up a term under 3 characters. Where words are that short as
        a rule (most Chinese, Japanese and Korean words are two characters), such terms are
        found by scanning the time range instead, and come after the indexed matches: those
        with more of the terms first, then the newest. Short terms in ASCII are skipped: as a
        substring, "de" is in most sentences of a language written in Latin letters.
        """
        words = sorted({w.strip().replace('"', "") for w in terms} - {""})
        indexed = [w for w in words if len(w) >= 3]
        scanned = [w for w in words if len(w) < 3 and not w.isascii()]
        where, args = _window_sql(station_ids, since, until, exports)
        ids: list[int] = []
        with self._read_lock:
            if indexed:
                match = " OR ".join(f'"{w}"' for w in indexed)
                sql = f"""
                    SELECT s.id, bm25(transcripts_fts) AS score
                    FROM transcripts_fts JOIN segments s ON s.id = transcripts_fts.rowid
                    WHERE transcripts_fts MATCH ?{where}
                    UNION ALL
                    SELECT s.id, bm25(translations_fts)
                    FROM translations_fts
                    JOIN translations tr ON tr.id = translations_fts.rowid
                    JOIN segments s ON s.id = tr.segment_id
                    WHERE translations_fts MATCH ?{where}
                    ORDER BY score LIMIT ?"""
                try:
                    found = self._reader.execute(sql, [match, *args, match, *args, limit * 3])
                    ids += [r[0] for r in found]
                except sqlite3.OperationalError as e:
                    log.info("find %r failed: %s", indexed, e)
            if scanned and since is not None:
                # CROSS JOIN keeps segments outermost: the time range picks the rows, and only
                # their text is read.
                said = " + ".join(["(t.text LIKE ?)"] * len(scanned))
                sql = f"""
                    SELECT id FROM (
                        SELECT s.id, s.t_start, {said} AS n
                        FROM segments s CROSS JOIN transcripts t ON t.segment_id = s.id
                        WHERE 1 = 1{where}
                        UNION ALL
                        SELECT s.id, s.t_start, {said}
                        FROM segments s CROSS JOIN translations t ON t.segment_id = s.id
                        WHERE 1 = 1{where}
                    ) WHERE n > 0 ORDER BY n DESC, t_start DESC LIMIT ?"""
                like = [f"%{w}%" for w in scanned]
                ids += [r[0] for r in self._reader.execute(sql, [*like, *args] * 2 + [limit * 3])]
            ids = list(dict.fromkeys(ids))[:limit]  # the best place of each utterance
            return self._payloads(ids, self._reader)

    def window(
        self,
        *,
        station_ids: list[str] | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 80,
        exports: Exports | None = None,
    ) -> list[dict[str, Any]]:
        """Utterances in a time range, oldest first, evenly thinned to ``limit``."""
        where, args = _window_sql(station_ids, since, until, exports)
        with self._read_lock:
            ids = [
                r[0]
                for r in self._reader.execute(
                    "SELECT s.id FROM segments s JOIN transcripts t ON t.segment_id = s.id "
                    f"WHERE 1 = 1{where} ORDER BY s.t_start",
                    args,
                )
            ]
            if len(ids) > limit:
                step = len(ids) / limit
                ids = [ids[int(i * step)] for i in range(limit)]
            return self._payloads(ids, self._reader)

    def count(
        self,
        *,
        station_ids: list[str] | None = None,
        since: float | None = None,
        until: float | None = None,
        exports: Exports | None = None,
    ) -> dict[str, int]:
        """How many utterances each station has in a time range; stations with none are left
        out."""
        where, args = _window_sql(station_ids, since, until, exports)
        with self._read_lock:
            rows = self._reader.execute(
                "SELECT s.station_id, COUNT(*) FROM segments s JOIN transcripts t "
                f"ON t.segment_id = s.id WHERE 1 = 1{where} GROUP BY s.station_id",
                args,
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def last_heard(self) -> dict[str, float]:
        """When each station last had something stored."""
        with self._lock:
            rows = self._db.execute(
                "SELECT station_id, MAX(t_start) FROM segments GROUP BY station_id"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def span(self) -> tuple[float, float] | None:
        """The first and last moment anything was stored, or None for an empty store."""
        with self._lock:
            row = self._db.execute("SELECT MIN(t_start), MAX(t_start) FROM segments").fetchone()
        return None if row[0] is None else (row[0], row[1])

    def audio_file(self, segment_id: int, *, exports: Exports | None = None) -> Path | None:
        if self.archive is None:
            return None
        leaving, leaving_args = _exports_sql(exports)
        with self._lock:
            row = self._db.execute(
                f"SELECT s.audio_path FROM segments s WHERE s.id = ?{leaving}",
                (segment_id, *leaving_args),
            ).fetchone()
        if row is None or row["audio_path"] is None:
            return None
        path = self.archive.root / row["audio_path"]
        return path if path.exists() else None

    def transcripts(
        self, *, since: float | None = None, batch: int = 5000
    ) -> Iterator[tuple[float, str, str, str, tuple[str, ...]]]:
        """Every stored utterance in arrival order: (t_start, station, language, text, flags).
        Read in batches, so a year of history doesn't load at once."""
        last = 0
        while True:
            with self._lock:
                rows = self._db.execute(
                    "SELECT s.id, s.t_start, s.station_id, t.lang, t.text, t.flags "
                    "FROM segments s JOIN transcripts t ON t.segment_id = s.id "
                    "WHERE s.id > ? AND s.t_start >= ? ORDER BY s.id LIMIT ?",
                    (last, since or 0.0, batch),
                ).fetchall()
            if not rows:
                return
            for r in rows:
                flags = tuple(f for f in r["flags"].split(",") if f)
                yield r["t_start"], r["station_id"], r["lang"], r["text"], flags
            last = rows[-1]["id"]

    def counts(self) -> dict[str, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(audio_bytes), 0) AS b, "
                "COUNT(audio_path) AS a FROM segments"
            ).fetchone()
        return {
            "segments": row["n"],
            "audio_segments": row["a"],
            "audio_bytes": row["b"],
            "db_bytes": self.used_bytes(),
        }

    def used_bytes(self) -> int:
        """Bytes of the database in use (pages minus the free list); for monitoring."""
        with self._lock:
            pages = self._db.execute("PRAGMA page_count").fetchone()[0]
            free = self._db.execute("PRAGMA freelist_count").fetchone()[0]
            size = self._db.execute("PRAGMA page_size").fetchone()[0]
        return (pages - free) * size

    def prune_text(self, *, max_age_s: float, batch: int = 200) -> int:
        """Delete utterances (text, translations, index entries, audio) older than ``max_age_s``.

        Works in batches so writers aren't blocked for long. Returns how many were deleted.
        """
        cutoff = time.time() - max_age_s
        deleted, more = 0, True
        while more and not self._closing.is_set():
            with self._lock:
                # By time, not by stopping at the first young row: a row dated in the future
                # (a clock that was wrong, in a database from before pushes were checked) would
                # hold back every older one behind it. The table is bounded by this very
                # pruning, so the scan stays small.
                rows = self._db.execute(
                    "SELECT id, audio_path FROM segments WHERE t_start < ? ORDER BY id LIMIT ?",
                    (cutoff, batch),
                ).fetchall()
                more = len(rows) == batch
                if not rows:
                    break
                with self._tx() as db:
                    ids = [r["id"] for r in rows]
                    db.execute(f"DELETE FROM segments WHERE id IN ({','.join('?' * len(ids))})",
                               ids)  # fmt: skip
            self._unlink_audio(r["audio_path"] for r in rows)
            deleted += len(rows)
            time.sleep(0.01)  # let a waiting writer in: a big prune mustn't stop storing
        if deleted:
            event(log, "text_pruned", segments=deleted, used_bytes=self.used_bytes())
        return deleted

    def _payloads(
        self, ids: list[int], db: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        """Utterances in the shape the web view uses for live ones (see web.server._payload),
        read through ``db`` (the main connection unless a search passes its own)."""
        if not ids:
            return []
        db = db or self._db
        marks = ",".join("?" * len(ids))
        rows = db.execute(
            f"SELECT s.id, s.station_id, s.t_start, s.t_end, s.audio_path, s.privacy, t.lang, "
            f"t.text, t.latency_ms, t.flags FROM segments s "
            f"JOIN transcripts t ON t.segment_id = s.id WHERE s.id IN ({marks})",
            ids,
        ).fetchall()
        translations: dict[int, dict[str, str]] = {}
        for tr in db.execute(
            f"SELECT segment_id, lang, text FROM translations WHERE segment_id IN ({marks})", ids
        ):
            translations.setdefault(tr["segment_id"], {})[tr["lang"]] = tr["text"]
        hits: dict[int, list[dict[str, Any]]] = {}
        for m in db.execute(
            f"SELECT m.segment_id, m.severity, m.spans, a.rule_id, a.id FROM alert_mentions m "
            f"JOIN alerts a ON a.id = m.alert_id WHERE m.segment_id IN ({marks})",
            ids,
        ):
            hits.setdefault(m["segment_id"], []).append(
                {
                    "rule": m["rule_id"],
                    "severity": m["severity"],
                    "alert": m["id"],
                    "spans": json.loads(m["spans"]),
                }
            )
        by_id = {
            r["id"]: {
                "id": r["id"],
                "key": f"{r['station_id']}:{r['id']}",
                "station": r["station_id"],
                "privacy": r["privacy"],
                "t_start": round(r["t_start"], 2),
                "duration": round(r["t_end"] - r["t_start"], 1),
                "language": r["lang"],
                "text": r["text"],
                "translations": translations.get(r["id"], {}),
                "pending": [],
                "final": True,
                "latency_ms": None,
                "audio": r["audio_path"] is not None,
                "flags": [f for f in r["flags"].split(",") if f],
                "hits": hits.get(r["id"], []),
            }
            for r in rows
        }
        return [by_id[i] for i in ids if i in by_id]

    # -- audio retention --------------------------------------------------------------------

    # -- catches (radio/watch.py) -----------------------------------------------------------

    def add_catch(self, catch: Catch) -> int | None:
        """Keep what a watch caught; sets ``catch.id``. Catch listener (``watch.subscribe``).

        A catch stays exactly as long as text of its class. So a class that keeps no text
        (private and digital radio, by default) keeps no catch either: when a channel was busy
        is part of what was heard there, as the words are."""
        from wavescribe.radio.bandplan import POLICIES

        policy = POLICIES.get(catch.privacy)
        if policy is None or policy.text_days == 0:
            return None  # shown as it's caught, never written down
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO catches (station_id, freq_hz, t_start, t_end, mode, band, label, "
                "snr_db, peak_db, voice, reason, privacy) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    catch.station_id,
                    catch.freq_hz,
                    catch.t_start,
                    catch.t_end,
                    catch.mode,
                    catch.band,
                    catch.label,
                    catch.snr_db,
                    catch.peak_db,
                    None if catch.voice is None else int(catch.voice),
                    catch.reason,
                    catch.privacy,
                ),
            )
        catch.id = int(cur.lastrowid or 0)
        self._prune_due()
        return catch.id

    def catches(
        self,
        *,
        freq_hz: int | None = None,
        width_hz: float = 0,
        station_ids: list[str] | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
        exports: Exports | None = None,
    ) -> list[dict[str, Any]]:
        """Catches, newest first, each with the words heard in it. ``freq_hz`` picks one
        channel, or with ``width_hz`` everything that far either side. With ``exports``, only
        what may leave this machine, words included."""
        where, args = _exports_sql(exports, "c")
        if freq_hz is not None:
            where += " AND c.freq_hz BETWEEN ? AND ?"
            args += [freq_hz - width_hz, freq_hz + width_hz]
        if since is not None:
            where += " AND c.t_start >= ?"
            args.append(since)
        if until is not None:
            where += " AND c.t_start <= ?"
            args.append(until)
        if station_ids:
            where += f" AND c.station_id IN ({','.join('?' * len(station_ids))})"
            args.extend(station_ids)
        words_where, words_args = _exports_sql(exports)
        out = []
        with self._read_lock:
            rows = self._reader.execute(
                f"SELECT * FROM catches c WHERE 1 = 1{where} ORDER BY c.t_start DESC LIMIT ?",
                [*args, limit],
            ).fetchall()
            for r in rows:
                # Its words: what the same station transcribed on the same frequency, starting
                # within the catch (its audio begins a moment before, and ends a moment after).
                said = self._reader.execute(
                    "SELECT s.id, s.t_start, t.lang, t.text FROM segments s "
                    "JOIN transcripts t ON t.segment_id = s.id WHERE s.station_id = ? "
                    f"AND s.freq_hz = ? AND s.t_start BETWEEN ? AND ?{words_where} "
                    "ORDER BY s.t_start",
                    [
                        r["station_id"],
                        r["freq_hz"],
                        r["t_start"] - CATCH_SLACK_S,
                        r["t_end"] + CATCH_SLACK_S,
                        *words_args,
                    ],
                ).fetchall()
                out.append(
                    {
                        "id": r["id"],
                        "station": r["station_id"],
                        "freq_hz": r["freq_hz"],
                        "mhz": r["freq_hz"] / 1e6,
                        "t_start": r["t_start"],
                        "t_end": r["t_end"],
                        "duration": round(r["t_end"] - r["t_start"], 2),
                        "mode": r["mode"],
                        "band": r["band"],
                        "label": r["label"],
                        "snr_db": r["snr_db"],
                        "peak_db": r["peak_db"],
                        "voice": None if r["voice"] is None else bool(r["voice"]),
                        "reason": r["reason"],
                        "privacy": r["privacy"],
                        "utterances": [
                            {
                                "id": u["id"],
                                "t": u["t_start"],
                                "language": u["lang"],
                                "text": u["text"],
                            }
                            for u in said
                        ],
                    }
                )
        return out

    def prune_catches(self) -> int:
        """Catches past their class's text retention, or past the store's own: the same ages
        as the words they hold."""
        from wavescribe.radio.bandplan import POLICIES

        now, deleted = time.time(), 0
        with self._tx() as db:
            for privacy, policy in POLICIES.items():
                if policy.text_days is not None:
                    cutoff = now - policy.text_days * 86400
                    deleted += db.execute(
                        "DELETE FROM catches WHERE privacy = ? AND t_start < ?", (privacy, cutoff)
                    ).rowcount
            if self.max_text_age_s is not None:
                cutoff = now - self.max_text_age_s
                deleted += db.execute("DELETE FROM catches WHERE t_start < ?", (cutoff,)).rowcount
        if deleted:
            event(log, "catches_pruned", catches=deleted)
        return deleted

    def prune_audio(self, *, max_bytes: int, max_age_s: float) -> int:
        """Delete archived audio older than ``max_age_s`` or beyond ``max_bytes`` (oldest first).

        Returns how many files were removed. The text stays.
        """
        cutoff = time.time() - max_age_s
        removed: list[tuple[int, str]] = []
        with self._lock:
            removed += [
                (r["id"], r["audio_path"])
                for r in self._db.execute(
                    "SELECT id, audio_path FROM segments "
                    "WHERE audio_path IS NOT NULL AND t_start < ?",
                    (cutoff,),
                )
            ]
            total = self._db.execute(
                "SELECT COALESCE(SUM(audio_bytes), 0) FROM segments "
                "WHERE audio_path IS NOT NULL AND t_start >= ?",
                (cutoff,),
            ).fetchone()[0]
            if total > max_bytes:
                for r in self._db.execute(
                    "SELECT id, audio_path, audio_bytes FROM segments "
                    "WHERE audio_path IS NOT NULL AND t_start >= ? ORDER BY t_start",
                    (cutoff,),
                ):
                    if total <= max_bytes:
                        break
                    removed.append((r["id"], r["audio_path"]))
                    total -= r["audio_bytes"]
            with self._tx() as db:
                db.executemany(
                    "UPDATE segments SET audio_path = NULL, audio_bytes = 0 WHERE id = ?",
                    [(i,) for i, _ in removed],
                )
        self._unlink_audio(rel for _, rel in removed)
        if removed:
            event(log, "audio_pruned", files=len(removed))
        return len(removed)


# The condition of the segments_class index, spelled the same in every query that should use it.
_PRUNED_BY_CLASS = "privacy <> 'broadcast'"
CATCH_SLACK_S = 0.5  # a catch's audio runs this far either side of its time above the noise


def _like_literal(text: str) -> str:
    """``text`` for a LIKE pattern with ``ESCAPE '\\'``, its wildcards (% and _) escaped."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _exports_sql(exports: Exports | None, table: str = "s") -> tuple[str, list[Any]]:
    """SQL conditions on ``table`` (segments as ``s``, or catches) for rows that may leave this
    machine (``Exports.heard``); none for None, which is this machine reading its own store."""
    if exports is None:
        return "", []
    stations, classes = sorted(exports.stations), sorted(exports.classes)
    return (
        f" AND {table}.station_id IN ({','.join('?' * len(stations))})"
        f" AND ({table}.privacy IS NULL OR {table}.privacy IN ({','.join('?' * len(classes))}))",
        [*stations, *classes],
    )


def _window_sql(
    station_ids: list[str] | None,
    since: float | None,
    until: float | None,
    exports: Exports | None = None,
) -> tuple[str, list[Any]]:
    """SQL conditions on ``s`` (segments) for a set of stations and a time range, and for what
    may leave this machine when ``exports`` is given."""
    where, args = _exports_sql(exports)
    if since is not None:
        where += " AND s.t_start >= ?"
        args.append(since)
    if until is not None:
        where += " AND s.t_start <= ?"
        args.append(until)
    if station_ids:
        where += f" AND s.station_id IN ({','.join('?' * len(station_ids))})"
        args.extend(station_ids)
    return where, args


class AudioArchive:
    """Speech audio as Opus files (~2.5 KB/s at 20 kbit/s), bounded by age and total size.

    Encoding runs on its own thread through an ffmpeg subprocess (ffmpeg is GPL, so never
    in-process); if it falls behind, utterances are skipped rather than queued without bound.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = 2 * 1024**3,
        max_age_days: float = 7.0,
        bitrate: str = "20k",
        ffmpeg: str = "ffmpeg",
        prune_every_s: float = 300.0,
    ) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.max_age_s = max_age_days * 86400
        self._bitrate = bitrate
        self._ffmpeg = ffmpeg
        self._prune_every_s = prune_every_s
        self._next_prune = 0.0
        self.enabled = True  # False: keep pruning what is there, record nothing new
        self._q: queue.Queue[tuple[Any, ...] | None] = queue.Queue(maxsize=64)
        self.skipped = 0
        self._thread = threading.Thread(target=self._worker, daemon=True, name="audio-archive")
        self._thread.start()

    def submit(
        self,
        segment_id: int,
        station_id: str,
        t_start: float,
        samples: np.ndarray,
        on_saved: Callable[[int, str, int], None],
    ) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(t_start))
        rel = f"{station_id}/{day}/{segment_id}.ogg"
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        try:
            self._q.put_nowait((rel, pcm, segment_id, on_saved))
        except queue.Full:
            self.skipped += 1
            event(log, "drop", level=logging.WARNING, stage="audio_archive", station=station_id)

    def prune_soon(self) -> None:
        self._next_prune = 0.0

    def maybe_prune(self, store: Store) -> None:
        now = time.monotonic()
        if now >= self._next_prune:
            self._next_prune = now + self._prune_every_s
            store.prune_audio(max_bytes=self.max_bytes, max_age_s=self.max_age_s)

    def decode(self, path: Path) -> bytes:
        """The file back as s16le mono PCM at SAMPLE_RATE (for the page's player)."""
        cmd = [self._ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
               "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"]  # fmt: skip
        return subprocess.run(cmd, capture_output=True, check=True, timeout=30).stdout

    def close(self) -> None:
        with contextlib.suppress(queue.Full):
            self._q.put(None, timeout=5)
        self._thread.join(timeout=10)

    def _worker(self) -> None:
        while (item := self._q.get()) is not None:
            rel, pcm, segment_id, on_saved = item
            path = self.root / rel
            cmd = [self._ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
                   "-c:a", "libopus", "-b:a", self._bitrate, "-application", "voip",
                   str(path)]  # fmt: skip
            try:
                # Inside the try: a full or read-only disk costs this utterance its audio, not
                # the thread, which also prunes and so is what frees the space again.
                path.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(cmd, input=pcm, capture_output=True, check=True, timeout=60)
                on_saved(segment_id, rel, path.stat().st_size)
            except (subprocess.SubprocessError, OSError) as e:
                log.warning("could not archive audio for segment %s: %s", segment_id, e)
            except Exception:
                log.exception("audio archive callback failed for segment %s", segment_id)
