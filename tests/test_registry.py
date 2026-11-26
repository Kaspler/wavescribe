# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest

from wavescribe.registry import ModelEntry, ModelError, ModelRegistry, _parse_entry


def entry(mid: str, languages: tuple[str, ...], source: str = "url:https://x/m.bin") -> ModelEntry:
    return ModelEntry(mid, "asr", "fake", source, "u", "MIT", "u", "default", False, languages, "")


def test_language_matching() -> None:
    e = entry("m", ("pt", "es-ES"))
    assert e.supports("pt-PT") and e.supports("pt-BR") and e.supports("PT")
    assert e.supports("es-ES") and not e.supports("es-MX")
    assert not e.supports("uk")
    assert entry("any", ("*",)).supports("zu")


def test_resolve_takes_first_installed_model_that_supports_the_language(tmp_path: Path) -> None:
    a, b, c = entry("a", ("uk",)), entry("b", ("pt",)), entry("c", ("*",))
    reg = ModelRegistry([a, b, c], {"asr": ["a", "b", "c"]}, models_dir=tmp_path)
    with pytest.raises(ModelError, match="wavescribe models fetch b"):
        reg.resolve("asr", "pt-PT")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "m.bin").write_bytes(b"x")
    assert reg.resolve("asr", "pt-PT")[0].id == "c"  # b isn't installed, so fall through
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "m.bin").write_bytes(b"x")
    assert reg.resolve("asr", "pt-PT")[0].id == "b"


def test_packaged_registry_is_valid_and_pinned() -> None:
    reg = ModelRegistry.load(local=None)  # never the private local file
    for e in reg.entries():
        assert e.tier == "default"
        assert e.revision or e.sha256
        assert e.license and e.license_url.startswith("https://")
    for capability in ("vad", "asr", "translate"):
        assert reg.chain(capability)


def test_packaged_asr_chain_falls_back_to_whisper_for_languages_parakeet_lacks() -> None:
    chain = ModelRegistry.load(local=None).chain("asr")
    first = lambda lang: next(e.id for e in chain if e.supports(lang))  # noqa: E731
    assert first("pt-PT") == "parakeet-tdt-0.6b-v3"
    assert first("uk") == "parakeet-tdt-0.6b-v3"
    assert first("he") == "whisper-large-v3-turbo"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"tier": "whatever"}, "unknown tier"),
        ({"revision": None}, "pinned revision"),
        ({"source": "url:https://x", "revision": None}, "sha256"),
    ],
)
def test_registry_rules_are_enforced(override: dict, message: str) -> None:
    raw = {
        "id": "x", "capability": "asr", "backend": "b", "source": "hf:org/x", "revision": "abc",
        "upstream": "u", "license": "MIT", "license_url": "u", "tier": "default",
        "languages": ["*"],
    }  # fmt: skip
    raw.update(override)
    raw = {k: v for k, v in raw.items() if v is not None}
    with pytest.raises(ModelError, match=message):
        _parse_entry(raw)


def gated(mid: str, tier: str, license: str = "Gemma") -> ModelEntry:
    return ModelEntry(mid, "asr", "fake", "url:https://x/m.bin", "u", license, "https://lic",
                      tier, False, ("*",), "")  # fmt: skip


def installed(tmp_path: Path, *ids: str) -> None:
    for mid in ids:
        (tmp_path / mid).mkdir()
        (tmp_path / mid / "m.bin").write_bytes(b"x")


def test_chains_skip_models_whose_licence_isnt_accepted(tmp_path: Path) -> None:
    from wavescribe.registry import LicenseError, Licenses

    licenses = Licenses(tmp_path / "accepted.json")
    opt, free = gated("opt", "opt-in"), entry("free", ("*",))
    reg = ModelRegistry([opt, free], {"asr": ["opt", "free"]}, tmp_path, licenses)
    installed(tmp_path, "opt", "free")
    assert reg.resolve("asr", "pt-PT")[0].id == "free"  # opt-in, not accepted: skipped
    with pytest.raises(LicenseError, match="wavescribe models accept opt --license 'Gemma'"):
        reg.installed("opt")  # named explicitly: the startup gate refuses
    licenses.accept(opt, "gemma")  # licence ids compare case-insensitively
    assert reg.resolve("asr", "pt-PT")[0].id == "opt"
    assert reg.installed("opt")[0].id == "opt"


def test_restricted_models_need_the_non_commercial_acknowledgement(tmp_path: Path) -> None:
    from wavescribe.registry import LicenseError, Licenses

    licenses = Licenses(tmp_path / "accepted.json")
    nc = gated("nllb", "restricted", "CC-BY-NC-4.0")
    with pytest.raises(LicenseError, match=r"not 'CC-BY-4\.0'"):
        licenses.accept(nc, "CC-BY-4.0")
    with pytest.raises(LicenseError, match="--non-commercial"):
        licenses.accept(nc, "CC-BY-NC-4.0")
    assert not licenses.accepted(nc)
    licenses.accept(nc, "CC-BY-NC-4.0", non_commercial=True)
    assert licenses.accepted(nc)
    # A licence change upstream voids the old acceptance.
    assert not licenses.accepted(gated("nllb", "restricted", "LicenseRef-New"))


def test_plan_names_its_non_commercial_models(tmp_path: Path) -> None:
    from wavescribe.pipeline import Plan

    free, nc = entry("free", ("*",)), gated("nllb", "restricted", "CC-BY-NC-4.0")
    plan = Plan(vad=(free, tmp_path), asr={"a": (free, tmp_path)}, mt=(nc, tmp_path))
    assert plan.noncommercial() == ["nllb"]


def test_bring_your_own_models_come_from_the_private_file(tmp_path: Path) -> None:
    local = tmp_path / "models.local.toml"
    weights = tmp_path / "my-model"
    weights.mkdir()
    local.write_text(
        '[[model]]\nid = "my-ep-whisper"\ncapability = "asr"\nlanguages = ["pt-PT"]\n'
        f'source = "local:{weights}"\nbackend = "mlx-audio"\nlicense = "unknown"\n'
    )
    reg = ModelRegistry.load(tmp_path / "models", local=local, ledger=tmp_path / "a.json")
    byo = reg.get("my-ep-whisper")
    assert byo.tier == "byo" and reg.local_path(byo) == weights
    assert reg.installed("my-ep-whisper", "pt-PT")[0] is byo  # no acceptance needed: it's theirs
    assert all(e.id != "my-ep-whisper" for e in reg.chain("asr"))  # never in our chains

    local.write_text(local.read_text().replace("my-ep-whisper", "parakeet-tdt-0.6b-v3"))
    with pytest.raises(ModelError, match="already in the registry"):
        ModelRegistry.load(tmp_path / "models", local=local)


def test_a_failed_download_leaves_nothing_behind(tmp_path: Path, monkeypatch) -> None:
    import io
    import urllib.request

    e = entry("m", ("*",))
    reg = ModelRegistry([e], {"asr": ["m"]}, models_dir=tmp_path)

    class CutShort(io.BytesIO):
        def read(self, n: int = -1) -> bytes:
            if self.tell():
                raise ConnectionResetError("connection reset by peer")
            return super().read(8)

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: CutShort(b"x" * 64))
    with pytest.raises(ConnectionResetError):
        reg.fetch(e)
    assert list((tmp_path / "m").iterdir()) == []  # no partial file next to the model
