# Measurements

Dated results behind the decisions in [ARCHITECTURE.md](ARCHITECTURE.md) and the model choices in
[MODELS.md](MODELS.md). Unless a result says otherwise, it was measured on a MacBook Pro with an
M1 Max and 64 GB of memory. Logs come from `data/logs/wavescribe.jsonl`. Reception on air is in
[HARDWARE.md](HARDWARE.md#what-a-first-scan-finds).

**Not measured: the Whisper re-check of lines heard in another language** (`asr/recheck.py`).
There are no figures here for the time one re-check takes (language ID only when Whisper hears
another language, language ID plus a transcription when it hears the station's), the time to
load Whisper at start, its memory in use, or its share of the GPU (`asr_busy`) at the rate
lines are flagged, which was 12 of 101 transcripts on talk radio, lyrics included.

**2026-11-26, Ask on the real store** (Qwen3.5-9B 4-bit, M1 Max; ~20k utterances): the plan
takes 4–6 s (≈1,100 prompt tokens at ~380 tokens/s, ≈90 output tokens at ~60 tokens/s), the
search under 0.1 s, the answer 1–6 s depending on the excerpts (≈2,000 prompt tokens for 40
translated excerpts). Whole answers in 5–11 s. With `serve` also translating, the
two compete for the GPU: generation fell to 22–28 tokens/s in a side test that loaded a second
copy of the model.

**2026-11-26, NFM, AM and RDS on synthetic signals:** the NFM
squelch opens at 4–5 dB in-channel SNR in both 12.5 and 25 kHz channels and is silent within
17 ms of the carrier ending; audio from 300 to 3000 Hz follows the 750 µs de-emphasis within
+0/−2 dB; SINAD at 30 dB SNR is 36.7 dB; CTCSS tones from 67 to 254.1 Hz end up at least 44 dB
under the audio. The DC spike at the dongle's centre holds a squelch open up to 0.55 channel
widths away. RDS decodes 97% of groups at 0 dB SNR in the 4.8 kHz RDS band, half at −5 dB.
About 2% of an M1 Max core per narrowband channel (NFM 2.05%, AM 2.2%).

**2026-11-25, WFM demodulator on synthetic IQ** (2.4 MS/s, 8-bit quantised like an RTL-SDR,
the station 300 kHz off centre, a 1 kHz tone at 50 kHz deviation): SINAD 59 dB with noise 22 dB
below the carrier; a neighbour 20 dB stronger at ±200 or +400 kHz doesn't leak in (SINAD > 85 dB
without noise); output identical however the IQ is chunked; ~6% of one M1 Max core per station.
Portuguese TTS speech, FM-modulated with pre-emphasis and fed through a fake `rtl_sdr`, comes out
of Parakeet and Qwen3.5 intact (`tests/test_sdr_source.py`).

**2026-11-09, two stations for 30 minutes** (Antena 1 + TSF internet streams on the M1 Max:
Parakeet-TDT-0.6B-v3 on MLX, MADLAD-400 3B int8 on 4 CPU threads, beam 2):

| Metric | Result |
|---|---|
| Utterances | 286 (56 min of speech from 2 × 30 min of audio); none dropped, ASR queue never above 0 |
| End of speech → English printed | median 3.6 s, p90 7.1 s, max 15.7 s |
| Falling behind? | No: median latency 3.8 s in the first third, 3.6 s in the last |
| ASR | median 0.30 s per utterance, RTF 0.03; busy 5% of wall time |
| MT | median 2.6 s per utterance; **busy 44% of wall time** |
| Memory | peak RSS 5.4 GB, flat from minute 1 to minute 30 |
| Dropouts | one TLS drop on Antena 1, reconnected after 1 s |

MT is the capacity limit: at ~22% per busy news station, four stations would put MADLAD near 90%,
with no headroom.

**2026-11-09, language-agnostic check.** `config/stations.es.example.toml` (RNE Radio 5, es-ES)
ran through the same code with no changes: Parakeet transcribed Spanish and MADLAD translated it
to English.

**2026-11-10, ASR bake-off on European Portuguese.** `wavescribe bakeoff` on 30 min of
Antena 1 + TSF recorded locally (not committed): 156 utterances, 27.8 min of speech, cut with the
live VAD settings. All models on MLX on the M1 Max.

| Model | Speed | Language hint | Into English | Repetition loops | Empty | Agreement with Whisper large-v3 |
|---|---|---|---|---|---|---|
| Parakeet-TDT-0.6B-v3 | **63× real time** (RTF 0.016) | no | **5** | 0 | 3 | 12.4% apart |
| Whisper large-v3-turbo | 18× (0.055) | yes | 1 | 0 | 0 | 5.7% apart |
| Whisper large-v3 | 11× (0.093) | yes | 2 | 0 | 3 | — |
| Nemotron 3.5 streaming 0.6B | 15× (0.067) | yes (`pt-PT`) | 1 | 0 | 14 | 15.6% apart |
| Qwen3-ASR 1.7B (8-bit) | 10× (0.097) | auto | 4 | **1** (78 s, 24k chars) | 2 | 15.4% apart (excluding the loop) |
| Voxtral Mini 4B Realtime (fp16) | **0.9× (slower than real time)** | auto | — | — | — | stopped after 50 utterances |

- **No word error rate.** That needs reference transcripts corrected by a Portuguese speaker (`*.ref-template.txt` from the bake-off). Agreement between models is not accuracy.
- **Parakeet's failure mode is the dangerous one.** On 5 of 156 utterances it answers in English, producing garbage such as "nuclear sabotage" for a sentence about cured ham, and *sabotagem* is in the Portuguese keyword pack. It can't be given a language hint. Parakeet is the default for speed and power (GPU 5% busy for two stations); how its accuracy compares with the others' wasn't measured. The ASR checks flag text that isn't in the station's language, and flagged text never raises an alert above `low`. A flagged line is re-checked by Whisper large-v3-turbo, which accepts a hint and drifted least, when it is installed (`asr/recheck.py`; its cost wasn't measured, as the top of this page says).
- **LLM-style ASR decoders can loop.** Qwen3-ASR repeated "há há há…" for 78 s on one utterance. Any such model needs an output cap tied to the audio length, plus the repeated-n-gram filter.
- **Nemotron is conservative.** Its 14 empty outputs are mostly jingles and fragments that the others turned into junk ("Yeah.", "Wolf."), plus a few short real phrases.
- **Voxtral is slower than real time** through mlx-audio at fp16.

**2026-11-10, translation: MADLAD-400 vs Qwen3.5.** The same 30 live pt-PT transcripts, then a
4-minute live two-station run with each:

| | MADLAD-400 3B int8 (CPU, 4 threads, beam 2) | Qwen3.5-9B 4-bit (GPU, mlx-lm, greedy) |
|---|---|---|
| Per utterance (median / p90) | 2.9 s / 4.6 s | **1.3 s / 1.9 s** |
| Live, end of speech → English (median / p90) | 3.6 s / 7.1 s | **1.7 s / 3.2 s** |
| Translation busy, two stations | 44% | **15%** |
| ASR time per utterance while translating | 0.30 s | 0.22 s (no contention) |
| Peak RSS | 5.4 GB | 5.8 GB |
| Into Ukrainian | **invents content** | correct, once the prompt names the language |

Qwen3.5's English is usually more natural, and it repairs ASR errors ("Estrela Amadoura" →
"Estrela Amadora"; a garbled "contradions for baralhar" → "contradicts attempts to confuse"). It
slips too: one duplicated word, an untranslated *duende*, and "at the bottom" for *no fundo*,
where MADLAD's "deep down" was right. **The prompt must name languages.** Asked for "uk", Qwen
answered in English (UK as in United Kingdom), so `language_name()` takes English names from
CLDR via Babel. **Default:** Qwen3.5-9B is the default translator; MADLAD stays in the chain as
the CPU fallback. Power on battery wasn't measured.

**ASR language confusion.** Parakeet v3 picks the language itself and can't be told which one to
use. On TSF it occasionally emitted English for Portuguese speech, and MADLAD then paraphrased
that English into a different English sentence. The ASR checks flag such text
(`lang:en` on a pt-PT station). On 101 real transcripts the flag
caught 12 lines, all genuinely foreign (Spanish and English lyrics, and one Spanish speaker that
Parakeet rendered in English), and no Portuguese.

**2026-11-22/23, soak: four stations, 34 h.** `wavescribe serve` on the M1 Max with the
Antena 1, TSF, Renascença and Observador streams (Silero, YAMNet, Parakeet, Qwen3.5-9B translating
everything to English, log-only alerts), sampled every 10 minutes by `scripts/soak.py` (141
samples; the CSV stays in `data/soak/`).

| Metric | Result |
|---|---|
| Awake | 23.3 h without a break (01:07 → 00:25), then the lid was closed and the Mac slept until 10:37: `caffeinate` prevents idle sleep, not clamshell sleep. Stopped at 34.3 h |
| Memory | footprint 6.57 GB at the start, 6.58 GB at the end; peaks up to 8.5 GB while translating were gone by the next sample. Threads 35–38, open files 88–96: no leak |
| Load | translation busy median 28% (max 48%) for four stations, ASR median 6% (max 14%); translation queue ≤ 3, nothing dropped |
| Disk | store +31 MB a day (text and FTS indexes), so ~11 GB at the 365-day text limit; speech audio ~23 MB an hour, so the 2 GB cap holds ~3.5 days and the 7-day limit never applies |
| Streams | 23 reconnects in 23 h awake, all recovered. Each wake during sleep cost a burst of reconnect attempts (80 in all); all four streams came back on full wake |
| Utterances | 19,463 stored, 3 dropped by the hallucination filter |
| Alerts | 111 under the first rules, 25 of them critical, so they would have been spoken aloud. All false |

- **False alarms.** `alerts replay` reproduced the live run (25 critical in both). Snowball made
  "tornado" match *torna/tornou* ("becomes"), "água" match *águias* (a football nickname) and
  "confinamento" match a tyre brand. "abrigo" was mostly *sem abrigo* (homeless) and *ao abrigo
  de* ("under"). A bare "Proteção Civil" is everyday national news (a new law, a house fire far
  away), and bare "ataque" is football. With `=` terms, weak topics for everyday words and
  `min_mentions`, a replay over everything stored by then (35.6 h: the soak and the test runs
  before it) gave nothing high or critical, and 6 medium (about 4
  silent notifications a day, each a common word next to a broad local place name, or a foreign
  story mentioned twice). Synthetic emergency bulletins (a blackout, a declared calamity,
  boil-water notices) still escalate (`tests/test_replay.py`). Keywords won't reach zero on talk
  radio. This run had neither the LLM check on keyword hits (`verify`) nor LLM rules.
- **Running unattended:** the Mac must not sleep with the lid closed (`sudo pmset -a disablesleep 1`,
  or keep the lid open), and the audio cap should grow to ~4 GB if a week of replayable speech
  is wanted.

**2026-11-09, MADLAD-400 first results.** MADLAD-400 3B int8 (CTranslate2, 4 CPU threads, beam
2) translates pt-PT → en well on synthetic and live speech: 2–5 s per 12–19 s utterance (RTF ≈
0.2–0.28 per station). Errors are mostly ASR mishearings carried into the translation. **It must
not be used for Ukrainian as it stands:** translating into it directly produced fluent, invented content at
beam sizes 1, 2 and 4 (a sentence about restoring a town's water came out as an evacuation ending
"by the end of 2014"). Pivoting through English mostly worked, but at beam 2 it answered in the
wrong language. Secondary languages therefore go through the LLM, `models.toml` lists `uk` in
MADLAD's `unsafe_targets`, and every alert translation passes a language check before delivery.
The original bf16 weights (rather than the int8 conversion) are untested.

## What these numbers don't cover

There is no word error rate for any language: that needs reference transcripts corrected by a
native speaker (`wavescribe bakeoff` writes `*.ref-template.txt` files to fill in, and they are
welcome as contributions). Translation quality is judged from spot checks.
