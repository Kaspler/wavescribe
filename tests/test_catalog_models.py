# SPDX-License-Identifier: Apache-2.0
"""The models catalogue. No network: Hugging Face answers come from synthetic fixtures shaped
like the real API's (checked against https://huggingface.co/api/models on 2026-11-26)."""

from __future__ import annotations

import dataclasses
import io
import json
import platform
import subprocess
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

import wavescribe
from wavescribe.catalog import models as catalog
from wavescribe.catalog.models import (
    CatalogError,
    CatalogModel,
    Machine,
    fit,
    infer_backend,
    installed_size_gb,
    license_tier,
    recommend,
    search_hf,
    speed_hint,
    to_registry_entry,
)
from wavescribe.registry import Licenses, ModelEntry, ModelRegistry, _parse_entry

GIB = 2**30
M1_MAX_64 = Machine("Apple M1 Max", 64.0, 32, 8, 2, "27.0.1", 400, 55662788608 / GIB)
M1_16 = Machine("Apple M1", 16.0, 8, 4, 4, "27.0.1", 68)

# -- Fixtures: Hugging Face API answers --------------------------------------------------------


def sha(c: str) -> str:
    return c * 40


def files(*names: str) -> list[dict]:
    return [{"rfilename": n} for n in names]


def model(repo: str, downloads: int, card: dict, *names: str, **extra) -> dict:
    """One item of GET /api/models with the expand[] fields search_hf asks for."""
    lic = card.get("license")
    return {
        "_id": f"id-{downloads}",
        "id": repo,
        "cardData": card,
        "gated": False,
        "lastModified": "2026-10-01T12:00:00.000Z",
        "likes": downloads // 100,
        "sha": sha(repo[-1]),
        "downloads": downloads,
        "tags": ["mlx", "region:us"] + ([f"license:{lic}"] if lic else []),
        "pipeline_tag": card.get("pipeline_tag", "automatic-speech-recognition"),
        "library_name": card.get("library_name", "mlx"),
        "siblings": files(".gitattributes", "README.md", *names),
        **extra,
    }


ASR = "automatic-speech-recognition"
LANGS = ["en", "pt", "uk", "de"]
ASR_RESULTS = [
    model(
        "example-org/parakeet-tdt-0.6b-v3-mlx-a",
        9000,
        {"license": "cc-by-4.0", "base_model": "example-up/parakeet-tdt-0.6b-v3",
         "language": LANGS},
        "config.json", "model.safetensors", "tokenizer.model", "vocab.txt",
    ),
    # mlx-whisper layout, no licence on the card
    model("example-org/whisper-small-mlx-b", 8000, {}, "config.json", "weights.npz", config={}),
    # mlx-audio Whisper: the converter says Apache-2.0, the upstream says MIT
    model(
        "example-org/whisper-large-v3-turbo-asr-fp16-c",
        7000,
        {"license": "apache-2.0", "base_model": ["example-up/whisper-large-v3-turbo"],
         "library_name": "mlx-audio", "language": LANGS},
        "config.json", "model.safetensors", "preprocessor_config.json", "tokenizer.json",
        config={"model_type": "whisper"},
    ),
    # A stale licence on the conversion, upstream relicensed to OpenMDW; gated
    model(
        "example-org/nemotron-asr-streaming-d",
        6000,
        {"license": "other", "license_name": "nvidia-open-model-license",
         "base_model": "example-up/nemotron-asr-streaming", "library_name": "mlx-audio"},
        "config.json", "model.safetensors", "tokenizer.model",
        config={"model_type": "nemotron_asr"}, gated="manual",
    ),
    # Non-commercial, a family our transcriber hasn't run
    model(
        "example-org/mms-asr-e",
        5000,
        {"license": "cc-by-nc-4.0", "library_name": "mlx-audio"},
        "config.json", "model.safetensors",
        config={"model_type": "mms"},
    ),
    # Several quantisations in subfolders
    model(
        "example-org/qwen3-asr-variants-f",
        4000,
        {"license": "apache-2.0"},
        "MLX/4bit/config.json", "MLX/4bit/model.safetensors",
        config={"model_type": "qwen3_asr"},
    ),
]  # fmt: skip

REVISIONS = {  # GET /api/models/<repo>/revision/<sha>?blobs=true -> siblings with sizes
    sha("a"): [{"rfilename": "config.json", "size": 900},
               {"rfilename": "model.safetensors", "size": 2_508_208_664}],
    sha("b"): [{"rfilename": "weights.npz", "size": 483_000_000}],
    sha("c"): [{"rfilename": "model.safetensors", "size": 1_617_859_712},
               {"rfilename": "model.safetensors.index.json", "size": 30_000}],
    sha("d"): [{"rfilename": "model.safetensors", "size": 1_276_000_000}],
    sha("e"): [{"rfilename": "model.safetensors", "size": 1_000_000_000}],
    sha("f"): [{"rfilename": "MLX/4bit/model.safetensors", "size": 1_200_000_000}],
}  # fmt: skip

BASES = {  # GET /api/models/<base>?expand[]=cardData&expand[]=sha&expand[]=siblings
    "example-up/parakeet-tdt-0.6b-v3": {
        "id": "example-up/parakeet-tdt-0.6b-v3",
        "sha": sha("1"),
        "cardData": {"license": "cc-by-4.0", "language": LANGS},
        "siblings": files("README.md", "model.nemo"),
    },
    "example-up/whisper-large-v3-turbo": {
        "id": "example-up/whisper-large-v3-turbo",
        "sha": sha("2"),
        "cardData": {"license": "mit"},
        "siblings": files("LICENSE", "README.md"),
    },
    "example-up/nemotron-asr-streaming": {
        "id": "example-up/nemotron-asr-streaming",
        "sha": sha("3"),
        "cardData": {
            "license": "other",
            "license_name": "openmdw-1.1",
            "license_link": "https://openmdw.ai/license/1-1/",
            "language": ["en", "es", "pt"],
        },
        "siblings": files("README.md"),
    },
}

TRANSLATE_RESULTS = {
    "text-generation": [
        model("example-org/Llama-3.2-3B-Instruct-4bit-g", 3000,
              {"license": "llama3.2", "pipeline_tag": "text-generation"},
              "config.json", "model.safetensors", "tokenizer.json"),
        model("example-org/custom-terms-lm-h", 2000,
              {"license": "other", "pipeline_tag": "text-generation"},
              "config.json", "model.safetensors"),
    ],
    "translation": [
        model("example-org/plamo-translate-mlx-i", 500,
              {"license": "other", "license_name": "plamo-community-license",
               "pipeline_tag": "translation"},
              "config.json", "model.safetensors"),
    ],
    "image-text-to-text": [
        model("example-org/Qwen3.5-9B-MLX-4bit-j", 10000,
              {"license": "apache-2.0", "pipeline_tag": "image-text-to-text"},
              "config.json", "model.safetensors", "tokenizer.json",
              config={"model_type": "qwen3_5"}),
        model("example-org/llava-mlx-k", 2500,
              {"license": "apache-2.0", "pipeline_tag": "image-text-to-text"},
              "config.json", "model.safetensors", config={"model_type": "llava"}),
    ],
}  # fmt: skip


class FakeHub:
    """Stands in for urllib.request.urlopen and serves the fixtures by URL."""

    def __init__(self, searches: dict, *, fail: tuple[str, ...] = ()) -> None:
        self.searches = searches
        self.fail = fail
        self.requests: list = []

    def __call__(self, request, timeout: float):
        self.requests.append(request)
        url = urllib.parse.urlsplit(request.full_url)
        path = urllib.parse.unquote(url.path).removeprefix("/api/models")
        if any(f in request.full_url for f in self.fail):
            raise urllib.error.URLError("offline")
        if not path:
            body = self.searches.get(urllib.parse.parse_qs(url.query)["pipeline_tag"][0], [])
        elif "/revision/" in path:
            repo, rev = path.strip("/").split("/revision/")
            body = self.at_revision(repo, rev)
        elif path.strip("/") in BASES:
            body = BASES[path.strip("/")]
        else:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", None, None)
        return io.BytesIO(json.dumps(body).encode())

    def at_revision(self, repo: str, rev: str) -> dict:
        """GET /api/models/<repo>/revision/<sha>?blobs=true: the repo's card, tags and config
        at that commit, and its files with their sizes."""
        listed = next((m for ms in self.searches.values() for m in ms if m["id"] == repo), {})
        sizes = {f["rfilename"]: f["size"] for f in REVISIONS.get(rev, [])}
        names = [f["rfilename"] for f in listed.get("siblings", [])]
        siblings = [{"rfilename": n} | ({"size": sizes[n]} if n in sizes else {}) for n in names]
        siblings += [f for f in REVISIONS.get(rev, []) if f["rfilename"] not in names]
        return {**listed, "id": repo, "sha": rev, "siblings": siblings}


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> FakeHub:
    fake = FakeHub({ASR: ASR_RESULTS, **TRANSLATE_RESULTS})
    monkeypatch.setattr(catalog.urllib.request, "urlopen", fake)
    return fake


# -- Licences ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("license_id", "tier"),
    [
        ("apache-2.0", "default"),
        ("MIT", "default"),
        ("bsd-3-clause", "default"),
        ("cc-by-4.0", "default"),
        ("cc0-1.0", "default"),
        ("openmdw-1.1", "default"),
        ("gemma", "opt-in"),
        ("llama3.1", "opt-in"),
        ("nvidia-open-model-license", "opt-in"),
        ("cc-by-nc-4.0", "restricted"),
        ("cc-by-nc-sa-4.0", "restricted"),
        ("cc-by-nc-nd-3.0", "restricted"),
        ("acme-non-commercial-license", "restricted"),
        ("llama4", "byo"),  # excludes the EU for multimodal models
        (None, "byo"),
        ("", "byo"),
        ("other", "byo"),
        ("unknown", "byo"),
        ("plamo-community-license", "byo"),
    ],
)
def test_licence_tiers(license_id: str | None, tier: str) -> None:
    assert license_tier(license_id) == tier


def test_every_listed_licence_has_a_valid_tier_and_an_https_text() -> None:
    for tier, spdx, url in catalog.LICENSES.values():
        assert tier in ("default", "opt-in", "restricted", "byo")
        assert spdx and url.startswith("https://")


# -- Backends ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("index", "backend"),
    [(0, "parakeet-mlx"), (1, None), (2, "mlx-audio"), (3, "mlx-audio"), (4, "mlx-audio"),
     (5, None)],
)  # fmt: skip
def test_asr_backend_inference(index: int, backend: str | None) -> None:
    assert infer_backend(ASR_RESULTS[index], "asr")[0] == backend


def test_backend_inference_explains_itself() -> None:
    assert "processor files" in infer_backend(ASR_RESULTS[1], "asr")[1]
    assert "subfolders" in infer_backend(ASR_RESULTS[5], "asr")[1]
    assert "hasn't run (mms)" in infer_backend(ASR_RESULTS[4], "asr")[1]
    gguf = model("example-org/x-gguf", 1, {"license": "mit"}, "x.gguf", library_name="gguf")
    gguf["tags"] = ["gguf"]
    assert infer_backend(gguf, "asr") == (None, "not an MLX model")


def test_llm_backend_inference() -> None:
    by_repo = {m["id"]: m for results in TRANSLATE_RESULTS.values() for m in results}
    assert infer_backend(by_repo["example-org/Llama-3.2-3B-Instruct-4bit-g"], "translate")[0] == (
        "mlx-lm"
    )
    assert infer_backend(by_repo["example-org/Qwen3.5-9B-MLX-4bit-j"], "translate")[0] == "mlx-lm"
    assert infer_backend(by_repo["example-org/llava-mlx-k"], "translate")[0] is None
    assert infer_backend(ASR_RESULTS[0], "translate")[0] is None  # an ASR model isn't an LLM


# -- Hugging Face search ----------------------------------------------------------------------


def test_search_hf_asks_the_public_api_for_mlx_models(hub: FakeHub) -> None:
    search_hf(" parakeet ", "asr", limit=5)
    first = hub.requests[0]
    url = urllib.parse.urlsplit(first.full_url)
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://huggingface.co/api/models"
    query = urllib.parse.parse_qs(url.query)
    assert query["search"] == ["parakeet"]
    assert query["filter"] == ["mlx"]
    assert query["pipeline_tag"] == [ASR]
    assert query["sort"] == ["downloads"] and query["direction"] == ["-1"]
    assert query["limit"] == ["5"]
    assert {"sha", "cardData", "config", "siblings", "gated", "lastModified"} <= set(
        query["expand[]"]
    )
    # A generic client name and nothing else about the user.
    for request in hub.requests:
        assert request.get_header("User-agent") == f"Wavescribe/{wavescribe.__version__}"
        assert set(request.headers) == {"User-agent", "Accept"}


def test_search_hf_classifies_each_result(hub: FakeHub) -> None:
    found = {m.repo.rsplit("-", 1)[1]: m for m in search_hf("", "asr")}
    assert [m.downloads for m in found.values()] == sorted(
        (m.downloads for m in found.values()), reverse=True
    )

    parakeet = found["a"]
    assert parakeet.offered and parakeet.reason == ""
    assert (parakeet.tier, parakeet.backend) == ("default", "parakeet-mlx")
    assert parakeet.license == "cc-by-4.0"
    assert parakeet.sha == sha("a")
    assert parakeet.license_url == "https://creativecommons.org/licenses/by/4.0/"
    assert parakeet.weights_gb == pytest.approx(2_508_208_664 / GIB)
    assert parakeet.languages == tuple(LANGS)
    assert parakeet.upstream == "example-up/parakeet-tdt-0.6b-v3" and parakeet.caveats == ()

    whisper_mlx = found["b"]
    assert not whisper_mlx.offered and whisper_mlx.tier == "byo"
    assert "no licence on the model card" in whisper_mlx.reason
    assert "processor files" in whisper_mlx.reason
    assert "models.local.toml" not in whisper_mlx.reason  # no backend could load it anyway
    assert whisper_mlx.weights_gb == pytest.approx(483_000_000 / GIB)

    whisper = found["c"]  # the upstream's licence wins, and its LICENSE file at the commit
    assert whisper.offered and whisper.license == "mit" and whisper.backend == "mlx-audio"
    assert whisper.license_url == (
        f"https://huggingface.co/example-up/whisper-large-v3-turbo/blob/{sha('2')}/LICENSE"
    )
    assert any("apache-2.0" in c and "mit" in c for c in whisper.caveats)

    nemotron = found["d"]
    assert nemotron.offered and nemotron.gated
    assert (nemotron.license, nemotron.tier) == ("openmdw-1.1", "default")
    assert nemotron.license_url == "https://openmdw.ai/license/1-1/"
    assert nemotron.languages == ("en", "es", "pt")  # from the upstream card

    mms = found["e"]
    assert mms.offered and mms.tier == "restricted"
    assert mms.license_url == "https://creativecommons.org/licenses/by-nc/4.0/"
    assert any("hasn't run" in c for c in mms.caveats)

    variants = found["f"]
    assert not variants.offered and "subfolders" in variants.reason


def test_search_hf_merges_pipeline_tags_for_translation(hub: FakeHub) -> None:
    found = search_hf("", "translate", limit=4)
    assert [m.repo for m in found] == [
        "example-org/Qwen3.5-9B-MLX-4bit-j",
        "example-org/Llama-3.2-3B-Instruct-4bit-g",
        "example-org/llava-mlx-k",
        "example-org/custom-terms-lm-h",
    ]
    queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(r.full_url).query) for r in hub.requests]
    asked = {q["pipeline_tag"][0] for q in queries if "pipeline_tag" in q}
    assert {"text-generation", "translation", "image-text-to-text"} <= asked
    qwen, llama, llava, custom = found
    assert qwen.offered and qwen.backend == "mlx-lm" and qwen.tier == "default"
    assert llama.offered and llama.tier == "opt-in"
    assert not llava.offered and "text-only" in llava.reason
    assert not custom.offered and "'other'" in custom.reason
    assert custom.backend == "mlx-lm" and "config/models.local.toml" in custom.reason


def test_search_hf_without_details_still_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeHub({ASR: ASR_RESULTS}, fail=("/revision/", "example-up/"))
    monkeypatch.setattr(catalog.urllib.request, "urlopen", fake)
    whisper = next(m for m in search_hf("", "asr") if m.repo.endswith("-c"))
    assert whisper.weights_gb is None
    assert whisper.license == "apache-2.0"  # its own card, flagged
    assert any("couldn't check" in c for c in whisper.caveats)


def test_search_hf_offline_raises_a_catalog_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog.urllib.request, "urlopen", FakeHub({}, fail=("/api/",)))
    with pytest.raises(CatalogError, match="Hugging Face search failed"):
        search_hf("whisper", "asr")
    with pytest.raises(ValueError, match="no catalogue"):
        search_hf("x", "vad")


def test_importing_the_catalogue_touches_no_network() -> None:
    code = (
        "import socket\n"
        "def deny(*a, **k): raise SystemExit('network used at import')\n"
        "socket.socket.connect = deny\n"
        "socket.create_connection = deny\n"
        "import wavescribe.catalog.models\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# -- Registry entries -------------------------------------------------------------------------


def test_to_registry_entry_passes_registry_validation(hub: FakeHub) -> None:
    found = {m.repo: m for m in search_hf("", "asr")}
    whisper = found["example-org/whisper-large-v3-turbo-asr-fp16-c"]
    raw = to_registry_entry(whisper, "asr")
    entry = _parse_entry(raw)
    assert entry.id == "whisper-large-v3-turbo-asr-fp16-c"
    assert (entry.source, entry.revision) == (f"hf:{whisper.repo}", whisper.sha)
    assert (entry.license, entry.tier, entry.backend) == ("MIT", "default", "mlx-audio")
    assert entry.upstream == "https://huggingface.co/example-up/whisper-large-v3-turbo"
    assert entry.supports("pt-PT") and not entry.supports("ja")
    assert entry.notes == "From the Hugging Face catalogue; untested."
    # TOML-ready: only strings, booleans and lists of strings.
    for value in raw.values():
        assert isinstance(value, str | bool) or all(isinstance(v, str) for v in value)

    nemotron = _parse_entry(to_registry_entry(found["example-org/nemotron-asr-streaming-d"], "asr"))
    assert nemotron.license == "LicenseRef-OpenMDW-1.1" and nemotron.gated
    mms = _parse_entry(to_registry_entry(found["example-org/mms-asr-e"], "asr"))
    assert (mms.tier, mms.license, mms.languages) == ("restricted", "CC-BY-NC-4.0", ("*",))
    with pytest.raises(ValueError, match="isn't offered"):
        to_registry_entry(found["example-org/whisper-small-mlx-b"], "asr")


def test_adding_a_model_reads_its_licence_again_at_its_commit(
    hub: FakeHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses
    import tomllib
    from types import SimpleNamespace

    from wavescribe import admin

    monkeypatch.setattr(admin, "CATALOG_MODELS_PATH", tmp_path / "models.catalog.toml")
    registry = ModelRegistry([], {}, tmp_path, Licenses(tmp_path / "accepted.json"))
    app = SimpleNamespace(registry=registry)
    # A search result as the control centre gets it back, with a licence the model doesn't have.
    found = {m.repo: m for m in search_hf("", "asr")}["example-org/mms-asr-e"]
    claimed = dataclasses.asdict(found) | {
        "license": "apache-2.0", "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
        "tier": "default", "languages": ["pt"],
    }  # fmt: skip
    hub.requests.clear()
    added = admin.add_catalog_model(app, claimed, "asr")
    entry = registry.get(added["id"])
    assert (added["tier"], entry.license) == ("restricted", "CC-BY-NC-4.0")
    assert entry.revision == sha("e")
    assert entry.languages == ("*",)  # what the card says, not what was sent
    assert any(f"/revision/{sha('e')}" in r.full_url for r in hub.requests)
    saved = tomllib.loads(admin.CATALOG_MODELS_PATH.read_text())["model"]
    assert [(m["tier"], m["license"]) for m in saved] == [("restricted", "CC-BY-NC-4.0")]

    # A conversion takes its upstream's licence, as in a search.
    nemotron = {**claimed, "repo": "example-org/nemotron-asr-streaming-d", "sha": sha("d")}
    added = admin.add_catalog_model(app, nemotron, "asr")
    assert registry.get(added["id"]).license == "LicenseRef-OpenMDW-1.1"
    # A card with no licence isn't offered, whatever the request says.
    unlicensed = {**claimed, "repo": "example-org/whisper-small-mlx-b", "sha": sha("b")}
    with pytest.raises(admin.AdminError, match="isn't offered"):
        admin.add_catalog_model(app, unlicensed, "asr")
    # Only a repo at a full commit is read again: no branch, no stand-in.
    with pytest.raises(admin.AdminError, match="40-character commit"):
        admin.add_catalog_model(app, {**claimed, "sha": "main"}, "asr")
    hub.fail = ("/revision/",)
    with pytest.raises(admin.AdminError, match="couldn't read"):
        admin.add_catalog_model(app, {**claimed, "repo": "example-org/other-e"}, "asr")


def test_registry_entry_for_an_opt_in_model_needs_acceptance(tmp_path: Path) -> None:
    llama = CatalogModel(
        repo="example-org/Llama-3.2-3B-Instruct-4bit", sha=sha("9"), license="llama3.2",
        license_url="https://www.llama.com/llama3_2/license/", tier="opt-in", downloads=1,
        likes=0, last_modified=None, weights_gb=1.8, gated=False, backend="mlx-lm",
        languages=("en", "de"), offered=True,
    )  # fmt: skip
    entry = _parse_entry(to_registry_entry(llama, "translate"))
    registry = ModelRegistry([entry], {}, tmp_path, Licenses(tmp_path / "accepted.json"))
    assert not registry.licenses.accepted(entry)
    assert entry.license == "LicenseRef-Llama3.2"


def test_a_conversion_of_an_unlicensed_upstream_is_not_offered() -> None:
    # The converter's card says MIT, but it can't license what the upstream never did.
    raw = model(
        "example-org/whisper-tiny-mlx-z", 100,
        {"license": "mit", "base_model": "example-up/unlicensed", "library_name": "mlx-audio"},
        "config.json", "model.safetensors", "preprocessor_config.json", "tokenizer.json",
        config={"model_type": "whisper"},
    )  # fmt: skip
    base = {"id": "example-up/unlicensed", "sha": sha("4"), "cardData": {},
            "siblings": files("README.md")}  # fmt: skip
    m = catalog._catalog_model(raw, "asr", None, base)
    assert not m.offered and (m.tier, m.license) == ("byo", None)
    assert "example-up/unlicensed, has no licence" in m.reason
    assert any("the upstream example-up/unlicensed says none" in c for c in m.caveats)


def test_a_registry_entry_takes_its_tier_from_the_licence_not_the_caller() -> None:
    # The page sends a search result back to be added; its tier and "offered" are its say-so.
    claimed = CatalogModel(
        repo="example-org/mms-asr", sha=sha("8"), license="cc-by-nc-4.0",
        license_url="https://creativecommons.org/licenses/by-nc/4.0/", tier="default",
        downloads=1, likes=0, last_modified=None, weights_gb=1.0, gated=False,
        backend="mlx-audio", languages=(), offered=True,
    )  # fmt: skip
    assert _parse_entry(to_registry_entry(claimed, "asr")).tier == "restricted"
    with pytest.raises(ValueError, match="isn't offered: its licence is 'other'"):
        to_registry_entry(dataclasses.replace(claimed, license="other"), "asr")


# -- Fit and speed ----------------------------------------------------------------------------


def test_budget_is_memory_minus_reserve_within_the_gpu_limit() -> None:
    assert M1_MAX_64.gpu_limit_gb == pytest.approx(51.84, abs=0.01)  # measured on an M1 Max
    assert M1_MAX_64.budget_gb == pytest.approx(51.84, abs=0.01)
    assert M1_16.gpu_limit_gb == pytest.approx(10.67, abs=0.01)  # estimated: 2/3
    assert M1_16.budget_gb == pytest.approx(10.0)  # 16 - 6
    unmeasured_64 = Machine("Apple M1 Max", 64.0, None, 8, 2, None, 400)
    assert unmeasured_64.budget_gb == pytest.approx(48.0)  # 3/4 above 36 GB


def test_llm_need_includes_a_kv_cache() -> None:
    qwen = 5.54  # Qwen3.5-9B 4-bit
    assert catalog.kv_cache_gb(qwen) == pytest.approx(1.32, abs=0.01)
    assert catalog.need_gb("translate", qwen) == pytest.approx(qwen * 1.1 + 1.32, abs=0.01)
    # MADLAD runs on CTranslate2: no KV cache estimate.
    assert catalog.need_gb("translate", 2.75, backend="ctranslate2-madlad") == pytest.approx(3.575)


def test_fit_on_a_64_and_a_16_gb_mac() -> None:
    qwen, parakeet = 5.54, 2.34
    assert fit("translate", qwen, M1_MAX_64).level == "good"
    tight = fit("translate", qwen, M1_16)
    assert tight.level == "tight" and tight.need_gb == pytest.approx(7.41, abs=0.01)
    assert "KV cache" in tight.reason and "10.0 GB usable" in tight.reason
    beside = fit("translate", qwen, M1_16, other_loaded_gb=parakeet * 1.2)
    assert beside.level == "too_big" and "next to the other models" in beside.reason
    assert fit("asr", parakeet, M1_16).level == "good"
    assert fit("asr", 8.26, M1_16).level == "tight"  # Voxtral fp16: 9.9 of 10 GB
    assert fit("asr", 20.0, M1_16).level == "too_big"
    assert fit("asr", 20.0, M1_MAX_64).level == "good"
    unknown = fit("asr", None, M1_16)
    assert unknown.level == "unknown" and unknown.need_gb is None


def test_speed_hint_is_a_bandwidth_bound_for_llms_only() -> None:
    hint = speed_hint("translate", 5.54, M1_MAX_64)
    assert hint is not None and hint.startswith("at most ~67 tokens/s")
    assert "~11 tokens/s" in (speed_hint("translate", 5.54, M1_16) or "")
    assert speed_hint("asr", 2.34, M1_MAX_64) is None
    assert speed_hint("translate", 2.75, M1_MAX_64, backend="ctranslate2-madlad") is None
    assert speed_hint("translate", 5.54, Machine("Apple M9", 32.0, None, 4, 4, None, None)) is None


@pytest.mark.parametrize(
    ("chip", "cores", "gbs"),
    [("Apple M1", 8, 68), ("Apple M1 Max", 10, 400), ("Apple M3 Max", 14, 300),
     ("Apple M3 Max", 16, 400), ("Apple M4 Max", 14, 410), ("Apple M4 Max", 16, 546),
     ("Apple M4 Pro", 12, 273), ("Apple M9", 8, None)],
)  # fmt: skip
def test_bandwidth_table(chip: str, cores: int, gbs: float | None) -> None:
    assert catalog.chip_bandwidth_gbs(chip, cores) == gbs


# -- Sizes ------------------------------------------------------------------------------------


def test_installed_size_counts_weight_files_once(tmp_path: Path) -> None:
    blobs, snap = tmp_path / "blobs", tmp_path / "snapshots" / "abc"
    blobs.mkdir()
    snap.mkdir(parents=True)
    (blobs / "w").write_bytes(b"x" * 3000)
    (snap / "model.safetensors").symlink_to(blobs / "w")
    (snap / "copy.safetensors").symlink_to(blobs / "w")  # same blob: counted once
    (snap / "weights.npz").write_bytes(b"x" * 500)  # the same weights in another format
    (snap / "config.json").write_bytes(b"{}" * 100)
    assert installed_size_gb(snap) == pytest.approx(3000 / GIB)
    ct2 = tmp_path / "ct2"
    ct2.mkdir()
    (ct2 / "model.bin").write_bytes(b"x" * 700)
    (ct2 / "spiece.model").write_bytes(b"x" * 50)
    assert installed_size_gb(ct2) == pytest.approx(700 / GIB)
    onnx = tmp_path / "silero_vad.onnx"
    onnx.write_bytes(b"x" * 100)
    assert installed_size_gb(onnx) == pytest.approx(100 / GIB)


def test_listing_size_skips_files_without_sizes() -> None:
    assert catalog.listing_size_gb(REVISIONS[sha("c")]) == pytest.approx(1_617_859_712 / GIB)
    assert catalog.listing_size_gb(files("model.safetensors")) is None


# -- Recommendations --------------------------------------------------------------------------


def entry(mid: str, languages: tuple[str, ...], *, capability: str = "asr",
          backend: str = "mlx-audio", **kw) -> ModelEntry:  # fmt: skip
    return ModelEntry(
        id=mid, capability=capability, backend=backend, source=f"hf:example-org/{mid}",
        upstream="u", license=kw.pop("license", "MIT"), license_url="https://x",
        tier=kw.pop("tier", "default"), gated=False, languages=languages, attribution="",
        revision="r", **kw,
    )  # fmt: skip


def test_a_16_gb_mac_skips_a_model_that_wont_fit() -> None:
    chain = [entry("big", ("*",)), entry("parakeet", ("pt", "uk")), entry("whisper", ("*",))]
    sizes = {"big": 12.0, "parakeet": 2.34, "whisper": 1.5}
    small = recommend("asr", "pt-PT", chain, M1_16, sizes)
    assert [mid for mid, _ in small] == ["parakeet", "whisper"]
    assert small[0][1].startswith("first choice for pt-PT; instead of big (too big")
    assert small[1][1].startswith("fallback for pt-PT; fits")
    large = recommend("asr", "pt-PT", chain, M1_MAX_64, sizes)
    assert [mid for mid, _ in large] == ["big", "parakeet", "whisper"]
    # Language support filters first: Parakeet doesn't do Hebrew.
    assert [mid for mid, _ in recommend("asr", "he", chain, M1_16, sizes)] == ["whisper"]


def test_recommend_flags_tight_unknown_licence_and_unsafe_targets() -> None:
    chain = [
        entry("qwen", ("*",), capability="translate", backend="mlx-lm"),
        entry("gemma", ("*",), capability="translate", backend="mlx-lm", tier="opt-in",
              license="LicenseRef-Gemma"),
        entry("madlad", ("*",), capability="translate", backend="ctranslate2-madlad",
              unsafe_targets=("uk",)),
    ]  # fmt: skip
    picks = dict(recommend("translate", "uk", chain, M1_16, {"qwen": 5.54, "madlad": 2.75}))
    assert picks["qwen"].startswith("first choice for uk; tight:")
    assert "size unknown" in picks["gemma"] and "needs your acceptance" in picks["gemma"]
    assert "never used for alerts" in picks["madlad"]
    # Next to Parakeet on 16 GB, Qwen3.5-9B no longer fits by this estimate.
    beside = recommend("translate", "en", chain, M1_16, {"qwen": 5.54, "madlad": 2.75},
                       other_loaded_gb=2.34 * 1.2)  # fmt: skip
    assert [mid for mid, _ in beside] == ["gemma", "madlad"]


def test_recommend_follows_the_packaged_asr_chain() -> None:
    registry = ModelRegistry.load(local=None)
    sizes = {"parakeet-tdt-0.6b-v3": 2.34, "whisper-large-v3-turbo": 1.5}
    picks = recommend("asr", "pt-PT", registry.chain("asr"), M1_16, sizes)
    assert [mid for mid, _ in picks] == ["parakeet-tdt-0.6b-v3", "whisper-large-v3-turbo"]
    assert [mid for mid, _ in recommend("asr", "he", registry.chain("asr"), M1_16, sizes)] == [
        "whisper-large-v3-turbo"
    ]


# -- This machine -----------------------------------------------------------------------------


ON_A_MAC = pytest.mark.skipif(sys.platform != "darwin", reason="reads macOS sysctl")
# A virtual Mac, as CI runs on, reports no efficiency cores and has little memory and no GPU of
# its own: what a real one reports is checked on a real one, and CI lists it as skipped.
VIRTUAL = catalog._sysctl("kern.hv_vmm_present").get("kern.hv_vmm_present") == "1"


@ON_A_MAC
def test_machine_on_this_mac() -> None:
    m = catalog.machine()
    assert m is catalog.machine()  # cached
    assert m.chip and m.memory_gb > 0 and m.macos


@ON_A_MAC
@pytest.mark.skipif(platform.machine() != "arm64", reason="Apple silicon only")
@pytest.mark.skipif(VIRTUAL, reason="a virtual Mac (CI) reports no efficiency cores or GPU")
def test_machine_on_a_real_apple_silicon_mac() -> None:
    m = catalog.machine()
    assert m.chip.startswith("Apple M") and m.budget_gb > 0
    assert m.performance_cores and m.efficiency_cores
    assert m.gpu_limit_gb <= m.memory_gb


def test_a_catalogue_answer_that_is_too_large_or_too_deep_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(catalog, "_MAX_RESPONSE_BYTES", 100)
    answers = iter([b"[" + b"0," * 100 + b"0]", b"[" * 100_000 + b"]" * 100_000])
    monkeypatch.setattr(
        catalog.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(next(answers))
    )
    with pytest.raises(ValueError, match="over"):
        catalog._get_json("https://huggingface.co/api/models", 5)
    monkeypatch.setattr(catalog, "_MAX_RESPONSE_BYTES", 1 << 20)
    with pytest.raises(ValueError, match="nested"):
        catalog._get_json("https://huggingface.co/api/models", 5)
