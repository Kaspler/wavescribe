# SPDX-License-Identifier: Apache-2.0
"""The station catalogue: playlists, language tags, snapshot search, ffprobe, station ids.

No test touches the internet: Radio Browser is replaced by a fake API, playlists are served
from a loopback HTTP server, and streams are tones that ffmpeg renders locally.
"""

import contextlib
import gzip
import json
import os
import re
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest

from tests.helpers import make_tone, requires_ffmpeg
from wavescribe.audio import SAMPLE_RATE
from wavescribe.catalog import stations as cs
from wavescribe.catalog.stations import (
    CatalogError,
    PlaylistEntry,
    fetch_playlist,
    language_tags,
    parse_ffprobe,
    parse_playlist,
    probe_stream,
    refresh_snapshot,
    sample_audio,
    search,
    search_online,
    station_id_for,
)
from wavescribe.config import parse_stations

# -- Playlists -----------------------------------------------------------------------------------


def test_extended_m3u_takes_titles_from_extinf() -> None:
    text = """#EXTM3U
#EXTINF:-1 tvg-name="Aurora, the first" tvg-logo="https://example.org/a.png",Rádio Aurora
http://stream.example.org/aurora.mp3

#EXTINF:0,Kestrel News
https://stream.example.org/kestrel.aac
#EXTINF:-1,Same stream again
http://stream.example.org/aurora.mp3
"""
    assert parse_playlist(text) == [
        PlaylistEntry("Rádio Aurora", "http://stream.example.org/aurora.mp3"),
        PlaylistEntry("Kestrel News", "https://stream.example.org/kestrel.aac"),
    ]


def test_m3u_without_extinf_and_plain_url_list() -> None:
    text = (
        "﻿# a comment\r\n"
        "http://stream.example.org/one.mp3\r\n"
        "\r\n"
        "https://stream.example.org/two.ogg\r\n"
        "relative/three.mp3\r\n"  # no base URL: dropped
        "file:///etc/hosts\r\n"  # not http(s): dropped
        "rtsp://stream.example.org/four\r\n"
    )
    assert parse_playlist(text) == [
        PlaylistEntry("", "http://stream.example.org/one.mp3"),
        PlaylistEntry("", "https://stream.example.org/two.ogg"),
    ]


def test_pls_with_and_without_header() -> None:
    text = """[playlist]
NumberOfEntries=3
File2=http://stream.example.org/b.mp3
Title2=Station B
file1=http://stream.example.org/a.mp3
Title1=Station A
Length1=-1
File3=http://stream.example.org/c.mp3
Version=2
"""
    expected = [
        PlaylistEntry("Station A", "http://stream.example.org/a.mp3"),
        PlaylistEntry("Station B", "http://stream.example.org/b.mp3"),
        PlaylistEntry("", "http://stream.example.org/c.mp3"),
    ]
    assert parse_playlist(text) == expected
    assert parse_playlist(text.replace("[playlist]\n", "")) == expected


def test_hls_playlist_is_one_stream() -> None:
    media = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:100
#EXTINF:6.0,
segment100.aac
#EXTINF:6.0,
segment101.aac
"""
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=48000,CODECS="mp4a.40.5"
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=128000,CODECS="mp4a.40.2"
high/index.m3u8
"""
    base = "https://cdn.example.org/live/index.m3u8"
    for text in (media, master):
        assert parse_playlist(text, base_url=base) == [PlaylistEntry("", base, hls=True)]
        with pytest.raises(ValueError, match="HLS"):
            parse_playlist(text)


def test_relative_entries_resolve_against_base_url() -> None:
    base = "https://lists.example.org/radio/all.m3u"
    m3u = "aurora.mp3\n/live/kestrel.aac\n../up.mp3\n//cdn.example.net/other.mp3\n"
    assert [e.url for e in parse_playlist(m3u, base_url=base)] == [
        "https://lists.example.org/radio/aurora.mp3",
        "https://lists.example.org/live/kestrel.aac",
        "https://lists.example.org/up.mp3",
        "https://cdn.example.net/other.mp3",
    ]
    pls = "[playlist]\nFile1=stream.mp3\nTitle1=Relative\n"
    assert parse_playlist(pls, base_url=base) == [
        PlaylistEntry("Relative", "https://lists.example.org/radio/stream.mp3")
    ]


def test_list_entries_pointing_at_m3u8_are_flagged_hls() -> None:
    text = "#EXTM3U\n#EXTINF:-1,Live HLS\nhttps://cdn.example.org/a/index.m3u8?token=1\n"
    text += "#EXTINF:-1,Icecast\nhttps://ice.example.org/a.mp3\n"
    entries = parse_playlist(text)
    assert [(e.name, e.hls) for e in entries] == [("Live HLS", True), ("Icecast", False)]


# -- Languages -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tags"),
    [
        ("portuguese", ["pt"]),
        ("english,spanish", ["en", "es"]),
        ("brazilian portuguese", ["pt-BR"]),
        ("ukrainian", ["uk"]),
        ("Español México", ["es-MX"]),
        ("português (brasil)", ["pt-BR"]),
        ("português (br)", ["pt-BR"]),
        ("deutsch", ["de"]),
        ("українська", ["uk"]),
        ("EN", ["en"]),
        ("pt-br", ["pt-BR"]),
        ("spa", ["es"]),
        ("american english, english", ["en-US", "en"]),
        ("low german", ["nds"]),
        ("français - lëtzebuergesch", ["fr", "lb"]),
        ("#greek.", ["el"]),
        ("english;english", ["en"]),
        ("germany", []),  # a country, not a language
        ("klingonish", []),
        ("", []),
    ],
)
def test_language_tags(text: str, tags: list[str]) -> None:
    assert language_tags(text) == tags


# -- Radio Browser snapshot and search -----------------------------------------------------------


def api_station(uuid: str, name: str, **fields: Any) -> dict[str, Any]:
    """A record shaped like Radio Browser's /json/stations output (made-up stations)."""
    url = fields.pop("url", f"http://stream.example.org/{uuid}.mp3")
    record = {
        "changeuuid": f"change-{uuid}",
        "stationuuid": uuid,
        "serveruuid": None,
        "name": name,
        "url": url.replace(".mp3", ".pls"),
        "url_resolved": url,
        "homepage": f"https://example.org/{uuid}",
        "favicon": f"https://example.org/{uuid}.png",
        "tags": "",
        "country": "",
        "countrycode": "",
        "iso_3166_2": None,
        "state": "",
        "language": "",
        "languagecodes": "",
        "votes": 0,
        "lastchangetime_iso8601": "2026-01-01T00:00:00Z",
        "codec": "MP3",
        "bitrate": 128,
        "hls": 0,
        "lastcheckok": 1,
        "clickcount": 0,
        "clicktrend": 0,
        "ssl_error": 0,
        "geo_lat": None,
        "geo_long": None,
        "has_extended_info": False,
    }
    record.update(fields)
    return record


STATIONS = [
    api_station("a1", "Rádio Aurora", language="portuguese", languagecodes="pt",
                country="Portugal", countrycode="PT", tags="news,talk", votes=50, clickcount=10),
    api_station("a2", "Aurora Jazz", language="english", languagecodes="en",
                countrycode="US", tags="jazz,smooth jazz", votes=900),
    api_station("a3", "Jazz", countrycode="FR", tags="jazz", votes=3),
    api_station("a4", "Blue Lantern", language="english", countrycode="GB",
                tags="jazz,blues", votes=500),
    api_station("a5", "Rádio Farol", language="brazilian portuguese,portuguese",
                languagecodes="pt", countrycode="BR", votes=2000),
    api_station("a6", "Hvylia FM", language="ukrainian", languagecodes="uk", countrycode="UA",
                tags="news", votes=40, lastcheckok=0),
    api_station("a7", "Hvylia News", language="ukrainian", languagecodes="uk", countrycode="UA",
                votes=10),
    api_station("a8", "Kestrel Talk", language="english", languagecodes="en", countrycode="GB",
                url="https://cdn.example.org/kestrel/index.m3u8", hls=1, codec="AAC", votes=100),
    api_station("a9", "Harbour English", language="english", countrycode="PT", votes=5),
    api_station("a1", "Rádio Aurora (duplicate row)"),  # same uuid on a later page
    api_station("a10", "No Stream", url=""),
]  # fmt: skip


class FakeApi:
    """Stands in for Radio Browser's API: pages of /json/stations and canned searches."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, path: str, params: dict[str, Any]) -> Any:
        self.calls.append((path, dict(params)))
        if path == "/json/stations":
            return self.records[params["offset"] : params["offset"] + params["limit"]]
        return self.records


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    api = FakeApi(STATIONS)
    monkeypatch.setattr(cs, "_Api", lambda timeout: api)
    monkeypatch.setattr(cs, "_PAGE_SIZE", 3)
    return api


@pytest.fixture
def snapshot(tmp_path: Path, fake_api: FakeApi) -> Path:
    path = tmp_path / "catalog" / "radio-browser.json.gz"
    refresh_snapshot(path)
    return path


def names(stations: list[cs.CatalogStation]) -> list[str]:
    return [s.name for s in stations]


def test_refresh_pages_through_the_api_and_writes_a_compact_snapshot(
    tmp_path: Path, fake_api: FakeApi
) -> None:
    path = tmp_path / "catalog" / "radio-browser.json.gz"
    assert refresh_snapshot(path) == 9  # 11 records: one duplicate uuid, one without a stream
    offsets = [params["offset"] for _, params in fake_api.calls]
    assert offsets == [0, 3, 6, 9]  # pages of 3 until a short page
    assert all(params["hidebroken"] == "false" for _, params in fake_api.calls)

    data = json.loads(gzip.decompress(path.read_bytes()))
    assert tuple(data["fields"]) == cs.SNAPSHOT_FIELDS
    first = dict(zip(data["fields"], data["rows"][0], strict=True))
    assert first["url"] == "http://stream.example.org/a1.mp3"  # url_resolved, not the .pls
    assert first["favicon"] == "https://example.org/a1.png"
    assert first["name"] == "Rádio Aurora"  # the first row for a uuid wins
    assert "changeuuid" not in json.dumps(data)
    assert list(path.parent.iterdir()) == [path]  # no temporary file left behind


def test_search_ignores_case_and_accents(snapshot: Path) -> None:
    assert names(search(snapshot, "RADIO aurora"))[0] == "Rádio Aurora"
    assert set(names(search(snapshot, "rádio"))) == {"Rádio Aurora", "Rádio Farol"}
    assert names(search(snapshot, "farol", limit=1)) == ["Rádio Farol"]


def test_search_ranks_name_matches_then_tags_then_popularity(snapshot: Path) -> None:
    # Name matches first (popular before obscure), then a station that only has the tag.
    assert names(search(snapshot, "jazz")) == ["Aurora Jazz", "Jazz", "Blue Lantern"]
    # Still typing: a prefix finds the name.
    assert names(search(snapshot, "kestr")) == ["Kestrel Talk"]
    # Several words in any order.
    assert names(search(snapshot, "talk kestrel")) == ["Kestrel Talk"]


def test_a_stream_that_failed_its_last_check_ranks_below_working_ones(snapshot: Path) -> None:
    found = search(snapshot, "hvylia")
    assert names(found) == ["Hvylia News", "Hvylia FM"]  # despite FM's 40 votes to 10
    assert [s.ok for s in found] == [True, False]


def test_language_filter_matches_any_region_and_prefers_the_exact_tag(snapshot: Path) -> None:
    assert names(search(snapshot, language="pt")) == ["Rádio Farol", "Rádio Aurora"]
    assert names(search(snapshot, language="pt-PT")) == ["Rádio Aurora", "Rádio Farol"]
    assert names(search(snapshot, language="uk")) == ["Hvylia News", "Hvylia FM"]
    assert names(search(snapshot, "news", language="uk")) == ["Hvylia News", "Hvylia FM"]


def test_country_filter_and_limit(snapshot: Path) -> None:
    assert names(search(snapshot, country="gb")) == ["Blue Lantern", "Kestrel Talk"]
    assert names(search(snapshot, limit=2)) == ["Rádio Farol", "Aurora Jazz"]
    assert len(search(snapshot)) == 9
    assert search(snapshot, "nothing like this") == []


def test_station_languages_come_from_names_codes_and_country(snapshot: Path) -> None:
    by_name = {s.name: s for s in search(snapshot, limit=100)}
    assert by_name["Rádio Aurora"].languages == ("pt-PT",)  # official in PT
    assert by_name["Rádio Farol"].languages == ("pt-BR",)
    assert by_name["Aurora Jazz"].languages == ("en-US",)
    assert by_name["Harbour English"].languages == ("en",)  # not an official language in PT
    assert by_name["Jazz"].languages == ()
    assert by_name["Hvylia FM"].languages == ("uk-UA",)


def test_catalog_station_fields(snapshot: Path) -> None:
    (talk,) = search(snapshot, "kestrel talk")
    assert talk.url == "https://cdn.example.org/kestrel/index.m3u8"
    assert talk.hls and talk.ok
    assert (talk.codec, talk.bitrate_kbps, talk.votes, talk.uuid) == ("AAC", 128, 100, "a8")
    assert talk.countrycode == "GB"
    assert talk.homepage == "https://example.org/a8"
    assert talk.favicon == "https://example.org/a8.png"


def test_to_station_dict_is_a_valid_config_entry(snapshot: Path) -> None:
    by_name = {s.name: s for s in search(snapshot, limit=100)}
    aurora = by_name["Rádio Aurora"].to_station_dict("radio-aurora")
    assert aurora == {
        "id": "radio-aurora",
        "name": "Rádio Aurora",
        "language": "pt-PT",
        "stream": "http://stream.example.org/a1.mp3",
        "priority": 3,
    }
    talk = by_name["Kestrel Talk"].to_station_dict("kestrel", priority=1)
    assert talk["stream_hls"] == "https://cdn.example.org/kestrel/index.m3u8"
    assert "stream" not in talk and talk["priority"] == 1
    jazz = by_name["Jazz"].to_station_dict("jazz")
    assert "language" not in jazz  # unknown: left to the caller or [defaults]

    config = parse_stations(
        {"defaults": {"language": "en"}, "station": [aurora, talk, jazz]}, Path("test.toml")
    )
    assert [s.language for s in config.stations] == ["pt-PT", "en-GB", "en"]


def test_snapshot_is_cached_and_reloaded_when_the_file_changes(
    snapshot: Path, fake_api: FakeApi
) -> None:
    assert len(search(snapshot)) == 9
    first = cs._load_snapshot(snapshot)
    assert cs._load_snapshot(snapshot) is first  # loaded once

    fake_api.records = STATIONS[:2]
    refresh_snapshot(snapshot)
    stat = snapshot.stat()
    os.utime(snapshot, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert names(search(snapshot)) == ["Aurora Jazz", "Rádio Aurora"]


def test_missing_or_foreign_snapshot_is_a_catalog_error(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="refresh"):
        search(tmp_path / "missing.json.gz")
    garbage = tmp_path / "garbage.json.gz"
    garbage.write_bytes(b"not gzip")
    with pytest.raises(CatalogError, match="not a station snapshot"):
        search(garbage)
    foreign = tmp_path / "foreign.json.gz"
    foreign.write_bytes(gzip.compress(json.dumps({"fields": ["x"], "rows": []}).encode()))
    with pytest.raises(CatalogError, match="format"):
        search(foreign)


def test_search_online_asks_the_api_and_ranks_like_offline(fake_api: FakeApi) -> None:
    found = search_online("jazz", language="en-US", country="us", limit=5)
    assert names(found) == ["Aurora Jazz"]  # the fake returns everything; filters still apply
    lookups = [params for path, params in fake_api.calls if path == "/json/stations/search"]
    assert [p.get("name") or p.get("tag") for p in lookups] == ["jazz", "jazz"]
    assert all(p["language"] == "english" and p["countrycode"] == "US" for p in lookups)


def test_api_servers_come_from_dns_with_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    addresses = {"192.0.2.1": "xx1.api.radio-browser.info.", "192.0.2.2": "other.example.com"}

    def getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        assert host == "all.api.radio-browser.info"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyaddr", lambda ip: (addresses[ip], [], [ip]))
    servers = cs._api_servers()
    assert servers[0] == "xx1.api.radio-browser.info"  # a foreign name is ignored
    assert servers[1:] == list(cs._FALLBACK_SERVERS)

    def no_dns(*args: Any, **kwargs: Any) -> list[Any]:
        raise socket.gaierror("no network")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    assert cs._api_servers() == list(cs._FALLBACK_SERVERS)


def test_api_fails_over_to_the_next_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "_api_servers", lambda: ["down.example", "up.example"])
    asked: list[str] = []

    def get_json(url: str, timeout: float) -> Any:
        asked.append(url)
        if "down.example" in url:
            raise TimeoutError("timed out")
        return [{"ok": True}]

    monkeypatch.setattr(cs, "_get_json", get_json)
    api = cs._Api(timeout=1)
    assert api.get("/json/stations", {"limit": 1}) == [{"ok": True}]
    assert asked == [
        "https://down.example/json/stations?limit=1",
        "https://up.example/json/stations?limit=1",
    ]
    asked.clear()
    api.get("/json/stations", {"limit": 1})
    assert asked == ["https://up.example/json/stations?limit=1"]  # the dead server went last

    def all_down(url: str, timeout: float) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(cs, "_get_json", all_down)
    with pytest.raises(CatalogError, match="unreachable"):
        api.get("/json/stations", {})


# -- Playlist download (loopback server) ---------------------------------------------------------


class _PlaylistHandler(BaseHTTPRequestHandler):
    routes: ClassVar[dict[str, tuple[str, bytes, dict[str, str]]]] = {
        "/radio/list.pls": (
            "audio/x-scpls",
            b"[playlist]\nFile1=aurora.mp3\nTitle1=R\xe1dio Aurora\n",  # latin-1, relative
            {},
        ),
        "/live.m3u8": ("application/vnd.apple.mpegurl", b"#EXTM3U\n#EXT-X-VERSION:3\n", {}),
        "/stream": ("audio/mpeg", b"\xff\xfb" * 64, {"icy-name": "Kestrel Talk"}),
        "/big.m3u": ("audio/x-mpegurl", b"#" * (2 << 20), {}),
        "/api/plain": ("application/json", b'[{"name": "Aurora"}]', {}),
        "/api/packed": (
            "application/json",
            gzip.compress(b'[{"name": "Aurora"}]'),
            {"Content-Encoding": "gzip"},
        ),
        "/api/long": ("application/json", b"[" + b" " * (1 << 20) + b"]", {}),
        "/api/bomb": (  # about a kilobyte that unpacks to a megabyte
            "application/json",
            gzip.compress(b"[" + b" " * (1 << 20) + b"]"),
            {"Content-Encoding": "gzip"},
        ),
        "/api/cut": (
            "application/json",
            gzip.compress(b"[1, 2, 3]")[:-9],
            {"Content-Encoding": "gzip"},
        ),
        "/api/noise": ("application/json", b"not gzip at all", {"Content-Encoding": "gzip"}),
    }

    def do_GET(self) -> None:
        ctype, body, headers = self.routes.get(self.path, ("text/plain", b"", {}))
        self.send_response(200 if self.path in self.routes else 404)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)  # the client hangs up early on purpose for /big.m3u

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def playlist_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PlaylistHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_playlist(playlist_server: str) -> None:
    assert fetch_playlist(f"{playlist_server}/radio/list.pls") == [
        PlaylistEntry("Rádio Aurora", f"{playlist_server}/radio/aurora.mp3")
    ]
    assert fetch_playlist(f"{playlist_server}/live.m3u8") == [
        PlaylistEntry("", f"{playlist_server}/live.m3u8", hls=True)
    ]
    # A stream rather than a playlist: one entry, named by its icy-name header.
    assert fetch_playlist(f"{playlist_server}/stream") == [
        PlaylistEntry("Kestrel Talk", f"{playlist_server}/stream")
    ]
    with pytest.raises(CatalogError, match="MiB"):
        fetch_playlist(f"{playlist_server}/big.m3u")
    with pytest.raises(CatalogError, match="404"):
        fetch_playlist(f"{playlist_server}/missing.pls")


def test_directory_answers_are_read_up_to_a_limit(
    playlist_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cs, "_MAX_RESPONSE_BYTES", 64 * 1024)
    assert cs._get_json(f"{playlist_server}/api/plain", 5) == [{"name": "Aurora"}]
    assert cs._get_json(f"{playlist_server}/api/packed", 5) == [{"name": "Aurora"}]
    for path in ("long", "bomb", "noise"):
        with pytest.raises(ValueError):
            cs._get_json(f"{playlist_server}/api/{path}", 5)
    with pytest.raises(EOFError):
        cs._get_json(f"{playlist_server}/api/cut", 5)
    # To the caller each of these is a server that failed, like one that is down.
    read = cs._get_json
    monkeypatch.setattr(cs, "_api_servers", lambda: ["directory.example"])
    monkeypatch.setattr(cs, "_get_json", lambda url, t: read(f"{playlist_server}/api/bomb", t))
    with pytest.raises(CatalogError, match="unreachable"):
        cs._Api(timeout=5).get("/json/stations", {})


def test_a_directory_that_never_runs_out_of_stations_is_cut_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cs, "_api_servers", lambda: ["api.example"])
    monkeypatch.setattr(cs, "_PAGE_SIZE", 2)
    monkeypatch.setattr(cs, "_MAX_STATIONS", 6)
    made = iter(range(1000))

    def endless(url: str, timeout: float) -> Any:
        return [{"stationuuid": f"u{next(made)}", "url": "http://s.example/a"} for _ in range(2)]

    monkeypatch.setattr(cs, "_get_json", endless)
    with pytest.raises(CatalogError, match="more stations"):
        refresh_snapshot(tmp_path / "snapshot.json.gz")
    assert next(made) < 20


def test_language_text_is_split_in_linear_time() -> None:
    assert language_tags("français  -  lëtzebuergesch - deutsch") == ["fr", "lb", "de"]
    spaces = " " * 200_000  # no dash anywhere: nothing to split on
    started = time.perf_counter()
    assert language_tags(spaces) == [] and language_tags(spaces + "english") == ["en"]
    assert time.perf_counter() - started < 2


def test_fetch_playlist_refuses_other_schemes() -> None:
    with pytest.raises(CatalogError, match="http"):
        fetch_playlist("file:///etc/hosts")


# -- Stream checks -------------------------------------------------------------------------------

ICY_PROBE = {
    "streams": [
        {"index": 0, "codec_name": "mp3", "codec_type": "audio", "sample_rate": "22050",
         "channels": 2, "bit_rate": "64604"}
    ],
    "format": {
        "format_name": "mp3",
        "bit_rate": "64604",
        "tags": {"icy-br": "64", "icy-name": " Rádio Aurora ", "StreamTitle": "a song"},
    },
}  # fmt: skip
HLS_PROBE = {
    "streams": [
        {"index": 0, "codec_name": "aac", "profile": "HE-AAC", "codec_type": "audio",
         "sample_rate": "48000", "channels": 2, "tags": {"variant_bitrate": "0"}}
    ],
    "format": {"format_name": "hls", "size": "622"},
}  # fmt: skip


def test_parse_ffprobe_icy_stream() -> None:
    probe = parse_ffprobe(json.dumps(ICY_PROBE))
    assert probe == cs.StreamProbe(
        ok=True, codec="mp3", bitrate_kbps=65, sample_rate=22050, channels=2, title="Rádio Aurora"
    )


def test_parse_ffprobe_hls_stream() -> None:
    probe = parse_ffprobe(json.dumps(HLS_PROBE).encode())
    assert probe.ok and probe.is_hls
    assert (probe.codec, probe.bitrate_kbps, probe.sample_rate) == ("aac", 0, 48000)


def test_parse_ffprobe_bitrate_from_icy_header_when_the_stream_has_none() -> None:
    data = json.loads(json.dumps(ICY_PROBE))
    del data["streams"][0]["bit_rate"], data["format"]["bit_rate"]
    data["format"]["tags"]["icy-br"] = "48,48"
    assert parse_ffprobe(data).bitrate_kbps == 48


def test_parse_ffprobe_failures() -> None:
    video_only = {"streams": [{"codec_type": "video", "codec_name": "h264"}], "format": {}}
    assert parse_ffprobe(video_only) == cs.StreamProbe(ok=False, error="no audio stream")
    assert not parse_ffprobe("not json").ok
    assert parse_ffprobe(b"").error == "no audio stream"


def test_probe_reports_a_missing_ffprobe() -> None:
    probe = probe_stream("http://stream.example.org/x.mp3", ffprobe="no-such-ffprobe-binary")
    assert not probe.ok and "not found" in probe.error


@requires_ffmpeg
def test_probe_and_sample_a_local_stream(tmp_path: Path) -> None:
    tone = make_tone(tmp_path / "tone.wav", 2.0, rate=44_100, channels=2)

    probe = probe_stream(str(tone))
    assert probe.ok, probe.error
    assert (probe.codec, probe.sample_rate, probe.channels) == ("pcm_s16le", 44_100, 2)
    assert probe.bitrate_kbps == 1411 and not probe.is_hls

    audio = sample_audio(str(tone), seconds=1.0)
    assert audio.dtype == np.float32
    assert abs(len(audio) - SAMPLE_RATE) < SAMPLE_RATE * 0.02
    assert 0.05 < float(np.abs(audio).max()) <= 1.0


@requires_ffmpeg
def test_probe_and_sample_a_missing_stream(tmp_path: Path) -> None:
    probe = probe_stream(str(tmp_path / "missing.mp3"))
    assert not probe.ok and probe.error
    with pytest.raises(CatalogError, match="no audio"):
        sample_audio(str(tmp_path / "missing.mp3"), seconds=1.0)


# -- Station ids ---------------------------------------------------------------------------------

_STATION_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")  # what config.py accepts


@pytest.mark.parametrize(
    ("name", "existing", "expected"),
    [
        ("Rádio Sól FM", set(), "radio-sol-fm"),
        ("Rádio Sól FM", {"radio-sol-fm", "radio-sol-fm-2"}, "radio-sol-fm-3"),
        ("Ραδιόφωνο", set(), "station"),  # no Latin letters
        ("Ραδιόφωνο", {"station"}, "station-2"),
        ("  !!!  ", set(), "station"),
        ("The Very Long Name of a Community Radio Station", set(), "the-very-long-name-of-a"),
        ("Kestrel_Talk 24/7", set(), "kestrel-talk-24-7"),
    ],
)
def test_station_id_for(name: str, existing: set[str], expected: str) -> None:
    station_id = station_id_for(name, existing)
    assert station_id == expected
    assert _STATION_ID.match(station_id) and len(station_id) <= 24


def test_a_web_url_is_probed_and_sampled_with_web_protocols_only(tmp_path: Path) -> None:
    # A playlist could point ffmpeg at file:, concat: or a listening socket.
    args = tmp_path / "args.txt"
    tool = tmp_path / "tool"
    tool.write_text(f'#!/bin/sh\necho "$@" >> {args}\nexit 1\n')
    tool.chmod(0o755)
    assert not probe_stream("https://stream.example.org/live.m3u8", ffprobe=str(tool)).ok
    with pytest.raises(CatalogError):
        sample_audio("https://stream.example.org/live.m3u8", seconds=1, ffmpeg=str(tool))
    runs = args.read_text().splitlines()
    assert len(runs) == 2
    assert all("-protocol_whitelist http,https,tcp,tls,crypto" in r for r in runs)
