# Models per capability and per language

Every model listed here has a licence that passed [LICENSING.md](LICENSING.md). The real
selection lives in a `models.toml` registry: pinned revisions, licence fields and language lists.
This page explains the defaults.

## How a language gets served

1. A station declares its source language (BCP-47, e.g. `pt-PT`).
2. The registry (`registry.py`) picks the **first model in the ASR chain** below that supports that language and is installed.
3. Keyword matching folds case and accents the same way for every script, and stems with Snowball where it has the language; scripts written without spaces match as substrings ([ARCHITECTURE.md](ARCHITECTURE.md), *Alerts*).
4. Keyword packs come from `config/keywords/<lang>.toml`. A language without a pack still gets the LLM rules, which are language-agnostic.
5. Translation goes to the primary language continuously and to secondary languages for alerts.
6. TTS uses the voice configured for each target language.

## Speech recognition chain (default order)

| Order | Model | Licence | Languages | Why it's here | Runtime |
|---|---|---|---|---|---|
| 1 | **Parakeet-TDT-0.6B-v3** | CC-BY-4.0 | most European languages | Fast and accurate. The European Portuguese training data suits Portuguese stations. | parakeet-mlx |
| 2 | **Whisper large-v3-turbo** | MIT | about 100 | The fallback for every language Parakeet doesn't cover | mlx-audio |

Also registered, used only when chosen for a station or put in the chain under Settings → Languages and models:
Whisper large-v3 (Apache-2.0), Nemotron 3.5 ASR streaming 0.6B (OpenMDW-1.1), Qwen3-ASR 1.7B
(Apache-2.0, strong on Asian languages and Arabic) and Voxtral Mini 4B Realtime (Apache-2.0), all
through mlx-audio. Omnilingual ASR (Apache-2.0, 1,600+ languages) would cover the long tail; it
isn't registered (PyTorch, untested on macOS).

The pt-PT bake-off measured speed and failure modes ([MEASUREMENTS.md](MEASUREMENTS.md)); WER
waits for reference transcripts. Whisper large-v3-turbo is the chain's fallback for languages
Parakeet doesn't cover (Hebrew in the world stations).
The order for other languages should be decided by their own bake-offs, recorded here.
To run one: `uv run wavescribe record <station id> --minutes 15` saves a clip of an internet
station under `data/bakeoff/` (broadcast audio is copyrighted: keep it to yourself), and
`uv run wavescribe bakeoff data/bakeoff/*.wav --models parakeet-tdt-0.6b-v3,whisper-large-v3-turbo
--language es-ES` runs each model over the same utterances and reports their speed and how far
they agree, and the word error rate once a speaker of the language has corrected the reference
file it writes.

**Language identification:** Whisper's built-in language ID identifies a new stream's language
when a station is added by its address, if **Also identify the language** is ticked beside
**Check** (it listens for 20 seconds; off by default). It also gives a second opinion on lines the ASR checks
flagged as another language than the station's (`asr/recheck.py`): Parakeet can't be told the
language and sometimes renders the station's speech in English. Whisper's language ID (one
encoder pass and one decoder step, `MlxAudioTranscriber.identify_language`) listens to the
line again; if it hears the station's language, Whisper transcribes it with that language
given and its text replaces the flagged one when it passes the checks. Any installed Whisper
that covers the station's language does it, the ASR chain's first (large-v3-turbo by default);
none is downloaded for it, and without one a log line at start names the model to fetch. The
Whisper stays loaded while the stations it serves run (about 1.6 GB). Cost: see
[MEASUREMENTS.md](MEASUREMENTS.md) (to be measured).

## Translation

| Role | Model | Licence | Languages | Runtime |
|---|---|---|---|---|
| **Continuous, alerts, secondary languages, LLM rules and questions** | **Qwen3.5 9B** 4-bit | Apache-2.0 | 201 | mlx-lm (GPU). Beat MADLAD on speed and quality ([MEASUREMENTS.md](MEASUREMENTS.md)) |
| **CPU fallback** | **MADLAD-400 3B** int8 | Apache-2.0 | 419 | CTranslate2 (CPU). Unsafe for Ukrainian: it invented content |

Not registered, but candidates: TranslateGemma 4B/12B/27B (opt-in: Gemma Terms of Use), Opus-MT / Tatoeba (CC-BY-4.0 or Apache-2.0, small per-pair
models for CTranslate2) and EuroLLM-9B-Instruct (Apache-2.0, 35 languages, mlx-lm).

NLLB-200 and SeamlessM4T are **restricted** (non-commercial; see below). Hunyuan-MT is not listed (its licence excludes the EU).

## Embeddings

Not used: search is FTS5 trigram matching, and rules are keywords or LLM checks.

## Speech vs music

**Silero VAD finds speech, then YAMNet checks each utterance** (`gate/yamnet.py`). Silero treats
singing as speech, so without the gate every song reaches ASR and comes back as lyrics or
invented text (one funk track came back partly as made-up Cyrillic).

| Option | Result (2026-11-22) |
|---|---|
| **YAMNet** (Google, Apache-2.0), MediaPipe's LiteRT build | **Chosen.** 4 MB model, ~60 MB install with LiteRT, 0.16 s of one CPU core for 3 minutes of audio, 80 MB RAM |
| inaSpeechSegmenter (MIT) | Dropped: 0.7.6 breaks on NumPy 2 (twice) and its Keras 2 model won't load in Keras 3; TensorFlow is a 1.6 GB install |
| Apple SoundAnalysis | Not needed so far; it could take over on battery if power measurements call for it |
| PANNs (CC-BY-4.0 weights) | Heavier (torch); not tried |

**Measurements** on 14 minutes from four Portuguese stations (news, a documentary over a music
bed, fado, funk and pop), YAMNet's scores per 0.975 s window, checked against ASR text per
5 s block:
- Inside songs, **no window scored Speech above 0.02**, and Music was 0.55–0.99, sung lyrics
  included.
- Talk, even over a music bed or jingles, always had windows with Speech ≥ 0.17, though
  Music sometimes outscored it.
- The "Singing" class almost never fired, so the gate doesn't use it.

**Rule** (`gate.decide`): an utterance is music when **no window reaches Speech 0.1 and mean Music
≥ 0.3**. A DJ talking for one second over a song keeps the whole utterance, because a missed
announcement costs more than a transcribed chorus. Synthetic tests: a Karplus–Strong plucked
melody → music (0.97); `say` speech → speech; speech mixed over the melody → speech.

**Live** (Antena 1, 4 minutes of a 1950s rock'n'roll programme): 9 song fragments dropped
(19 s) and about 4 short lyric lines leaked. Old mono recordings with dry vocals look like
speech to YAMNet; the hallucination filter is the second line of defence. Talk was never
dropped. Every verdict is logged (`gated`; passes at debug level), so a long run can be used to
retune the thresholds.

## Text language identification (ASR checks)

**py3langid** (BSD-3-Clause, 97 languages, a 4.4 MB model, numpy only) flags transcripts that
are clearly not in the station's language. lingua (Apache-2.0) was compared: similar on long
text, better on some short Spanish lines, but 305 MB. On 101 real transcripts from Portuguese
radio, every Portuguese sentence of 20+ characters scored p(pt) ≥ 0.14, and lyrics and English
clips mostly ≤ 0.013, so the flag fires below 0.05. Shorter texts get no verdict ("Foi preso."
reads as Romanian). Languages py3langid doesn't know get no verdict either.

**Alert translations.** On a synthetic civil-protection bulletin, Qwen3.5-9B's English and
Ukrainian were accurate except for one idiom: *Última hora* ("breaking news") came out as "Last
hour" / "Остання година". A prompt line asking to translate idioms by meaning made it worse
("Final bulletin", and the Ukrainian changed a present-tense water cut into a future one), so the
prompt stays as it is.

## TTS for spoken alerts

| Option | Licence | Notes |
|---|---|---|
| macOS `say` | Apple macOS licence | Default on macOS. Voices for many languages (download them ahead of time). **Local playback only**: never record or rebroadcast it. Voices include Daniel (en-GB), Anna (de-DE) and Mónica (es-ES). `say -v <missing voice>` silently falls back to the default voice, so Wavescribe checks the voice list first. |
| Piper | GPL-3.0 (subprocess only) | Cross-platform. **Check each voice's licence.** Some voices are research-only. |

## Opt-in and restricted models

None is registered today; the tiers and the licence gate are there for when one earns a place. Such models are never the default. The user unlocks them per model with `wavescribe models fetch <id> --accept-license <license-id>` (or `wavescribe models accept <id> --license <license-id>` for weights already on disk); restricted models also need `--non-commercial`. Until then, chains skip them. `wavescribe models list` shows which are accepted.

| Model | Tier | Licence | Why someone might want it |
|---|---|---|---|
| TranslateGemma 4B/12B/27B | opt-in | Gemma Terms of Use | Possibly better MT quality (to be measured against MADLAD/Qwen3.5) |
| Gemma 3 / 3n | opt-in | Gemma Terms of Use | 140+ languages, small sizes |
| NLLB-200 (600M–3.3B) | **restricted ⚠ NC** | CC-BY-NC-4.0 | Direct translation between 200 languages, no pivot |
| SeamlessM4T v2 | **restricted ⚠ NC** | CC-BY-NC-4.0 | Speech → text translation for ~100 languages |
| Meta MMS (ASR + LID) | **restricted ⚠ NC** | CC-BY-NC-4.0 | 1,100+ languages, though Omnilingual ASR (Apache-2.0) covers this now |

## Will it fit? (Settings → Languages and models)

That page marks each model **fits**, **tight** or **won't fit** on the machine the app runs
on (`catalog/models.py`):

- **Usable memory** is unified memory minus 6 GB for macOS and other apps, capped at Metal's
  working-set limit (`recommendedMaxWorkingSetSize`, measured through MLX: 51.8 GB on the 64 GB
  M1 Max).
- **Each model's need** is its weights times an overhead. The weights come from the files on
  disk, or from the repository's file listing before a download.

  | Kind of model | Need |
  |---|---|
  | ASR | weights × 1.2 |
  | CTranslate2 translator | weights × 1.3 |
  | VAD, music gate | weights × 1.5 |
  | LLM | weights × 1.1, plus a KV cache for 8k tokens estimated from its size |

- **Fit:** the models already loaded are counted too. Above 70% of what is left, a model is
  "tight".
- **Speed:** an LLM's tokens per second can't exceed memory bandwidth ÷ weights (Apple's figure
  for the chip). The page shows that as an upper bound, not a measurement.
- **Recommendations:** the chain for each station language, filtered to models that fit.

"Tested" means measured on this project's M1 Max (the results above);
catalogue models are always untested.

## Bring your own model

To use a model that isn't in the registry (no licence, a private fine-tune, an excluded territory), add it to the gitignored
`config/models.local.toml` (see `config/models.local.example.toml`). The engine loads it if a
backend supports its format. These models (tier `byo`) never join the chains: name one with
`--asr <id>` or `--translator <id>`. The app warns about unknown licences and unpinned
revisions, and complying with the licence is your responsibility.

## Languages used so far

Portuguese stations (pt-PT) and world news stations in several languages, translated into
English and other languages.
- **ASR:** Parakeet v3 (fastest), Whisper large-v3-turbo for the languages Parakeet lacks
- **Translation, continuous and for alerts:** Qwen3.5 9B
- **Spoken alerts:** `say` voices
