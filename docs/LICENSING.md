# Licences

Wavescribe's own code and documentation are licensed under Apache-2.0 ([LICENSE](../LICENSE)).
This file lists every third-party library, program, model and data source Wavescribe uses: what
it is, its licence (an SPDX identifier where one exists, as its source states it), how
Wavescribe uses it, and whether Wavescribe ships it. It is not legal advice.

## What Wavescribe ships

A clone of the repository holds Wavescribe's own work under Apache-2.0: the source, the
documentation, the configuration examples in `config/`, the model registry
(`src/wavescribe/models.toml`: ids, sources, pinned revisions and licences, no weights), the
radio band plans (`src/wavescribe/radio/bands.toml`) and the other data files
(`src/wavescribe/asr/junk.toml`, `src/wavescribe/translate/remarks.toml`). The example station
files name public internet stations and their stream addresses; the streams belong to their
broadcasters. The Wavescribe name and logo are covered under [Logo and icons](#logo-and-icons).

The only third-party code in the repository is in the built page, `src/wavescribe/web/dist/`:

| Third-party part | Licence | What is included for it |
|---|---|---|
| React (`react`, `react-dom`, `scheduler`) | MIT | Each package's copyright and licence text in [`dist/licenses.txt`](../src/wavescribe/web/dist/licenses.txt) |
| Lucide icons (`lucide-react`; only the icons the page uses) | ISC; the icons derived from Feather: MIT | Both licence texts and the list of Feather-derived icons, in `licenses.txt` |
| Vite's module-preload helper (a few lines Vite writes into the bundle) | MIT | Vite's licence text, in `licenses.txt` |

`frontend/scripts/notices.mjs` writes `licenses.txt` from the packages' own licence files at
every build, and the page links it ("Licences of the page's parts").

Nothing else third-party ships. Each user fetches the rest from its publisher:
`wavescribe models fetch` downloads models from the rights holders' repositories, uv installs
Python and the Python packages from PyPI, and Homebrew installs ffmpeg and librtlsdr. There is
no binary bundle (installer, `.app` or Docker image).

## Policy

### Code dependencies

| Tier | Licences | Rule |
|---|---|---|
| **A: may be imported or linked** | MIT, MIT-0, BSD-2-Clause, BSD-3-Clause, 0BSD, ISC, Apache-2.0, BSL-1.0, Zlib, PSF-2.0, CC0-1.0, Unlicense, MPL-2.0 (unmodified) | Allowed as required dependencies |
| **A−: in-process with conditions** | LGPL-2.1-or-later, LGPL-3.0-or-later | Dynamic linking only. Unmodified, never vendored, replaceable by the user. |
| **B: process boundary only** | GPL-2.0-only, GPL-2.0-or-later, GPL-3.0-only, GPL-3.0-or-later; AGPL-3.0-only, AGPL-3.0-or-later (an unmodified external service only) | Talk to the tool only through subprocess/exec, stdin/stdout, files, TCP/UDP, HTTP or WebSocket. **No `import`, no `dlopen`/ctypes, no vendored source or headers.** It must be optional and detected at runtime, installed by the user, and documented as an external GPL tool ([External programs](#external-programs)). |
| **C: never** | No licence, BUSL, SSPL, Commons Clause, PolyForm Noncommercial, research-only | Not allowed |

Every direct dependency has an entry in this file. A transitive package needs one when its
licence isn't tier A.

**Binary bundles.** A binary bundle (Docker image, `.app`, Homebrew bottle) that contains GPL
parts needs a licence review, an SBOM and GPL source compliance.

### Model tiers

Model weights are separate works under their own licences, which Wavescribe's licence doesn't
cover. Wavescribe never redistributes weights: each user downloads them from the rights holder
(for example from Hugging Face) and accepts the terms there.

| Tier | Licences | What the app does |
|---|---|---|
| **Default** | Apache-2.0, MIT, BSD-2-Clause, BSD-3-Clause, CC0-1.0, CC-BY-3.0, CC-BY-4.0, OpenMDW-1.0, OpenMDW-1.1, CDLA-Permissive-2.0 | Lists it. Setup may download it without asking. Its attribution goes in the generated `THIRD_PARTY_MODELS.md`. |
| **Opt-in** (custom open-weights terms) | Gemma Terms of Use, NVIDIA Open Model License, text-only Llama community licences, OpenRAIL-M | Lists it with its licence name and link. A download needs the licence accepted: `wavescribe models fetch <id> --accept-license <licence id>`, `wavescribe models accept <id> --license <licence id>`, or the page's Accept button. The acceptance is recorded locally (`data/licenses-accepted.json`), and a changed licence voids it. Never part of the default preset. |
| **Restricted** (non-commercial or research-only) | CC-BY-NC-*, research-only | Lists it marked non-commercial. Needs the same acceptance **and** an explicit non-commercial acknowledgement (`--non-commercial`). While one is in use, `listen` and `serve` print "⚠ non-commercial models active" and the page's Settings name it. Never in default presets. |
| **Bring your own** (not listed) | No licence, territory exclusions (for example EU-excluded), anything else | Not in the registry or presets. The engine still loads any compatible model a user names in their **private** `config/models.local.toml` (a Hugging Face ID or a local path). That file requires a `license` field, and the app warns at start when it is `unknown`. Responsibility is entirely the user's. |

**Never, in any tier:**
- commit, mirror or attach weights;
- upload conversions (MLX, GGUF, Core ML) of opt-in or restricted models under the project's org;
- bundle weights in installers or Docker images;
- make a non-default model the default;
- strip licence notices;
- ship the project's own Hugging Face tokens: gated models use the user's own login
  (`hf auth login`).

### Model registry rules (`models.toml`)

- **Every entry has:** id, upstream URL, **pinned revision SHA** (a sha256 for a direct
  download), SPDX id or `LicenseRef-*`, licence URL, tier, attribution text, gated yes/no,
  languages, data notes.
- An entry needs a licence that fits one of the tiers above; anything else is bring-your-own.
  The commit checks block a new or re-pinned model unless this file changes in the same commit.
- **Conversions inherit the upstream licence** (mlx-community, GGUF, Core ML). Don't trust a
  converter's metadata.
- **Re-check the licence on every version bump.**

### Online model catalogue

The page (Settings → Languages and models) can search Hugging Face for MLX models to add
(`catalog/models.py`). Searching takes a click on the machine itself; LAN viewers can't trigger
it. It sends anonymous GETs to the public API, with no token and a generic User-Agent. The rules
match the registry's:

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
  Adding takes only the repository and the commit from the page: the server reads the card
  at that commit again (and the base model's) and takes the licence, and so the tier, from
  there, never from the request.
  Opt-in and restricted entries need the same licence acceptance as registry models before a
  download.
- **Weights still come only from the rights holder's repository**, through
  `wavescribe models fetch`. Nothing is mirrored or re-hosted.

### Disclosure to users

- `wavescribe models list` prints every model with its licence and install state, and notes a
  licence that needs acceptance (and whether it is accepted), a restricted one ("non-commercial
  use only") and the user's own models; `--json` gives every model's tier and licence URL too.
  External programs are listed in this file.
- `wavescribe models fetch` writes `data/THIRD_PARTY_MODELS.md`, the attribution, licence and
  source of every installed model, and `wavescribe models credits` prints it.
- Settings → Languages and models shows every model's licence (linked to its text), tier,
  caveats and attribution, and asks for an opt-in or restricted licence to be accepted before a
  download.
- While a restricted model is in use, `listen` and `serve` print "⚠ non-commercial models
  active" and the page's Settings say "Non-commercial use only" with its name.
- The page links the notices of the packages it bundles ("Licences of the page's parts").
- This file and [MODELS.md](MODELS.md) say that macOS `say` voices are for **local personal
  playback only**.

### Hygiene

- PEP 639 `license = "Apache-2.0"` in `pyproject.toml`, and SPDX headers in the Python sources
  and scripts. The page's sources (`frontend/`) carry no SPDX headers; the repository's
  `LICENSE` covers them.
- **Every commit is checked** by `scripts/commit_checks.py` (git hooks, installed with
  `python3 scripts/commit_checks.py install`; see CONTRIBUTING.md). It blocks a commit that adds
  a direct dependency with no entry here, a transitive package whose licence isn't tier A and
  isn't recorded here, or a new or re-pinned model without this file updated in the same commit.
  CI runs the same licence check over everything a push or pull request brings
  (`check --base <rev>`), so a commit made without the hook is caught there.
- **Third-party telemetry off.** Importing Wavescribe sets two switches before any library
  loads, so every process has them (`src/wavescribe/__init__.py`; `tests/test_no_telemetry.py`
  holds it):
  - **huggingface-hub** (and transformers, which uses it) can send usage telemetry;
    `HF_HUB_DISABLE_TELEMETRY=1` switches it off.
  - **onnxruntime**'s macOS build contains Microsoft's 1DS telemetry SDK (endpoint
    `mobile.events.data.microsoft.com/OneCollector`), which creates a store under
    `~/Library/Application Support/Microsoft/DeveloperTools/.onnxruntime` on its first session.
    Every process sets `ORT_DISABLE_TELEMETRY=1`, which onnxruntime reads when it loads;
    `onnxruntime.disable_telemetry_events()` alone doesn't stop the store from being created.

## Python dependencies

uv installs these from PyPI, in the versions `uv.lock` pins. None ships with Wavescribe.

| Package | What it is | Licence | How Wavescribe uses it |
|---|---|---|---|
| CPython 3.12 (python-build-standalone) | The Python interpreter | Python-2.0, with the licences of the libraries it includes (OpenSSL: Apache-2.0; SQLite: public domain) | Runs Wavescribe; installed by uv |
| SQLite (+ FTS5, Python's `sqlite3`) | Embedded database | Public domain (SPDX `blessing`) | Imported, as part of Python: the store. The trigram tokenizer needs SQLite ≥ 3.34 (`remove_diacritics` ≥ 3.45) |
| numpy | Arrays and maths | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | Imported |
| scipy | Scientific computing | BSD-3-Clause; the macOS wheel also contains the GCC runtime libraries (GPL-3.0-or-later WITH GCC-exception-3.1) and libquadmath (LGPL-2.1-or-later), which only `scipy.odr` loads | Imported: `scipy.signal` (the SDR demodulators' filters and resampling) and `scipy.ndimage`. `scipy.odr` is never loaded (`tests/test_in_process_licences.py`) |
| onnxruntime | ONNX model runtime | MIT | Imported: runs Silero VAD on the CPU, with its telemetry switched off ([Hygiene](#hygiene)) |
| ai-edge-litert (LiteRT) | TensorFlow Lite runtime | Apache-2.0 | Imported: runs YAMNet for the speech/music gate on the CPU |
| parakeet-mlx | Parakeet speech recognition on MLX | Apache-2.0 | Imported: runs Parakeet. Pulls in librosa, typer and dacite |
| mlx-audio | Speech models on MLX | MIT | Imported: runs Whisper, Voxtral, Qwen3-ASR and Nemotron. Pulls in miniaudio, sounddevice and zstandard |
| MLX, mlx-lm | Apple's array framework, and its language-model runner | MIT | Imported: speech and translation models on the GPU |
| CTranslate2 | Inference engine for translation models | MIT | Imported: runs MADLAD-400 on the CPU |
| sentencepiece | Tokeniser | Apache-2.0 | Imported: MADLAD-400's tokeniser |
| huggingface-hub (+ hf-xet) | Hugging Face client | Apache-2.0 | Imported: `wavescribe models fetch` downloads models with it. Every other command runs it offline (`HF_HUB_OFFLINE=1`), to find downloaded models in its cache and, in `wavescribe models remove`, to delete them. Its telemetry is switched off ([Hygiene](#hygiene)) |
| truststore | The operating system's certificate store for TLS | MIT | Imported: the downloads Wavescribe makes itself (URL models, the station and model catalogues, playlists) check the server's certificate against the system's trust store. It bundles no certificates |
| Babel | CLDR locale data | BSD-3-Clause | Imported: language names for LLM prompts, from the data in the package |
| py3langid (with its bundled langid.py model) | Text language identification | BSD-3-Clause | Imported: the ASR checks and alert translations |
| snowballstemmer | Stemmers | BSD-3-Clause | Imported: per-language stemming for keyword rules |
| hatchling | Python build backend | MIT | Builds the package; not used at runtime |
| pytest, ruff | Test runner; linter and formatter | MIT / MIT | Development only |

### Transitive Python dependencies

Every other package in `uv.lock` has a tier-A licence. These are the ones to know about:

| Package | Pulled in by | Licence | How Wavescribe uses it |
|---|---|---|---|
| librosa | parakeet-mlx | ISC | Imported by parakeet-mlx for its mel filterbank |
| soundfile (bundles libsndfile) | librosa | BSD-3-Clause; the bundled libsndfile is LGPL-2.1-or-later | Installed, never loaded (`tests/test_in_process_licences.py`) |
| soxr (python-soxr, bundles libsoxr) | librosa | LGPL-2.1-or-later | Installed, never loaded (the same test) |
| numba, llvmlite | librosa | numba: BSD-2-Clause; llvmlite: BSD-2-Clause AND Apache-2.0 WITH LLVM-exception | Imported by librosa |
| scikit-learn (bundles libomp), joblib, pooch | librosa | BSD-3-Clause; libomp: Apache-2.0 WITH LLVM-exception | Installed with librosa |
| transformers | mlx-lm, mlx-audio | Apache-2.0 | Imported by mlx-lm and mlx-audio |
| regex | transformers | Apache-2.0 AND CNRI-Python | Imported by transformers |
| certifi, tqdm | requests (through pooch), huggingface-hub | certifi: MPL-2.0; tqdm: MPL-2.0 AND MIT | Installed unmodified |
| protobuf, flatbuffers | onnxruntime, ai-edge-litert, mlx-lm | BSD-3-Clause / Apache-2.0 | Imported by those packages |
| ml-dtypes | ai-edge-litert | Apache-2.0 (bundles Eigen headers, MPL-2.0) | Imported by ai-edge-litert, unmodified |
| backports.strenum | ai-edge-litert | MIT | Imported by ai-edge-litert |
| miniaudio, zstandard | mlx-audio | MIT / BSD-3-Clause | Installed with mlx-audio |
| sounddevice | mlx-audio | MIT; bundles PortAudio (MIT), and Windows DLLs built with Steinberg's ASIO SDK, whose licence the package doesn't state | Installed, never loaded (`tests/test_in_process_licences.py`); macOS can't load the Windows DLLs |
| colorama | tqdm, typer, pytest (Windows only) | BSD-3-Clause | Not installed on macOS |
| httpx2-jsfetch | httpx2 (Python in a browser only) | BSD-3-Clause | Not installed on macOS |

To list every installed package with its licence:
`uv run --with pip-licenses pip-licenses --order=license`.

## External programs

Wavescribe runs these as separate programs, through pipes and files; it doesn't import, link or
ship them. The user installs them. ffmpeg, the rtl-sdr programs and `meshtastic` are the
external GPL tools: each is optional and detected at runtime.

| Program | What it is | Licence | How Wavescribe uses it |
|---|---|---|---|
| ffmpeg, ffprobe (Homebrew `ffmpeg`) | Audio and video converter | GPL-3.0-or-later (Homebrew builds it with `--enable-gpl --enable-version3`) | Run as a separate program: decodes internet streams (`sources/stream.py`), writes and cuts the speech archive (`store.py`), checks a pasted stream address (`catalog/stations.py`). Without it, internet stations, the speech archive and replay don't work |
| libopus (inside ffmpeg) | Opus audio codec | BSD-3-Clause | Encodes the speech archive, inside the ffmpeg process |
| `rtl_sdr`, `rtl_power`, `rtl_test` (Homebrew `librtlsdr`, Osmocom rtl-sdr) | RTL-SDR driver library and its tools | GPL-2.0-or-later | Run as separate programs: `rtl_sdr` streams samples to the demodulators over a pipe, `rtl_power` sweeps bands for scans (`radio/scan.py`, read as CSV), `rtl_test` lists the dongles. The library itself is never loaded by Wavescribe. Without it, radio receivers don't work |
| `meshtastic` (the Meshtastic® Python command-line program, installed by the user, e.g. `pipx install "meshtastic[cli]"`) | Command-line client for Meshtastic LoRa mesh nodes | GPL-3.0 | Run as a separate program, only for a mesh output (`mesh.py`): `meshtastic [--port … \| --host …] --dest !<node> --sendtext <message> --ack` sends one direct message to one node and waits for its acknowledgement. Never imported; Wavescribe doesn't ship, bundle or install it. Without it, a mesh output reports "not sent" |
| macOS `say` | macOS text to speech | Apple's macOS Software License Agreement (section 2F in macOS Tahoe's covers the system voices) | Run as a separate program: spoken alerts on the Mac's own speakers. The voices are for local personal playback only |
| macOS tools: `osascript`, `ioreg`, `sysctl`, `system_profiler`, `pmset`, `vm_stat`, `defaults`, `launchctl`, `lsof`, `ps` | Part of macOS | Apple's macOS Software License Agreement | Run as separate programs: notifications, USB devices and battery, memory, the locale, the background service, `wavescribe doctor` |

Meshtastic® is a registered trademark of Meshtastic LLC.

## Build and install tools

| Tool | What it is | Licence | How Wavescribe uses it |
|---|---|---|---|
| uv | Python installer and project manager | Apache-2.0 OR MIT | Installed by the user; installs Python and the Python packages |
| Homebrew | macOS package manager | BSD-2-Clause | Installed by the user; installs ffmpeg, librtlsdr, uv and Node.js |
| Node.js, npm | JavaScript runtime and package manager | Node.js: MIT; npm: Artistic-2.0 | Build the page only; running Wavescribe doesn't need them |
| GitHub Actions: `actions/checkout`, `actions/setup-node`, `astral-sh/setup-uv` | CI steps | MIT | Run the checks on GitHub |

## Page dependencies (npm)

The page is React + TypeScript + Vite, built into `src/wavescribe/web/dist/` and committed. Only
what the bundle contains ships; the build tools run on the developer's machine.

| Package | What it is | Licence | How Wavescribe uses it | Ships |
|---|---|---|---|---|
| react, react-dom | UI library | MIT | Bundled in the built page | Yes, with its notice in `licenses.txt` |
| scheduler | React's scheduler (pulled in by react-dom) | MIT | Bundled in the built page | Yes, with its notice in `licenses.txt` |
| lucide-react | Icon set | ISC (some icons derived from Feather: MIT) | Bundled in the built page: only the icons used | Yes, with both notices in `licenses.txt` |
| vite | Build tool | MIT | Builds the page, and writes its module-preload helper into the bundle | The helper, with Vite's notice in `licenses.txt` |
| @vitejs/plugin-react | Vite's React plugin | MIT | Build tool | No |
| typescript | Compiler | Apache-2.0 | Type-checks the page | No |
| @types/react, @types/react-dom | Type definitions | MIT | Type-checking | No |
| lightningcss (+ its platform binary) | CSS minifier | MPL-2.0 | Vite's CSS minifier, run unmodified as a build tool | No |
| the rest of Vite's toolchain (rolldown, postcss, tinyglobby, tslib, …) | Build tools | MIT, ISC, BSD-3-Clause, Apache-2.0, 0BSD | Build tools | No |

`scripts/commit_checks.py` checks every new package in `frontend/package-lock.json`: tier-A
licences pass, anything else needs a row here first.

## Logo and icons

The logo, the wordmark and the page's icons (`assets/`, `frontend/public/`, copied into
`dist/`) are Wavescribe's own work, as are the other files in `assets/` (the README's demo,
made from synthetic test broadcasts, and the social preview). The wordmark and tagline are
outlines drawn from the Inter typeface (OFL-1.1); no font file is included. As the README's
License section says, Wavescribe's licence covers the code and documentation, not the
Wavescribe name or logo.

## Radio data

- **Band plans** (`src/wavescribe/radio/bands.toml`) are written for Wavescribe from public
  allocation facts: band edges, channel rasters and channel lists from the ITU Radio
  Regulations, CEPT decisions, IARU band plans and national rules. No third-party text or table
  is copied.
- **No station list or frequency register is shipped or downloaded.** Wavescribe's list of
  radio stations is built from the user's own scans (`data/radio/heard.json`).
- **RDS** names and texts come from the stations' own broadcasts as received; they are kept
  locally like the transcripts.

## Internet station directory

| Source | What it is | Licence | How Wavescribe uses it |
|---|---|---|---|
| [Radio Browser](https://www.radio-browser.info) | Community directory of internet radio stations, with an HTTP API | Data: public domain (the site: "Data license: public domain"; submissions are released to the public domain) | Queried over HTTP only on the user's explicit actions (catalogue refresh, online search), with a generic User-Agent. A refresh keeps a compact local copy of the directory, which search then reads offline (`catalog/stations.py`). Station names, logos, homepages and streams belong to the stations; Wavescribe stores links only |

## Models

The registry's models (`src/wavescribe/models.toml`). The page shows the default tier as
"permissive".

| Model | Used for | Licence | Tier the app shows | Languages |
|---|---|---|---|---|
| Silero VAD v6 (`silero_vad.onnx`, from the snakers4/silero-vad GitHub tag v6.2.3) | Speech detection | MIT | default | Language-independent |
| YAMNet | Music gate | Apache-2.0 (its embedded AudioSet class names: CC-BY-SA-4.0) | default | Language-independent (521 sound classes) |
| Parakeet-TDT-0.6B-v3 | Speech to text, first in the chain | CC-BY-4.0 | default | 25 European |
| Whisper large-v3-turbo | Speech to text, the chain's fallback | MIT | default | 99 |
| Whisper large-v3 | Speech to text, selectable | Apache-2.0 (its model card; OpenAI's code is MIT) | default | 99 |
| Voxtral Mini 4B Realtime | Speech to text, selectable | Apache-2.0 | default | 13 |
| Qwen3-ASR 1.7B | Speech to text, selectable | Apache-2.0 | default | 30, and 22 Chinese dialects |
| Nemotron 3.5 ASR streaming 0.6B | Speech to text, selectable | OpenMDW-1.1 (`LicenseRef-OpenMDW-1.1`) | default | 34 (40 locales) |
| Qwen3.5-9B | Translation, first in the chain | Apache-2.0 | default | 201 languages and dialects |
| MADLAD-400 3B MT | Translation, second in the chain | Apache-2.0 | default | 419 |

### Conversions

What `wavescribe models fetch` downloads. Each conversion inherits its upstream licence, and the
registry pins its revision. Each entry's attribution text is in `models.toml` and goes into
`THIRD_PARTY_MODELS.md`.

| Conversion | Upstream | Licence |
|---|---|---|
| `mlx-community/parakeet-tdt-0.6b-v3` (MLX) | `nvidia/parakeet-tdt-0.6b-v3` | CC-BY-4.0 |
| `yamnet.tflite` from MediaPipe's model storage (Google's own LiteRT build, pinned by sha256) | YAMNet (tensorflow/models) | Apache-2.0; embedded AudioSet class names CC-BY-SA-4.0 |
| `mlx-community/whisper-large-v3-turbo-asr-fp16` (MLX, with the processor files mlx-audio needs) | `openai/whisper-large-v3-turbo` | MIT |
| `mlx-community/whisper-large-v3-asr-fp16` (MLX, with processor files) | `openai/whisper-large-v3` | Apache-2.0 on the card (the openai/whisper repository says MIT) |
| `mlx-community/Voxtral-Mini-4B-Realtime-2602-fp16` (MLX) | `mistralai/Voxtral-Mini-4B-Realtime-2602` | Apache-2.0 |
| `mlx-community/Qwen3-ASR-1.7B-8bit` (MLX) | `Qwen/Qwen3-ASR-1.7B` | Apache-2.0 |
| `mlx-community/nemotron-3.5-asr-streaming-0.6b` (MLX) | `nvidia/nemotron-3.5-asr-streaming-0.6b` | OpenMDW-1.1; its notices are kept |
| `mlx-community/Qwen3.5-9B-MLX-4bit` (MLX) | `Qwen/Qwen3.5-9B` | Apache-2.0 |
| `Nextcloud-AI/madlad400-3b-mt-ct2-int8` (CTranslate2 int8) | `google/madlad400-3b-mt` | Apache-2.0 |

## Where the licences are read

- Python packages: the metadata (`License-Expression`, `License`, classifiers) and licence files
  of the locked versions.
- npm packages: the `license` fields in `frontend/package-lock.json` and each package's licence
  file.
- Homebrew programs: the formula's licence and, for ffmpeg, its build configuration
  ([Homebrew ffmpeg formula](https://formulae.brew.sh/formula/ffmpeg)).
- Models: the model card and licence at the pinned revision (`license_url` in `models.toml`).
- Licences named in the tiers: [Gemma Terms of Use](https://ai.google.dev/gemma/terms),
  [NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/),
  [OpenMDW-1.1](https://openmdw.ai/license/1-1),
  [macOS Software License Agreements](https://www.apple.com/legal/sla/).
