# SPDX-License-Identifier: Apache-2.0
"""Model registry: which models exist, their licences, and where they live on disk.

The tracked registry is ``models.toml`` next to this file. A user's private
``config/models.local.toml`` adds bring-your-own models (tier ``byo``: never listed by us).
Downloads happen only through ``wavescribe models fetch``; every other command runs with
``HF_HUB_OFFLINE=1``.

The licence gate (docs/LICENSING.md): default-tier models need nothing. Opt-in models need the
user's acceptance of their licence, and restricted (non-commercial) ones also an explicit
non-commercial acknowledgement, both recorded in ``data/licenses-accepted.json``. Chains skip a
model that isn't accepted, and naming one explicitly fails with the command that accepts it.

A download that stops continues where it stopped (partfiles.py): a model from a URL with a
checksum, and every file of a Hugging Face model of BIG_FILE bytes or more, which Wavescribe
downloads itself over HTTP. The library downloads the rest; a file of its own that is cut off
starts again from its beginning, because it keeps nothing of it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any, BinaryIO

from wavescribe import partfiles
from wavescribe.httpclient import trust_store
from wavescribe.invocation import typed

log = logging.getLogger(__name__)

TIERS = ("default", "opt-in", "restricted", "byo")
MODELS_DIR_ENV = "WAVESCRIBE_MODELS_DIR"
LOCAL_MODELS_PATH = Path("config/models.local.toml")
# Models added from the page's online catalogue. Machine-written, and parsed like models.toml
# (tiers kept, so opt-in and restricted models still need an acceptance), unlike the
# bring-your-own file, whose entries are all tier byo.
CATALOG_MODELS_PATH = Path("config/models.catalog.toml")
LEDGER_PATH = Path("data/licenses-accepted.json")
PART_SUFFIX = partfiles.SUFFIX  # a download that the next one continues (partfiles.py)
# A download that nothing continues: the library's own, and a URL model's without a checksum.
TEMP_SUFFIX = ".incomplete"
BIG_FILE = 100_000_000  # a Hugging Face file this large downloads so that a rerun continues it
# What a failed download tells the person to do. The page shows the same sentences and puts its
# own words here (downloads.py), so every such sentence uses this constant.
RETRY = "run the same command again"


class ModelError(RuntimeError):
    """A model is unknown, not installed, or unusable for the requested language."""


class LicenseError(ModelError):
    """A model's licence hasn't been accepted (opt-in and restricted tiers)."""


@dataclass(frozen=True)
class ModelEntry:
    id: str
    capability: str
    backend: str
    source: str  # "hf:<repo>" or "url:<https url>"
    upstream: str
    license: str
    license_url: str
    tier: str
    gated: bool
    languages: tuple[str, ...]
    attribution: str
    revision: str | None = None
    sha256: str | None = None
    files: tuple[str, ...] | None = None
    notes: str = ""
    # Target languages the model was measured inventing content for: never used for alerts there.
    unsafe_targets: tuple[str, ...] = ()
    size_gb: float | None = None  # download (weights) size, for "will it fit" before fetching
    tested: str = ""  # what was measured, e.g. "bake-off, pt-PT: 63x real time"
    origin: str = "registry"  # registry (models.toml), catalog (added from the page), local (byo)

    @property
    def kind(self) -> str:
        return self.source.split(":", 1)[0]

    @property
    def location(self) -> str:
        return self.source.split(":", 1)[1]

    def supports(self, language: str) -> bool:
        if "*" in self.languages:
            return True
        lang = language.lower()
        primary = lang.split("-", 1)[0]
        return any(tag.lower() in (lang, primary) for tag in self.languages)


def hub_cache() -> Path:
    """The Hugging Face cache, where the library puts what it downloads: read from the same
    variables it reads, in the same order (HF_HUB_CACHE, HUGGINGFACE_HUB_CACHE, HF_HOME,
    XDG_CACHE_HOME), as they are now."""
    env = os.environ
    home = env.get("HF_HOME") or str(Path(env.get("XDG_CACHE_HOME", "~/.cache")) / "huggingface")
    hub = env.get("HF_HUB_CACHE") or env.get("HUGGINGFACE_HUB_CACHE") or str(Path(home) / "hub")
    return Path(os.path.expandvars(os.path.expanduser(hub)))


def repo_folder(entry: ModelEntry) -> str:
    """A Hugging Face model's folder in the cache: models--<owner>--<name>."""
    return "models--" + entry.location.replace("/", "--")


def accept_command(entry: ModelEntry) -> str:
    nc = " --non-commercial" if entry.tier == "restricted" else ""
    return typed(f"models accept {entry.id} --license '{entry.license}'{nc}")


class Licenses:
    """The local record of licences the user accepted (``data/licenses-accepted.json``)."""

    def __init__(self, path: Path = LEDGER_PATH) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def accepted(self, entry: ModelEntry) -> bool:
        if entry.tier in ("default", "byo"):
            return True
        record = self._read().get(entry.id)
        # A licence change upstream voids the old acceptance.
        if not record or record.get("license") != entry.license:
            return False
        return entry.tier != "restricted" or bool(record.get("non_commercial"))

    def accept(self, entry: ModelEntry, license_id: str, *, non_commercial: bool = False) -> None:
        if license_id.strip().lower() != entry.license.lower():
            raise LicenseError(
                f"{entry.id} is under {entry.license!r}, not {license_id!r}; read "
                f"{entry.license_url} and pass that licence id"
            )
        if entry.tier == "restricted" and not non_commercial:
            raise LicenseError(
                f"{entry.id} is non-commercial ({entry.license}): add --non-commercial to confirm "
                "you'll use it only for non-commercial purposes"
            )
        records = self._read()
        records[entry.id] = {
            "license": entry.license,
            "license_url": entry.license_url,
            "tier": entry.tier,
            "revision": entry.revision,
            "non_commercial": non_commercial,
            "accepted_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)


class ModelRegistry:
    def __init__(
        self,
        entries: list[ModelEntry],
        chains: dict[str, list[str]],
        models_dir: Path | None = None,
        licenses: Licenses | None = None,
    ) -> None:
        self._entries = {e.id: e for e in entries}
        self._chains = chains
        self.models_dir = models_dir or Path(os.environ.get(MODELS_DIR_ENV, "models"))
        self.licenses = licenses or Licenses()
        for capability, ids in chains.items():
            for model_id in ids:
                if model_id not in self._entries:
                    raise ModelError(f"chain {capability!r} names unknown model {model_id!r}")

    @classmethod
    def load(
        cls,
        models_dir: Path | None = None,
        *,
        local: Path | None = LOCAL_MODELS_PATH,
        ledger: Path = LEDGER_PATH,
        catalog: Path | None = CATALOG_MODELS_PATH,
    ) -> ModelRegistry:
        text = resources.files("wavescribe").joinpath("models.toml").read_text(encoding="utf-8")
        raw = tomllib.loads(text)
        entries = [_parse_entry(m) for m in raw.get("model", [])]
        chains = raw.get("chains", {})
        if catalog is not None and catalog.exists():
            ids = {e.id for e in entries}
            for m in tomllib.loads(catalog.read_text(encoding="utf-8")).get("model", []):
                try:
                    entry = replace(_parse_entry(m), origin="catalog")
                except ModelError as e:
                    log.warning("%s: %s", catalog, e)
                    continue
                if entry.id in ids:
                    continue
                entries.append(entry)
                ids.add(entry.id)
                chain = chains.setdefault(entry.capability, [])
                chain.append(entry.id)  # added models go last; the page reorders
        if local is not None and local.exists():
            ids = {e.id for e in entries}
            for m in tomllib.loads(local.read_text(encoding="utf-8")).get("model", []):
                byo = _parse_local_entry(m, local)
                if byo.id in ids:
                    raise ModelError(f"{local}: {byo.id!r} is already in the registry")
                entries.append(byo)
        return cls(entries, chains, models_dir, Licenses(ledger))

    def entries(self) -> list[ModelEntry]:
        return list(self._entries.values())

    def get(self, model_id: str) -> ModelEntry:
        try:
            return self._entries[model_id]
        except KeyError:
            raise ModelError(f"unknown model {model_id!r}") from None

    def chain(self, capability: str) -> list[ModelEntry]:
        return [self._entries[i] for i in self._chains.get(capability, [])]

    def add(self, entry: ModelEntry) -> None:
        """A model added while running (from the page's catalogue): last in its chain."""
        if entry.id in self._entries:
            raise ModelError(f"{entry.id} is already in the registry")
        self._entries[entry.id] = entry
        self._chains.setdefault(entry.capability, []).append(entry.id)

    def chains(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._chains.items()}

    def set_chain(self, capability: str, ids: list[str]) -> None:
        """Reorder (or extend) a capability's chain: the page's choice of preferred models."""
        for model_id in ids:
            entry = self.get(model_id)
            if entry.capability != capability:
                raise ModelError(f"{model_id} is a {entry.capability} model, not {capability}")
        self._chains[capability] = list(dict.fromkeys(ids))

    def resolve(self, capability: str, language: str = "*") -> tuple[ModelEntry, Path]:
        """The first installed, licence-cleared model in the chain that supports ``language``."""
        candidates = [e for e in self.chain(capability) if e.supports(language)]
        if not candidates:
            raise ModelError(f"no {capability} model in the registry supports {language!r}")
        for entry in candidates:
            if not self.licenses.accepted(entry):
                log.info("%s skipped: its licence (%s) isn't accepted", entry.id, entry.license)
                continue
            path = self.local_path(entry)
            if path is not None:
                return entry, path
        ids = " ".join(e.id for e in candidates)
        raise ModelError(
            f"no {capability} model for {language!r} is installed; "
            f"run: {typed('models fetch ' + candidates[0].id)}  (candidates: {ids})"
        )

    def installed(self, model_id: str, language: str = "*") -> tuple[ModelEntry, Path]:
        """A specific model and its path, if it supports ``language`` and is downloaded."""
        entry = self.get(model_id)
        if not entry.supports(language):
            raise ModelError(f"{model_id} does not support {language!r}")
        self.check_license(entry)
        path = self.local_path(entry)
        if path is None:
            raise ModelError(
                f"{model_id} is not installed; run: {typed('models fetch ' + model_id)}"
            )
        return entry, path

    def check_license(self, entry: ModelEntry) -> None:
        """The startup gate: refuse an opt-in or restricted model without an acceptance."""
        if not self.licenses.accepted(entry):
            raise LicenseError(
                f"{entry.id} is tier {entry.tier} under {entry.license} ({entry.license_url}). "
                f"Read it, then accept it with: {accept_command(entry)}"
            )
        if entry.tier == "byo" and entry.license.lower() in ("", "unknown", "none"):
            log.warning("%s: no known licence; using it is your responsibility", entry.id)

    # -- local storage -------------------------------------------------------------------

    def local_path(self, entry: ModelEntry) -> Path | None:
        """Where the model is on disk, or None if it isn't fully downloaded. Never downloads."""
        if entry.kind == "local":
            path = Path(entry.location).expanduser()
            return path if path.exists() else None
        if entry.kind == "url":
            path = self._url_target(entry)
            return path if path.exists() else None
        if entry.kind == "hf":
            from huggingface_hub import snapshot_download
            from huggingface_hub.errors import LocalEntryNotFoundError

            try:
                return Path(
                    snapshot_download(
                        entry.location,
                        revision=entry.revision,
                        allow_patterns=list(entry.files) if entry.files else None,
                        local_files_only=True,
                    )
                )
            except LocalEntryNotFoundError:
                return None
        raise ModelError(f"{entry.id}: unsupported source {entry.source!r}")

    def fetch(self, entry: ModelEntry) -> Path:
        """Download a model from its rights holder. The only place that touches the network."""
        self.check_license(entry)
        if entry.kind == "local":
            path = self.local_path(entry)
            if path is None:
                raise ModelError(f"{entry.id}: {entry.location} doesn't exist")
            return path
        if entry.kind == "url":
            return self._fetch_url(entry)
        if entry.kind == "hf":
            from huggingface_hub import snapshot_download

            self._fetch_big_files(entry)
            return Path(
                snapshot_download(
                    entry.location,
                    revision=entry.revision,
                    allow_patterns=list(entry.files) if entry.files else None,
                )
            )
        raise ModelError(f"{entry.id}: unsupported source {entry.source!r}")

    def _url_target(self, entry: ModelEntry) -> Path:
        return self.models_dir / entry.id / entry.location.rsplit("/", 1)[-1]

    def _fetch_url(self, entry: ModelEntry) -> Path:
        target = self._url_target(entry)
        if target.exists():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.sha256:  # a part-file per pinned file: another pin's is no use to anyone
            part = target.with_name(f"{target.name}.{entry.sha256[:16]}{PART_SUFFIX}")
            partfiles.remove(p for p in partfiles.found(target.parent) if p != part)
            with _held(entry, part) as out:
                _download(entry.location, out)
                _install(entry, out, part, target, entry.sha256, entry.location)
            return target
        # Nothing to check a continued file against: each download starts from the beginning.
        with tempfile.NamedTemporaryFile(
            dir=target.parent, suffix=TEMP_SUFFIX, delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
            try:
                _download(entry.location, tmp)
            except BaseException:  # a dropped connection or Ctrl-C: no partial file is left
                tmp_path.unlink()
                raise
        tmp_path.replace(target)
        return target

    def _fetch_big_files(self, entry: ModelEntry) -> None:
        """Download a Hugging Face model's files of BIG_FILE bytes or more into part-files that
        the next download continues, and from there into the library's cache, where
        ``snapshot_download`` finds them whole and only links them. The library downloads the
        others itself. A part-file of a file this revision doesn't have (another revision's)
        is removed."""
        from huggingface_hub import HfApi, get_hf_file_metadata, hf_hub_url
        from huggingface_hub.file_download import http_get
        from huggingface_hub.utils import build_hf_headers, filter_repo_objects

        tree = HfApi().list_repo_tree(entry.location, revision=entry.revision, recursive=True)
        big = [f for f in tree if getattr(f, "lfs", None) is not None and f.size >= BIG_FILE]
        patterns = list(entry.files) if entry.files else None
        big = list(filter_repo_objects(big, allow_patterns=patterns, key=lambda f: f.path))
        blobs = hub_cache() / repo_folder(entry) / "blobs"
        wanted = {blobs / (f.lfs.sha256 + PART_SUFFIX) for f in big}
        partfiles.remove(p for p in partfiles.found(blobs.parent) if p not in wanted)
        for f in big:
            if (blobs / f.lfs.sha256).exists():
                continue
            url = hf_hub_url(entry.location, f.path, revision=entry.revision)
            meta = get_hf_file_metadata(url)
            blob = blobs / (meta.etag or f.lfs.sha256)  # the name the library looks for
            if blob.exists():
                continue
            headers = build_hf_headers()
            if urllib.parse.urlsplit(meta.location).netloc != urllib.parse.urlsplit(url).netloc:
                headers.pop("authorization", None)  # a signed address elsewhere: no token there
            blobs.mkdir(parents=True, exist_ok=True)
            part = blobs / (f.lfs.sha256 + PART_SUFFIX)
            with _held(entry, part) as out:
                have = out.seek(0, os.SEEK_END)
                if have > f.size:  # not this file's beginning: start again
                    out.truncate(0)
                    have = 0
                http_get(meta.location, out, resume_size=have, headers=headers,
                         expected_size=f.size, displayed_filename=f.path)  # fmt: skip
                _install(entry, out, part, blob, f.lfs.sha256, meta.location)


@contextmanager
def _held(entry: ModelEntry, part: Path) -> Iterator[BinaryIO]:
    try:
        with partfiles.held(part) as out:
            yield out
    except partfiles.Busy:
        raise ModelError(
            f"couldn't download {entry.id}: another download of it is under way (on the page or "
            "in another window); nothing is broken: let that one finish, or stop it, then "
            f"{RETRY}."
        ) from None


def _download(url: str, out: BinaryIO) -> None:
    """Write what ``url`` holds to ``out``, after the bytes ``out`` has already: the download
    continues where an earlier one stopped (an HTTP range request). A server that sends the
    whole file instead has it written from the beginning."""
    have = out.seek(0, os.SEEK_END)
    request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"}) if have else url
    try:
        resp = urllib.request.urlopen(request, timeout=60, context=trust_store())
    except urllib.error.HTTPError as e:
        if have and e.code == 416:  # nothing after what is there: the checksum says if it's whole
            return
        raise
    with resp:
        if have and resp.status != 206:
            out.seek(0)
            out.truncate()
        while block := resp.read(1 << 20):
            out.write(block)


def _install(
    entry: ModelEntry, out: BinaryIO, part: Path, target: Path, sha256: str, source: str
) -> None:
    """Put a whole part-file in its place once its checksum matches; one that doesn't is
    removed (the next download starts from the beginning)."""
    out.flush()
    got = partfiles.checksum(part)
    if got != sha256:
        part.unlink()
        host = urllib.parse.urlsplit(source).hostname
        raise ModelError(
            f"couldn't download {entry.id}: what {host} sent isn't the model (sha256 mismatch: "
            f"got {got}, want {sha256}), as happens when a Wi-Fi sign-in page or a proxy answers "
            "instead; nothing was installed: open a web page in a browser to check the "
            f"connection, then {RETRY}."
        )
    part.replace(target)


def _parse_local_entry(raw: dict[str, Any], path: Path) -> ModelEntry:
    """A bring-your-own entry from ``config/models.local.toml``: fewer rules, all on the user."""
    missing = [k for k in ("id", "capability", "backend", "source", "languages", "license")
               if k not in raw]  # fmt: skip
    if missing:
        raise ModelError(f"{path}: entry {raw.get('id', '?')} is missing {missing}")
    if raw["source"].startswith("hf:") and not raw.get("revision"):
        log.warning("%s: %s isn't pinned to a revision", path, raw["id"])
    files = raw.get("files")
    return ModelEntry(
        id=raw["id"],
        capability=raw["capability"],
        backend=raw["backend"],
        source=raw["source"],
        upstream=raw.get("upstream", raw["source"]),
        license=raw["license"],
        license_url=raw.get("license_url", ""),
        tier="byo",
        gated=bool(raw.get("gated", False)),
        languages=tuple(raw["languages"]),
        attribution=raw.get("attribution", ""),
        revision=raw.get("revision"),
        sha256=raw.get("sha256"),
        files=tuple(files) if files else None,
        notes=raw.get("note", raw.get("notes", "")),
        size_gb=float(raw["size_gb"]) if raw.get("size_gb") is not None else None,
        origin="local",
    )


def _parse_entry(raw: dict[str, Any]) -> ModelEntry:
    required = ("id", "capability", "backend", "source", "upstream", "license", "license_url")
    missing = [k for k in (*required, "tier", "languages") if k not in raw]
    if missing:
        raise ModelError(f"registry entry {raw.get('id', '?')}: missing {missing}")
    if raw["tier"] not in TIERS or raw["tier"] == "byo":
        raise ModelError(f"registry entry {raw['id']}: unknown tier {raw['tier']!r}")
    if raw["source"].startswith("hf:") and not raw.get("revision"):
        raise ModelError(f"registry entry {raw['id']}: Hugging Face sources need a pinned revision")
    if raw["source"].startswith("url:") and not raw.get("sha256"):
        raise ModelError(f"registry entry {raw['id']}: URL sources need a sha256")
    files = raw.get("files")
    return ModelEntry(
        id=raw["id"],
        capability=raw["capability"],
        backend=raw["backend"],
        source=raw["source"],
        upstream=raw["upstream"],
        license=raw["license"],
        license_url=raw["license_url"],
        tier=raw["tier"],
        gated=bool(raw.get("gated", False)),
        languages=tuple(raw["languages"]),
        attribution=raw.get("attribution", ""),
        revision=raw.get("revision"),
        sha256=raw.get("sha256"),
        unsafe_targets=tuple(raw.get("unsafe_targets", ())),
        files=tuple(files) if files else None,
        notes=raw.get("notes", ""),
        size_gb=float(raw["size_gb"]) if raw.get("size_gb") is not None else None,
        tested=raw.get("tested", ""),
    )
