# SPDX-License-Identifier: Apache-2.0
"""Find stations to add: the Radio Browser directory, playlist files, or a pasted stream URL.

The web page adds stations three ways and this module serves all three, apart from the pipeline.
Radio Browser (radio-browser.info) is a community directory of ~54,000 internet stations whose
data is public domain (docs/LICENSING.md). The app is offline at runtime, so only functions the
user triggers touch the network: ``refresh_snapshot`` saves a compact copy of the directory that
``search`` then queries offline, while ``search_online`` and ``fetch_playlist`` go online
directly. ``probe_stream`` and ``sample_audio`` check a stream with ffprobe/ffmpeg, which (GPL in
the Homebrew build) only ever run as subprocesses.

Radio Browser's languages are free text in many spellings ("brazilian portuguese", "español
mexico", "EN"); ``language_tags`` turns them into BCP-47 tags with CLDR data from Babel, so no
language is special-cased in code.
"""

from __future__ import annotations

import functools
import gzip
import heapq
import http.client
import json
import logging
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from wavescribe import __version__, jsonsafe
from wavescribe.audio import SAMPLE_RATE
from wavescribe.httpclient import trust_store
from wavescribe.languages import language_name, primary_subtag, same_language
from wavescribe.log import event
from wavescribe.sources.stream import WEB_PROTOCOLS, ffmpeg_error

log = logging.getLogger(__name__)

# A generic client name, as Radio Browser asks; nothing about the user goes into any request.
USER_AGENT = f"Wavescribe/{__version__}"

_DISCOVERY_HOST = "all.api.radio-browser.info"
_API_DOMAIN = ".api.radio-browser.info"
# Used when DNS discovery fails. On 2026-11-26 DNS listed only de1 (de2 is an alias of it).
_FALLBACK_SERVERS = ("de1.api.radio-browser.info", "de2.api.radio-browser.info")
# The whole list is ~62 MB of JSON (the API doesn't compress). Pages keep memory bounded.
_PAGE_SIZE = 10_000
# A page is about 12 MB. More than this in one answer, as sent or once unpacked, isn't the
# directory: it is refused before it is held in memory.
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_STATIONS = 500_000  # ten times the directory; a server that keeps sending pages is cut off

# What the snapshot keeps of each station. "url" is Radio Browser's url_resolved, else its url.
SNAPSHOT_FIELDS = (
    "stationuuid", "name", "url", "homepage", "country", "countrycode", "language",
    "languagecodes", "tags", "codec", "bitrate", "hls", "lastcheckok", "votes", "clickcount",
    "favicon",
)  # fmt: skip
_INT_FIELDS = frozenset({"bitrate", "hls", "lastcheckok", "votes", "clickcount"})
_SNAPSHOT_FORMAT = 1

_WEB_SCHEMES = ("http", "https")
_MAX_PLAYLIST_BYTES = 1 << 20  # station lists are a few KB; anything bigger isn't a playlist
_PLAYLIST_TYPES = frozenset({
    "audio/x-mpegurl", "audio/mpegurl", "application/x-mpegurl", "application/vnd.apple.mpegurl",
    "audio/x-scpls", "audio/scpls", "application/pls+xml",
})  # fmt: skip
_MAX_ID_LENGTH = 24


class CatalogError(RuntimeError):
    """A directory, playlist or stream couldn't be fetched or read."""


# -- Data types ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CatalogStation:
    """One directory station. ``languages`` are BCP-47 tags, best first; may be empty."""

    name: str
    url: str
    homepage: str = ""
    country: str = ""
    countrycode: str = ""  # ISO 3166-1 alpha-2, upper case
    languages: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    codec: str = ""
    bitrate_kbps: int = 0
    hls: bool = False
    ok: bool = True  # Radio Browser's last check reached the stream
    votes: int = 0
    clicks: int = 0
    uuid: str = ""
    favicon: str = ""  # a URL on the station's site; never downloaded by us

    def to_station_dict(self, station_id: str, *, priority: int = 3) -> dict[str, Any]:
        """A ``[[station]]`` entry for config/stations.toml.

        ``language`` is left out when the directory doesn't say: the caller fills it in (from
        language detection or the user), or the file's ``[defaults]`` supply it.
        """
        entry: dict[str, Any] = {"id": station_id, "name": self.name}
        if self.languages:
            entry["language"] = self.languages[0]
        entry["stream_hls" if self.hls else "stream"] = self.url
        entry["priority"] = priority
        return entry


@dataclass(frozen=True, slots=True)
class PlaylistEntry:
    name: str  # "" when the playlist gives no title
    url: str
    hls: bool = False


@dataclass(frozen=True, slots=True)
class StreamProbe:
    ok: bool
    codec: str = ""
    bitrate_kbps: int = 0  # 0 when unknown (typical for HLS)
    sample_rate: int = 0
    channels: int = 0
    is_hls: bool = False
    title: str = ""  # the station name the stream reports (icy-name), if any
    error: str = ""


# -- Radio Browser: snapshot and search ----------------------------------------------------------


def refresh_snapshot(dest: Path, *, timeout: float = 60) -> int:
    """Download Radio Browser's station list to a compact gzip JSON at ``dest``; return the count.

    An explicit user action: the app is otherwise offline. ``timeout`` applies to each request.
    Stations that failed Radio Browser's last check are kept (ranked last), since many come back.
    About 62 MB is downloaded; the file is ~5 MB.
    """
    started = time.monotonic()
    api = _Api(timeout)
    rows: dict[str, list[Any]] = {}
    offset = 0
    while True:
        params = {"hidebroken": "false", "order": "name", "limit": _PAGE_SIZE, "offset": offset}
        page = api.get("/json/stations", params)
        if not isinstance(page, list):
            raise CatalogError("Radio Browser sent an unexpected response")
        before = len(rows)
        for record in page:
            row = _compact(record) if isinstance(record, dict) else None
            if row is not None:
                rows.setdefault(row[0], row)
        # A short page is the last one; a page with nothing new means the server ignores offset.
        if len(page) < _PAGE_SIZE or len(rows) == before:
            break
        if len(rows) > _MAX_STATIONS:
            raise CatalogError("Radio Browser sent more stations than a directory holds")
        offset += _PAGE_SIZE
    if not rows:
        raise CatalogError("Radio Browser returned no stations")

    payload = {
        "format": _SNAPSHOT_FORMAT,
        "source": "radio-browser.info",
        "fetched": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fields": SNAPSHOT_FIELDS,
        "rows": list(rows.values()),
    }
    blob = gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=6,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, dest)  # atomic: a search never sees half a file
    event(
        log,
        "catalog_refresh",
        stations=len(rows),
        pages=offset // _PAGE_SIZE + 1,
        bytes=len(blob),
        elapsed_s=round(time.monotonic() - started, 2),
    )
    return len(rows)


def search(
    snapshot: Path,
    query: str = "",
    *,
    language: str | None = None,
    country: str | None = None,
    limit: int = 50,
) -> list[CatalogStation]:
    """Search a snapshot offline. The snapshot is loaded once and kept in memory.

    ``query`` matches name and tags, ignoring case and accents. ``language`` is a BCP-47 tag
    (any region matches; the exact tag ranks first), ``country`` an ISO 3166-1 alpha-2 code.
    Ranked by match quality (the query's words together in the name, then words starting name
    words, then whole words in name or tags, then substrings), then whether the stream worked at
    Radio Browser's last check, votes and clicks. A full snapshot searches in ~2-30 ms.
    """
    started = time.monotonic()
    found = _rank(_load_snapshot(snapshot), query, language, country, limit)
    event(
        log,
        "catalog_search",
        level=logging.DEBUG,
        results=len(found),
        elapsed_ms=round((time.monotonic() - started) * 1000, 1),
    )
    return found


def search_online(
    query: str,
    *,
    language: str | None = None,
    country: str | None = None,
    limit: int = 50,
    timeout: float = 15,
) -> list[CatalogStation]:
    """Like ``search``, through Radio Browser's API, for when there's no snapshot yet."""
    api = _Api(timeout)
    params: dict[str, Any] = {
        "hidebroken": "false",
        "order": "clickcount",
        "reverse": "true",
        "limit": max(limit * 4, 100),  # headroom for our own ranking
    }
    if language:
        # The API filters on its free-text language names, so send the English name.
        params["language"] = language_name(primary_subtag(language)).lower()
    if country:
        params["countrycode"] = country.strip().upper()
    lookups = [{"name": query}, {"tag": query}] if query.strip() else [{}]
    records: dict[str, dict[str, Any]] = {}
    for extra in lookups:
        page = api.get("/json/stations/search", {**params, **extra})
        for record in page if isinstance(page, list) else []:
            if isinstance(record, dict):
                records.setdefault(str(record.get("stationuuid", "")), record)
    entries = [_entry(row) for row in map(_compact, records.values()) if row is not None]
    return _rank(entries, query, language, country, limit)


class _Api:
    """Radio Browser's JSON API, failing over between its servers."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._servers = _api_servers()

    def get(self, path: str, params: dict[str, Any]) -> Any:
        last: Exception | None = None
        for host in list(self._servers):
            url = f"https://{host}{path}?{urllib.parse.urlencode(params)}"
            try:
                return _get_json(url, self._timeout)
            except (OSError, ValueError, EOFError, http.client.HTTPException) as e:
                last = e
                event(log, "catalog_server_failed", level=logging.WARNING, host=host, error=str(e))
                self._servers.remove(host)
                self._servers.append(host)  # try the others first from now on
        raise CatalogError(f"Radio Browser is unreachable: {last}")


def _api_servers() -> list[str]:
    """API hosts in random order, found through DNS as Radio Browser's docs ask.

    all.api.radio-browser.info resolves to every server's address, and a reverse lookup gives
    each server's name (HTTPS needs the name). A fixed list follows, in case DNS fails.
    """
    try:
        infos = socket.getaddrinfo(_DISCOVERY_HOST, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        infos = []
    hosts: set[str] = set()
    for info in infos:
        try:
            name = socket.gethostbyaddr(str(info[4][0]))[0].rstrip(".").lower()
        except OSError:
            continue
        if name.endswith(_API_DOMAIN):
            hosts.add(name)
    ordered = sorted(hosts)
    random.shuffle(ordered)  # spread the load, as the docs ask
    return ordered + [h for h in _FALLBACK_SERVERS if h not in hosts]


def _get_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    )
    with urllib.request.urlopen(request, timeout=timeout, context=trust_store()) as response:
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError(f"the answer is over {_MAX_RESPONSE_BYTES >> 20} MiB")
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            body = _gunzip(body, _MAX_RESPONSE_BYTES)
    return jsonsafe.loads(body)


def _gunzip(data: bytes, limit: int) -> bytes:
    """Unpack gzip data that may be at most ``limit`` bytes unpacked (``ValueError`` beyond:
    a few kilobytes of gzip can unpack into gigabytes)."""
    inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)  # 16: a gzip header and trailer
    try:
        out = inflater.decompress(data, limit + 1)
    except zlib.error as e:
        raise ValueError(f"not gzip data: {e}") from e
    if len(out) > limit:
        raise ValueError(f"the answer unpacks to over {limit >> 20} MiB")
    if not inflater.eof:
        raise EOFError("the gzip data ends early")
    return out


def _compact(record: dict[str, Any]) -> list[Any] | None:
    """One API station reduced to SNAPSHOT_FIELDS, or None without a uuid or stream URL."""
    url = str(record.get("url_resolved") or record.get("url") or "").strip()
    uuid = str(record.get("stationuuid") or "").strip()
    if not url or not uuid:
        return None
    values = {**record, "url": url, "stationuuid": uuid}
    return [
        _int(values.get(f)) if f in _INT_FIELDS else str(values.get(f) or "").strip()
        for f in SNAPSHOT_FIELDS
    ]


_FIELD = {f: i for i, f in enumerate(SNAPSHOT_FIELDS)}
# Fields with few distinct values, shared between rows to save memory.
_INTERNED = tuple(_FIELD[f] for f in ("country", "countrycode", "language", "languagecodes"))


@dataclass(frozen=True, slots=True)
class _Entry:
    """What search needs of one station. A CatalogStation is built only for the results, which
    keeps a loaded snapshot small and quick to load."""

    row: list[Any]  # SNAPSHOT_FIELDS order
    name: str  # folded name
    text: str  # " folded name and tags ", padded so " word " finds whole words
    languages: tuple[str, ...]  # one cached tuple shared by every station with the same values
    countrycode: str


def _entry(row: list[Any]) -> _Entry:
    countrycode = row[_FIELD["countrycode"]].upper()
    name = _fold(row[_FIELD["name"]])
    return _Entry(
        row=row,
        name=name,
        text=f" {name} {_fold(row[_FIELD['tags']])} ",
        languages=_station_languages(
            row[_FIELD["language"]], row[_FIELD["languagecodes"]], countrycode
        ),
        countrycode=countrycode,
    )


_snapshot_lock = threading.Lock()
_snapshot_cache: dict[Path, tuple[tuple[int, int], list[_Entry]]] = {}


def _load_snapshot(path: Path) -> list[_Entry]:
    """The snapshot's stations, from memory unless the file changed. Only one is kept."""
    path = path.resolve()
    try:
        stat = path.stat()
    except FileNotFoundError as e:
        raise CatalogError(f"no station snapshot at {path}: refresh it first") from e
    key = (stat.st_mtime_ns, stat.st_size)
    with _snapshot_lock:
        cached = _snapshot_cache.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]
        started = time.monotonic()
        entries = _read_snapshot(path)
        _snapshot_cache.clear()
        _snapshot_cache[path] = (key, entries)
    event(
        log, "catalog_load", stations=len(entries), elapsed_s=round(time.monotonic() - started, 2)
    )
    return entries


def _read_snapshot(path: Path) -> list[_Entry]:
    try:
        data = json.loads(gzip.decompress(path.read_bytes()))
    except (OSError, EOFError, ValueError) as e:
        raise CatalogError(f"{path}: not a station snapshot ({e})") from e
    if (
        not isinstance(data, dict)
        or data.get("format") != _SNAPSHOT_FORMAT
        or tuple(data.get("fields") or ()) != SNAPSHOT_FIELDS
    ):
        raise CatalogError(f"{path}: unknown snapshot format; refresh it")
    entries = []
    for row in data.get("rows") or ():
        try:
            if len(row) != len(SNAPSHOT_FIELDS):
                continue
            for i in _INTERNED:
                row[i] = sys.intern(row[i])
            entries.append(_entry(row))
        except (TypeError, AttributeError):
            continue  # a damaged row
    return entries


def _station_from_row(row: list[Any]) -> CatalogStation:
    return _station_from_record(dict(zip(SNAPSHOT_FIELDS, row, strict=True)))


def _station_from_record(record: dict[str, Any]) -> CatalogStation:
    """A CatalogStation from an API record or a snapshot row (same field names)."""
    countrycode = str(record.get("countrycode") or "").strip().upper()
    url = str(record.get("url_resolved") or record.get("url") or "").strip()
    return CatalogStation(
        name=" ".join(str(record.get("name") or "").split()),
        url=url,
        homepage=str(record.get("homepage") or "").strip(),
        country=str(record.get("country") or "").strip(),
        countrycode=countrycode,
        languages=_station_languages(
            str(record.get("language") or ""), str(record.get("languagecodes") or ""), countrycode
        ),
        tags=tuple(t for t in (s.strip() for s in str(record.get("tags") or "").split(",")) if t),
        codec=str(record.get("codec") or "").strip(),
        bitrate_kbps=_int(record.get("bitrate")),
        hls=bool(_int(record.get("hls"))) or _looks_hls(url),
        ok=bool(_int(record.get("lastcheckok", 1))),
        votes=_int(record.get("votes")),
        clicks=_int(record.get("clickcount")),
        uuid=str(record.get("stationuuid") or ""),
        favicon=str(record.get("favicon") or "").strip(),
    )


def _rank(
    entries: Iterable[_Entry],
    query: str,
    language: str | None,
    country: str | None,
    limit: int,
) -> list[CatalogStation]:
    q = _fold(query)
    words = q.split()
    want_primary = primary_subtag(language) if language else None
    # Only a tag with a region or script ("pt-PT") earns the exact-match bonus.
    exact = language.replace("_", "-").lower() if language else ""
    want_exact = exact if "-" in exact else None
    want_country = country.strip().upper() if country else None
    ok, votes, clicks = _FIELD["lastcheckok"], _FIELD["votes"], _FIELD["clickcount"]
    scored = []
    for e in entries:
        if want_country and e.countrycode != want_country:
            continue
        if want_primary and want_primary not in _primaries(e.languages):
            continue
        if words and not all(w in e.text for w in words):
            continue  # cheap reject before grading
        quality = _match_quality(e, q, words) if words else 1
        exact = want_exact is not None and any(t.lower() == want_exact for t in e.languages)
        row = e.row
        scored.append(((quality, exact, row[ok] > 0, row[votes], row[clicks]), e))
    best = heapq.nlargest(max(limit, 0), scored, key=lambda item: item[0])
    return [_station_from_row(e.row) for _, e in best]


def _match_quality(e: _Entry, q: str, words: list[str]) -> int:
    """How well a folded query matches, from 4 (its words together in the name) to 1.

    Deliberately coarse: within a level, popularity decides, so "jazz" lists a well-known jazz
    station before an obscure one named just "Jazz".
    """
    name = f" {e.name} "
    if f" {q} " in name:
        return 4  # the words together in the name (the exact name, or part of it)
    if all(f" {w}" in name for w in words):
        return 3  # every word starts a word of the name (also while still typing)
    if all(f" {w} " in e.text for w in words):
        return 2  # whole words in the name or tags
    return 1  # substrings of the name or tags (the caller checked they're there)


@functools.lru_cache(maxsize=4096)
def _primaries(languages: tuple[str, ...]) -> frozenset[str]:
    return frozenset(primary_subtag(t) for t in languages)


# -- Languages -----------------------------------------------------------------------------------

# Common English names CLDR doesn't list (it has "Bangla", "Persian", "Chinese"...).
_EXTRA_LANGUAGE_NAMES = {
    "bengali": "bn",
    "farsi": "fa",
    "mandarin": "zh",
    "castilian": "es",
    "castellano": "es",
    "bahasa indonesia": "id",
    "bahasa melayu": "ms",
}
# CLDR codes that aren't a language you can transcribe.
_NOT_LANGUAGES = frozenset({"und", "zxx", "mul", "mis"})
_NOT_REGIONS = frozenset({"001", "EU", "EZ", "UN", "QO", "ZZ"})
_CODE = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*")
# One space each side of the dash, not ``\s+``: parts are stripped anyway, and a run of spaces
# with no dash would otherwise be scanned again from each of its positions.
_LIST_SEPARATORS = re.compile(r"[,;/|]+|\s-\s")


def language_tags(text: str) -> list[str]:
    """BCP-47 tags for Radio Browser's free-text languages, in order; unknown ones are skipped.

    Understands CLDR's English names, including regional ones ("brazilian portuguese" ->
    "pt-BR"), each language's own name ("deutsch" -> "de"), codes ("EN", "pt-br", "spa"), and a
    language followed by a territory ("español mexico" -> "es-MX").
    """
    tags: list[str] = []
    for part in _LIST_SEPARATORS.split(text or ""):
        tag = _language_tag(part.strip())
        if tag and tag not in tags:
            tags.append(tag)
    return tags


@functools.lru_cache(maxsize=4096)
def _language_tag(part: str) -> str | None:
    if not part:
        return None
    if tag := _code_tag(part):
        return tag
    names = _language_names()
    folded = _fold(part)
    if folded in names:
        return names[folded]
    # The longest run of words that names a language; a territory may be among the rest.
    words = folded.split()
    for size in range(min(len(words), 4), 0, -1):
        for start in range(len(words) - size + 1):
            base = names.get(" ".join(words[start : start + size]))
            if base:
                return _add_region(base, words[:start] + words[start + size :])
    return None


def _code_tag(text: str) -> str | None:
    """A code-shaped token as a BCP-47 tag ("EN" -> "en", "pt_br" -> "pt-BR"), if CLDR knows it."""
    if not _CODE.fullmatch(text):
        return None
    lang, *rest = re.split(r"[-_]", text)
    lang = lang.lower()
    if alias := _language_aliases().get(lang):  # ISO 639-2/3 and legacy codes: "spa" -> "es"
        lang, *alias_rest = alias.split("_")
        rest = alias_rest + rest
    if lang not in _known_codes():
        return None
    out = [lang]
    for sub in rest:
        if len(sub) == 4 and sub.isalpha():
            out.append(sub.title())  # script
        elif (len(sub) == 2 and sub.isalpha()) or (len(sub) == 3 and sub.isdigit()):
            out.append(sub.upper())  # region
        else:
            return None
    return "-".join(out)


def _add_region(base: str, words: list[str]) -> str:
    """``base`` plus the first territory named in ``words`` (in English or in that language)."""
    if "-" in base:
        return base  # already regional, e.g. "American English"
    territories = _territory_names(primary_subtag(base))
    for size in range(min(len(words), 3), 0, -1):
        for start in range(len(words) - size + 1):
            code = territories.get(" ".join(words[start : start + size]))
            if code:
                return f"{base}-{code}"
    codes = set(territories.values())
    for word in words:  # a bare region code: "português (br)"
        if len(word) == 2 and word.upper() in codes:
            return f"{base}-{word.upper()}"
    return base


@functools.lru_cache(maxsize=32768)
def _station_languages(names: str, codes: str, countrycode: str) -> tuple[str, ...]:
    """A station's tags from its language names and codes, with its country as the region when
    the language is official there ("pt" in PT -> "pt-PT"). Regional tags replace bare ones."""
    tags = language_tags(names)
    for code in re.split(r"[,;/\s]+", codes):
        tag = _code_tag(code) if code else None
        if tag and not any(same_language(tag, t) for t in tags):
            tags.append(tag)
    refined = list(dict.fromkeys(_with_region(t, countrycode) for t in tags))
    regional = {primary_subtag(t) for t in refined if "-" in t}
    return tuple(t for t in refined if "-" in t or t not in regional)


def _with_region(tag: str, countrycode: str) -> str:
    if "-" in tag or not re.fullmatch(r"[A-Z]{2}", countrycode):
        return tag
    return f"{tag}-{countrycode}" if tag in _official_languages(countrycode) else tag


@functools.lru_cache(maxsize=512)
def _official_languages(territory: str) -> frozenset[str]:
    from babel.languages import get_official_languages

    return frozenset(
        primary_subtag(code) for code in get_official_languages(territory, de_facto=True)
    )


@functools.lru_cache(maxsize=1)
def _language_names() -> dict[str, str]:
    """Folded language name -> BCP-47 tag, from CLDR: English names, then autonyms."""
    from babel import Locale, UnknownLocaleError, localedata

    names: dict[str, str] = {}

    def add(name: str, code: str) -> None:
        key = _fold(name)
        if key and code.split("_")[0] not in _NOT_LANGUAGES:
            names.setdefault(key, code.replace("_", "-"))

    for name, code in _EXTRA_LANGUAGE_NAMES.items():
        add(name, code)
    for code, name in Locale("en").languages.items():
        add(name, code)
    for code in sorted({i.split("_")[0] for i in localedata.locale_identifiers()}):
        try:
            own = Locale.parse(code).languages.get(code)
        except (ValueError, UnknownLocaleError):
            continue
        if own:
            add(own, code)
    return names


@functools.lru_cache(maxsize=64)
def _territory_names(language: str) -> dict[str, str]:
    """Folded territory name -> region code, in English and in ``language`` itself."""
    from babel import Locale, UnknownLocaleError

    out: dict[str, str] = {}
    for locale in ("en", language):
        try:
            territories = Locale.parse(locale).territories
        except (ValueError, UnknownLocaleError):
            continue
        for code, name in territories.items():
            if re.fullmatch(r"[A-Z]{2}|\d{3}", code) and code not in _NOT_REGIONS:
                out.setdefault(_fold(name), code)
    return out


@functools.lru_cache(maxsize=1)
def _language_aliases() -> dict[str, str]:
    from babel.core import get_global

    return dict(get_global("language_aliases"))


@functools.lru_cache(maxsize=1)
def _known_codes() -> frozenset[str]:
    from babel import Locale

    codes = {c for c in Locale("en").languages if "_" not in c}
    return frozenset(codes - _NOT_LANGUAGES)


# -- Text folding --------------------------------------------------------------------------------

_NON_WORD = re.compile(r"[\W_]+")
# Latin letters that don't decompose into a base letter plus accent.
_EXTRA_FOLDS = {
    "\u0142": "l",  # l with stroke
    "\u00f8": "o",  # o with stroke
    "\u0111": "d",  # d with stroke
    "\u0127": "h",  # h with stroke
    "\u0131": "i",  # dotless i
    "\u00e6": "ae",
    "\u0153": "oe",
    "\u00fe": "th",
    "\u00f0": "d",
}


@functools.lru_cache(maxsize=1)
def _fold_table() -> dict[int, str | None]:
    table: dict[int, str | None] = {
        cp: None for cp in range(0x300, 0x10000) if unicodedata.combining(chr(cp))
    }
    table.update({ord(k): v for k, v in _EXTRA_FOLDS.items()})
    return table


def _fold(text: str) -> str:
    """Lower case, no accents, punctuation as single spaces: "Rádio-Sól!" -> "radio sol"."""
    if not text.isascii():
        text = unicodedata.normalize("NFKD", text).casefold().translate(_fold_table())
    else:
        text = text.lower()
    return _NON_WORD.sub(" ", text).strip()


# -- Playlists -----------------------------------------------------------------------------------

_PLS_KEY = re.compile(r"(file|title)(\d+)", re.IGNORECASE)
_EXTINF = re.compile(r'#EXTINF:\s*[-+]?[\d.]*((?:\s+[^\s=]+="[^"]*")*)\s*,(.*)', re.IGNORECASE)


def parse_playlist(text: str, base_url: str | None = None) -> list[PlaylistEntry]:
    """Stations in an M3U (with or without #EXTINF), PLS, or plain URL-per-line list.

    Relative entries resolve against ``base_url`` and are dropped without one; only http(s)
    entries are kept. An HLS playlist (``#EXT-X-`` tags) is one stream, not a list: it comes back
    as a single entry for ``base_url``, and without a URL it raises ValueError.
    """
    lines = [line.strip() for line in text.lstrip("\ufeff").splitlines()]
    if any(line.upper().startswith("#EXT-X-") for line in lines):
        if not base_url:
            raise ValueError("an HLS playlist is a single stream, not a station list: add its URL")
        return [PlaylistEntry(name="", url=base_url, hls=True)]
    first = next((line for line in lines if line), "")
    if first.lower() == "[playlist]" or any(_PLS_KEY.fullmatch(_pls_key(ln)) for ln in lines):
        return _parse_pls(lines, base_url)
    return _parse_m3u(lines, base_url)


def _pls_key(line: str) -> str:
    key, sep, _ = line.partition("=")
    return key.strip() if sep else ""


def _parse_pls(lines: list[str], base_url: str | None) -> list[PlaylistEntry]:
    files: dict[int, str] = {}
    titles: dict[int, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        m = _PLS_KEY.fullmatch(key.strip()) if sep else None
        if m:
            (files if m[1].lower() == "file" else titles)[int(m[2])] = value.strip()
    entries = []
    for n in sorted(files):
        if url := _resolve(files[n], base_url):
            entries.append(PlaylistEntry(titles.get(n, ""), url, _looks_hls(url)))
    return _dedupe(entries)


def _parse_m3u(lines: list[str], base_url: str | None) -> list[PlaylistEntry]:
    entries = []
    title = ""
    for line in lines:
        if not line:
            continue
        if line.startswith("#"):
            if line[:8].upper() == "#EXTINF:":
                m = _EXTINF.match(line)
                title = (m[2] if m else line.partition(",")[2]).strip()
            continue
        if url := _resolve(line, base_url):
            entries.append(PlaylistEntry(title, url, _looks_hls(url)))
        title = ""
    return _dedupe(entries)


def _resolve(ref: str, base_url: str | None) -> str | None:
    url = urllib.parse.urljoin(base_url, ref) if base_url else ref
    parts = urllib.parse.urlsplit(url)
    return url if parts.scheme.lower() in _WEB_SCHEMES and parts.netloc else None


def _dedupe(entries: list[PlaylistEntry]) -> list[PlaylistEntry]:
    seen: set[str] = set()
    out = []
    for entry in entries:
        if entry.url not in seen:
            seen.add(entry.url)
            out.append(entry)
    return out


def _looks_hls(url: str) -> bool:
    return urllib.parse.urlsplit(url).path.lower().endswith(".m3u8")


def fetch_playlist(url: str, *, timeout: float = 10) -> list[PlaylistEntry]:
    """Download and parse a playlist (an explicit user action). Only http(s), at most 1 MiB.

    A URL that turns out to be an audio stream comes back as a single entry, named by its
    icy-name header if it sends one.
    """
    if urllib.parse.urlsplit(url).scheme.lower() not in _WEB_SCHEMES:
        raise CatalogError(f"{url!r}: only http and https playlists can be fetched")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=trust_store()) as response:
            ctype = response.headers.get_content_type()
            if ctype not in _PLAYLIST_TYPES and (
                ctype.startswith(("audio/", "video/")) or ctype == "application/ogg"
            ):
                name = (response.headers.get("icy-name") or "").strip()
                return [PlaylistEntry(name, url, _looks_hls(url))]
            body = response.read(_MAX_PLAYLIST_BYTES + 1)
            final_url = response.geturl()
            charset = response.headers.get_content_charset()
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise CatalogError(f"could not fetch {url}: {e}") from e
    if len(body) > _MAX_PLAYLIST_BYTES:
        raise CatalogError(f"{url}: over {_MAX_PLAYLIST_BYTES >> 20} MiB, not a playlist")
    try:
        text = body.decode(charset or "utf-8")
    except (LookupError, UnicodeDecodeError):
        text = body.decode("latin-1")  # old PLS files
    entries = parse_playlist(text, base_url=final_url)
    if len(entries) == 1 and entries[0].hls and entries[0].url == final_url:
        entries = [replace(entries[0], url=url)]  # keep the URL the user gave, not a redirect
    return entries


# -- Stream checks (ffprobe/ffmpeg as subprocesses) ----------------------------------------------


def probe_stream(url: str, *, timeout: float = 12, ffprobe: str = "ffprobe") -> StreamProbe:
    """What a stream carries, from ``ffprobe -show_streams -show_format`` run as a subprocess.

    Never raises for a bad stream: ``ok`` is False and ``error`` says why. Callers should only
    pass URLs the user gave (http/https); ffprobe would also open local paths.
    """
    cmd = [ffprobe, "-v", "error", "-hide_banner"]
    if _is_web(url):
        cmd += ["-rw_timeout", str(int(timeout * 1_000_000))]
        cmd += ["-protocol_whitelist", WEB_PROTOCOLS]
    cmd += ["-show_streams", "-show_format", "-of", "json", url]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return StreamProbe(ok=False, error=f"{ffprobe!r} not found; install ffmpeg")
    except subprocess.TimeoutExpired:
        return StreamProbe(ok=False, error=f"no answer within {timeout:g} s")
    if proc.returncode != 0:
        reason = ffmpeg_error(proc.stderr, url) or f"ffprobe exited with {proc.returncode}"
        return StreamProbe(ok=False, error=reason)
    probe = parse_ffprobe(proc.stdout)
    event(
        log,
        "catalog_probe",
        url=url,
        ok=probe.ok,
        codec=probe.codec,
        elapsed_s=round(time.monotonic() - started, 2),
    )
    return probe


def parse_ffprobe(output: str | bytes | dict[str, Any]) -> StreamProbe:
    """A StreamProbe from ffprobe's JSON (``-show_streams -show_format -of json``)."""
    try:
        data = output if isinstance(output, dict) else json.loads(output or "{}")
    except ValueError:
        return StreamProbe(ok=False, error="ffprobe output isn't JSON")
    if not isinstance(data, dict):
        return StreamProbe(ok=False, error="ffprobe output isn't a JSON object")
    fmt = data.get("format") or {}
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    formats = str(fmt.get("format_name") or "").split(",")
    is_hls = "hls" in formats or "applehttp" in formats
    title = str(tags.get("icy-name") or "").strip()
    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        return StreamProbe(ok=False, is_hls=is_hls, title=title, error="no audio stream")
    bps = _int(audio.get("bit_rate")) or _int(fmt.get("bit_rate"))
    if not bps:  # ICY servers announce kbps; HLS variants may carry their bandwidth
        bps = _int(str(tags.get("icy-br") or "").split(",")[0]) * 1000
        bps = bps or _int((audio.get("tags") or {}).get("variant_bitrate"))
    return StreamProbe(
        ok=True,
        codec=str(audio.get("codec_name") or ""),
        bitrate_kbps=round(bps / 1000),
        sample_rate=_int(audio.get("sample_rate")),
        channels=_int(audio.get("channels")),
        is_hls=is_hls,
        title=title,
    )


def sample_audio(
    url: str, seconds: float = 20, *, timeout: float = 40, ffmpeg: str = "ffmpeg"
) -> np.ndarray:
    """Up to ``seconds`` of a stream as 16 kHz mono float32, decoded by an ffmpeg subprocess.

    For checking a pasted URL and detecting its language. If the timeout cuts the stream short,
    what arrived is returned; CatalogError if nothing did.
    """
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if _is_web(url):
        cmd += ["-rw_timeout", str(int(timeout * 1_000_000))]
        cmd += ["-protocol_whitelist", WEB_PROTOCOLS]
    cmd += ["-i", url, "-t", f"{seconds:g}", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE)]
    cmd += ["-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        raw, err, returncode = proc.stdout, proc.stderr, proc.returncode
    except FileNotFoundError as e:
        raise CatalogError(f"{ffmpeg!r} not found; install ffmpeg") from e
    except subprocess.TimeoutExpired as e:
        raw, err, returncode = e.stdout or b"", e.stderr or b"", None
    raw = raw[: len(raw) - len(raw) % 2]
    if not raw:
        reason = ffmpeg_error(err or b"", url) or (
            f"no audio within {timeout:g} s" if returncode is None else f"exit {returncode}"
        )
        raise CatalogError(f"no audio from {url}: {reason}")
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _is_web(url: str) -> bool:
    return url.startswith(("http://", "https://"))


# -- Station ids ---------------------------------------------------------------------------------


def station_id_for(name: str, existing: Iterable[str] = ()) -> str:
    """A short lowercase ASCII id for a station name, unique against ``existing``.

    "Rádio Sól FM" -> "radio-sol-fm"; a taken id gets "-2", "-3"... A name with no Latin
    letters becomes "station".
    """
    ascii_name = _fold(name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    if len(slug) > _MAX_ID_LENGTH:
        cut = slug.rfind("-", 0, _MAX_ID_LENGTH + 1)
        slug = slug[:cut] if cut >= 8 else slug[:_MAX_ID_LENGTH].rstrip("-")
    slug = slug or "station"
    taken = set(existing)
    if slug not in taken:
        return slug
    n = 2
    while f"{slug}-{n}" in taken:
        n += 1
    return f"{slug}-{n}"


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0
