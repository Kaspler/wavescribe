# SPDX-License-Identifier: Apache-2.0
"""``wavescribe models remove``: what a model put on disk goes, and nothing anyone else's.

Every test runs in a home folder of its own, with a Hugging Face cache laid out as the library
lays it out, the cache-wide store of shared files included (made with the library's own code).
Nothing real is touched: the fixture checks that the cache is the temporary one first.
"""

from __future__ import annotations

import builtins
import hashlib
import os
import shutil
from pathlib import Path

import huggingface_hub
import pytest
from huggingface_hub.utils import _shared_blobs

from wavescribe import modelfetch, modelremove, partfiles
from wavescribe.cli import build_parser, main
from wavescribe.invocation import typed
from wavescribe.registry import Licenses, ModelEntry, ModelRegistry

OURS, THEIRS, OTHER = "a" * 40, "b" * 40, "c" * 40
MB = 1_000_000
WEIGHTS = b"w" * (3 * MB)  # speech's weights, which another revision of its repository shares
WHISPER = b"x" * (2 * MB)  # whisper's weights, which another program's repository shares
CONFIG = b'{"model": "speech"}'


def entry(mid: str, source: str, revision: str | None = None, files=None) -> ModelEntry:
    sha = "0" * 64 if source.startswith("url:") else None
    return ModelEntry(mid, "asr", "fake", source, "u", "MIT", "https://licence.example",
                      "default", False, ("*",), "", revision=revision, files=files,
                      sha256=sha)  # fmt: skip


SPEECH = entry("speech", "hf:org/speech", OURS, ("config.json", "model.safetensors"))
WHISPER_MODEL = entry("whisper", "hf:org/whisper", OURS)
VAD = entry("vad", "url:https://models.example/vad.onnx")
MINE = entry("mine", "local:/somewhere/mine")


def put(cache: Path, repo: str, revision: str, name: str, data: bytes, *, shared: bool = False,
        ref: str | None = None) -> Path:  # fmt: skip
    """A file as the library caches it: a blob (or a link into the shared store, as for Xet
    files), and a link to it in the revision's snapshot."""
    folder = cache / ("models--" + repo.replace("/", "--"))
    etag = hashlib.sha256(data).hexdigest()
    blob = folder / "blobs" / etag
    blob.parent.mkdir(parents=True, exist_ok=True)
    if not blob.is_symlink() and not blob.exists():
        blob.write_bytes(data)
        if shared:
            assert _shared_blobs.publish_blob_to_shared_store(
                blob_path=str(blob), xet_hash=etag, cache_dir=cache, expected_size=len(data)
            )
    link = folder / "snapshots" / revision / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(blob, link.parent))
    if ref:
        (folder / "refs").mkdir(exist_ok=True)
        (folder / "refs" / ref).write_text(revision)
    return link


class Disk:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cache = root / "home" / ".cache" / "huggingface" / "hub"
        self.models = root / "models"
        self.registry = ModelRegistry(
            [SPEECH, WHISPER_MODEL, VAD, MINE], {"asr": ["speech", "whisper", "vad", "mine"]},
            models_dir=self.models, licenses=Licenses(root / "accepted.json"),
        )  # fmt: skip
        # speech, at the pinned revision and at another one a program of its own fetched.
        put(self.cache, "org/speech", OURS, "config.json", CONFIG, ref="main")
        put(self.cache, "org/speech", OURS, "model.safetensors", WEIGHTS, shared=True)
        put(self.cache, "org/speech", THEIRS, "config.json", b'{"model": "older"}')
        put(self.cache, "org/speech", THEIRS, "model.safetensors", WEIGHTS, shared=True)
        # whisper, whose weights another program's repository has too.
        put(self.cache, "org/whisper", OURS, "weights.npz", WHISPER, shared=True, ref="main")
        put(self.cache, "org/whisper", OURS, "tokenizer.json", b"t" * 1_200_000)
        self.app = put(self.cache, "someone/app", OTHER, "weights.npz", WHISPER, shared=True)
        # vad, from a URL, with a part-file a broken download left.
        (self.models / "vad").mkdir(parents=True)
        (self.models / "vad" / "vad.onnx").write_bytes(b"v" * 2_000)
        (self.models / "vad" / "tmpab.part").write_bytes(b"v" * 500)

    def folder(self, repo: str) -> Path:
        return self.cache / ("models--" + repo.replace("/", "--"))

    def store(self) -> list[Path]:
        """The payloads in the cache-wide store of shared files."""
        return sorted(p for p in (self.cache / "blobs").glob("*/*") if len(p.name) == 64)


@pytest.fixture
def disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Disk:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    cache = home / ".cache" / "huggingface" / "hub"
    monkeypatch.setattr(huggingface_hub.constants, "HF_HUB_CACHE", str(cache))
    assert modelfetch.hub_cache() == cache  # the temporary cache, never the real one
    monkeypatch.chdir(tmp_path)
    d = Disk(tmp_path)
    monkeypatch.setattr(ModelRegistry, "load", classmethod(lambda cls, *a, **k: d.registry))
    return d


def answer(monkeypatch: pytest.MonkeyPatch, said: str | None) -> list[str]:
    """What the person types at the question (None: there is no one to ask)."""
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        if said is None:
            raise EOFError
        return said

    monkeypatch.setattr(builtins, "input", ask)
    return asked


def test_a_model_goes_with_its_cache_entry_and_says_what_that_freed(
    disk: Disk, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked = answer(monkeypatch, "y")
    shared_before = disk.store()
    assert main(["models", "remove", "whisper"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert asked == ["Remove whisper (1 MB)? [y/N] "]
    assert out[0].split() == [
        "whisper",
        "1",
        "MB",
        "~/.cache/huggingface/hub/models--org--whisper",
    ]
    assert out[1].strip() == (
        "(another 2 MB stays: another entry in the Hugging Face cache has the same files)"
    )
    assert out[2] == (
        f"Removed whisper: 1 MB freed. To download it again: {typed('models fetch whisper')}, "
        "or under Settings → Languages and models."
    )
    assert not disk.folder("org/whisper").exists()
    # Another program's repository keeps the weights it shares with whisper.
    assert disk.app.read_bytes() == WHISPER and disk.store() == shared_before


def test_only_the_pinned_revision_goes_and_files_another_program_added_to_it_stay(
    disk: Disk, capsys: pytest.CaptureFixture[str]
) -> None:
    extra = put(disk.cache, "org/speech", OURS, "extra.bin", b"e" * 1000)  # not in `files`
    assert main(["models", "remove", "speech", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Removed speech: under 1 MB freed." in out  # its weights are the other revision's too
    snapshot = disk.folder("org/speech") / "snapshots"
    assert not (snapshot / OURS / "config.json").exists()
    assert not (snapshot / OURS / "model.safetensors").exists()
    assert extra.read_bytes() == b"e" * 1000
    assert (snapshot / THEIRS / "model.safetensors").read_bytes() == WEIGHTS
    assert (snapshot / THEIRS / "config.json").read_bytes() == b'{"model": "older"}'

    extra.unlink()  # now the revision holds only what Wavescribe fetched: all of it goes
    put(disk.cache, "org/speech", OURS, "config.json", CONFIG)
    put(disk.cache, "org/speech", OURS, "model.safetensors", WEIGHTS, shared=True)
    assert main(["models", "remove", "speech", "--yes"]) == 0
    assert not (snapshot / OURS).exists() and (snapshot / THEIRS).is_dir()
    assert not (disk.folder("org/speech") / "refs" / "main").exists()
    assert (snapshot / THEIRS / "model.safetensors").read_bytes() == WEIGHTS


def test_a_url_model_goes_with_its_folder_and_part_files(
    disk: Disk, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["models", "remove", "vad", "-y"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["vad", "under", "1", "MB", "models/vad"]
    assert out[1].startswith("Removed vad: under 1 MB freed.")
    assert not (disk.models / "vad").exists() and disk.models.is_dir()


def test_the_part_files_of_a_stopped_download_go_with_the_model(
    disk: Disk, capsys: pytest.CaptureFixture[str]
) -> None:
    # A download of "half" stopped in its first file: a part-file is all there is of it.
    half = entry("half", "hf:org/half", OURS)
    disk.registry.add(half)
    blobs = disk.folder("org/half") / "blobs"
    blobs.mkdir(parents=True)
    part = blobs / ("d" * 64 + ".part")
    part.write_bytes(b"p" * (2 * MB))
    assert main(["models", "remove", "half", "--yes"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["half", "2", "MB", modelremove.where(part)]
    assert out[1].startswith("Removed half: 2 MB freed.")
    assert not part.exists()

    # One a download is writing stays with that download.
    part.write_bytes(b"p" * MB)
    with partfiles.held(part):
        assert main(["models", "remove", "half", "--yes"]) == 0
    assert part.exists()


def test_what_a_stopped_download_kept_counts_once_and_what_cant_continue_doesnt(
    disk: Disk,
) -> None:
    folder = disk.folder("org/speech")
    # The pinned revision: its config, and its weights through the store, counted once although
    # the other revision links the same weights. The other revision's own config isn't counted.
    assert modelfetch.model_bytes(folder, OURS) == len(CONFIG) + len(WEIGHTS)
    assert modelfetch.model_bytes(folder, None) == len(CONFIG) + len(WEIGHTS) + 18  # a branch
    blobs = folder / "blobs"
    (blobs / ("e" * 64)).write_bytes(b"e" * MB)  # finished, not linked yet: the download stopped
    (blobs / ("d" * 64 + ".part")).write_bytes(b"p" * MB)  # Wavescribe's, which continues
    (blobs / ("f" * 64 + ".1a2b3c4d.incomplete")).write_bytes(b"i" * (2 * MB))  # the library's
    assert modelfetch.model_bytes(folder, OURS) == len(CONFIG) + len(WEIGHTS) + 2 * MB
    assert modelfetch.cut_bytes(folder) == 2 * MB
    assert modelfetch.model_bytes(disk.folder("org/nothing"), OURS) == 0


def test_a_part_finished_download_goes_whole_with_remove(
    disk: Disk, capsys: pytest.CaptureFixture[str]
) -> None:
    # A download of "half" stopped in its second file: the first is whole but not linked yet.
    disk.registry.add(entry("half", "hf:org/half", OURS))
    blobs = disk.folder("org/half") / "blobs"
    blobs.mkdir(parents=True)
    finished, part = blobs / ("e" * 64), blobs / ("d" * 64 + ".part")
    finished.write_bytes(b"e" * (2 * MB))
    part.write_bytes(b"p" * MB)
    assert main(["models", "remove", "half", "--yes"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["half", "3", "MB", modelremove.where(part) + ",",
                              modelremove.where(finished)]  # fmt: skip
    assert out[1].startswith("Removed half: 3 MB freed.")
    assert not finished.exists() and not part.exists()
    # The files of a revision the registry doesn't pin are someone else's, linked or not.
    assert (disk.folder("org/speech") / "snapshots" / THEIRS / "config.json").exists()


def test_the_last_reference_to_a_shared_file_frees_it(
    disk: Disk, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = next(p for p in disk.store() if p.read_bytes()[:1] == b"x")
    disk.app.unlink()  # another program's copy is gone, and with it the other reference
    shutil.rmtree(disk.folder("someone/app"))
    assert main(["models", "remove", "whisper", "--yes"]) == 0
    assert "Removed whisper: 3 MB freed." in capsys.readouterr().out
    assert not payload.exists()


def test_nothing_is_removed_without_a_yes(
    disk: Disk, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for said in ("n", "", None):  # no, just Enter, and nobody there to ask
        answer(monkeypatch, said)
        assert main(["models", "remove", "whisper", "vad"]) == 1
        out = capsys.readouterr().out
        assert out.endswith("Nothing removed.\n")
    assert disk.folder("org/whisper").is_dir() and (disk.models / "vad" / "vad.onnx").exists()
    asked = answer(monkeypatch, "yes")
    assert main(["models", "remove", "whisper", "vad"]) == 0
    assert asked == ["Remove these 2 models (1 MB in all)? [y/N] "]
    assert "Removed whisper and vad: 1 MB freed. To download one again: " in (
        capsys.readouterr().out
    )


def test_a_model_that_isnt_installed_is_refused_and_nothing_is_removed(
    disk: Disk, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answer(monkeypatch, "y")
    shutil.rmtree(disk.models / "vad")
    assert main(["models", "remove", "whisper", "vad"]) == 1
    assert capsys.readouterr().err == (
        "wavescribe: vad isn't installed (nothing of it is on this disk), so nothing was removed\n"
    )
    assert disk.folder("org/whisper").is_dir()
    # A revision other than the pinned one is someone else's: speech without its own isn't there.
    put(disk.cache, "org/other", THEIRS, "f", b"f")
    other = entry("other", "hf:org/other", OURS)
    disk.registry.add(other)
    assert main(["models", "remove", "other"]) == 1
    assert "other isn't installed" in capsys.readouterr().err
    assert (disk.folder("org/other") / "snapshots" / THEIRS / "f").exists()


def test_your_own_model_and_an_unknown_id_are_refused(
    disk: Disk, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["models", "remove", "mine", "--yes"]) == 1
    assert "mine is your own model, at /somewhere/mine" in capsys.readouterr().err
    assert main(["models", "remove", "nope", "--yes"]) == 1
    assert "unknown model 'nope'" in capsys.readouterr().err
    assert main(["models", "remove"]) == 2
    assert main(["models", "remove", "vad", "--all"]) == 2
    assert "name the models to remove, or give --all" in capsys.readouterr().err
    assert (disk.models / "vad" / "vad.onnx").exists()


def test_all_removes_every_installed_model_and_only_those(
    disk: Disk, capsys: pytest.CaptureFixture[str]
) -> None:
    (disk.root / "data").mkdir()
    credits = disk.root / "data" / "THIRD_PARTY_MODELS.md"
    credits.write_text("# Third-party models\n\n- **vad** (asr): u\n")
    assert main(["models", "remove", "--all", "--yes"]) == 0
    out = capsys.readouterr().out
    assert [line.split()[0] for line in out.splitlines()[:-1]
            if not line.startswith(" ")] == ["speech", "whisper", "vad"]  # fmt: skip
    assert "Removed speech, whisper and vad:" in out
    assert not disk.folder("org/whisper").exists() and not (disk.models / "vad").exists()
    assert disk.app.read_bytes() == WHISPER  # another program's
    assert (disk.folder("org/speech") / "snapshots" / THEIRS / "config.json").exists()
    assert "vad" not in credits.read_text()  # the credits list what is still installed
    assert main(["models", "remove", "--all", "--yes"]) == 0
    assert capsys.readouterr().out == "No model is installed: nothing to remove.\n"


def test_remove_is_in_the_models_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["models", "remove", "a", "b", "-y"])
    assert args.ids == ["a", "b"] and args.yes and not args.all
