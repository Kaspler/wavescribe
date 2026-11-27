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
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import tempfile
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any

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
PART_SUFFIX = ".part"  # a URL model while it downloads, next to where it will be
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
        with tempfile.NamedTemporaryFile(
            dir=target.parent, suffix=PART_SUFFIX, delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
            try:
                digest = hashlib.sha256()
                with urllib.request.urlopen(
                    entry.location, timeout=60, context=trust_store()
                ) as resp:
                    while block := resp.read(1 << 20):
                        digest.update(block)
                        tmp.write(block)
            except BaseException:  # a dropped connection or Ctrl-C: no partial file is left
                tmp_path.unlink()
                raise
        if entry.sha256 and digest.hexdigest() != entry.sha256:
            tmp_path.unlink()
            host = urllib.parse.urlsplit(entry.location).hostname
            raise ModelError(
                f"couldn't download {entry.id}: what {host} sent isn't the model (sha256 mismatch: "
                f"got {digest.hexdigest()}, want {entry.sha256}), as happens when a Wi-Fi sign-in "
                "page or a proxy answers instead; nothing was installed: open a web page in a "
                f"browser to check the connection, then {RETRY}."
            )
        tmp_path.replace(target)
        return target


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
