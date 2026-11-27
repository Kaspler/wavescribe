# SPDX-License-Identifier: Apache-2.0
"""``wavescribe models fetch`` around its downloads: what it says first, when a download fails,
when it is stopped, and at the end. No network: the URL opener and the hub's download function
are stand-ins."""

from __future__ import annotations

import errno
import http.client
import io
import logging
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import huggingface_hub
import pytest
from huggingface_hub.errors import LocalEntryNotFoundError

from wavescribe import cli, modelfetch
from wavescribe.cli import main
from wavescribe.registry import Licenses, ModelEntry, ModelRegistry

REVISION = "0" * 40
NOTICE = (
    "Warning: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN "
    "to enable higher rate limits and faster downloads."
)
AGAIN = "run the same command again and it downloads only what is missing"
KEPT = "nothing is broken and the files that finished are kept"


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


class FakeWeb:
    """Stands in for urllib.request.urlopen."""

    def __init__(self) -> None:
        self.error: BaseException | None = None
        self.answer: io.BytesIO | None = None
        self.opened: list[str] = []
        self.contexts: list[object] = []

    def __call__(self, url: str, timeout: float | None = None, context: object = None):
        self.opened.append(url)
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return self.answer or io.BytesIO(b"small model")


class FakeHub:
    """Stands in for huggingface_hub.snapshot_download. Files land in the cache the way the
    library lays them out: each first as a part-file among the blobs, then the whole blob and
    a link to it in the snapshot folder. A file that is there already isn't transferred again.
    """

    FILES = {"config.json": b"c" * 2_000_000, "weights.bin": b"w" * 4096}  # noqa: RUF012

    def __init__(self, cache: Path) -> None:
        self.cache = cache
        self.transferred: list[str] = []
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
            part = blobs / f"{name}.1a2b3c4d.incomplete"
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
            part.rename(blobs / name)
            (snapshot / name).symlink_to(blobs / name)
            self.transferred.append(key)
        return str(snapshot)


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
    monkeypatch.setattr(ModelRegistry, "load", classmethod(lambda cls, *a, **k: s.registry))
    monkeypatch.setattr(modelfetch, "free_bytes", lambda _path: int(s.free))
    yield s
    s.hub.release.set()
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli.typed("serve --open") == "uv run wavescribe serve --open"  # a clone, as here
    installed = tmp_path / "site-packages" / "wavescribe" / "cli.py"
    monkeypatch.setattr(cli, "__file__", str(installed))
    assert cli.typed("serve --open") == "wavescribe serve --open"


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


def test_stopping_says_what_finished_and_the_next_run_downloads_the_rest(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def leave(code: int) -> None:
        raise SystemExit(code)

    def ctrl_c(_progress: object) -> None:
        if setup.hub.stalled.is_set():
            raise KeyboardInterrupt

    monkeypatch.setattr(modelfetch, "_exit", leave)
    monkeypatch.setattr(modelfetch._Progress, "tick", ctrl_c)
    setup.hub.stall = "example-org/big/weights.bin"
    with pytest.raises(SystemExit) as stopped:
        main(["models", "fetch"])
    assert stopped.value.code == 130
    out, err = capsys.readouterr()
    assert err == (
        "Stopped. Downloaded: small. Not downloaded: big (2 MB of it kept), llm. Nothing is "
        "broken and the files that finished are kept: run the same command again and it "
        "downloads only what is missing.\n"
    )
    assert "Done." not in out
    blobs = setup.hub.folder("example-org/big") / "blobs"
    assert sorted(p.name for p in blobs.iterdir()) == ["config.json"]  # the part-file is gone

    monkeypatch.undo()  # no Ctrl-C this time; the stand-ins are put back below
    monkeypatch.chdir(setup.tmp)
    monkeypatch.setenv("HF_HUB_CACHE", str(setup.tmp / "hub"))
    monkeypatch.setattr(urllib.request, "urlopen", setup.web)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", setup.hub)
    monkeypatch.setattr(ModelRegistry, "load", classmethod(lambda cls, *a, **k: setup.registry))
    setup.hub.stall = ""
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
    theirs.write_bytes(b"x" * 10)
    ours.write_bytes(b"y" * 20)
    assert modelfetch.remove_parts(tmp_path, keep=frozenset({theirs})) == 20
    assert theirs.exists() and not ours.exists()
    assert modelfetch.remove_parts(tmp_path, older_than_s=3600) == 0  # still being written
    assert theirs.exists()


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
    import os

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
