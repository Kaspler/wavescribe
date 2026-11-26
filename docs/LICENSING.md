# Licensing policy and third-party audit

This project's own code is **Apache-2.0**. It **does not redistribute** third-party binaries
or model weights: users install tools with Homebrew/pip and download models during setup.
**Every library, tool and model gets an entry here before it's used.** Audited on 2026-11-09
against licence files, file headers, PyPI and Homebrew metadata, and Hugging Face model cards.
This is not legal advice.

## Policy

### Code dependencies

| Tier | Licences | Rule |
|---|---|---|
| **A: may be imported or linked** | MIT, MIT-0, BSD-2/3-Clause, 0BSD, ISC, Apache-2.0, BSL-1.0, Zlib, PSF-2.0, CC0-1.0, Unlicense, MPL-2.0 (unmodified) | Allowed as required dependencies |
| **A−: in-process with conditions** | LGPL-2.1+/3.0+ | Dynamic linking only. Unmodified, never vendored, replaceable by the user. |
| **B: process boundary only** | GPL-2.0(-or-later), GPL-3.0(-or-later), AGPL-3.0 (unmodified external service only) | Talk to the tool only through subprocess/exec, stdin/stdout, files, TCP/UDP, HTTP or WebSocket. **No `import`, no `dlopen`/ctypes, no vendored source or headers.** It must be optional and detected at runtime, installed by the user, and documented as an "external GPL tool". |
| **C: never** | No licence, BUSL, SSPL, Commons Clause, PolyForm-Noncommercial, research-only | Not allowed |

**Binary bundles.** Today users install the GPL tools themselves (Homebrew). **Any binary
bundle** (Docker image, `.app`, Homebrew bottle) that *contains* GPL parts needs a licence
review, an SBOM and GPL source compliance. All the GPL parts found so far are "-or-later", so a
combined bundle could be shipped as GPL-3.0.

### Models and data

**How this works.** Copyright means "all rights reserved" unless a licence grants permission.
- Our **Apache-2.0 licence covers our code only.** Model weights are separate works with their own licences.
- We **never redistribute weights**. Each user downloads them from the rights holder (e.g. Hugging Face) and accepts the terms there. A model's conditions (attribution, non-commercial, use policy, territory) bind **whoever downloads and uses it**.
- Supporting a model in code (a model ID, a loader, a download command) is not redistribution, so we believe it needs no licence from the rights holder. Using it lawfully is the user's responsibility.
- Our only duties: be honest about each licence, make restricted models clearly opt-in, and never ship restricted weights ourselves.

| Tier | Licences | How the app treats it |
|---|---|---|
| **Default** | Apache-2.0, MIT, BSD, CC0-1.0, CC-BY-3.0/4.0, OpenMDW-1.0/1.1, CDLA-Permissive-2.0 | Listed. Setup may download without asking. Attribution goes in the generated `THIRD_PARTY_MODELS.md`. |
| **Opt-in** (custom open-weights terms) | Gemma Terms of Use, NVIDIA Open Model License, text-only Llama community licences, OpenRAIL-M | Listed. Download needs `--accept-license <id>` (the licence name and link are shown first). The acceptance is recorded locally. Never part of the default preset. |
| **Restricted** (non-commercial or research-only) | CC-BY-NC*, research-only | Listed with a **⚠ NC** flag. Needs `--accept-license <id>` **and** an explicit non-commercial acknowledgement. The app shows "non-commercial models active". Never in default presets. |
| **Bring your own** (not listed by us) | No licence, territory exclusions (e.g. EU-excluded), anything else | **Not in the registry or presets** (named in this audit only to say why). The engine still loads any compatible model a user names in their **private** `config/models.local.toml` (a Hugging Face ID or a local path). The app requires a `license` field there and warns if it's `unknown`. Responsibility is entirely the user's. |

**Never do these, in any tier:**
- commit, mirror or attach weights;
- upload conversions (MLX, GGUF, Core ML) of opt-in or restricted models under the project's org;
- bundle them in installers or Docker images;
- make a non-default model the default;
- strip licence notices;
- ship our own Hugging Face tokens. Gated models use the user's own `huggingface-cli login`.

### Model registry rules (`models.toml`)

- **Every entry has:** id, upstream URL, **pinned revision SHA**, SPDX id or `LicenseRef-*`, licence URL, tier, attribution text, gated yes/no, languages, data notes.
- An entry needs a licence that fits one of the listed tiers; anything else is bring-your-own. The commit checks block a new or re-pinned model unless this file changes in the same commit.
- **Conversions inherit the upstream licence** (mlx-community, GGUF, Core ML). Don't trust a converter's metadata.
- **Re-check the licence on every version bump.** Drift seen in this audit alone:
  - SimulStreaming: PolyForm-NC → MIT
  - Sortformer: CC-BY → NVIDIA licence
  - Nemotron: NVIDIA licence → OpenMDW
  - Piper: MIT → GPL
  - Gemma: Terms of Use → Apache (Gemma 4)

### Online model catalogue (Settings → Languages and models, 2026-11-26)

The page can search Hugging Face for MLX models to add (`catalog/models.py`). Searching takes a
click on the machine itself; LAN viewers can't trigger it. It sends anonymous GETs to the
public API, with no token and a generic User-Agent. The rules match the registry's:

- **The tier comes from the licence**, through one table in `catalog/models.py`:

  | Tier | Licences |
  |---|---|
  | default | Apache-2.0, MIT, BSD, ISC, MPL-2.0, Unlicense, CC0, CC-BY, CDLA-Permissive-2.0, OpenMDW |
  | opt-in | Gemma, HAI-DEF, Llama 2/3.x, NVIDIA Open Model License, OpenRAIL |
  | restricted | CC-BY-NC(-SA) and anything else marked non-commercial or research-only |

  Anything not in the table, with no licence or under a licence that excludes the EU (Llama 4
  multimodal), is **never offered**. It stays bring-your-own, through `config/models.local.toml`.
- **Conversions inherit the original's licence.** For an mlx-community (or similar) conversion,
  the licence is read from the base model's card, not the converter's.
- **Each entry added is pinned** to the commit it was found at. It goes to the gitignored
  `config/models.catalog.toml` with its licence URL and attribution, and is marked untested.
  Opt-in and restricted entries need the same licence acceptance as registry models before a
  download.
- **Weights still come only from the rights holder's repository**, through
  `wavescribe models fetch`. We never mirror or re-host them.

### Disclosure to users

- `wavescribe models list` prints every model with its licence, tier, install state and acceptance; external tools are listed in this file.
- `wavescribe models fetch` writes `data/THIRD_PARTY_MODELS.md` (CC-BY credits for the installed models), and `wavescribe models credits` prints it.
- Settings → Languages and models shows every model's licence, tier, caveats and attribution.
- Explicit warnings:
  - macOS `say` voices are for **local personal playback only**.
  - Piper voices are licensed **per voice**.

### Hygiene

In place:

- PEP 639 `license = "Apache-2.0"` in `pyproject.toml`, and SPDX headers in the Python sources and scripts.
- **Every commit is checked** by `scripts/commit_checks.py` (git hooks, installed with
  `python3 scripts/commit_checks.py install`; see CONTRIBUTING.md). It blocks a commit that adds
  a direct dependency with no entry here, a transitive package whose licence isn't tier A and
  isn't recorded here, or a new or re-pinned model without this file updated in the same commit.
- **Third-party telemetry off:** `HF_HUB_DISABLE_TELEMETRY=1` in every process.

Planned: SPDX headers in the page's sources, and the licence check in CI too (today it runs in
the pre-commit hook; CI checks the page).

## Findings that changed the design

1. **Homebrew `ffmpeg` is GPL-3.0-or-later** (`--enable-gpl --enable-version3`). Use it only as a subprocess with pipes. Never load libav* in-process (e.g. through torchaudio's FFmpeg backends).
2. **TranslateGemma, Gemma 3 and Gemma 3n are under the Gemma Terms of Use** (non-OSI; use restrictions pass on to recipients; Google may restrict use remotely), so they're **opt-in only**. **Gemma 4 is plain Apache-2.0.**
3. **`inesc-id/WhisperLv3-EP-X-CPP` has no licence**, so it's all rights reserved. It's **bring-your-own only** until the authors add a licence.
4. **pyrtlsdr is GPL-3.0-or-later**, so it isn't imported. Wavescribe runs `rtl_sdr` as a subprocess instead.
5. **Non-commercial models are restricted opt-in:** Meta MMS, NLLB-200 and SeamlessM4T are CC-BY-NC. Permissive models do their jobs: **Qwen3.5** (Apache-2.0, 201 languages) and **MADLAD-400** (Apache-2.0, 419) translate, and **Omnilingual ASR** (Apache-2.0, 1,600+ languages) would cover the long tail of speech recognition (not registered yet).
6. **Hunyuan-MT's licence excludes the EU, UK and South Korea**, so it's bring-your-own only, and it grants no rights to users in those territories.
7. **macOS `say` voices** (macOS licence §2F): personal, non-commercial, local use. No recording, publishing or redistribution. Our reading: **fine for spoken alerts in the room; not for anything recorded or rebroadcast.**
8. **WhisperLiveKit** (not used): if it ever is, disable its NLLB backend. Pin **SimulStreaming** to a version after 2025-10-22, when it was relicensed to MIT.
9. **pyannote.audio 4.x sends telemetry by default.** Not used; avoid it, or disable that.
10. **Nemotron 3.5 ASR streaming uses OpenMDW-1.1**, a permissive licence, not NVIDIA's Open Model License. Older Nemotron and Sortformer releases differ.

## Audit: libraries and tools

| Component | Licence | Verdict | Notes |
|---|---|---|---|
| librtlsdr (osmocom) | GPL-2.0-or-later | ⚠️ | Fine at runtime; never bundle |
| `rtl_sdr` / `rtl_power` / `rtl_test` | GPL-2.0-or-later | ⚠️ B | Subprocesses, the cleanest boundary. `rtl_sdr` feeds the demodulators; `rtl_power` sweeps bands for scans (`radio/scan.py`), read as CSV |
| numpy, scipy | BSD-3-Clause (+ permissive) | ✅ | |
| ffmpeg (Homebrew) | GPL-3.0-or-later | ⚠️ B | Subprocess + pipes |
| libopus (inside the ffmpeg subprocess) | BSD-3-Clause; Opus is royalty-free (RFC 6716) | ✅ | Speech audio archive |
| Silero VAD | MIT | ✅ | We run its ONNX model with onnxruntime. The `silero-vad` pip package isn't used, because it pulls in torch. |
| onnxruntime | MIT | ✅ | CPU inference for Silero VAD |
| ai-edge-litert (LiteRT) | Apache-2.0 | ✅ | Runs YAMNet for the speech/music gate; CPU, no TensorFlow |
| py3langid (with its bundled langid.py model) | BSD-3-Clause | ✅ | Text language ID for the ASR checks and alert translations |
| snowballstemmer | BSD-3-Clause | ✅ | Per-language stemming for keyword rules (pure Python) |
| parakeet-mlx | Apache-2.0 | ✅ | Pulls in librosa, typer and dacite (see transitive dependencies) |
| MLX, mlx-lm | MIT | ✅ | Speech and translation models on the GPU |
| mlx-audio | MIT | ✅ | Whisper (the ASR chain's fallback), plus Voxtral, Qwen3-ASR and Nemotron for bake-offs, on MLX. Pulls in miniaudio (MIT), sounddevice (MIT, bundles PortAudio, MIT) and zstandard (BSD-3-Clause). |
| CTranslate2 | MIT | ✅ | |
| sentencepiece | Apache-2.0 | ✅ | Tokeniser for MADLAD-400 |
| huggingface-hub (+ hf-xet) | Apache-2.0 | ✅ | Used only by `models fetch`. At runtime we set `HF_HUB_OFFLINE=1` and `HF_HUB_DISABLE_TELEMETRY=1`. |
| transformers | Apache-2.0 | ✅ | Pulled in by mlx-lm |
| Babel | BSD-3-Clause | ✅ | CLDR language names for LLM prompts; ships its data, works offline |
| SQLite (+ FTS5, via Python's `sqlite3`) | Public domain | ✅ | The store; the trigram tokenizer needs SQLite ≥ 3.34 (`remove_diacritics` ≥ 3.45) |
| macOS `say` | Apple macOS licence §2F | ⚠️ | Local personal playback only |
| hatchling | MIT | ✅ | Build backend |
| pytest, ruff | MIT / MIT | ✅ | Dev only |
| Radio Browser station directory (service + data, [radio-browser.info](https://www.radio-browser.info)) | Data: public domain (the site: "Data license: public domain"; submissions are released to the public domain); server software AGPL-3.0 | ✅ | HTTP API only, and only from explicit user actions (catalogue refresh, online search) with a generic User-Agent. A local copy is allowed: the FAQ offers full daily exports. Station names, logos, homepages and streams belong to the stations; we store links only (`catalog/stations.py`). Checked 2026-11-26 |

### Evaluated, not used

Kept so the reasons aren't lost: each was considered for a part of Wavescribe, and none is
installed or called by it. GQRX and SDR++ appear in HARDWARE.md as tools you may use alongside.

| Component | Licence | Verdict | Notes |
|---|---|---|---|
| SoapySDR (+ Python bindings) | BSL-1.0 | ✅ | Device-neutral API |
| SoapyAirspy + libairspy | MIT + BSD-3-Clause | ✅ | Permissive all the way down |
| SoapyRTLSDR | MIT (links GPL librtlsdr) | ⚠️ | User-installed plugin only; never bundle |
| `airspy_rx` | GPL-2.0-or-later | ⚠️ B | Subprocess |
| pyrtlsdr | GPL-3.0-or-later | ❌ in-process | Not imported: `rtl_sdr` runs as a subprocess |
| liquid-dsp | MIT | ✅ | |
| redsea (RDS) | MIT | ✅ | CLI. Not used (2026-11-26): no Homebrew formula, and building it pulls in liquid-dsp and LGPL libsndfile. RDS is our own decoder (`dsp/rds.py`), written from the public standard |
| RTLSDR-Airband | GPL-2.0-or-later | ⚠️ B | Separate process; UDP/Icecast/files |
| SDRangel | GPL-3.0 | ⚠️ B | REST/UDP |
| SDR++ | GPL-3.0 | ⚠️ B | Survey tool only |
| GQRX | GPL-3.0 | ⚠️ B | Survey tool only (user-installed; with the V4L, via `rtl_tcp`) |
| inaSpeechSegmenter | MIT (model weights: no separate licence, assumed MIT) | ✅ (not used) | Dropped: doesn't run on current TensorFlow/Keras/NumPy (MODELS.md) |
| pyannote.audio | MIT, gated models, telemetry on | ⚠️ | Avoid unless needed |
| Apple SoundAnalysis | Apple platform | ✅ macOS | Via PyObjC (MIT) or a Swift helper |
| whisper.cpp | MIT | ✅ | |
| WhisperKit | MIT | ✅ | Swift, via a CLI/server bridge |
| WhisperLiveKit | Apache-2.0 | ⚠️ | Disable NLLB; check each backend's model licence |
| SimulStreaming | MIT (since 2025-10-22) | ⚠️ | Pin a version after the relicence |
| FluidAudio | Apache-2.0 | ✅ | Each model keeps its upstream licence |
| Ollama, llama.cpp | MIT | ✅ | Check each model (`ollama show --license`) |
| Argos Translate | MIT OR CC0-1.0 | ✅ | Model packages are licensed per package (see below) |
| sentence-transformers | Apache-2.0 | ✅ | |
| sqlite-vec | MIT OR Apache-2.0 | ✅ | |
| Apprise | BSD-2-Clause | ✅ | |
| meshtastic-python CLI | GPL-3.0 | ⚠️ B | Run the CLI as a subprocess; don't import |
| Piper (piper1-gpl) | GPL-3.0 | ⚠️ B | CLI/HTTP only; check each voice's licence |
| FastAPI / Starlette / uvicorn | MIT / BSD-3 / BSD-3 | ✅ | |
| Textual, Typer, Click, Pydantic, tomli | MIT / MIT / BSD-3 / MIT / MIT | ✅ | |
| mlx-whisper | MIT | ✅ | Not used: it hard-requires torch (mlx-audio runs Whisper) |
| `rtl_fm` / `rtl_tcp` | GPL-2.0-or-later | ⚠️ B | Not used by Wavescribe; `rtl_tcp` serves GQRX in HARDWARE.md |

### Transitive Python dependencies

First checked with `pip-licenses` against `uv.lock` on 2026-11-09; since then the commit checks
read the licence of every package a commit adds to the lock (81 packages on 2026-11-26). **None is tier
B or C.** The ones worth knowing about:

| Package | Pulled in by | Licence | Verdict |
|---|---|---|---|
| librosa | parakeet-mlx (mel filterbank) | ISC | ✅ |
| soundfile (bundles libsndfile) | librosa | BSD-3-Clause; libsndfile is LGPL-2.1-or-later | ⚠️ A−: a dynamically loaded, unmodified library the user installs; we never vendor it |
| soxr (python-soxr, bundles libsoxr) | librosa | LGPL-2.1-or-later | ⚠️ A−: same rule as above |
| numba, llvmlite | librosa | BSD-2-Clause; LLVM is Apache-2.0 WITH LLVM-exception | ✅ |
| scikit-learn, scipy, joblib, pooch | librosa | BSD-3-Clause | ✅ |
| certifi, tqdm | requests / huggingface-hub | MPL-2.0 (tqdm: MPL-2.0 AND MIT) | ✅ unmodified |
| colorama | tqdm, typer, pytest (Windows only) | BSD-3-Clause | ✅ not installed on macOS |
| httpx2-jsfetch | httpx2 (Python in the browser only) | BSD-3-Clause | ✅ not installed on macOS |
| regex | transformers | Apache-2.0 AND CNRI-Python | ✅ |
| protobuf, flatbuffers | onnxruntime, ai-edge-litert | BSD-3-Clause / Apache-2.0 | ✅ |
| ml-dtypes | ai-edge-litert | Apache-2.0 (bundles Eigen headers, MPL-2.0) | ✅ unmodified |
| backports.strenum | ai-edge-litert | MIT | ✅ |

Re-run the check whenever `uv.lock` changes:
`uv run --with pip-licenses pip-licenses --order=license`.

### Page dependencies (npm)

The page is React + TypeScript + Vite (decided 2026-11-26), built into
`src/wavescribe/web/dist/` and committed. Only what the bundle contains ships; the build tools
run on the developer's machine. Checked on the npm registry on 2026-11-26.

| Package | Licence | Ships in the bundle | Verdict |
|---|---|---|---|
| react, react-dom | MIT | Yes | ✅ keep the notice |
| scheduler | MIT | Yes (pulled in by react-dom) | ✅ keep the notice |
| lucide-react (1.72.0) | ISC (some icons derived from Feather, MIT) | Yes (only the icons used) | ✅ keep the notice; `frontend/scripts/notices.mjs` copies it into `licenses.txt` |
| vite | MIT | No (build tool) | ✅ |
| @vitejs/plugin-react | MIT | No (build tool) | ✅ |
| typescript | Apache-2.0 | No (compiler) | ✅ |
| @types/react, @types/react-dom | MIT | No (type definitions) | ✅ |
| lightningcss (+ its platform binary) | MPL-2.0 | No (Vite's CSS minifier, a build tool) | ✅ unmodified and run as a tool; MPL is file-level copyleft and nothing of it ends up in the page |
| the rest of Vite's toolchain (rolldown, postcss, tinyglobby, …) | MIT, ISC, BSD-3-Clause, Apache-2.0 | No (build tools) | ✅ |

`scripts/commit_checks.py` checks every new package in `frontend/package-lock.json`: tier-A
licences pass, anything else needs a row here first.

**The logo** (`assets/`, the page's icons in `frontend/public/`) is the project's own work under
the project's licence. The wordmark and tagline are outlines of the Inter typeface (SIL Open
Font License 1.1); outlines carry no font software, so no font is bundled.

### Radio data

- **Band plans** (`src/wavescribe/radio/bands.toml`) are written by us from public facts:
  band edges, channel rasters and channel lists from the ITU Radio Regulations and CEPT
  decisions. No third-party list is copied.
- **Station lists are not shipped or downloaded.** National frequency registers are often
  published for non-commercial use without derivatives, community lists such as FMLIST are for
  private use, and open data (Wikidata) holds frequencies for very few stations. Wavescribe's list
  is built from the user's own scans (`data/radio/heard.json`). An import of an openly licensed
  register (for example one under an open government licence) would be a user-started download,
  with its licence recorded here first.
- **RDS** names and texts come from the stations' own broadcasts as received; they are kept
  locally like the transcripts.

## Audit: models

| Model | Licence | Verdict | Languages |
|---|---|---|---|
| Whisper large-v3-turbo | MIT | ✅ | 99 |
| Whisper large-v3 | Apache-2.0 (its model card; OpenAI's code is MIT) | ✅ | 99 |
| Parakeet-TDT-0.6B-v3 | CC-BY-4.0 | ✅ (attribution) | 25 European |
| Canary-1B-v2 | CC-BY-4.0 | ✅ (attribution) | 25 European ASR; translation between EN and 24 others, both directions |
| Nemotron 3.5 ASR streaming 0.6B | OpenMDW-1.1 (`LicenseRef-OpenMDW-1.1`) | ✅ (keep notices) | 40 locales (19 production-grade) |
| nemotron-speech-streaming-en-0.6b (older) | NVIDIA Open Model License | ⚠️ opt-in | English |
| Voxtral Mini 3B / Mini 4B Realtime | Apache-2.0 | ✅ | 8 / 13 |
| Qwen3-ASR 0.6B / 1.7B | Apache-2.0 | ✅ | 30 + 22 Chinese dialects |
| Omnilingual ASR (Meta) | Apache-2.0 | ✅ | 1,600+ |
| Meta MMS | CC-BY-NC-4.0 | ⚠️ restricted (NC) | 1,100+ ASR + language ID |
| inesc-id/WhisperLv3-EP-X-CPP | none | bring-your-own only (ask the authors for a licence) | pt-PT |
| Qwen3 / Qwen3.5 (incl. 35B-A3B, checked 2026-11-22) | Apache-2.0 | ✅ | 119 / 201 |
| Gemma 4 | Apache-2.0 | ✅ | 140+ (35+ strong) |
| Gemma 3 / 3n / TranslateGemma | Gemma Terms of Use | ⚠️ opt-in | 140+ / 55 |
| MADLAD-400 3B/7B/10B | Apache-2.0 | ✅ | 419 |
| Opus-MT (Helsinki-NLP) | CC-BY-4.0 or Apache-2.0, per model | ✅ (attribution) | Hundreds of languages |
| EuroLLM-9B-Instruct | Apache-2.0 | ✅ | 35 |
| Argos model packages | Per package (newer: CC-BY-4.0; some older: none) | ⚠️ | ~50 |
| NLLB-200, SeamlessM4T v2 | CC-BY-NC-4.0 | ⚠️ restricted (NC) | 200 / ~100 |
| Hunyuan-MT | Tencent licence, excludes EU/UK/KR | bring-your-own only | 33 |
| bge-m3 | MIT | ✅ | 100+ |
| multilingual-e5 | MIT | ✅ | ~100 |
| LaBSE | Apache-2.0 | ✅ | 109 |
| YAMNet | Apache-2.0 (class map CC-BY-SA-4.0) | ✅ | 521 classes |
| PANNs | Code MIT, weights CC-BY-4.0 | ✅ (attribution) | 527 classes |
| Silero VAD v6 (`silero_vad.onnx`, from the snakers4/silero-vad GitHub tag) | MIT | ✅ | Language-independent |

**Conversions we load** (each inherits its upstream licence; the registry pins the revision):

| Conversion | Upstream | Licence | Notes |
|---|---|---|---|
| `mlx-community/parakeet-tdt-0.6b-v3` (MLX) | `nvidia/parakeet-tdt-0.6b-v3` | CC-BY-4.0 | Attribution in `THIRD_PARTY_MODELS.md` |
| `yamnet.tflite` from MediaPipe's model storage (Google's own LiteRT build, pinned by sha256) | YAMNet (tensorflow/models) | Apache-2.0; embedded AudioSet class names CC-BY-SA-4.0 | Speech/music gate |
| `mlx-community/whisper-large-v3-turbo-asr-fp16` (MLX, with the processor files mlx-audio needs) | `openai/whisper-large-v3-turbo` | MIT | ASR fallback (languages Parakeet lacks, e.g. Hebrew); bake-off |
| `mlx-community/whisper-large-v3-asr-fp16` (MLX, with processor files) | `openai/whisper-large-v3` | Apache-2.0 on the card (the openai/whisper repo says MIT) | Bake-off |
| `mlx-community/Voxtral-Mini-4B-Realtime-2602-fp16` (MLX) | `mistralai/Voxtral-Mini-4B-Realtime-2602` | Apache-2.0 | Bake-off |
| `mlx-community/Qwen3-ASR-1.7B-8bit` (MLX) | `Qwen/Qwen3-ASR-1.7B` | Apache-2.0 | Bake-off |
| `mlx-community/nemotron-3.5-asr-streaming-0.6b` (MLX) | `nvidia/nemotron-3.5-asr-streaming-0.6b` | OpenMDW-1.1 | Bake-off; keep the notices |
| `mlx-community/Qwen3.5-9B-MLX-4bit` (MLX) | `Qwen/Qwen3.5-9B` | Apache-2.0 | `LlmTranslator` |
| `Nextcloud-AI/madlad400-3b-mt-ct2-int8` (CTranslate2 int8) | `google/madlad400-3b-mt` | Apache-2.0 | Nextcloud runs this conversion in its own translate app. To convert locally instead: `ct2-transformers-converter --model google/madlad400-3b-mt --quantization int8` (needs torch; setup only). |

## Open uncertainties

- The inaSpeechSegmenter weights have no explicit licence.
- Some older Argos packages have no licence.
- OpenMDW-1.1 isn't on the SPDX list yet.
- How well Nemotron 3.5, Omnilingual ASR and Canary run on Apple Silicon is unverified.
- The FFmpeg licence inside PyAV wheels is unchecked.
- The macOS 27 licence wording should be re-checked against the Tahoe text quoted here.

## Key sources

- Licence files and model cards linked from each project's repo or Hugging Face page
- [Homebrew ffmpeg formula](https://formulae.brew.sh/formula/ffmpeg)
- [Gemma Terms of Use](https://ai.google.dev/gemma/terms) and the [Gemma 4 licence](https://ai.google.dev/gemma/docs/gemma_4_license)
- [NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/)
- [OpenMDW-1.1](https://openmdw.ai/license/1-1)
- [SimulStreaming licence history](https://github.com/ufal/SimulStreaming)
- [macOS SLA §2F](https://www.apple.com/legal/sla/docs/macOSTahoe.pdf)
