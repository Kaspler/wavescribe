# SPDX-License-Identifier: Apache-2.0
"""The models catalogue: what this Mac can run, licence tiers, and a Hugging Face search.

Everything here works offline except ``search_hf``, ``model_at`` and the helpers they call:
explicit actions from the web page, never run at import or in the background.

Sizes are in GB of 2**30 bytes, the way Apple counts unified memory ("64 GB" is 64 * 2**30).
Memory bandwidth is in decimal GB/s, as Apple's specs give it.

Licence tiers follow docs/LICENSING.md. A model whose licence we haven't classified gets tier
``byo``: we never list it, but the user can still add it to ``config/models.local.toml``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import platform
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from wavescribe import __version__, jsonsafe
from wavescribe.httpclient import trust_store

if TYPE_CHECKING:
    from wavescribe.registry import ModelEntry, ModelRegistry

log = logging.getLogger(__name__)

GB = 2**30

# -- This machine ---------------------------------------------------------------------------

# Memory bandwidth in GB/s, from Apple's tech specs. Unknown chips get None.
BANDWIDTH_GBS: dict[str, float] = {
    "M1": 68, "M1 Pro": 200, "M1 Max": 400, "M1 Ultra": 800,
    "M2": 100, "M2 Pro": 200, "M2 Max": 400, "M2 Ultra": 800,
    "M3": 100, "M3 Pro": 150, "M3 Max": 400, "M3 Ultra": 819,
    "M4": 120, "M4 Pro": 273, "M4 Max": 546,
    "M5": 153,
}  # fmt: skip
# The binned Max chips (14 CPU cores instead of 16) have a narrower memory bus.
BINNED_BANDWIDTH_GBS: dict[tuple[str, int], float] = {("M3 Max", 14): 300, ("M4 Max", 14): 410}

# Left for macOS and Wavescribe itself (Python, VAD, gate, store, web page).
RESERVED_GB = 6.0


@dataclass(frozen=True)
class Machine:
    chip: str  # e.g. "Apple M1 Max"
    memory_gb: float
    gpu_cores: int | None
    performance_cores: int | None
    efficiency_cores: int | None
    macos: str | None
    bandwidth_gbs: float | None
    # Metal's recommendedMaxWorkingSetSize read through MLX (or the user's iogpu.wired_limit_mb);
    # None when neither could be read.
    gpu_limit_measured_gb: float | None = None

    @property
    def gpu_limit_gb(self) -> float:
        """How much memory the GPU may use without slowing down: measured, else estimated.

        Apple documents ``MTLDevice.recommendedMaxWorkingSetSize`` only as "an approximation of
        how much memory, in bytes, this GPU device can allocate without affecting its runtime
        performance". Measured: 51.8 of 64 GB (0.81) on an M1 Max; llama.cpp logs show about 2/3
        on 8-32 GB Macs (16 GB -> 10.7 GB). The estimate takes 2/3 up to 36 GB and 3/4 above.
        """
        if self.gpu_limit_measured_gb:
            return self.gpu_limit_measured_gb
        return self.memory_gb * (2 / 3 if self.memory_gb <= 36 else 3 / 4)

    @property
    def budget_gb(self) -> float:
        """Memory for models: unified memory minus macOS and the app, within the GPU's limit."""
        return max(0.0, min(self.memory_gb - RESERVED_GB, self.gpu_limit_gb))


def chip_bandwidth_gbs(chip: str, cpu_cores: int | None = None) -> float | None:
    name = chip.removeprefix("Apple").strip()
    if cpu_cores is not None and (name, cpu_cores) in BINNED_BANDWIDTH_GBS:
        return BINNED_BANDWIDTH_GBS[name, cpu_cores]
    return BANDWIDTH_GBS.get(name)


@functools.cache
def machine() -> Machine:
    """This computer, read once: sysctl, one system_profiler call, and Metal through MLX."""
    if sys.platform != "darwin":
        return _portable_machine()
    s = _sysctl(
        "machdep.cpu.brand_string",
        "hw.memsize",
        "hw.perflevel0.physicalcpu",
        "hw.perflevel1.physicalcpu",
        "kern.osproductversion",
        "iogpu.wired_limit_mb",
    )
    chip = s.get("machdep.cpu.brand_string") or platform.processor() or "unknown"
    memory = _int(s.get("hw.memsize"))
    perf = _int(s.get("hw.perflevel0.physicalcpu"))
    eff = _int(s.get("hw.perflevel1.physicalcpu"))
    limit = _measured_gpu_limit()
    if limit is None and (wired_mb := _int(s.get("iogpu.wired_limit_mb"))):
        limit = wired_mb * 2**20  # the user changed the GPU's wired limit with sysctl
    cores = perf + eff if perf is not None and eff is not None else None
    return Machine(
        chip=chip,
        memory_gb=memory / GB if memory else _physical_memory() / GB,
        gpu_cores=_gpu_cores(),
        performance_cores=perf,
        efficiency_cores=eff,
        macos=s.get("kern.osproductversion") or platform.mac_ver()[0] or None,
        bandwidth_gbs=chip_bandwidth_gbs(chip, cores),
        gpu_limit_measured_gb=limit / GB if limit else None,
    )


def _portable_machine() -> Machine:
    """Elsewhere: memory and cores only. The GPU estimate then just keeps the budget cautious."""
    return Machine(
        chip=platform.processor() or platform.machine() or "unknown",
        memory_gb=_physical_memory() / GB,
        gpu_cores=None,
        performance_cores=os.cpu_count(),
        efficiency_cores=None,
        macos=None,
        bandwidth_gbs=None,
    )


def _sysctl(*keys: str) -> dict[str, str]:
    """``key: value`` pairs; a key this Mac doesn't have is just missing from the result."""
    try:
        out = subprocess.run(["sysctl", *keys], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    pairs = (line.split(": ", 1) for line in out.splitlines() if ": " in line)
    return {k.strip(): v.strip() for k, v in pairs}


def _gpu_cores() -> int | None:
    try:
        out = subprocess.run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
        for gpu in json.loads(out).get("SPDisplaysDataType", []):
            if str(gpu.get("sppci_cores", "")).isdigit():
                return int(gpu["sppci_cores"])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def _measured_gpu_limit() -> int | None:
    """``recommendedMaxWorkingSetSize`` in bytes, via MLX (installed on Apple Silicon)."""
    try:
        import mlx.core as mx

        info = mx.device_info() if hasattr(mx, "device_info") else mx.metal.device_info()
        return int(info["max_recommended_working_set_size"])
    except Exception:  # no MLX, or no Metal device
        return None


def _physical_memory() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


# -- Fit --------------------------------------------------------------------------------------

# Memory at run time per GB of weights: activations, mel features, beam and decoder state.
OVERHEAD = {"vad": 1.5, "gate": 1.5, "asr": 1.2, "translate": 1.3}
DEFAULT_OVERHEAD = 1.3
# An LLM (mlx-lm) needs its runtime buffers plus a KV cache for the context.
LLM_OVERHEAD = 1.1
LLM_CAPABILITIES = ("translate", "llm")
CONTEXT_TOKENS = 8192
# The KV cache from the model's size alone: a grouped-query model keeps ~16 KiB per token per
# billion parameters at fp16 (Llama-3-8B: 32 layers x 8 KV heads x 128 dims x 2 (K and V) x
# 2 bytes = 128 KiB per token), and MLX's 4-bit weights take ~0.5625 bytes per parameter (4 bits
# plus a 16-bit scale and bias per group of 64). Models with sliding-window or linear attention
# (Gemma 3, Qwen3.5) and 8-bit or fp16 weights need less, so this errs on the safe side.
KV_BYTES_PER_TOKEN_PER_BILLION = 16 * 1024
BYTES_PER_PARAM_4BIT = 0.5625
# Below this share of the usable memory a model fits comfortably; above it, it's tight.
GOOD_SHARE = 0.7

FitLevel = Literal["good", "tight", "too_big", "unknown"]


@dataclass(frozen=True)
class Fit:
    level: FitLevel
    need_gb: float | None
    available_gb: float
    reason: str


def is_llm(capability: str, backend: str | None = None) -> bool:
    if backend is not None:
        return backend == "mlx-lm"
    return capability in LLM_CAPABILITIES


def kv_cache_gb(weights_gb: float, tokens: int = CONTEXT_TOKENS) -> float:
    """An estimate of an LLM's KV cache for ``tokens`` of context (see the constants above)."""
    billions = weights_gb * GB / BYTES_PER_PARAM_4BIT / 1e9
    return billions * KV_BYTES_PER_TOKEN_PER_BILLION * tokens / GB


def need_gb(capability: str, weights_gb: float, *, backend: str | None = None) -> float:
    if is_llm(capability, backend):
        return weights_gb * LLM_OVERHEAD + kv_cache_gb(weights_gb)
    return weights_gb * OVERHEAD.get(capability, DEFAULT_OVERHEAD)


def fit(
    capability: str,
    weights_gb: float | None,
    m: Machine,
    *,
    other_loaded_gb: float = 0.0,
    backend: str | None = None,
) -> Fit:
    """Whether a model fits next to ``other_loaded_gb`` of other models on this machine.

    ``backend`` decides whether it's an LLM (``mlx-lm``); without one, ``translate`` and ``llm``
    count as LLMs, so MADLAD's CTranslate2 should pass its backend.
    """
    available = max(0.0, m.budget_gb - other_loaded_gb)
    beside = " next to the other models" if other_loaded_gb else ""
    if weights_gb is None:
        return Fit("unknown", None, available, "size unknown until it's downloaded or looked up")
    need = need_gb(capability, weights_gb, backend=backend)
    kv = f", {kv_cache_gb(weights_gb):.1f} GB of it KV cache" if is_llm(capability, backend) else ""
    numbers = f"needs ~{need:.1f} GB{kv}; {available:.1f} GB usable{beside}"
    if need > available:
        return Fit("too_big", need, available, f"too big: {numbers}")
    if need > GOOD_SHARE * available:
        return Fit("tight", need, available, f"tight: {numbers}")
    return Fit("good", need, available, f"fits: {numbers}")


def speed_hint(
    capability: str, weights_gb: float | None, m: Machine, *, backend: str | None = None
) -> str | None:
    """For an LLM, the decoding speed memory bandwidth allows. ASR speed comes from measurements.

    Each generated token reads every weight once, so tokens/s <= bandwidth / weights. It's an
    upper bound: Qwen3.5-9B 4-bit (5.5 GB) measured ~60 tokens/s on an M1 Max against 67 here.
    """
    if not is_llm(capability, backend) or not weights_gb or m.bandwidth_gbs is None:
        return None
    tokens = m.bandwidth_gbs * 1e9 / (weights_gb * GB)
    return (
        f"at most ~{tokens:.0f} tokens/s ({m.bandwidth_gbs:.0f} GB/s memory bandwidth / "
        f"{weights_gb:.1f} GB of weights; real decoding is slower)"
    )


# -- Weights size ---------------------------------------------------------------------------

WEIGHT_SUFFIXES = (".safetensors", ".bin", ".npz", ".gguf", ".onnx", ".tflite")


def _weights_bytes(files: Iterable[tuple[str, int]]) -> int | None:
    weights = [(name, size) for name, size in files if name.lower().endswith(WEIGHT_SUFFIXES)]
    # A repo that ships the same weights twice (.safetensors and .bin or .npz) loads only one.
    if any(name.endswith(".safetensors") for name, _ in weights):
        weights = [(name, size) for name, size in weights if name.endswith(".safetensors")]
    return sum(size for _, size in weights) if weights else None


def installed_size_gb(path: Path) -> float:
    """The weights on disk: a model file itself, or the weight files under a model directory."""
    if path.is_file():
        return path.stat().st_size / GB
    seen: set[Path] = set()
    files = []
    for p in path.rglob("*"):
        real = p.resolve()  # Hugging Face snapshots are symlinks into blobs/
        if real.is_file() and real not in seen:
            seen.add(real)
            files.append((p.name, real.stat().st_size))
    return (_weights_bytes(files) or 0) / GB


def installed_sizes(registry: ModelRegistry) -> dict[str, float | None]:
    """Each registry model's weights on disk, or None if it isn't downloaded. Never downloads."""
    sizes: dict[str, float | None] = {}
    for entry in registry.entries():
        path = registry.local_path(entry)
        sizes[entry.id] = installed_size_gb(path) if path is not None else None
    return sizes


def listing_size_gb(siblings: list[dict[str, Any]]) -> float | None:
    """Weights size from a Hugging Face file listing with sizes (``?blobs=true``)."""
    files = [(s["rfilename"], s["size"]) for s in siblings if isinstance(s.get("size"), int)]
    size = _weights_bytes(files)
    return size / GB if size else None


# -- Licences -------------------------------------------------------------------------------

_NVIDIA_OML = (
    "https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/"
)
_HAI_DEF = "https://developers.google.com/health-ai-developer-foundations/terms"
_CC_BY_NC = "https://creativecommons.org/licenses/by-nc/4.0/"
_CC_BY_NC_SA = "https://creativecommons.org/licenses/by-nc-sa/4.0/"

# Hugging Face licence id (the card's ``license``, or its ``license_name`` when that is
# "other") -> (our tier, SPDX id or LicenseRef, where the text is). Policy: docs/LICENSING.md.
# Anything missing is "byo": not offered. Any other non-commercial licence is "restricted".
LICENSES: dict[str, tuple[str, str, str]] = {
    # default: permissive
    "apache-2.0": ("default", "Apache-2.0", "https://www.apache.org/licenses/LICENSE-2.0"),
    "mit": ("default", "MIT", "https://opensource.org/license/mit"),
    "bsd-2-clause": ("default", "BSD-2-Clause", "https://opensource.org/license/bsd-2-clause"),
    "bsd-3-clause": ("default", "BSD-3-Clause", "https://opensource.org/license/bsd-3-clause"),
    "isc": ("default", "ISC", "https://opensource.org/license/isc-license-txt"),
    "mpl-2.0": ("default", "MPL-2.0", "https://www.mozilla.org/en-US/MPL/2.0/"),
    "unlicense": ("default", "Unlicense", "https://unlicense.org/"),
    "cc0-1.0": ("default", "CC0-1.0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    "cc-by-3.0": ("default", "CC-BY-3.0", "https://creativecommons.org/licenses/by/3.0/"),
    "cc-by-4.0": ("default", "CC-BY-4.0", "https://creativecommons.org/licenses/by/4.0/"),
    "cdla-permissive-2.0": ("default", "CDLA-Permissive-2.0", "https://cdla.dev/permissive-2-0/"),
    "openmdw-1.0": ("default", "LicenseRef-OpenMDW-1.0", "https://openmdw.ai/license/1-0"),
    "openmdw-1.1": ("default", "LicenseRef-OpenMDW-1.1", "https://openmdw.ai/license/1-1"),
    # opt-in: terms to read and accept
    "gemma": ("opt-in", "LicenseRef-Gemma", "https://ai.google.dev/gemma/terms"),
    "health-ai-developer-foundations": ("opt-in", "LicenseRef-HAI-DEF", _HAI_DEF),
    "llama2": ("opt-in", "LicenseRef-Llama2", "https://www.llama.com/llama2/license/"),
    "llama3": ("opt-in", "LicenseRef-Llama3", "https://www.llama.com/llama3/license/"),
    "llama3.1": ("opt-in", "LicenseRef-Llama3.1", "https://www.llama.com/llama3_1/license/"),
    # Llama 3.2's multimodal models exclude the EU; mlx-lm doesn't load those (mllama) anyway.
    "llama3.2": ("opt-in", "LicenseRef-Llama3.2", "https://www.llama.com/llama3_2/license/"),
    "llama3.3": ("opt-in", "LicenseRef-Llama3.3", "https://www.llama.com/llama3_3/license/"),
    "nvidia-open-model-license": ("opt-in", "LicenseRef-NVIDIA-Open-Model-License", _NVIDIA_OML),
    "openrail": ("opt-in", "LicenseRef-OpenRAIL", "https://huggingface.co/blog/open_rail"),
    # restricted: non-commercial
    "cc-by-nc-4.0": ("restricted", "CC-BY-NC-4.0", _CC_BY_NC),
    "cc-by-nc-sa-4.0": ("restricted", "CC-BY-NC-SA-4.0", _CC_BY_NC_SA),
    # byo: known, but we don't list it
    "llama4": ("byo", "LicenseRef-Llama4", "https://www.llama.com/llama4/license/"),
}
# Why a known licence is byo.
BYO_REASONS = {"llama4": "its licence excludes the EU for multimodal models"}
_NON_COMMERCIAL = re.compile(r"(^|[-_])nc([-_]|$)|non-?commercial|research-only")
_CC = re.compile(r"cc-(by(?:-nc)?(?:-sa|-nd)?)-(\d\.\d)")


def license_tier(license_id: str | None) -> str:
    """Our tier for a Hugging Face licence id: default, opt-in, restricted, or byo (not listed)."""
    lic = (license_id or "").strip().lower()
    if lic in LICENSES:
        return LICENSES[lic][0]
    if lic and _NON_COMMERCIAL.search(lic):
        return "restricted"
    return "byo"


def spdx_id(license_id: str) -> str:
    lic = license_id.lower()
    if lic in LICENSES:
        return LICENSES[lic][1]
    if _CC.fullmatch(lic):
        return lic.upper()
    return f"LicenseRef-{license_id}"


def _canonical_url(license_id: str) -> str | None:
    if license_id in LICENSES:
        return LICENSES[license_id][2]
    if cc := _CC.fullmatch(license_id):
        return f"https://creativecommons.org/licenses/{cc[1]}/{cc[2]}/"
    return None


def _byo_reason(license_id: str | None) -> str:
    if not license_id:
        return "no licence on the model card"
    if license_id in ("other", "unknown"):
        return f"its licence is {license_id!r}: custom terms we haven't reviewed"
    if license_id in BYO_REASONS:
        return BYO_REASONS[license_id]
    return f"licence {license_id!r} isn't one docs/LICENSING.md classifies"


# -- Backends -------------------------------------------------------------------------------

# mlx-audio families our transcriber has run (asr/mlx_audio.py), by config.json model_type.
MLX_AUDIO_FAMILIES = (
    "whisper",
    "qwen3_asr",
    "voxtral_realtime",
    "nemotron_asr",
    "nemotron_asr_streaming",
)
# Vision-language checkpoints whose text part mlx-lm loads (it has a module for each).
MLX_LM_VLM_FAMILIES = (
    "qwen3_5",
    "qwen3_5_moe",
    "qwen3_vl",
    "qwen3_vl_moe",
    "gemma3",
    "gemma4",
    "mistral3",
)
PIPELINE_TAGS = {
    "asr": ("automatic-speech-recognition",),
    # Qwen3.5 checkpoints are tagged image-text-to-text; mlx-lm translates with their text part.
    "translate": ("text-generation", "translation", "image-text-to-text"),
}


def infer_backend(raw: Mapping[str, Any], capability: str) -> tuple[str | None, str]:
    """The Wavescribe backend that could load a Hugging Face repo, and a note.

    The note says why there's none, or what to watch for when there is one. A search result
    carries ``siblings``, ``library_name``, ``tags``, ``pipeline_tag`` and ``config.model_type``.
    """
    names = [s.get("rfilename", "") for s in raw.get("siblings") or []]
    root = {n for n in names if "/" not in n}
    if not any(n.lower().endswith(WEIGHT_SUFFIXES) for n in root):
        if any(n.lower().endswith(WEIGHT_SUFFIXES) for n in names):
            return None, "its weights are in subfolders; Wavescribe loads a repo's top level"
        return None, "no weight files"
    library = (raw.get("library_name") or "").lower()
    tags = {str(t).lower() for t in raw.get("tags") or []}
    if library not in ("mlx", "mlx-audio", "parakeet-mlx") and "mlx" not in tags:
        return None, "not an MLX model"
    family = ((raw.get("config") or {}).get("model_type") or "").lower()
    repo = str(raw.get("id", "")).lower()
    if capability == "asr":
        if "parakeet" in repo or "parakeet" in tags or library == "parakeet-mlx":
            if {"config.json", "model.safetensors"} <= root:
                return "parakeet-mlx", ""
            return None, "not in parakeet-mlx's layout (config.json + model.safetensors)"
        if family == "whisper" or (not family and "whisper" in repo):
            if "preprocessor_config.json" in root and root & {"tokenizer.json", "vocab.json"}:
                return "mlx-audio", ""
            return None, "an mlx-whisper conversion without the processor files mlx-audio needs"
        if family in MLX_AUDIO_FAMILIES:
            return "mlx-audio", ""
        if library == "mlx-audio":
            return "mlx-audio", f"a model family Wavescribe hasn't run ({family or 'unknown'})"
        return None, "no Wavescribe speech backend loads this model family"
    if capability in ("translate", "llm"):
        tag = raw.get("pipeline_tag")
        if tag in ("text-generation", "translation"):
            return "mlx-lm", ""
        if tag == "image-text-to-text" and family in MLX_LM_VLM_FAMILIES:
            return "mlx-lm", "a vision-language checkpoint: mlx-lm uses its text part"
        return None, "mlx-lm doesn't load this model as text-only"
    return None, f"no catalogue backend for {capability!r}"


# -- Hugging Face search (online) -------------------------------------------------------------

HF_URL = "https://huggingface.co"  # links stored in the registry always point here
USER_AGENT = f"Wavescribe/{__version__}"
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024  # a search answer is a few MB; more is not one
EXPAND = (
    "sha",
    "cardData",
    "config",
    "gated",
    "lastModified",
    "downloads",
    "likes",
    "siblings",
    "tags",
    "pipeline_tag",
    "library_name",
)
_REPO = re.compile(r"[\w.-]+/[\w.-]+")
_SHA = re.compile(r"[0-9a-f]{40}")
_LANGUAGE = re.compile(r"[a-z]{2,3}(-[a-z0-9]{2,8})*")


class CatalogError(RuntimeError):
    """The online search failed."""


@dataclass(frozen=True)
class CatalogModel:
    repo: str
    sha: str | None  # the commit to pin
    license: str | None  # Hugging Face licence id, e.g. "apache-2.0"
    license_url: str | None
    tier: str  # default / opt-in / restricted, or byo (never offered)
    downloads: int
    likes: int
    last_modified: str | None
    weights_gb: float | None
    gated: bool
    backend: str | None
    languages: tuple[str, ...]  # empty when the card doesn't say
    offered: bool
    reason: str = ""  # why it isn't offered
    upstream: str | None = None  # the original a conversion was made from
    caveats: tuple[str, ...] = ()
    pipeline_tag: str | None = None


def search_hf(
    query: str, capability: str, *, limit: int = 30, timeout: float = 15
) -> list[CatalogModel]:
    """Search Hugging Face for MLX models for a capability, most downloaded first. Online.

    One request per pipeline tag, then (in parallel) each result's file sizes at its commit and
    each conversion's upstream licence. ``timeout`` bounds the whole search; details that don't
    arrive in time are left out (size unknown, upstream licence unchecked).
    """
    tags = PIPELINE_TAGS.get(capability)
    if tags is None:
        raise ValueError(f"no catalogue for {capability!r}; try one of {', '.join(PIPELINE_TAGS)}")
    deadline = time.monotonic() + timeout
    found: dict[str, dict[str, Any]] = {}
    errors = []
    for tag in tags:
        params = [("search", query.strip())] if query.strip() else []
        params += [
            ("filter", "mlx"),
            ("pipeline_tag", tag),
            ("sort", "downloads"),
            ("direction", "-1"),
            ("limit", str(limit)),
        ]
        params += [("expand[]", e) for e in EXPAND]
        try:
            items = _get_json(
                f"{_endpoint()}/api/models?{urllib.parse.urlencode(params)}", _left(deadline)
            )
        except (OSError, ValueError) as e:  # URLError, HTTPError and timeouts are OSErrors
            errors.append(f"{tag}: {e}")
            continue
        for raw in items:
            found.setdefault(raw["id"], raw)
    if errors and len(errors) == len(tags):
        raise CatalogError(f"Hugging Face search failed ({'; '.join(errors)})")
    top = sorted(found.values(), key=lambda r: r.get("downloads") or 0, reverse=True)[:limit]
    revisions, bases = _details(top, deadline)
    return [
        _catalog_model(raw, capability, revisions.get(raw["id"]), bases.get(_base_model(raw) or ""))
        for raw in top
    ]


def model_at(repo: str, sha: str, capability: str, *, timeout: float = 15) -> CatalogModel:
    """One repo at a commit, read again from Hugging Face the way a search reads its results:
    the licence from the model card at that commit (or its upstream's), the backend from its
    files, the size from their listing. Adding a model from the page goes by this, never by what
    the page sends back about it. Online."""
    if not _REPO.fullmatch(repo) or not _SHA.fullmatch(sha):
        raise ValueError("expected a Hugging Face repo (org/name) and its 40-character commit")
    if capability not in PIPELINE_TAGS:
        raise ValueError(f"no catalogue for {capability!r}; try one of {', '.join(PIPELINE_TAGS)}")
    deadline = time.monotonic() + timeout
    # At a revision the API answers with the card, tags, config and files of that commit.
    raw = _get_json(f"{_endpoint()}/api/models/{_quote(repo)}/revision/{sha}?blobs=true",
                    _left(deadline))  # fmt: skip
    if not isinstance(raw, dict) or raw.get("sha", sha) != sha:
        raise CatalogError(f"Hugging Face didn't answer with {repo} at {sha[:12]}")
    raw = {**raw, "id": raw.get("id") or repo, "sha": sha}
    base = None
    if upstream := _base_model(raw):
        try:
            base = _get_json(_base_url(upstream), _left(deadline))
        except (OSError, ValueError) as e:  # then it is added with the caveat a search shows
            log.debug("Hugging Face card for %s failed: %s", upstream, e)
    return _catalog_model(raw, capability, raw, base)


def hf_weights_gb(repo: str, revision: str = "main", *, timeout: float = 15) -> float | None:
    """Weights size of a repo at a revision, from its file listing. Online."""
    url = f"{_endpoint()}/api/models/{_quote(repo)}/revision/{_quote(revision)}?blobs=true"
    return listing_size_gb(_get_json(url, timeout).get("siblings") or [])


def to_registry_entry(m: CatalogModel, capability: str) -> dict[str, Any]:
    """A ``models.toml`` / ``models.local.toml`` entry for an offered catalogue model. The tier
    comes from the licence here, never from ``m.tier``: the page sends search results back, and
    a tier it sends must not decide whether the licence gate applies."""
    tier = license_tier(m.license)
    if not m.offered or not m.sha or not m.license or not m.license_url or not m.backend:
        raise ValueError(f"{m.repo} isn't offered: {m.reason or 'missing details'}")
    if tier == "byo":
        raise ValueError(f"{m.repo} isn't offered: {_byo_reason(m.license)}")
    org, name = m.repo.split("/", 1)
    spdx = spdx_id(m.license)
    attribution = f"{m.upstream or m.repo} ({spdx})"
    if m.upstream:
        attribution += f"; conversion by {org}"
    return {
        "id": re.sub(r"[^a-z0-9.]+", "-", name.lower()).strip("-."),
        "capability": capability,
        "backend": m.backend,
        "source": f"hf:{m.repo}",
        "revision": m.sha,
        "upstream": f"{HF_URL}/{m.upstream or m.repo}",
        "license": spdx,
        "license_url": m.license_url,
        "tier": tier,
        "gated": m.gated,
        "languages": list(m.languages) or ["*"],
        "attribution": attribution + ".",
        "notes": "From the Hugging Face catalogue; untested.",
    }


def _endpoint() -> str:
    return os.environ.get("HF_ENDPOINT", HF_URL).rstrip("/")


def _quote(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def _left(deadline: float) -> float:
    return max(1.0, deadline - time.monotonic())


def _get_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout, context=trust_store()) as resp:
        body = resp.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ValueError(f"the answer is over {_MAX_RESPONSE_BYTES >> 20} MiB")
    return jsonsafe.loads(body)


def _details(
    top: list[dict[str, Any]], deadline: float
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """File sizes per result (at its commit) and the card of each upstream model, in parallel."""
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="hf-catalog")
    jobs = {}
    for raw in top:
        if raw.get("sha"):
            url = f"{_endpoint()}/api/models/{_quote(raw['id'])}/revision/{raw['sha']}?blobs=true"
            jobs[pool.submit(_get_json, url, _left(deadline))] = ("revision", raw["id"])
    for base in sorted({b for raw in top if (b := _base_model(raw))}):
        jobs[pool.submit(_get_json, _base_url(base), _left(deadline))] = ("base", base)
    done, _ = wait(jobs, timeout=max(0.0, deadline - time.monotonic()))
    pool.shutdown(wait=False, cancel_futures=True)
    revisions: dict[str, dict[str, Any]] = {}
    bases: dict[str, dict[str, Any]] = {}
    for future in done:
        kind, key = jobs[future]
        try:
            (revisions if kind == "revision" else bases)[key] = future.result()
        except (OSError, ValueError) as e:
            log.debug("Hugging Face details for %s failed: %s", key, e)
    return revisions, bases


def _base_url(base: str) -> str:
    """The card of the model a repo was converted from (its licence is the one that counts)."""
    expand = urllib.parse.urlencode([("expand[]", e) for e in ("cardData", "sha", "siblings")])
    return f"{_endpoint()}/api/models/{_quote(base)}?{expand}"


def _base_model(raw: Mapping[str, Any]) -> str | None:
    """The single model a repo was converted or fine-tuned from, per its card."""
    base = (raw.get("cardData") or {}).get("base_model")
    if isinstance(base, list):
        base = base[0] if len(base) == 1 else None
    if isinstance(base, str) and _REPO.fullmatch(base) and base != raw.get("id"):
        return base
    return None


def _card_license(raw: Mapping[str, Any]) -> str | None:
    card = raw.get("cardData") or {}
    lic = card.get("license")
    if isinstance(lic, list):
        lic = lic[0] if lic else None
    if not lic:
        tags = [str(t) for t in raw.get("tags") or []]
        lic = next((t.split(":", 1)[1] for t in tags if t.startswith("license:")), None)
    if not lic:
        return None
    lic = str(lic).strip().lower()
    if lic == "other" and card.get("license_name"):
        return str(card["license_name"]).strip().lower()
    return lic


def _license_url(raw: Mapping[str, Any], license_id: str) -> str | None:
    """The LICENSE file at the pinned commit, else the card's link, else the canonical text."""
    names = [s.get("rfilename", "") for s in raw.get("siblings") or []]
    exact = [n for n in names if n.upper() in ("LICENSE", "LICENSE.TXT", "LICENSE.MD", "LICENCE")]
    weights = [
        n
        for n in names
        if "/" not in n
        and n.upper().startswith(("LICENSE", "LICENCE"))
        and ("WEIGHT" in n.upper() or "MODEL" in n.upper())
    ]
    if (files := exact or weights) and raw.get("sha"):
        return f"{HF_URL}/{raw['id']}/blob/{raw['sha']}/{_quote(files[0])}"
    link = str((raw.get("cardData") or {}).get("license_link") or "")
    if link.startswith("https://"):
        return link
    return _canonical_url(license_id)


def _languages(raw: Mapping[str, Any]) -> tuple[str, ...]:
    value = (raw.get("cardData") or {}).get("language") or []
    tags = [value] if isinstance(value, str) else value
    return tuple(t.lower() for t in tags if isinstance(t, str) and _LANGUAGE.fullmatch(t.lower()))


def _catalog_model(
    raw: dict[str, Any],
    capability: str,
    revision: Mapping[str, Any] | None = None,
    base: Mapping[str, Any] | None = None,
) -> CatalogModel:
    """One search result, with its file listing at the commit and its upstream's card if known."""
    caveats = []
    upstream = _base_model(raw)
    own = _card_license(raw)
    license_id, license_source = own, raw
    if upstream and base is None:
        caveats.append(f"couldn't check the licence of its upstream, {upstream}")
    elif upstream and base is not None:
        # Conversions inherit the upstream licence; a converter's card can be stale, and it
        # can't grant what the upstream never licensed.
        up = _card_license(base)
        if up != own:
            caveats.append(
                f"its card says {own or 'no licence'}, the upstream {upstream} says {up or 'none'}"
            )
        license_id, license_source = up, base
    tier = license_tier(license_id)
    backend, note = infer_backend(raw, capability)
    reasons = []
    if tier == "byo":
        unlicensed = upstream and base is not None and not license_id
        reasons.append(
            f"its upstream, {upstream}, has no licence on its card"
            if unlicensed
            else _byo_reason(license_id)
        )
    if backend is None:
        reasons.append(note)
    elif tier == "byo":
        reasons.append("you can still add it to config/models.local.toml yourself")
    if backend and note:
        caveats.append(note)
    if not raw.get("sha"):
        reasons.append("no commit to pin")
    url = _license_url(license_source, license_id) if license_id else None
    if tier != "byo" and not url:
        reasons.append("no licence text to link")
    return CatalogModel(
        repo=raw["id"],
        sha=raw.get("sha"),
        license=license_id,
        license_url=url,
        tier=tier,
        downloads=int(raw.get("downloads") or 0),
        likes=int(raw.get("likes") or 0),
        last_modified=raw.get("lastModified"),
        weights_gb=listing_size_gb(revision.get("siblings") or []) if revision else None,
        gated=bool(raw.get("gated")),
        backend=backend,
        languages=_languages(raw) or (_languages(base) if base else ()),
        offered=not reasons,
        reason="; ".join(reasons),
        upstream=upstream,
        caveats=tuple(caveats),
        pipeline_tag=raw.get("pipeline_tag"),
    )


# -- Recommendations ------------------------------------------------------------------------


def recommend(
    capability: str,
    language: str,
    entries: list[ModelEntry],
    m: Machine,
    sizes: Mapping[str, float | None],
    *,
    other_loaded_gb: float = 0.0,
) -> list[tuple[str, str]]:
    """The models to use for ``language``, best first, each with the reason.

    ``entries`` come in the registry chain's order (``registry.chain(capability)``). A model too
    big for this machine is dropped, and the reason goes on the model that takes its place; a
    tight fit is flagged. ``sizes`` are weights in GB (``installed_sizes``), None if unknown.
    """
    picks: list[tuple[str, str]] = []
    dropped: list[str] = []
    for entry in entries:
        if entry.capability != capability or not entry.supports(language):
            continue
        f = fit(
            capability,
            sizes.get(entry.id),
            m,
            other_loaded_gb=other_loaded_gb,
            backend=entry.backend,
        )
        if f.level == "too_big":
            dropped.append(f"{entry.id} ({f.reason})")
            continue
        why = [f"{'first choice' if not picks else 'fallback'} for {language}"]
        if dropped:
            why.append(f"instead of {', '.join(dropped)}")
            dropped = []
        why.append(f.reason)
        if entry.tier in ("opt-in", "restricted"):
            why.append(f"{entry.tier} licence {entry.license}: needs your acceptance")
        if language.lower().split("-")[0] in (t.lower() for t in entry.unsafe_targets):
            why.append(f"invented content into {language} when tested: never used for alerts")
        picks.append((entry.id, "; ".join(why)))
    for reason in dropped:
        log.info("no %s model after %s", capability, reason)
    return picks
