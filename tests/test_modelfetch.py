# SPDX-License-Identifier: Apache-2.0
"""``wavescribe models fetch`` around its downloads: what it says first, when a download fails,
when it is stopped, and at the end; which part-files it keeps, continues and cleans. No network:
the URL opener and the hub library's functions are stand-ins."""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import http.client
import io
import logging
import os
import signal
import ssl
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import huggingface_hub
import huggingface_hub.file_download
import huggingface_hub.utils
import pytest
from huggingface_hub.errors import LocalEntryNotFoundError

from wavescribe import invocation, modelfetch, partfiles, registry
from wavescribe.cli import main
from wavescribe.registry import Licenses, ModelEntry, ModelRegistry

REVISION = "0" * 40
NOTICE = (
    "Warning: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN "
    "to enable higher rate limits and faster downloads."
)
AGAIN = "run the same command again and it downloads only what is missing"
KEPT = "nothing is broken and the files that finished are kept"
CONTINUES = (
    "nothing is broken and what was downloaded is kept: run the same command again and it "
    "continues where it stopped"
)


def model(mid: str, capability: str, source: str, size_gb: float | None) -> ModelEntry:
    revision = REVISION if source.startswith("hf:") else None
    return ModelEntry(mid, capability, "fake", source, "u", "MIT", "https://licence.example",
                      "default", False, ("*",), "", revision=revision, size_gb=size_gb)  # fmt: skip


SMALL = model("small", "vad", "url:https://models.example/small.bin", 0.002)
BIG = model("big", "asr", "hf:example-org/big", 1.5)
LLM = model("llm", "translate", "hf:example-org/llm", 5.5)


class ConnectError(Exception):
    """Named as the hub library's HTTP client names a connection that couldn't be made."""


class RemoteProtocolError(Exception):
    """Named as that client names a connection the other end closed midway."""


def because(outer: BaseException, inner: BaseException) -> BaseException:
    outer.__cause__ = inner
    return outer


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class Answer(io.BytesIO):
    """An answer from the web: its status, and a body that breaks off after ``cut_after``."""

    def __init__(self, body: bytes, status: int = 200, cut_after: int | None = None) -> None:
        super().__init__(body)
        self.status, self.cut_after = status, cut_after

    def read(self, n: int | None = -1) -> bytes:
        if self.cut_after is None:
            return super().read(n)
        if self.tell() >= self.cut_after:
            raise http.client.IncompleteRead(b"", 0)
        return super().read(min(n if n and n > 0 else self.cut_after, self.cut_after - self.tell()))


class FakeWeb:
    """Stands in for urllib.request.urlopen. With ``body`` set it serves that file and honours a
    Range header (unless ``ranges`` is off), as a web server does."""

    def __init__(self) -> None:
        self.error: BaseException | None = None
        self.answer: io.BytesIO | None = None
        self.body: bytes | None = None
        self.ranges = True
        self.cut_after: int | None = None  # the answer breaks off after this many bytes
        self.opened: list[str] = []
        self.asked: list[str] = []  # each request's Range header ("" for none)
        self.contexts: list[object] = []

    def __call__(self, url, timeout: float | None = None, context: object = None):
        request = url if isinstance(url, urllib.request.Request) else None
        full = request.get_full_url() if request else url
        self.opened.append(full)
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        if self.body is None:
            return self.answer or io.BytesIO(b"small model")
        asked = request.get_header("Range", "") if request else ""
        self.asked.append(asked)
        start = int(asked.removeprefix("bytes=").rstrip("-")) if asked and self.ranges else 0
        if start and start >= len(self.body):
            raise urllib.error.HTTPError(full, 416, "Range Not Satisfiable", None, None)  # type: ignore[arg-type]
        return Answer(self.body[start:], 206 if start else 200, self.cut_after)


class FakeHub:
    """Stands in for the hub library: ``snapshot_download``, and what Wavescribe downloads a
    large file with itself (``list_repo_tree``, ``get_hf_file_metadata``, ``http_get``). Files
    land in the cache the way the library lays them out: each first as a part-file among the
    blobs, then the whole blob, named by its sha256, and a link to it in the snapshot folder. A
    blob that is there already isn't transferred again.
    """

    FILES = {"config.json": b"c" * 2_000_000, "weights.bin": b"w" * 4096}  # noqa: RUF012

    def __init__(self, cache: Path) -> None:
        self.cache = cache
        self.transferred: list[str] = []  # what snapshot_download transferred
        self.ranges: list[tuple[str, int]] = []  # what http_get was asked for: file, first byte
        self.fail: dict[str, BaseException] = {}  # "repo/file" -> what its download raises
        self.stall = ""  # "repo/file" whose download hangs, its part-file on disk
        self.stalled = threading.Event()
        self.release = threading.Event()
        self.says: list[str] = []  # warnings logged as the library logs them

    def folder(self, repo: str) -> Path:
        return self.cache / ("models--" + repo.replace("/", "--"))

    def __call__(self, repo: str, *, revision: str | None = None, allow_patterns=None,
                 local_files_only: bool = False) -> str:  # fmt: skip
        snapshot = self.folder(repo) / "snapshots" / str(revision)
        if local_files_only:
            if all((snapshot / name).exists() for name in self.FILES):
                return str(snapshot)
            raise LocalEntryNotFoundError("not in the cache")
        for text in self.says:
            logging.getLogger("huggingface_hub.utils._http").warning(text)
        blobs = self.folder(repo) / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        snapshot.mkdir(parents=True, exist_ok=True)
        for name, body in self.FILES.items():
            if (snapshot / name).exists():
                continue
            blob = blobs / sha(body)
            if not blob.exists():
                part = blobs / f"{sha(body)}.1a2b3c4d.incomplete"
                part.write_bytes(body[: len(body) // 2])
                key = f"{repo}/{name}"
                if key == self.stall:
                    self.stalled.set()
                    self.release.wait()
                    raise RuntimeError("the test is over")
                if key in self.fail:
                    part.unlink()  # the library removes the part of a download that failed
                    raise self.fail[key]
                part.write_bytes(body)
                part.rename(blob)
                self.transferred.append(key)
            (snapshot / name).symlink_to(blob)
        return str(snapshot)

    def tree(self, repo: str, *, revision=None, recursive=False) -> Iterator[object]:
        """``HfApi.list_repo_tree`` (a bound method on the class is called without the api)."""
        for name, body in self.FILES.items():
            lfs = types.SimpleNamespace(sha256=sha(body))
            yield types.SimpleNamespace(path=name, size=len(body), lfs=lfs)

    def metadata(self, url: str) -> object:
        repo, name = url.removeprefix("https://huggingface.co/").split("/resolve/")
        name = name.split("/", 1)[1]
        return types.SimpleNamespace(etag=sha(self.FILES[name]), size=len(self.FILES[name]),
                                     location=f"https://cdn.example/{repo}/{name}")  # fmt: skip

    def http_get(self, url: str, out, *, resume_size: int = 0, headers=None,
                 expected_size=None, displayed_filename=None) -> None:  # fmt: skip
        key = url.removeprefix("https://cdn.example/")
        name = key.rsplit("/", 1)[1]
        assert "authorization" not in headers  # the token stays off another host
        self.ranges.append((name, resume_size))
        rest = self.FILES[name][resume_size:]
        if key == self.stall or key in self.fail:
            out.write(rest[: len(rest) // 2])
            out.flush()
            if key in self.fail:
                raise self.fail[key]
            self.stalled.set()
            self.release.wait()
            raise RuntimeError("the test is over")
        out.write(rest)


class Setup:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.web = FakeWeb()
        self.hub = FakeHub(tmp_path / "hub")
        self.free = 500e9
        self.registry = ModelRegistry(
            [SMALL, BIG, LLM], {"vad": ["small"], "asr": ["big"], "translate": ["llm"]},
            models_dir=tmp_path / "models", licenses=Licenses(tmp_path / "accepted.json"),
        )  # fmt: skip


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Setup]:
    s = Setup(tmp_path)
    monkeypatch.chdir(tmp_path)  # the credits file goes to ./data
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    for name in ("HF_ENDPOINT", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", s.web)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", s.hub)
    monkeypatch.setattr(huggingface_hub.HfApi, "list_repo_tree", s.hub.tree)
    monkeypatch.setattr(huggingface_hub, "get_hf_file_metadata", s.hub.metadata)
    monkeypatch.setattr(huggingface_hub.file_download, "http_get", s.hub.http_get)
    monkeypatch.setattr(huggingface_hub.utils, "build_hf_headers",
                        lambda: {"authorization": "Bearer hf_x", "user-agent": "x"})  # fmt: skip
    monkeypatch.setattr(ModelRegistry, "load", classmethod(lambda cls, *a, **k: s.registry))
    monkeypatch.setattr(modelfetch, "free_bytes", lambda _path: int(s.free))
    yield s
    s.hub.release.set()
    for thread in threading.enumerate():  # a stopped download's thread, and the lock it holds
        if thread.name == "model-download":
            thread.join(5)
    huggingface_hub.utils.enable_progress_bars()  # a fetch whose output isn't a terminal


@pytest.fixture
def library_log() -> Iterator[list[str]]:
    """What the hub library's log would show: every record that reaches its handlers."""
    seen: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    logger, handler = logging.getLogger("huggingface_hub"), Collect()
    logger.addHandler(handler)
    yield seen
    logger.removeHandler(handler)


def one_sentence(err: str) -> str:
    """The error output is one line that starts with the program's name: no traceback."""
    assert err.count("\n") == 1 and err.startswith("wavescribe: ") and "Traceback" not in err
    return err.removeprefix("wavescribe: ").rstrip("\n")


# -- A run that works ------------------------------------------------------------------------


def test_fetch_says_the_size_first_and_ends_with_what_to_run(
    setup: Setup, library_log: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    setup.hub.says = [NOTICE]
    assert main(["models", "fetch"]) == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert lines[0] == "Downloading 3 models, 7.0 GB in all; 500.0 GB free on the disk."
    assert "  from hf:example-org/big @ " + REVISION + ", 1.5 GB" in lines
    assert lines[-2] == "credits for the installed models: data/THIRD_PARTY_MODELS.md"
    assert lines[-1] == (
        "Done. The models are installed. Start Wavescribe with: uv run wavescribe serve --open"
    )
    assert err == "" and "\r" not in out
    # No account is needed for these models, and nothing says one is.
    assert "HF_TOKEN" not in out + err and library_log == []
    assert (setup.tmp / "data" / "THIRD_PARTY_MODELS.md").exists()


def test_one_model_is_one_model_in_every_line(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["models", "fetch", "small"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Downloading 1 model, 2 MB; 500.0 GB free on the disk."
    assert lines[-1].startswith("Done. The model is installed. Start Wavescribe with: ")


def test_the_next_command_is_printed_as_it_is_typed_in_this_install(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(invocation, "invocation", lambda: "wavescribe")  # installed, not a clone
    assert main(["models", "fetch", "small"]) == 0
    last = capsys.readouterr().out.splitlines()[-1]
    assert last == "Done. The model is installed. Start Wavescribe with: wavescribe serve --open"


# -- Before anything is downloaded ---------------------------------------------------------------


def test_fetch_refuses_before_downloading_when_the_disk_has_no_room(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.free = 3e9
    assert main(["models", "fetch"]) == 1
    out, err = capsys.readouterr()
    assert out == "Downloading 3 models, 7.0 GB in all; 3.0 GB free on the disk.\n"
    assert one_sentence(err) == (
        f"not enough room on the disk: 7.0 GB to download and 3.0 GB free at {setup.tmp}; "
        "nothing was downloaded: make 4.0 GB of room, then run the same command again."
    )
    assert setup.web.opened == [] and setup.hub.transferred == []


def test_what_is_installed_needs_no_room_and_no_network(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["models", "fetch"]) == 0
    capsys.readouterr()
    setup.free = 0
    setup.web.error = urllib.error.URLError(OSError(errno.ENETDOWN, "Network is down"))
    setup.hub.fail = dict.fromkeys(("example-org/big/config.json",), OSError("no network"))
    before = list(setup.hub.transferred)
    assert main(["models", "fetch"]) == 0
    out, err = capsys.readouterr()
    assert out.count("  already installed: ") == 3 and "Downloading" not in out
    assert out.splitlines()[-1].startswith("Done. The models are installed.")
    assert err == "" and setup.hub.transferred == before and len(setup.web.opened) == 1


# -- A download that fails: one sentence, a non-zero exit, no traceback --------------------------


def cert_error() -> ssl.SSLCertVerificationError:
    return ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local "
        "issuer certificate (_ssl.c:1000)",
    )  # fmt: skip


def test_a_certificate_that_isnt_trusted_is_one_sentence(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.web.error = urllib.error.URLError(cert_error())
    assert main(["models", "fetch", "small"]) == 1
    assert one_sentence(capsys.readouterr().err) == (
        "couldn't download small: this computer doesn't trust the certificate that "
        "models.example presented (certificate verify failed: unable to get local issuer "
        "certificate (_ssl.c:1000)); check the date and time, and if a proxy or a security "
        f"program inspects HTTPS add its certificate to the system keychain, then {AGAIN} "
        f"({KEPT})."
    )
    # The hub library wraps the same failure in its own errors.
    wrapped = because(LocalEntryNotFoundError("Got: ConnectError"), ConnectError(str(cert_error())))
    setup.hub.fail = {"example-org/big/config.json": wrapped}
    assert main(["models", "fetch", "big"]) == 1
    sentence = one_sentence(capsys.readouterr().err)
    assert sentence.startswith(
        "couldn't download big: this computer doesn't trust the certificate that "
        "huggingface.co presented ("
    )
    assert AGAIN in sentence


def test_no_network_is_one_sentence(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    refused = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
    setup.web.error = urllib.error.URLError(refused)
    assert main(["models", "fetch"]) == 1
    out, err = capsys.readouterr()
    assert one_sentence(err) == (
        "couldn't download small: no connection to models.example (Connection refused); "
        f"{KEPT}: check the network, then {AGAIN}."
    )
    assert "big" not in out  # it stops at the first model that fails

    wrapped = because(
        LocalEntryNotFoundError("Got: ConnectError: [Errno 61] Connection refused\nAn error…"),
        ConnectError("[Errno 61] Connection refused"),
    )
    setup.hub.fail = {"example-org/big/config.json": wrapped}
    assert main(["models", "fetch", "big"]) == 1
    assert one_sentence(capsys.readouterr().err) == (
        "couldn't download big: no connection to huggingface.co (Connection refused); "
        f"{KEPT}: check the network, then {AGAIN}."
    )


def test_a_name_that_cant_be_looked_up_reads_as_being_offline(setup: Setup) -> None:
    import socket

    lookup = socket.gaierror(8, "nodename nor servname provided, or not known")
    sentence = modelfetch.explain(SMALL, urllib.error.URLError(lookup))
    assert "no connection to models.example (its name couldn't be looked up, as when this " in (
        sentence
    )
    assert "nodename" not in sentence and "Errno" not in sentence


def test_a_download_that_breaks_off_midway_is_one_sentence(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    class CutShort(io.BytesIO):
        def read(self, n: int = -1) -> bytes:
            if self.tell():
                raise http.client.IncompleteRead(b"x" * 8, 56)
            return super().read(8)

    setup.web.answer = CutShort(b"x" * 64)
    assert main(["models", "fetch", "small"]) == 1
    assert one_sentence(capsys.readouterr().err) == (
        "couldn't download small: the connection to models.example broke off (the other end "
        f"closed it before the file was whole); {KEPT}: {AGAIN}."
    )
    assert list((setup.tmp / "models" / "small").iterdir()) == []  # no part of it is left

    setup.hub.fail = {
        "example-org/big/weights.bin": RemoteProtocolError(
            "peer closed connection without sending complete message body (received 6291456 "
            "bytes, expected 62914560)"
        )
    }
    assert main(["models", "fetch", "big"]) == 1
    assert one_sentence(capsys.readouterr().err) == (
        "couldn't download big: the connection to huggingface.co broke off (the other end "
        f"closed it before the file was whole); {KEPT}: {AGAIN}."
    )
    # The file that finished before the break is kept, and the next run starts from it.
    setup.hub.fail = {}
    assert main(["models", "fetch", "big"]) == 0
    assert setup.hub.transferred == ["example-org/big/config.json", "example-org/big/weights.bin"]


def test_a_full_disk_is_one_sentence(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    def fill_up(*_a: object, **_k: object) -> str:
        setup.free = 12e6
        raise OSError(errno.ENOSPC, "No space left on device")

    setup.hub.fail = {}
    setup.registry.fetch = fill_up  # type: ignore[method-assign]
    assert main(["models", "fetch", "big"]) == 1
    assert one_sentence(capsys.readouterr().err) == (
        f"couldn't download big: the disk is full (12 MB free); {KEPT}: make room, then {AGAIN}."
    )


@pytest.mark.parametrize(
    ("status", "expect"),
    [
        (503, f"answered with an error (HTTP 503); {KEPT}: wait a few minutes, then {AGAIN}."),
        (404, "answered with an error (HTTP 404): nothing on this computer is broken and the "
              "files that finished are kept, but the same command will get the same answer "
              "until models.example offers https://models.example/small.bin again."),
    ],
)  # fmt: skip
def test_an_error_answer_says_whether_trying_again_helps(status: int, expect: str) -> None:
    error = urllib.error.HTTPError("https://models.example/small.bin", status, "x", None, None)
    assert modelfetch.explain(SMALL, error) == (f"couldn't download small: models.example {expect}")


# -- Ctrl-C ----------------------------------------------------------------------------------------


def ctrl_c_while_stalled(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-C while the stalled download hangs. The process doesn't end: SystemExit instead."""

    def leave(code: int) -> None:
        raise SystemExit(code)

    def ctrl_c(_progress: object) -> None:
        if setup.hub.stalled.is_set() and not setup.hub.release.is_set():
            raise KeyboardInterrupt

    monkeypatch.setattr(modelfetch, "_exit", leave)
    monkeypatch.setattr(modelfetch._Progress, "tick", ctrl_c)


def end_the_stopped_download(setup: Setup) -> None:
    """What the end of the process does to a stopped download: its thread goes, and with it
    the lock on the part-file it was writing."""
    setup.hub.release.set()
    for thread in threading.enumerate():
        if thread.name == "model-download":
            thread.join(5)
    setup.hub.stall = ""


def test_stopping_says_what_finished_and_the_next_run_downloads_the_rest(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ctrl_c_while_stalled(setup, monkeypatch)
    setup.hub.stall = "example-org/big/weights.bin"
    with pytest.raises(SystemExit) as stopped:
        main(["models", "fetch"])
    assert stopped.value.code == 130
    out, err = capsys.readouterr()
    assert err == (  # the library's file that was cut off is downloaded again, and it says so
        "Stopped. Downloaded: small. Not downloaded: big (2 MB of it kept), llm. Nothing is "
        "broken and the files that finished are kept: run the same command again and it "
        "downloads only what is missing; a file that was cut off starts again from its "
        "beginning.\n"
    )
    assert "Done." not in out
    blobs = setup.hub.folder("example-org/big") / "blobs"
    config = sha(FakeHub.FILES["config.json"])
    assert sorted(p.name for p in blobs.iterdir()) == [config]  # the part-file is gone

    end_the_stopped_download(setup)
    assert main(["models", "fetch"]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[0].startswith(
        "Downloading 2 models, 7.0 GB in all (1 already installed); "
    )
    assert "  already installed: " in out.splitlines()[2]
    assert len(setup.web.opened) == 1  # the finished model isn't downloaded again
    assert setup.hub.transferred == [  # nor the finished file of the one that was stopped
        "example-org/big/config.json",
        "example-org/big/weights.bin",
        "example-org/llm/config.json",
        "example-org/llm/weights.bin",
    ]
    assert setup.hub.transferred.count("example-org/big/config.json") == 1


def test_a_part_file_that_was_there_before_belongs_to_another_download(tmp_path: Path) -> None:
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    theirs, ours = blobs / "a.11111111.incomplete", blobs / "b.22222222.incomplete"
    continued = blobs / ("c" * 64 + ".part")  # Wavescribe's own: the next download continues it
    theirs.write_bytes(b"x" * 10)
    ours.write_bytes(b"y" * 20)
    continued.write_bytes(b"z" * 30)
    assert modelfetch.remove_parts(tmp_path, keep=frozenset({theirs})) == 20
    assert theirs.exists() and not ours.exists() and continued.exists()
    assert modelfetch.remove_parts(tmp_path, older_than_s=3600) == 0  # still being written
    assert theirs.exists()


# -- Continuing where a download stopped -----------------------------------------------------------

LARGE = {"config.json": b"c" * 1000, "weights.bin": bytes(range(256)) * 16_000}  # 4.1 MB


def test_a_large_hub_file_that_was_stopped_continues_where_it_stopped(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(FakeHub, "FILES", LARGE)
    monkeypatch.setattr(registry, "BIG_FILE", 1_000_000)  # the weights download as a large file
    ctrl_c_while_stalled(setup, monkeypatch)
    setup.hub.stall = "example-org/big/weights.bin"
    with pytest.raises(SystemExit) as stopped:
        main(["models", "fetch", "big"])
    assert stopped.value.code == 130
    assert capsys.readouterr().err == (
        "Stopped. Downloaded: nothing. Not downloaded: big (2 MB of it kept). Nothing is broken "
        "and what was downloaded is kept: run the same command again and it continues where it "
        "stopped.\n"
    )
    weights = LARGE["weights.bin"]
    part = setup.hub.folder("example-org/big") / "blobs" / f"{sha(weights)}.part"
    assert part.stat().st_size == len(weights) // 2  # kept, for the next download

    end_the_stopped_download(setup)
    assert main(["models", "fetch", "big"]) == 0
    assert setup.hub.ranges == [("weights.bin", 0), ("weights.bin", len(weights) // 2)]
    assert not part.exists() and setup.registry.local_path(BIG) is not None
    assert (setup.registry.local_path(BIG) / "weights.bin").read_bytes() == weights
    assert setup.hub.transferred == ["example-org/big/config.json"]  # the library, the rest only


def test_a_large_hub_file_that_broke_off_says_the_rerun_continues(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(FakeHub, "FILES", LARGE)
    monkeypatch.setattr(registry, "BIG_FILE", 1_000_000)
    broke = RemoteProtocolError("peer closed connection without sending complete message body")
    setup.hub.fail = {"example-org/big/weights.bin": broke}
    assert main(["models", "fetch", "big"]) == 1
    assert one_sentence(capsys.readouterr().err) == (
        "couldn't download big: the connection to huggingface.co broke off (the other end "
        f"closed it before the file was whole); {CONTINUES}."
    )
    setup.hub.fail = {}
    assert main(["models", "fetch", "big"]) == 0
    assert setup.hub.ranges[-1] == ("weights.bin", len(LARGE["weights.bin"]) // 2)


def test_a_continued_file_that_isnt_the_model_starts_again(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(FakeHub, "FILES", LARGE)
    monkeypatch.setattr(registry, "BIG_FILE", 1_000_000)
    weights = LARGE["weights.bin"]
    part = setup.hub.folder("example-org/big") / "blobs" / f"{sha(weights)}.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"x" * 1000)  # not how the file begins
    assert main(["models", "fetch", "big"]) == 1
    sentence = one_sentence(capsys.readouterr().err)
    assert sentence.startswith("couldn't download big: what cdn.example sent isn't the model ")
    assert sentence.endswith("nothing was installed: open a web page in a browser to check the "
                             "connection, then run the same command again.")  # fmt: skip
    assert not part.exists()  # so the next download starts from the beginning
    assert main(["models", "fetch", "big"]) == 0
    assert setup.hub.ranges == [("weights.bin", 1000), ("weights.bin", 0)]


def checked_model(setup: Setup, body: bytes) -> tuple[ModelEntry, Path, Path]:
    """A model from a URL with a checksum, its file, and the part-file its download writes."""
    entry = dataclasses.replace(
        model("checked", "vad", "url:https://models.example/checked.onnx", 0.001), sha256=sha(body)
    )
    setup.registry.add(entry)
    setup.web.body = body
    folder = setup.tmp / "models" / "checked"
    return entry, folder / "checked.onnx", folder / f"checked.onnx.{sha(body)[:16]}.part"


def test_a_url_model_that_broke_off_continues_where_it_stopped(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    body = bytes(range(256)) * 4000
    _entry, target, part = checked_model(setup, body)
    setup.web.cut_after = 300_000
    assert main(["models", "fetch", "checked"]) == 1
    assert one_sentence(capsys.readouterr().err) == (
        "couldn't download checked: the connection to models.example broke off (the other end "
        f"closed it before the file was whole); {CONTINUES}."
    )
    assert part.stat().st_size == 300_000 and not target.exists()
    setup.web.cut_after = None
    assert main(["models", "fetch", "checked"]) == 0
    assert setup.web.asked == ["", "bytes=300000-"]  # only what was missing
    assert target.read_bytes() == body and not part.exists()


def test_a_server_that_sends_the_whole_file_again_has_it_written_from_the_beginning(
    setup: Setup,
) -> None:
    body = bytes(range(256)) * 4000
    _entry, target, part = checked_model(setup, body)
    part.parent.mkdir(parents=True)
    part.write_bytes(body[:5000])
    setup.web.ranges = False  # answers a range request with the whole file (HTTP 200)
    assert main(["models", "fetch", "checked"]) == 0
    assert target.read_bytes() == body
    part.write_bytes(body)  # a part-file that is whole: nothing after it to ask for (HTTP 416)
    target.unlink()
    setup.web.ranges = True
    assert main(["models", "fetch", "checked"]) == 0
    assert setup.web.asked[-1] == f"bytes={len(body)}-" and target.read_bytes() == body


def test_a_file_another_download_is_writing_is_left_to_it(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    _entry, target, part = checked_model(setup, b"model")
    part.parent.mkdir(parents=True)
    with partfiles.held(part):  # the page's download, say
        assert main(["models", "fetch", "checked"]) == 1
    assert one_sentence(capsys.readouterr().err) == (
        "couldn't download checked: another download of it is under way (on the page or in "
        "another window); nothing is broken: let that one finish, or stop it, then run the "
        "same command again."
    )
    assert setup.web.opened == [] and not target.exists()


def test_part_files_nobody_continues_are_cleaned_and_one_being_written_never(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    week_ago = time.time() - 8 * 86400
    blobs = setup.hub.folder("example-org/big") / "blobs"
    blobs.mkdir(parents=True)
    old, recent, written = (blobs / f"{c * 64}.part" for c in "abc")
    library = blobs / "d.1a2b3c4d.incomplete"  # the library's, which nothing continues
    small = setup.tmp / "models" / "small" / "small.bin.0123456789abcdef.part"
    small.parent.mkdir(parents=True)
    for path in (old, recent, written, library, small):
        path.write_bytes(b"x" * 10)
    for path in (old, written, small):
        os.utime(path, (week_ago, week_ago))
    os.utime(library, (time.time() - 7200, time.time() - 7200))
    with partfiles.held(written):  # a download at work, however long it has been quiet
        modelfetch.survey(setup.registry, [BIG])
    assert written.exists() and recent.exists()
    assert not old.exists() and not library.exists()
    assert not small.exists()  # of every model in the registry, not only those fetched

    # Fetching a revision removes the part-files of files it doesn't have (another revision's).
    monkeypatch.setattr(FakeHub, "FILES", LARGE)
    monkeypatch.setattr(registry, "BIG_FILE", 1_000_000)
    assert main(["models", "fetch", "big"]) == 0
    assert sorted(blobs.glob("*.part")) == []


CHILD = """
import sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import huggingface_hub
from huggingface_hub.errors import LocalEntryNotFoundError

from wavescribe import cli
from wavescribe.registry import ModelEntry, ModelRegistry


def download(repo, *, revision=None, allow_patterns=None, local_files_only=False):
    if local_files_only:
        raise LocalEntryNotFoundError("not in the cache")
    # As the library does: a model's files in worker threads, and a wait for all of them.
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(time.sleep, [60, 60]))
    return "never"


huggingface_hub.snapshot_download = download
huggingface_hub.HfApi.list_repo_tree = lambda *a, **k: iter(())  # no file of BIG_FILE bytes
entry = ModelEntry("big", "asr", "fake", "hf:example-org/big", "u", "MIT",
                   "https://licence.example", "default", False, ("*",), "",
                   revision="0" * 40, size_gb=1.5)
registry = ModelRegistry([entry], {"asr": ["big"]}, models_dir=Path("models"))
ModelRegistry.load = classmethod(lambda cls, *a, **k: registry)
raise SystemExit(cli.main(["models", "fetch", "big"]))
"""


@pytest.mark.parametrize(("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_a_download_stops_at_once_not_when_its_file_ends(
    sig: signal.Signals, code: int, tmp_path: Path
) -> None:
    env = {**os.environ, "HF_HUB_CACHE": str(tmp_path / "hub")}
    p = subprocess.Popen([sys.executable, "-c", CHILD], cwd=tmp_path, env=env, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # fmt: skip
    try:
        assert p.stdout is not None
        while "  from hf:" not in p.stdout.readline():
            assert p.poll() is None
        time.sleep(0.5)  # the download is under way
        sent = time.monotonic()
        p.send_signal(sig)
        _out, err = p.communicate(timeout=20)
    finally:
        p.kill()
    assert time.monotonic() - sent < 3
    assert p.returncode == code and "Traceback" not in err
    if sig == signal.SIGINT:  # Ctrl-C says what happened; `serve` stopping its download doesn't
        assert err == (
            "Stopped. Downloaded: nothing. Not downloaded: big. Nothing is broken and the "
            "files that finished are kept: run the same command again and it downloads only "
            "what is missing.\n"
        )
    else:
        assert err == ""


# -- What the library says on the way --------------------------------------------------------------


def test_the_token_notice_is_dropped_and_the_retries_are_one_line(
    library_log: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    http_log = logging.getLogger("huggingface_hub.utils._http")
    modelfetch.calm_library(new_model=True)
    http_log.warning(NOTICE)
    http_log.warning("'[Errno 61] Connection refused' thrown while requesting GET https://x/y")
    http_log.warning("Retrying in 1s [Retry 1/5].")
    http_log.warning("Retrying in 2s [Retry 2/5].")
    http_log.warning("A warning about something else")
    http_log.error("401: this model needs a HF_TOKEN; unauthenticated requests are refused")
    assert library_log == [  # other warnings and every error still show
        "A warning about something else",
        "401: this model needs a HF_TOKEN; unauthenticated requests are refused",
    ]
    said = "  no proper answer from the server; trying again a few times\n"
    assert capsys.readouterr().out == said
    modelfetch.calm_library(new_model=True)  # the next model says it again, once
    http_log.warning("Retrying in 1s [Retry 1/5].")
    http_log.warning("Retrying in 2s [Retry 2/5].")
    assert capsys.readouterr().out == said and len(library_log) == 2


def test_progress_is_whole_lines_when_the_output_is_not_a_terminal(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(modelfetch, "PROGRESS_EVERY_S", 0.0)
    unsized = model("unsized", "asr", "hf:example-org/unsized", None)
    for entry, line in ((BIG, "  300 MB of 1.5 GB (20%)\n"), (unsized, "  300 MB so far\n")):
        item = modelfetch.survey(ModelRegistry([entry], {}, setup.tmp / "models"), [entry])[0]
        progress = modelfetch._Progress(item, enabled=True)
        progress.tick()
        assert capsys.readouterr().out == ""  # nothing has arrived yet
        item.folder.mkdir(parents=True)
        with (item.folder / "weights").open("wb") as f:
            f.truncate(300_000_000)  # a sparse file: the size without the bytes
        progress.tick()
        progress.tick()  # no line twice for the same figure
        out = capsys.readouterr().out
        assert out == line and "\r" not in out
