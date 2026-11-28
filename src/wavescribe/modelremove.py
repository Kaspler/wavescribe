# SPDX-License-Identifier: Apache-2.0
"""What ``wavescribe models remove`` takes off the disk, and nothing else.

A model downloaded from Hugging Face lives in the library's shared cache
(``~/.cache/huggingface/hub``), which other programs use too. Its folder there holds links: the
library keeps the weights in a cache-wide store (``hub/blobs``) shared by every repository with
the same file, so deleting the folder by hand frees next to nothing. Removal goes through the
library's own cache code instead (``scan_cache_dir`` and its deletion plans), which removes the
links and then whatever in the shared store nothing else links to any more, and says how many
bytes that frees. Only the exact repository and revision the registry pins are touched: another
revision of the same repository, and every other repository, belong to someone else. Where that
revision holds files beyond the ones the registry asks for (``files``), another program fetched
them, and only Wavescribe's are removed.

The part-files of a download that stopped, which the next download would continue (partfiles.py),
go too, unless a download is writing one. A model from a URL is a file in ``models/<id>/``, and
that folder goes, part-files and all. A model of your own at a path you gave (``local:``) is
yours, and is never removed.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from wavescribe import partfiles
from wavescribe.modelfetch import dir_bytes, hub_cache, repo_folder
from wavescribe.registry import ModelEntry, ModelError, ModelRegistry


@dataclass
class Removal:
    """What removing one model takes off the disk."""

    entry: ModelEntry
    places: list[Path] = field(default_factory=list)  # where its bytes are, for the listing
    size: int = 0  # bytes it frees
    shared: int = 0  # bytes that stay: another entry in the cache has the same files
    strategy: Any = None  # the library's deletion plan for its cache entry
    folder: Path | None = None  # its folder under the models folder
    parts: list[Path] = field(default_factory=list)  # part-files the next download continues

    @property
    def id(self) -> str:
        return self.entry.id


def _own_folder(registry: ModelRegistry, entry: ModelEntry) -> Path | None:
    """The model's folder under the models folder, when there is one: models/<id>, and never a
    path that leads out of the models folder."""
    root = registry.models_dir
    folder = root / entry.id
    if not entry.id or Path(entry.id).name != entry.id or entry.id in (".", ".."):
        return None
    if not (folder.is_dir() or folder.is_symlink()):
        return None
    return folder


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    """A file of the snapshot (its path inside it) that ``files`` asks for, as the library
    matches allow_patterns."""
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(name, p.rstrip("/") + "/*")
               for p in patterns)  # fmt: skip


def _hub_plan(entry: ModelEntry, cache: Path) -> tuple[Any, list[Path], int, int] | None:
    """The deletion plan for the model's revision in the cache: (plan, places, bytes it frees,
    bytes that stay because another entry has the same files); None when it isn't there."""
    folder = cache / repo_folder(entry)
    if not folder.is_dir():
        return None
    from huggingface_hub import scan_cache_dir

    info = scan_cache_dir(cache)
    repo = next((r for r in info.repos
                 if r.repo_type == "model" and r.repo_id == entry.location), None)  # fmt: skip
    if repo is None:
        return None
    revision = entry.revision or "main"  # one of your own may follow a branch, as fetch does
    pinned = [r for r in repo.revisions
              if r.commit_hash == revision or revision in r.refs]  # fmt: skip
    if not pinned:
        return None
    only = replace(info, repos=frozenset({repo}))  # every other repository is out of reach
    files = [f for r in pinned for f in r.files]
    ours = files
    if entry.files:
        ours = [
            f
            for r in pinned
            for f in r.files
            if _matches(f.file_path.relative_to(r.snapshot_path).as_posix(), entry.files)
        ]
    if len(ours) < len(files):  # someone else's files in the same revision stay
        if not hasattr(only, "delete_files"):
            raise ModelError(f"{entry.id}: this version of huggingface-hub can't remove only "
                             "some files of a revision; nothing of it was removed")  # fmt: skip
        plan = only.delete_files(*ours)
        places = [f.file_path for f in ours]
    else:
        plan = only.delete_revisions(*(r.commit_hash for r in pinned))
        whole = len(repo.revisions) == len(pinned)
        places = [repo.repo_path] if whole else [r.snapshot_path for r in pinned]
    logical = sum({f.blob_path: f.size_on_disk for f in ours}.values())
    return plan, places, plan.expected_freed_size, max(0, logical - plan.expected_freed_size)


def plan(registry: ModelRegistry, entry: ModelEntry) -> Removal | None:
    """What removing ``entry`` would take off the disk; None when nothing of it is there."""
    if entry.kind == "local":
        raise ModelError(
            f"{entry.id} is your own model, at {entry.location}: Wavescribe didn't download it, "
            "so it doesn't remove it; delete it yourself if you no longer want it"
        )
    removal = Removal(entry)
    if entry.kind == "hf":
        found = _hub_plan(entry, hub_cache())
        if found is not None:
            removal.strategy, places, removal.size, removal.shared = found
            removal.places += places
        removal.parts = partfiles.found(hub_cache() / repo_folder(entry))
        removal.places += [p for p in removal.parts
                           if not any(p.is_relative_to(q) for q in removal.places)]  # fmt: skip
        removal.size += partfiles.size(removal.parts)
    folder = _own_folder(registry, entry)
    if folder is not None:
        removal.folder = folder
        removal.places.append(folder)
        removal.size += 0 if folder.is_symlink() else dir_bytes(folder)
    return removal if removal.places else None


def remove(registry: ModelRegistry, removal: Removal) -> int:
    """Take ``removal`` off the disk. Returns the bytes freed. The plan is made again first, so
    what an earlier removal changed (a file two models shared) counts."""
    fresh = plan(registry, removal.entry)
    if fresh is None:
        return 0
    partfiles.remove(fresh.parts)  # first: the plan may take the whole folder they are in
    if fresh.strategy is not None:
        fresh.strategy.execute()
    if fresh.folder is not None:
        if fresh.folder.is_symlink():
            fresh.folder.unlink()
        else:
            shutil.rmtree(fresh.folder)
    return fresh.size


def where(path: Path) -> str:
    """A path as the listing shows it: under the home folder as ~, else relative when it is in
    the folder Wavescribe runs in."""
    text = str(path)
    home = str(Path.home())
    if text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return text
