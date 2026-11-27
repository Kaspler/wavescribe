# Architecture

How Wavescribe works today. When the code changes, this file changes in the same commit. Measured
results are in [MEASUREMENTS.md](MEASUREMENTS.md).

## Pipeline

```
          ┌──────────── sources (one per station) ──────────────────────────┐
stream:   ffmpeg HTTP/HLS ─► PCM 16 kHz
radio:    rtl_sdr IQ (one pipe per dongle) ─► per station: WFM (+ RDS), NFM or AM ─► PCM 16 kHz
          └────────────────────────────┬─────────────────────────────────────┘
                                       ▼
                     segmenter: VAD → utterances (start, end, station)
                                       ▼
                     gate: speech or music, per utterance (songs stop here)
                                       ▼
                ┌──── ASR worker (GPU, priority queue) → hallucination filter
                │       (a line in another language: Whisper listens again, after live work)
                ▼
     transcript (source language) ──► rules ──► alert ──► actions (notify, speak, log, webhooks)
                │                                 ▲
                ▼                                 │ checked translations for the alert
     translation worker (primary language live; others for alerts and on demand)
                │
                ▼
     SQLite (FTS5) ◄── web page (local, LAN read-only) / text feed / CLI search
```

## Components

Each interface is a Python protocol with one or more implementations:

| Interface | Responsibility | Implementations |
|---|---|---|
| `AudioSource` | PCM 16 kHz mono chunks with timestamps for one station, and its health (connected, reconnects, last data; for radio also signal level and ADC clipping) | `StreamSource` (an ffmpeg subprocess; it reconnects http(s) streams with a backoff that resets after 30 s of audio, logging one warning per try with ffmpeg's reason, which its health keeps as `error` with what to check until audio comes; it paces streams to real time and reads a file once), `SdrChannelSource` (fed by a shared `RtlSdrDevice`, demodulated in `dsp/`); `open_source()` picks the station's dongle when it has one, else its stream |
| SDR device | Owns one dongle: tuning, manual gain, sample rate; fans IQ out to its channels | `RtlSdrDevice` (an `rtl_sdr` subprocess pipe, shared per serial), `SpectrumMonitor` (a dongle running with no station, for its spectrum) |
| `Gate` | Whether a finished utterance is speech worth transcribing or music | `YamnetGate` (YAMNet on LiteRT, CPU) |
| `Segmenter` | Turns speech into utterances of about 2–20 s | `VadSegmenter` driven by `SileroVad` (Silero's ONNX model on onnxruntime; no torch) |
| `Transcriber` | utterance → text + language + confidence | `ParakeetMlx` (default for European languages), `MlxAudioTranscriber` (Whisper, Qwen3-ASR, Voxtral, Nemotron through mlx-audio) |
| Filter | Checks ASR output before anything else sees it (`check_transcript()` in `asr/checks.py`). **Drops** what can't be speech: over 35 chars/s, a zlib ratio above 2.4, or a phrase repeated 4+ times over 40% of the words with fewer than three other words beside it; and a line that is nothing but what models write over silence and music, subtitle credits and video sign-offs ("Thanks for watching!", "Untertitel im Auftrag des ZDF", "字幕由Amara.org社区提供"), from `asr/junk.toml`, which lists them for many languages and applies every one to every station (ignoring case, accents and punctuation; anything else said keeps the line). A repeated phrase inside a message ("Atención, atención, atención, atención. Evacuen…") is kept and flagged `repetition`. A dropped loop is still read by the keyword rules; a hit is kept as a low alert and never announced. **Flags** text clearly in another language than the station's (py3langid, ≥ 20 characters, p(station language) < 0.05): kept, stored with the flag, shown dimmed. **Re-check** (`asr/recheck.py`): when a Whisper model that covers the station's language is installed and the station isn't transcribed by Whisper already, Whisper's language identification listens to a flagged line's audio again. If it hears the station's language, Whisper transcribes the line in that language and, if its text passes these checks, it replaces the flagged text (model: the Whisper's id, flags: `rechecked`); if it hears another language, the line stays as it was, with `rechecked` added. Re-checks run on the ASR thread behind every live utterance; at most 3 per station and 8 in all wait, one that waited over 60 s is skipped, and a line that finds no room stays flagged (`recheck_skipped` in the log). With no such Whisper installed nothing changes, and one log line at start names the model that would do it. **The alert watchlist never goes into the ASR prompt** (bandwatch's lesson) | — |
| `Translator` | text → text in a target language | `LlmTranslator` (Qwen3.5 through mlx-lm, the default; TranslateGemma opt-in), `MadladTranslator` (MADLAD-400 through CTranslate2, CPU, the fallback) |
| Reply check | Keeps only a reply that translates the line (`check_reply()` in `translate/checks.py`), for station lines and alert translations alike. The prompt gives the model one fixed reply for text it can't translate, `[[untranslatable]]`; a reply that carries it is no translation (`declined`). Shape, in any language: a reply of several lines to a line of one keeps the one line that translates, after a heading ("Here is the translation:") and notes are cut, and is no translation when none or two are left (`remark`); a reply that is only a bracketed note, to a line without brackets, is a `remark`; a reply longer than 3 times its original plus 30 characters (a character of a script written without spaces counting 3) is `too_long`. Wording, only in the languages of `translate/remarks.toml` (German, English, Spanish, French, Portuguese, Ukrainian): a reply that talks about recognition errors, the source text or translating it is a `remark`, unless the line itself says such things; and a bracketed part worded as a note ("[missing word]", "(possibly a misrecognition)") is cut out and the rest kept. Other brackets stay: in real translations they were mostly the speaker's aside, an abbreviation, or a word the translator filled in. A reply that fails is never re-asked or repaired: the line keeps its words alone, with the flag `untranslated:<target>:<why>` (`declined`, `remark`, `too_long`, `empty`, or `failed` when the model raised), and it counts in `dropped` (not as the machine falling behind) | — |
| Rules | Evaluate source-language text and emit hits (`rules/`) | `KeywordRule` in a `RuleSet` (per-language packs or the rule's own terms per language, regular expressions, a station filter, `verify` and `unverified`); `LlmRule` checked by `LlmRuleChecker` (below); `SilenceRule` and `TrafficRule`, about a station rather than its words, checked by `StationWatch` (`alerts/watch.py`) on `Pipeline.status` |
| `Actions` | Deliver an alert (`alerts/actions.py`) | `MacActions`: `notify` (a macOS notification through `osascript`, with the sound `[delivery] notify_sound` names, Glass by default, or none for ""), `speak:<lang>` (macOS `say`, one voice per language), `log` (an `alert` line at info level: an alert is the program working, a warning is something to fix; the console shows it with `-v`); `LogOnlyActions` elsewhere. Webhooks go through `Outputs` |
| `Outputs` | Push events to URLs the user configured (`outputs.py`) | Webhooks and live pushes, filtered by event (alert, utterance), rule, minimum severity and station, with the translations chosen; JSON (`events.py`, the same shape as the pull feed), CAP 1.2 or plain text; utterances batched per `batch_s`; alerts at once with six tries over about a minute, utterances three (network errors, 408/425/429, 5xx), then they wait at the head of the bounded queue and go again every 5 to 60 s until the other end is back; an alert that hasn't got there is also a row in the store's `outbox`, written before the first try and deleted after the delivery, so the next run sends what a restart or crash left (at least once: each alert delivery carries the same random id on every try, `X-Wavescribe-Delivery` and the JSON's `delivery_id`), and it is given up when older than the output's `alert_max_age_hours` (24, at most 168); utterances, and everything under `--no-store`, wait in memory only; an HMAC-SHA256 signature when a secret is set; never a redirect followed (`httpclient.py`, as for remote stations and `follow`: a 3xx is a failed delivery, so headers and signature go nowhere else); one thread and a bounded queue per output; every request logged to the `deliveries` table |
| `Store` | Persistence, search and the speech audio archive | `Store` (`store.py`: SQLite with FTS5 trigram search) and `AudioArchive` (Opus through an ffmpeg subprocess) |
| `Asker` | Answers a question about what was said (`ask.py`) | The translation LLM plans a search (stations, time range, words in the translation and station languages), FTS5 finds excerpts, and the LLM answers in the question's language citing them as [n]. Runs on the translation thread through `Pipeline.request_llm` |
| `Digester` | A few points the local model picked out of what some stations said over a time, or what one is on about now (`digest/`) | Selects what was said (no songs, idents or repeats; a fair share per station), the translation LLM writes points citing [n], at the lowest priority of `Pipeline.request_llm`, giving way between tokens to alerts and questions; `Speaker` reads them aloud through the alerts' `Actions`, stopped at once by an alert or Stop; `Scheduler` makes them on the clock (`[digest]` in `alerts.toml`, or the page's in its place); a digest is cancelled by its request id through the same give-way path |
| `ModelRegistry` | Merges the tracked `models.toml` (default, opt-in and restricted tiers) with the private `config/models.local.toml` (bring-your-own). Downloads only through `models fetch`, with licence acceptance. **Licence gate:** chains skip an opt-in or restricted model with no acceptance record in `data/licenses-accepted.json` (licence id, URL, tier, revision, non-commercial acknowledgement, timestamp), and naming one explicitly (`--asr`, `--translator`) fails with the command that accepts it. A licence change upstream voids the record. Restricted models in use are announced at start and on the page. `models fetch` writes `data/THIRD_PARTY_MODELS.md`. Around each download (`modelfetch.py`): the total and a free-disk check first; the download in a thread of its own, so Ctrl-C and SIGTERM stop the command at once (the library's worker threads can't be interrupted, so the process leaves without them and removes the part-files they were writing); a failure as one sentence. The downloads Wavescribe makes itself check certificates against the system trust store (`httpclient.trust_store`, the truststore package), as the Hugging Face library's client does | `ModelRegistry` (`registry.py`) |
| Languages | A station's BCP-47 code picks its ASR model (the registry's chain), keyword pack (`config/keywords/<lang>.toml`), voice and translation targets; LLM prompts name languages from CLDR (`language_name()` in `languages.py`), never bare codes | — |

## Process model

One process with threads. Each station has a source thread (an ffmpeg subprocess, or a channel
of a dongle's shared `rtl_sdr` pipe, plus the VAD), one ASR thread owns the MLX speech model and
works through a priority queue (station priority, then arrival; Whisper's re-checks of lines heard in another language after all of them), and one translation thread
owns the translator and the LLM jobs (alert translations first, then LLM rules, then questions,
then the continuous backlog). Each thread loads its model itself, because MLX arrays are bound
to the thread that created them. The translation thread waits until the ASR models are loaded:
loading two backends at once raced on shared imports (mlx-audio and mlx-lm both import
transformers). Utterances from a station that is already in every target language skip the
translation queue, and a run in which no station needs translating loads no translator.

**Models load on demand.** The plan resolves an ASR model for every configured
station (a station's own choice from the page, else the first installed model of the chain for
its language) and the translator, but nothing loads until a running station needs it: the
starting stations' ASR models load before their audio is queued, a station started later loads
its model ahead of its first utterance, and the translator loads on the first utterance that
needs translating, the first alert translation, question or LLM rule. One lock serialises every
load, across both threads. The Whisper that re-checks a station's lines heard in another
language loads with the station, before its audio is read, and counts as in use while the
station runs. An ASR model no running station uses is unloaded after five idle
minutes (Whisper alone is ~1.6 GB). Stations can be added, changed and deleted while the
pipeline runs: name, language, priority and translation languages change in place for the next
utterance; a new URL, device or frequency restarts the station's source. Lines already waiting
for translation are translated as the station is when their turn comes (its languages and its
translator), so a language removed meanwhile is left out and the others are kept.

**Per-station switches.** `transcribe = false` makes a station listen-only: its
audio reaches the page, nothing else. It needs no model at all, not even the VAD
(`plan_available` leaves it out, and `Plan.vad` may be `None` on a new install), so a user can
listen before downloading anything. `translate = false` keeps the text and drops the
translations (`translation_targets` is empty; `translate_to` is kept for when it's switched back
on). Both switch per audio chunk in `Pipeline._capture`: going listen-only flushes what was being
said and drops the segmenter, and switching back makes a new one, so the source never restarts.
`mt_model` gives a station its own translator (`Plan.mt_station`, `translator_for()`); the
translation thread loads it on its first job and unloads it after 15 idle minutes, while the
shared translator serves everything else. A station that should be transcribed but
whose models aren't downloaded plays without text meanwhile (`App.listen_only`, with the reason;
the pipeline gets a copy with `transcribe` off); `App.models_changed()`, called when a page
download finishes, re-plans those and switches a running one to transcribing. The page can also
start a station listen-only for one run, without changing its saved mode:
`POST /api/manage/stations/<id>/start` with `{"listen_only": true}` (`App.listen_only_run`, a
copy with `transcribe` off again). It holds through edits, retunes and scans that pause the
station; stopping the station, starting it again without the flag (also while it runs: it
switches back in place), or the next `serve` returns it to its saved mode. The station's
`listen_only_run` in `/api/state` and `/api/manage/stations` says so; `transcribe` stays the
saved mode. A remote station refuses it (it has no audio). A station whose
translator lacks one of its translation languages (MADLAD has a fixed list) runs all the same:
it is written down, alerts on its own words and is translated into the rest, while
`App.untranslated` names what is missing (`App._check_translations()`, after every change of a
station, its translator, the model order or a download). The page gets that as the station's
`reason` and `translate_to_missing`, from startup on. `Pipeline.ingest` takes
a transcript made by another Wavescribe (remote stations) through the same store, rules and page,
translating only into languages it didn't arrive with. `status()` reports the loaded models,
load failures, models that failed 20 calls in a row, why a station is down and when it is tried again, and per-station lag: seconds from the end of an utterance to its text and to its
translations, median and 95th percentile over ten minutes (a stream's own buffering behind the
broadcast comes on top and can't be measured here). A model call that raises costs that one
utterance (counted under `dropped`), never the worker. A station whose capture raises (its source,
its VAD, or loading its VAD or gate) is restarted on its own after a delay that doubles from 1 s
to 60 s, while the other stations go on; until it hears audio again, `status()` shows it as not
connected, with the `error` and `retry_in_s`. onnxruntime, MLX and CTranslate2 release the GIL while they compute. The queues are priority
queues capped at 200 entries, so memory stays bounded and a slow stage never blocks the stream
readers.

**Overload.** Keyword rules run on the ASR thread as soon as a line is written down, so they are
never behind translation. On the shared LLM thread the order is: model changes, alert translations
(0), LLM rule checks and verifications (0.3), questions (0.5), then station lines by station
priority. When a queue is full, more urgent work takes the place of the least urgent station
entry (on the ASR queue: the least important station's newest utterance; on the translation
queue: the least important station's newest line, which is stored untranslated) instead of being
turned away; jobs are never displaced, and a station entry that is no more urgent than everything
queued is the one dropped. A station line that has waited 5 minutes (`MT_STALE_S`) for
translation is skipped: stored and shown untranslated now rather than translated late, since a
late translation only pushes every other line later. A crash of the ASR worker lets the
translation worker finish what is queued before it ends. Every drop is counted: in `dropped`
(totals since start: `asr`, `mt`, `rules`, `filter`), per station over the last ten minutes, and
in the log as one `drop` line per stage and reason at most once a minute (with the count and the
stations), never one per item. `status()` shows it:

- per station, `behind_s: {"text": s|null, "translation": s|null}`: how long its oldest speech
  has waited for text (from when it was cut) and its oldest line for translation (from when its
  text was ready), including the one being worked on; null when nothing waits;
- per station, `dropped: {"asr": n, "mt": n, "rules": n}`: utterances never written down, lines
  left untranslated and LLM rule checks that couldn't run, over the last ten minutes (any reason,
  a model that raised included);
- per watch (`sources/watch.py`), while it has been deaf for 3 s or more (at once for an
  overload on none of its channels), `warning` (a sentence) and
  `deaf: "overload"|"interference"`; and from the moment a transmitter close by overloads its
  receiver until 10 minutes after the overload ended, `overload: {"t": epoch seconds of its last
  moment, "freq_hz": the channel it was caught on, or, on none of the channels, where it was to
  the kHz (null where that isn't clear), "on_channel": true|false (null listening by hand, and
  for a watch overloaded with no one channel standing out though its strongest signal was on
  one, as two transmitters close by at once; `freq_hz` null too),
  "message": "A very strong signal nearby overloaded Receiver A. Move the radio further away, or
  lower the gain in Settings → Receivers."}`, for the page to show in the watch's column and
  under Caught here (a kind of radio that keeps nothing lists no catch there). With
  `on_channel` false, nothing was caught, and `message` and `warning` say so before what to do:
  "It was at about 445.906 MHz, not on one of the channels of PMR446 walkie-talkies (446.00625
  to 446.19375 MHz), so nothing was caught." (`radio/watch.py`, `off_channels`; "It wasn't on
  one of the channels of …" without a frequency; "the channels watched" for a range);
- overall, `load: {"state": "ok"|"behind"|"overloaded", "reason": sentence|null}`. `overloaded`
  when an LLM rule check or an utterance was lost for lack of room in the last ten minutes, text
  is 2 minutes behind, an LLM rule check has waited 2 minutes, translation is 5 minutes behind,
  or lines were skipped as too late; `behind` when text is 30 s behind, translation 2 minutes
  behind, or lines went untranslated because the queue was full. The reason names the worst one,
  for example "Too many stations for this Mac: translation is 6 minutes behind".

Thresholds are constants at the top of `pipeline.py`. Every process runs with `HF_HUB_OFFLINE=1`
except `wavescribe models fetch`; the page's network use is listed under "Network use" below.

**SDR sources.** A `[[device]]` in the stations file names a dongle by USB
serial, with its centre frequency, manual gain and sample rate (a multiple of 240 kHz that the
RTL2832U takes: 0.24, or 0.96 to 3.12 MS/s; 2.4 MS/s by default); a station that sets `device = "a"` is heard on it at whichever of its `fm_mhz` falls in
the band (±1.08 MHz at 2.4 MS/s), and then the SDR is used instead of the stream. `RtlSdrDevice`
runs `rtl_sdr -d <serial>` (GPL, so a subprocess) on a thread in the main process, restarts it with
backoff when it exits or stalls for 5 s (1 s, doubling to 30 s), and hands each 0.256 s block of
IQ to every station on the dongle. What listens on a dongle stays attached while it is gone from
USB, so a station, a watch and the hand tuner carry on by themselves when it comes back
(tests/test_dongle_recovery.py). `radio watch` makes its device with `command_line=True`: a dongle that gives nothing
when first started stops it and ends its stations, with what `rtl_sdr` said (`unopened`), and
`rtl_sdr`'s words go to the log only; the command reads `heard_at` and `reconnects` to say when
the dongle stopped sending, and before it opens anything it asks the USB registry what holds the
dongle (`radio/holders.py`), so a busy receiver is refused at once. Each `SdrChannelSource` demodulates its station in numpy/scipy (`dsp/wfm.py`): mix to
0 Hz → two Kaiser-window FIR stages down to 240 kHz (flat to ±90 kHz, 60 dB down from ±120 kHz, so
a neighbour can't fold in) → phase discriminator → 48 kHz → de-emphasis (`deemphasis_us`, 50 µs
by default, 75 µs in the Americas) → 7 kHz low-pass → 16 kHz. Every stage keeps its state, so the
output doesn't depend on block boundaries; the first block after a restart starts a fresh
demodulator and is marked as a discontinuity. A station whose demodulator or RDS decoder raises
is restarted by the pipeline like any failed source; the dongle and its other stations go on.
Health adds the station's power in dBFS and the
share of 8-bit samples at the ADC's limits (overload), logged as `sdr_level` every 30 s. A
separate process per dongle isn't needed: demodulation costs about 6% of one core per station
([MEASUREMENTS.md](MEASUREMENTS.md)). The rtl tools read `-d` as a device index before a serial (C `strtol`,
base 0), so a serial like the factory `00000001` is replaced by the dongle's index from the USB
listing each time a dongle is opened (`device_arg()`).

**Web page (`wavescribe serve`).** The same pipeline plus a local page, with no
new dependencies: `http.server.ThreadingHTTPServer`, Server-Sent Events for live updates (source
text as soon as ASR finishes, the translation a few seconds later, a re-checked line's new
text under the same key, and a status snapshot every second), and a page with no CDNs or web fonts, so it works offline. The page is built from
`frontend/` (React + TypeScript + Vite) into `web/dist/`, which ships in the package; the server
serves `index.html`, the hashed files under `/assets/` (cached for a year, since their names
change with their content), the icon and the bundle's licence notices, and nothing else.
Stations can be started and stopped at runtime. **Listen** streams the station's decoded 16 kHz
PCM over a plain `fetch`, and the page plays it with the Web Audio API, so you hear exactly what
is transcribed. A first version re-encoded to MP3 for an `<audio>` element, but embedded WebKit
views wouldn't play the endless stream; raw PCM (32 KB/s) avoids the browsers' media players and
needs no encoder. It costs nothing when nobody listens and works unchanged for SDR sources.

The reading view has three parts. **The station list** down the left lists every station like
the panel of a receiver: a lamp (filled green for speech, a ring when quiet, amber for trouble,
an empty socket when off), the name, the language, a status word when the lamp can't say it
(music while the gate drops songs, listening, no speech for N minutes, reconnecting, stream
down), and a row of ticks that follows the audio level. Each row has a menu (show alone,
settings, stop), and the one playing shows a speaker; stations that are off sit below with
Start, and *Edit list* chooses which stations this device shows. Hovering a
station previews its last lines; clicking it (or pressing its number) adds or removes its
column. **Transcripts** fill the
columns below: continuous text per station, as many side by side as the width allows.
Paragraphs break on a 3 s pause or after 45 s, with a small timestamp at the start of each that
replays the audio. A station's text stays grey in the original language until its translation
arrives, and a click shows the original under any paragraph. **Alerts** sit in a rail on the
right on wide screens (1600px and up) and float over the columns on narrower ones while the
reader has them open; a new alert raises a short notice when the rail is closed, and *Show in
the transcript* opens the alert's station at its line (paging back if it is older than what is
loaded) and marks it for a moment. Phones show a list of stations with the last two lines of
each (Start on those that are off), and open one transcript at a time. The design tokens are in `frontend/src/styles/tokens.css`. Continuous text was chosen over a ticker because moving
text at speech pace is hard to read and whatever the reader misses is gone. Each viewer picks a
display language, text size, theme, and which stations to show; these live in that browser
only. A station not translated into the viewer's language falls back to its first
translation, and the settings say so.

`GET /api/state` gives each station with what the page shows of it: besides its name, languages,
mode and whether it runs (`available`, with `reason` when it can't), its radio, so the page
needn't work that out: `device` (its receiver's id; null for a stream or another machine's
station), `watch` (what a watch listens to: the band id, or `[lo_mhz, hi_mhz]` for a range; null
for any other station), `languages` (the languages a watch or a station in language `auto`
transcribes: a watch's own, or those `auto` is narrowed to, `[]` for any Whisper tells; null for
any other station),
`privacy` (its class, radio/bandplan.py), `out_of_reach` (why its receiver doesn't hear it
where that is tuned, the sentence `reason` gives unless the station is switched off; null when
it is heard) and `reach_center_mhz` (the centre that brings it back: the shared planner's
answer for it, `radio/service.py` `reach_centre`, which keeps the receiver's other stations
where a centre can and still answers where none can, leaving the hand tuner aside; null when it
isn't out of reach, is switched off, or no centre hears it). `GET /api/manage/stations` gives each station `out_of_reach` and `reach_center_mhz`
the same way, and so do the stations listed under each receiver in `GET /api/manage/radio`
and `GET /api/manage/receivers`. Starting the station moves its receiver there (unless the
hand tuner has it somewhere that also hears it), asking first when that leaves others out
(*Radio*, below).

It binds to 127.0.0.1 by default. `--lan` binds every interface and prints the LAN addresses for
phones and tablets; requests that don't come from the machine itself are read-only (they can read
and listen but not start or stop stations), and every POST needs an `X-Wavescribe` header, which
a cross-site form can't send. There is no authentication, so use it only on a LAN you trust.
Other machines get at most 64 connections at once. Every request has 30 s to arrive, however
slowly its bytes come (a body gets more by its size), and a read or write that stalls for 30 s
ends the connection, so a stuck or hostile device can't hold the server's threads.
A request that a proxy forwarded (`Forwarded`, `X-Forwarded-For`, `X-Real-IP`, `Via`), or any
request under `serve --behind-proxy`, counts as another machine's, for those limits too (a
proxy on this machine connects from its address). Such a request reads the page's data (what was
said, alerts, catches, audio, live events: every GET under `/api/` but `/api/manage/`, and
`/audio/`) only with a feed token with the `read` scope, or a ticket
made from one (`feed.page_grant`), unless `serve --public-read`; the LAN reads it without one.
Asking the local model (`POST /api/ask`) from another machine needs `serve --allow-remote-ask`:
it spends the one LLM everything else waits for. `serve` prints one line at start saying which
of these holds (`server.access_line`). Every response carries a
Content-Security-Policy (`default-src 'self'`, no framing), `X-Frame-Options: DENY`, `nosniff`
and `Referrer-Policy: no-referrer`.
Before loading models, `serve` checks that no other program holds the port on 127.0.0.1 (macOS
lets a wildcard bind succeed anyway, and "localhost" would then reach the other program).

**History, search and replay.** With the store on (the default), the page opens with the last
300 utterances per station from SQLite, so a restart loses nothing, and each transcript has a
**Load earlier** button that pages back by segment id (`GET /api/history?station=&before_id=`).
**Show in the transcript**, from an alert or a search hit, opens a window around one utterance
(`GET /api/history?station=&around_id=`, `Store.around`): that utterance if it is still kept,
about half the window before it and the rest after, oldest first; near either end of what is
kept, the other side fills the window, and an id that is gone (or another station's) still
places it where that was said. Every `/api/history` request takes `limit`, at most 300 (the
default; 50 for `around_id`), and `around_id` wins over `before_id`. Old text is trimmed from the page only while the reader follows the live end, with a hard cap of
3,000 utterances per station. The search box (`GET /api/search?q=`) finds words in the source text
and in every translation, ignoring case and accents, at the start of a word (`*word` anywhere
inside one, `"a phrase"` in order; below, *Search*), and highlights the matches. A paragraph's timestamp, and each search hit, plays the archived speech
(`GET /api/segments/<id>/audio.pcm`) through the same Web Audio player as **Listen**. Live
payloads carry the stored segment id, so the live stream and history merge into one list on the
page. Each utterance, live or from `/api/history` and `/api/search`, also says where it was
heard: `freq_hz`, the channel a watch caught it on or a radio station's own frequency
(`segments.freq_hz`; null for a stream or another machine's station), and `label`, what the band
plan in effect calls that frequency, with the channel number where the band has a list
(`bandplan.channel_label`: "PMR446 walkie-talkies, channel 1", as a watch's catches name it; null
without a frequency or for one no plan lists), with that band's id (`band`) and the channel
number (`channel`) beside it, so a program shows its own names without reading the label's
words; catches and scan rows carry `channel` beside their `band` and `label` too. `--no-store` falls back to 300 utterances per station in memory, with no search or replay.

**Ask.** Above the search box, a question in any language goes to
`POST /api/ask`, which streams Server-Sent Events back: `plan` (stations, time range, search
words), `sources` (the numbered excerpts), `text` (the answer as the model writes it), then
`done` with timings, or `error`. Another machine may ask only when `serve` runs with
`--allow-remote-ask` (above); without it the answer is 403. Steps (`ask.py`):

1. **Plan.** The translation LLM (Qwen3.5-9B) turns the question into one line of JSON:
   stations, from/to, 3–8 search words, and whether it is an overview with no topic. The prompt
   lists each station with its language and when it was last heard, plus ready-made reference
   times ("30 min ago …", "yesterday …"), because the model got clock arithmetic wrong without
   them. Search words are asked for in the translation languages, since every translated
   utterance is stored in them too, plus the languages of any stations named. A reply cut off
   mid-way is parsed field by field; one that lists (nearly) every station means all of them.
2. **Search.** BM25 over transcripts and translations (`Store.find`) in that range, at most 30
   excerpts; an overview takes 40 evenly spread ones instead (`Store.window`). If nothing turns
   up: the same words over the last 24 h, then a sample of everything said in the range, so the
   answer can say "nothing about X; meanwhile they talked about Y". The page shows which of these
   happened.
3. **Answer.** Excerpts go in as their translation only (half the prompt, so twice as fast; each
   citation on the page shows the original and plays its audio), at most 12,000 characters. The
   model answers in the question's language and cites `[n]`; follow-ups carry the previous two
   turns.

The job waits behind alert translations and runs ahead of the continuous backlog, which pauses
for the few seconds an answer takes. Alerts never wait for it: they don't use translation.

**Digests.** `POST /api/digest` (`wavescribe digest`, the agent tools `digest` and `now`) takes
stations, a time and a language, and answers once the digest is written; it isn't stored. The
time is `minutes` or `since`, and `until` (seconds since 1970, by default now) for a window that
ended earlier: never in the future, and the window no longer than for `minutes` (a day, six
hours for another machine); `now` takes none. Steps (`digest/__init__.py`):

1. **Select.** What was said in the window, as its translation into the reader's language where
   there is one, without songs (flagged as another language), utterances of under three words
   (idents, greetings) and repeats. The stations share about 36,000 characters fairly, at most
   a prompt each (a quiet station leaves what it doesn't need to the others), sampled evenly
   over the window when one said more. Without stations named, those heard in the window,
   watches left out. Another machine's request sees only what may leave this one.
2. **Write.** The excerpts go to the translation LLM in prompts of about 12,000 characters
   (about 10 s each on a recent Mac), each station whole in one of them, and each prompt in
   parts of the window of about 4,000 characters, as many as it fills, rounded: asked for five
   points from fifty excerpts, oldest first, a small model writes about the first stories it
   reads (a real twenty-minute digest of one station had every point in its first seven
   minutes), and reading a prompt in three calls costs about what one does. A part asks for a
   few one-line points (notes) in the reader's language, the most important first, each
   citing the excerpts it rests on as `[n]`; the stations a point names come from those
   excerpts, never from the model's words. It is told to leave out advertising and sponsorship
   and the station's promotion of itself (programmes and when they are on, trailers, jingles,
   contests), which fill much of a station's airtime and read like news to a small model, and
   to answer `NONE` when that is all there was; the choice below is told so too. On real
   transcripts the model still wrote ad notes for a part that held an ad break (it listed every
   excerpt), which the choice then left out. Nothing checks it: no cheap test tells an
   advert from news (the existing ones, idents of under three words and the ASR's junk
   phrases, catch almost none of them). A range (`[1-23]`) is read whole. A point that cites none of them is
   dropped and counted, and so is one that shares no word with any of those it cites (`sourced`:
   folded words of four letters or more, compared by their first six, numbers, and pairs of
   characters in scripts written without spaces; checked only when one of the lines has text in
   the reader's language). Sharing a word let through what a small model most often gets
   wrong: a name from another story or from what it knows, a figure no one said. So every name
   and number a point states (`stated`) must also be in one of the excerpts it keeps, in any
   of its texts or in the station's name and time it was shown with. A name is a word with a
   capital within a sentence or after its first letter (EU), compared as words are; numbers
   are digits, compared by value, "2,300" being 2300 or 2.3 and "2,9" 2.9. Names are checked
   only when one of the lines is in the reader's language (place names change with the
   language), numbers always. Whether a capital makes a name is told from the text, as no code
   knows languages: when over a fifth of the words within the sentences given start with a
   capital (German writes its nouns so), only acronyms count; a point written mostly in
   capitals states no name. Known limits: a detail taken from another story told in the same
   excerpt, a relation turned round, a number in words, and a name whose stem changes when it
   is inflected, which leaves a good point out. A point keeps three of its excerpts at most:
   first those that say what it states, as few as say it all, then those that share a word
   with it, spread over all it cited; two points citing the same excerpts are one. A prompt
   rule asks for one story per point, its details from the excerpts about it. A point
   is cut at a sentence's end to 300 characters, and a reply of `NONE` is never shown: only when
   every part said so does the digest say, in Wavescribe's words, that nothing stood out in
   the lines read. A window that fills one part is the digest, in one call (so is `now`,
   which is about the last excerpts); otherwise a short last call picks the five points that
   matter most, by number, from the notes shown one from each part in turn, the latest part
   first, each with the time of what it rests on, since a small model favours what it reads
   first; of two that matter as much, it is asked for the later. When it picks fewer than
   five, the digest has fewer points: the notes it left out, it left out on purpose (on real
   transcripts, mostly advertising and trailers), so nothing makes the number up. Only a reply
   that names no note is replaced, by the notes in the order shown. A part keeps six notes at
   most, twice those asked for, spread over the model's list from its first to its last: that
   bounds the choice's prompt when a model lists every excerpt, and filters nothing. When a
   model lists more points than asked in a digest of one call, each station's share is spread
   over its list, first and last, never its first few. When the model read a sample, the
   notice says it was spread evenly. Each call logs a line, `digest_call`, with the request's
   id, its `stage` ("part 2 of 3", "choice"), the points the model gave (`given`) and those
   kept (`kept`), those dropped by why (`uncited`: citing nothing given; `no_word`, `no_name`,
   `no_number`: nothing cited says it; `same_sources`: the sources of a point before it;
   `over`: beyond the bound), the span of time the kept ones cite (`cited_since`,
   `cited_until`) and the `seconds` it took; never a word of what was said. It is how a digest
   whose points all come from one end of its window is traced to the model or to the checks.
3. **Queue.** Each call is a job of its own at the lowest priority, behind every station's
   translation, so live translation runs between them; an hour of two busy stations takes
   a minute or two while they are translated. While a call generates, it checks between tokens
   whether a job of priority under 1 waits (an alert's translation, a rule check, a question,
   a model load) and stops, to start again from scratch behind it; after three times the
   digest gives up and says so. Reading a prompt can't be stopped, so an alert's translation
   can still wait that long: about 10 s for a full prompt on a recent Mac. When the queue is
   full, a digest's call is the first thing any other work takes the place of, and it takes
   nobody's; the digest then says that live translation needed the room. A digest waiting,
   however long, is neither a station behind nor dropped work in the status. At most two
   digests are made at once; a digest takes its turn before reading the store, and a third
   is refused at once (503 with `Retry-After`).

**Cancelling and reading again.** Each request has an `id`, the asker's (8 to 64 letters,
digits, `-` and `_`: the page sends a random UUID) or one made for it, which the answer
carries; a second request with the id of one being made is refused (`id_in_use`). The request
blocks until the answer, so the page cancels by the id it chose: `POST
/api/digest/<id>/cancel`, past the same gate as asking, sets the digest's cancel event. That is
the event the give-way path already checks, so no second path stops the model: a call still
queued is skipped when its turn comes, without touching the model; a call being written
raises at the next token, as when it gives way, and isn't queued again, so the model goes back
to live translation at once (a prompt being read still can't be stopped). The waiting request
answers 409 `cancelled`. Another machine cancels only what other machines asked for. The last
eight digests are kept in memory by id (`Digester.kept`), so that the page can have one read
aloud again (`POST /api/digest/speak`) without spending the model; `POST
/api/digest/speech/stop` stops the one being read (`Speech.stop`). Both are controls.

The answer says what became of the digest (`outcome`: `points`, `nothing_said`, `not_read` with
the stations and why in `unread`, `nothing_stood_out` or `unsourced`; with nothing kept from a
station whose songs the gate dropped in the window, `nothing_said` says it played music, from
`Pipeline.music_heard`) and who wrote it from how
much (`notice`: the local model, lines read of those said, the window, and the stretches of
it in which nothing was heard). Those are judged from the store (`Digester.gaps`, `unheard`),
not from when this run started, since a run started a minute ago reads what an earlier one
wrote down. The store keeps no record of when a station was listened to, only the lines
(`Store.heard`), and a stretch without lines is a station playing music as often as Wavescribe
not running: on a day of three talk and news stations, each went 3 to 25 minutes without a
line several times while another was being written down, yet all together never for 4
minutes, apart from a restart. So the lines of every station the asker may see count, whichever
the digest is about, and only a stretch with none from any of them can be one nobody listened
in. It is one when it lasts longer than the shortest limit of the stations heard until it
began; a station's limit (`quiet_limits`) is half as long again as its longest pause of the
last day that another station's lines, one every five minutes at least, show Wavescribe
listened through, ten minutes at least and an hour at most, and thirty minutes for a station
with fewer than twenty such pauses (a new one, or the only one, whose pauses show nothing). A
stretch that no line came before in the hour before the window always is one. This run's
start is used one way only: it has listened since, so a stretch ends there at the latest.
Each is cut to the window and named when it takes over ten minutes of it ("Nothing was heard
between 16:50 and 17:05", "before", "after"; more than three are counted). Known limits: a
restart shorter than the limit isn't named; with one station, an earlier run's hour of music
is; and a stretch of this run in which a station dropped or the machine slept isn't, as
nothing in the store tells it from silence. A record of when each station was listened to
would settle all three. The alerts
announced in the window come with it in the words they were announced in: each incident once,
at the time it was announced (first, or when it got more serious; the first mention of one
whose delivery reached nobody, which the store keeps no time for), about what was said, medium
and above; never one only logged, whose severity's actions tell nobody.

A digest spends the model as a question does, so it passes the same gate (`do_POST`): from
another machine only with `serve --allow-remote-ask`, and through a proxy with a read token
unless `--public-read`. For another machine, a digest covers six hours at most, reads and
counts only words that may leave, and names no station that may not. Reading it aloud is a
control: this machine only, never through a proxy.

Read aloud, a digest takes the alerts' voice for its language and never sounds like an alert:
it opens with a fixed sentence (`summary` in `[speak.<lang>]`, built in for English) saying it
is a Wavescribe summary, of which stations and hours, written by the local model from how many
lines; names the earlier alerts about what was said only by time and rule (`earlier`); then
reads its points. A digest without points is shown, never read. Each sentence is a `say` of
its own, which an alert, the page's Stop or the app closing stops at once; a reminder, never
spoken, leaves it. A digest that comes while an alert is being spoken waits for the voice, a
minute at most (`ALERT_WAIT_S`); then it isn't read, and the log says so.

Scheduled digests run at the first real minute whose local time is on a period, so a change of
the clocks neither skips nor repeats one, at least a minute apart; each covers the time since
the one before, and each is shown as a notification without the alerts' sound, whatever became
of it. The page sets the schedule too (`/api/manage/digest/schedule`, local only like every
control): checked as `[digest]` is, with a coded refusal naming the field, saved as
`[alerts.digest]` in `config/ui.toml`, which takes the place of alerts.toml's whole table
(`off = true`: none), and applied at once: the running `Scheduler` is stopped (its own closed
event; a digest it is making is dropped) and a new one started. A page schedule that no longer
loads at start (a station it names was deleted) is logged and skipped; a broken file table
still stops the start.

### Settings, Find and Listen; feed and receivers

**Settings from the page.** Whatever the page changes goes to `config/ui.toml` (`settings.py`),
written by the program (mode 0600, replaced atomically) and laid over the hand-edited
`stations.toml` and `alerts.toml`, which are never rewritten. Sections: `[[station]]` and
`[[device]]` added on the page; `[overrides.<id>]` and `[device_overrides.<id>]` for file
stations and dongles; `[run] stations`, the stations to start next time; `[models]`, the chain
order per capability; `[retention]`; `[node]` (name, public URL); `[[output]]`; `[[feed_token]]`
(SHA-256 hashes only); `[alerts]` (delivery, actions, voices, rules added on the page, rules
switched off, and `[alerts.digest]`, the digest schedule in place of the file's); `[radio]`
(the band plan's region, and `[radio.policy.<class>]`, what is kept of each kind of radio);
`[remote_privacy]` (the class another machine gave each remote station). A broken override is reported and skipped, never fatal: the station keeps its
file settings. An override that leaves the station as the file has it (a language added and
removed again, a form that sends unchanged fields) is not saved, and the page's "changed here"
(`overridden`) lists only the keys that make the station differ from the file; confirmations
(`acknowledge`, `privacy_floor`) are always kept. `App` (`app.py`) owns the running parts and applies each change live, without a
restart: stations are added, changed, started or stopped in the pipeline; rules and outputs are
swapped in place; retention applies at once: a change to retention or to a class of radio
(Settings → Storage and privacy) prunes straight away, and the store prunes at start and every ten minutes in the background (it
checks every minute).

**Listen, Find and Settings.** Beside Read, the page's `#/listen`, `#/add/<page>` and
`#/settings/<page>` views (older `#/manage/<tab>` and `#/radio/<page>` links are redirected to
their new homes), a separate chunk of the built page that loads only when it's opened (still no
CDNs). Each control has one home; where a second entry point helps, it is a link to that home.

**Listen** plays every station, from the internet or a receiver, from one list with one volume
(a stopped station starts first). **On the air here** follows: what scans found, broadcast first,
each played through the tuner on the receiver that heard it, and before any scan, that scan in
one press. Below it, **Tune by hand**: a receiver tuned by hand, with a spectrum, waterfall, seek
and scanner; nothing is stored (below, *Listening by hand*). The scope is `manage/Scope.tsx` and
draws what it is given, so other views of a receiver can reuse it.

**Find** is where stations are found and added. **On the air** scans a band on an idle
receiver, shows what was heard here, and adds what it found as a station (below, *Radio*). **On
the internet** searches the station directory and checks a stream's address; *Other ways to add*
reads a playlist or follows another Wavescribe.

**Settings** opens on Stations:

- **Stations:** listen only, transcribe, or transcribe and translate; edit languages, order and
  the speech and translation models; switch off or delete. *Add a station* opens Find. An edit
  (`POST /api/manage/stations/<id>`) may change `languages`, the ones a station in language
  `auto` may be; a station in one language refuses them (send `languages: []` with a change
  away from `auto` that had some). `GET /api/manage/stations` and `GET /api/manage/radio` both
  give the stations file's `[defaults]` as `defaults: {language, translate_to}` (language null
  when the file gives none), which the add forms fill a new station from.
- **Languages and models:** each model's licence tier, size, fit on this Mac and speed
  estimate, whether it was tested for Wavescribe, installed and in use; download, accept a
  licence, reorder the chains; search Hugging Face; the translation languages.
- **Alerts:** keyword and LLM rules ("Words to listen for", "Describe it in your words"), a
  try-it box, term suggestions in each station language, actions per severity, voices.
- **Receivers:** dongles, centre and gain; sample rate, ppm and a spectrum and waterfall under
  *Advanced*; the region whose band plan names each frequency; each receiver's own signals.
- **Storage and privacy:** how long text and audio are kept, the audio cap, and what is kept
  of each kind of radio.
- **Connections:** webhooks and live pushes, a test button, the delivery log; under *For other
  programs*, feed tokens and this node's name and URL.
- **Health:** resources with an hour of history, per-station lag, queues, loaded models, disk.

Its API (`/api/manage/…`, `web/api.py`, helpers in `admin.py`) only answers the machine itself,
even under `--lan`, and every request needs the `X-Wavescribe` header, reads too (some reach the
network or open a receiver, and another site could request them as an image). Every request must
name the machine in `Host` (else 421: DNS rebinding); other machines may also use a `.local` name
or the public URL's host. Pushing to `/feed/v1/ingest`
needs a `push` token and JSON even from the machine, since any page open in its browser could
post a plain form there.

**Network use.** Runtime stays offline. The network is used only by:

- a click on the page, from the machine itself (LAN viewers can't trigger any of these):
  - a model download, run as a `wavescribe models fetch <id>` child process, which is stopped
    when `serve` stops;
  - a Hugging Face search, or adding one of its results (its card is read again), anonymous
    GETs to the public API;
  - a Radio Browser search or snapshot refresh;
  - checking a stream or playlist URL, and recording 20 s of it to detect its language;
- what the user configured: the stations' streams, outputs (text and metadata only, to URLs
  the user entered) and remote stations.

Everything else (transcription, translation, search, Ask, alerts, the page itself) works with
no network at all. The online model catalogue lists only models whose licence is clear, sorted
into the same tiers as `models.toml` (LICENSING.md, "Online model catalogue"); a model with no
licence never appears and stays bring-your-own. Catalogue models added from the page go to
`config/models.catalog.toml`, pinned to the commit they were found at and marked untested; the
server reads the licence again from the model card at that commit (`catalog.models.model_at`)
and ignores whatever licence the request carries.

**Feed, outputs and remote stations** ([FEED.md](FEED.md) is the reference). One JSON shape
per event (`events.py`, version 1: `utterance`, `alert`, `batch`) serves all three:

- `/feed/v1` (`web/feed.py`) pulls it: Server-Sent Events, JSON, NDJSON or CSV exports,
  stations, node info, `/healthz` and Prometheus `/metrics`. Lines finish out of order (a
  translation that waited, a re-check), so a stream's event id is a resume point
  (`feed.Cursor`): just below the oldest line still in flight, plus the ids sent past it. The
  hub notes a segment id in flight in the same step as the store gives it
  (`WebHub.store_transcript`) and queues each final line with where things stood
  (`feed.Mark`); a resuming stream replays the lines final by then and gets the rest live, each
  once. Clients skip a line a resume sends again by its id (`events.Seen`).
- `Output`s push it to webhooks. Alerts on their way are kept in the store until they are
  delivered (`Outputs` and the `outbox` table): `Outputs` reads the outbox once, at start,
  and from then on each output's thread holds its waiting alerts in memory (`_Worker._alerts`,
  which `Outputs.status()` reports as `alerts_waiting` and `alerts_oldest_age_s`, with
  `alerts_expired` and `alerts_dropped` counted since the output started) and writes through:
  a row before the first try, its tries and next try after a failed round, its removal after
  the request. A crash between the request and the removal sends the alert again with the same
  delivery id; nothing in between loses it. The store's writes are serialised by its own lock
  on its one connection, as every other writer's. Closing stops the threads, waits up to 2 s
  for requests on their way, and leaves the rows.
- A remote station follows another Wavescribe's feed (`remote.py`), or is created by that
  machine's pushes to `/feed/v1/ingest`. Each machine has a random id (`App.node_id`, kept in
  ui.toml) in its events' `node.id`; a line passed on carries the ids it passed through
  (`via`, from `App.via`, which remembers each remote station's path in memory). Ingest and
  the follower refuse a line that passed through this machine or more than 8 machines
  (`Heard.refused_by`), which ends the echo between two machines that push to each other.

Feed tokens (scopes `read`, `push`) guard other machines once the first one exists, and any
request through a proxy always (FEED.md, "Who can read what"); the machine itself needs none to
read, and a push always needs one. They go in the `Authorization` header;
a client that can't set one trades a read token for a one-time, 60-second stream ticket
(`POST /feed/v1/ticket`, `feed.Tickets`, kept in memory only) for `?ticket=`. `?token=` still
works and is logged as a warning. A stream let in by a token is held by it (`WebHub.hold`):
deleting the token (`WebHub.revoke`) ends those streams at once and voids its tickets.

Remote text is checked field by field (`remote.read_utterance`) and then goes through
`Pipeline.ingest` with the privacy class its events carry (never looser than a `privacy` the
station's config gives it), so it is stored, searched, asked about and checked by
**this** machine's rules like local text. Rules match its source-language text; translations
are added only for languages it didn't arrive with. The feed carries no audio: `audio_url`
(`/api/segments/<id>/audio.wav`) points back to the machine that recorded it.

**Unknown keys are refused.** `alerts.toml` takes `[delivery]`, `[speak.<lang>]`, `[[rule]]`,
`[actions]` and `[digest]`, and each takes only its own keys; a rule takes those of its `kind`
(`RULE_KEYS` in `rules/__init__.py`). Anything else, a misspelt `kind` included, stops the load
with the key and where it is ("rule water: unknown keys ['min_mention']; a keyword rule takes
[…]"), as the stations file does: a misspelt key would otherwise run as its default without a
word. `serve`, `listen`, `doctor` and `alerts check` all say it. Rules and settings saved from
the page are checked the same way (a 400 that names the key); what the server adds to a rule
for the page (`origin`, `enabled`) is dropped when the rule comes back.

**Rules from the page.** A keyword rule is the same as in `alerts.toml`, with terms per
language. A rule with `verify = true` has each hit held at low severity until the LLM confirms
it in context; a hit the LLM says isn't real stays there. A rule never goes silent because its
check couldn't run: when the check doesn't answer (no LLM loaded, its queue full, a failure,
an unreadable reply, or no verdict within 90 s), the hit is raised all the same, marked as not
checked, with "not checked by the LLM: …" as its reason. The rule's `unverified` says how:

- `lower` (the default): one severity below the hit's own (critical → high → medium → low).
  Whoever switched `verify` on asked for matches to be confirmed, so an unconfirmed one is
  worth less to them, and it still reaches them.
- `announce`: at the hit's own severity.
- `hold`: not until the check answers. It is asked again 30 s after the try that didn't
  answer, then twice as long each time, which is five tries at most: a model that fails every
  time isn't asked for ever. 10 minutes after the hit was heard it is raised as `lower` would
  raise it, so a held hit can't be lost: that is time for a model to load or a digest to
  finish, and inside the default cooldown, so the hit joins the alert it opened (with a
  `cooldown_minutes` under 10 it opens another). At most 32 hits are held, each with its
  utterance; one more raises the oldest, and stopping Wavescribe raises them all.

The alert says so in `checked` (`Alert.checked`, the store's `alerts.checked`, the event,
CAP's `checked` parameter): true when the LLM confirmed the match the alert's severity comes
from, false when that match was raised without its answer, null otherwise (a rule without
`verify`, or no answer yet). A later match changes it only when it asks for a higher severity,
or was confirmed at the same one. A notification's title says ", not checked" after the
severity ("MEDIUM, not checked · water · …"), as do the `log` action's line (`checked`) and
the first line of a `text` output; nothing is added to what is spoken. `LlmRuleChecker.status()`
(`llm_rules` in `/api/manage/status`) counts since the start, under `verify`: `confirmed` and
`rejected` (checks that answered), `not_run` by why (`timeout`, `failed`, `queue_full`,
`no_model`; a held hit counts once for each try) and `held` (now); `unchecked` is the hits
raised without an answer. `listen` and `alerts replay` have no LLM: there `verify` isn't in
force, a hit is raised at its rule's severity, and `checked` stays null.

An LLM rule (`kind = "llm"`, `rules/llm.py`) is a sentence ("people are told to
leave their homes"). Each station's source text is collected and checked against every LLM
rule about every 45 s (or at 1,500 characters), as one prompt at priority 0.3, behind alert
translations and ahead of Ask; a backlog of station lines makes way for it (see Overload). A
check that still can't be queued is counted against its station (`dropped.rules`) and makes the
load `overloaded`: alerts may be missed. A match comes back with a one-line reason and the utterance it
rests on, and goes through the same Alerter as keyword hits (cooldown, `min_mentions`,
actions). The LLM reads the source text, never a translation, and its reply never becomes the
announced text. Page rules with the id of a file rule replace it; file rules can be switched off
from the page but not deleted.

**Receivers.**

- *Dongles:* `rtl_test` lists the plugged-in dongles and their serials.
- *Spectrum:* computed only while someone watches. The page polls it and each poll extends a
  3 s window. It is a 2,048-point FFT of the IQ already flowing, averaged into 512 bins every
  0.4 s; the page draws it as a line and a waterfall. A **monitor** keeps a dongle running with
  no station on it, to check the antenna and gain before adding stations.
- *Retuning* (new centre, gain or sample rate): stops the dongle's stations, closes it, reopens
  it with the new settings and restarts the stations that were running.

**Resources** (`system.py`): a snapshot every 15 s, kept for an hour, with no sudo. It covers:

- the process's memory footprint (the Activity Monitor number), CPU and its children;
- system memory and pressure, swap, CPU;
- GPU use and memory (`ioreg`);
- power draw and battery (`ioreg` AppleSmartBattery);
- free disk, database and audio sizes;
- network throughput.

**Running unattended** (`service.py`, `wavescribe service install|uninstall|status`). A
per-user launchd LaunchAgent, `~/Library/LaunchAgents/io.github.kaspler.wavescribe.plist`, runs
`caffeinate -i <the environment's python> -m wavescribe serve <options>` in the Wavescribe
folder, at login and at install:

- `caffeinate -i` holds an idle-sleep assertion while serve runs. It passes SIGTERM on to serve
  and exits with serve's exit code (checked), so launchd stops it cleanly and still sees a
  crash. It doesn't prevent clamshell sleep.
- `KeepAlive` with `SuccessfulExit = false` restarts any failure, no sooner than 30 s after the
  previous start (`ThrottleInterval`), so a broken config costs one short start every 30 s.
  `ExitTimeOut` gives serve 30 s to close. `ProcessType = Interactive` lifts the light CPU and
  I/O limits launchd puts on agents by default.
- stdout and stderr go to `data/logs/service.log` (start-up, warnings, tracebacks); launchd
  doesn't rotate it, so each install moves one over 10 MB aside. `PATH` gets the folders of
  ffmpeg and the rtl tools; the locale and the model folders' variables are copied over.
- `install` takes serve's options (its parser is serve's), parses them back with serve's
  parser, and first checks the stations, page settings, alerts and port, so a mistake shows at
  install rather than as a restart loop. Installing again replaces the running agent.
- Every launchctl call (`print`, `enable`, `bootstrap`, `bootout` in `gui/<uid>`) goes through
  the `Launchd` interface; the tests use a fake and a temporary home.

**Self-check** (`doctor.py`, `wavescribe doctor`). Read-only and offline; each finding is ok,
warn, fail (exit code 1) or skip, with a next step for each problem, as text or JSON lines:

- tools: ffmpeg runs `-version`; rtl_sdr and rtl_power are only checked on disk (their
  Homebrew version from the Cellar path), since running one opens a dongle;
- Python, the environment and the libraries Wavescribe requires (package metadata, no imports);
- the stations, page settings and alerts through the loaders serve uses, so the errors are
  serve's;
- the models each configured station and language needs, resolved like the pipeline's plan
  against the registry and the local caches, with the command that downloads or accepts each;
- receivers: RTL2832U dongles from macOS's USB registry (`ioreg -p IOUSB`, never opened) against
  each `[[device]]` serial; duplicate serials are flagged;
- free disk against the retention limits (speech audio cap, text at about 7.75 MB a day per
  station from the soak, models still to download), and whether `data/` is writable;
- the port: its listener from `lsof` and `ps` (this machine's service, another Wavescribe, or
  another program), then serve's own probe bind;
- the voices of spoken alerts from `say -v ?`;
- the background service (`launchctl print`) and sleep (`pmset -g`).

Commands go through a `Host` interface whose real implementation refuses the rtl tools. With
no options given, doctor checks the installed service's.

### Radio: scanning, narrowband channels and RDS

[RADIO.md](RADIO.md) is the user's guide; this is how it works.

**Demodulators** (`dsp/`, numpy and scipy, one `rtl_sdr` pipe per dongle shared by every channel
on it):

- `wfm.py`: broadcast FM. `WfmDemodulator.mpx()` returns the multiplex (±75 kHz deviation = ±1)
  at 240 kHz, which `on_mpx` hands to the RDS decoder before de-emphasis and the audio chain.
- `channel.py`: NFM and AM. A frequency-translating polyphase decimator (complex taps
  `h·e^{jωk}`, about 2% of an M1 Max core per channel at 2.4 MS/s) brings the channel to 48 kHz, a
  channel filter cuts it to its width, then:
  - **NFM**: discriminator, 750 µs de-emphasis (unity at 300 Hz), a sixth-order elliptic 300 Hz
    high-pass for CTCSS, a 3.8 kHz voice low-pass, and a **noise squelch**. The discriminator's
    noise above 0.72 × the channel width falls when a carrier arrives, at any level; it is
    compared with the noise-alone level computed for that width: open 11 dB under it, stay open
    6 dB under it, gone within 3 dB of it, per 10 ms block.
  - **AM**: envelope over its own average (automatic gain), and a **carrier squelch** from a
    100 ms FFT: the strongest bin within ±3 kHz against the channel's median, open at 16 dB,
    stay at 13 dB.
  - A fixed `squelch_db` overrides either. When the carrier goes the audio is muted at once (it
    runs 20 ms behind the squelch and fades over 5 ms); the 0.6 s hang only bridges a weak
    signal that flutters. The AM carrier squelch still holds 0.6 s.
  - A narrowband channel must sit at least one channel width from the device's centre: the DC
    spike there holds a squelch open (measured up to 0.55 of the width). The config refuses it;
    Tune by hand and a scan under Find move the receiver. `ppm` on a `[[device]]` corrects the dongle's
    crystal error (`rtl_sdr -p`, `rtl_power -p`).
- `rds.py`: 57 kHz mixed down from a lookup table (57/240 = 19/80), decimated to 9.6 kHz, the
  carrier phase from squaring, a biphase matched filter, symbol timing per 2048-sample chunk,
  differential decoding and block sync by syndrome. Any single wrong bit or two adjacent ones are
  corrected. PI and the TP/TA flags count after two matching groups; the name, programme type
  and each alternative frequency need to be seen twice; a radiotext segment counts once it has
  come twice running, so a text changed without the A/B flag replaces the old one, and a short
  text with no end mark ends where two passes agree. Groups 0A/0B (name, TA, AF) and 2A/2B
  (radiotext). On a synthetic multiplex, 97% of groups decode at 0 dB SNR in the 4.8 kHz RDS
  band and half at -5 dB.

**Scanning** (`radio/scan.py`). `rtl_power -c 20% -1` sweeps the range. The bin follows the band
(a twentieth of an FM channel, a fifth of a narrow one; about 25 000 bins for a free range), and
the integration time scales with the hops. `rtl_power` prints every hop at once when its pass
ends, so it is read against a deadline: silent for 10 s plus three sweeps' worth of time (the
integration time, or 50 ms a hop when that is longer), it has stalled and is killed, and the scan
fails with an error that says so. A cancelled scan kills it within 0.1 s. librtlsdr reports a
tuner that didn't lock on stderr, but `rtl_power` reports no progress hop by hop, so neither the
order nor the timing of the message places it. A message that names a frequency (the LO, the
hop's centre plus an IF of up to 4.57 MHz, or the centre itself on zero-IF tuners) makes the hops
it can be **gaps** (`Sweep.gaps`): bridged, and no signal or spur is taken there. The usual
`[R82XX] PLL not locked!` names none: the sweep carries a warning (`Sweep.warnings`, logged, and
in the spectrum the page and `data/radio/scans/` get) that its whole range may be wrong. Messages
before the gain is set are the tuner's filter calibration at start-up and don't count. The
dongle's DC offset at each hop's centre is
bridged over about 3 kHz from either side, where a narrow carrier would be lost (always the same
channels, since the hops fall in the same places). At 5 kHz bins or finer a second run, with the
hops a quarter of a hop away and rtl_power's shortest interval (1 s), fills each centre in,
matched in level to the readings either side. `rtl_power -c` can't make hops overlap instead: it
only discards each hop's edges, and the hops still tile the range. With the fake's timing (0.5 s
to open the dongle, 40 ms a hop) the second run adds about 2 s to a narrowband sweep (PMR446 2.7
to 4.4 s, the airband 4.0 to 5.8 s); FM, at 10 kHz bins, has one run. The noise floor is a 20th-percentile filter over about
2 MHz, taken within each hop, so a crowded band doesn't lift it and a hop that reads louder than
its neighbours doesn't turn into a row of signals. Peaks 8 dB above it are signals. Each signal's
centre is measured on a spectrum smoothed to a tenth of its band's channel, then snapped to the
band's raster or channel list; its width is where its power above the floor stays within 6 dB of
its peak. Narrow lines on odd multiples of a 2.4 MHz clock are marked as another receiver's
clock (`clock_hz`), never deleted. FM signals are grouped so that each group fits one 2.4 MS/s recording with 140 kHz to spare
at both edges, which puts its stations at most 2.12 MHz apart (`clusters()`), and every station in
a group is demodulated at once from that recording, until each has a stable name or 12 s pass.
A **watch** repeats the sweep, keeps the maximum per bin and counts the sweeps each channel was
busy in. **Spurs** are learnt per serial with the antenna disconnected: lines in both of two
sweeps at bins of at most 6 kHz (`SPUR_BIN_HZ`: 512-bin FFTs, 5.47 kHz over a whole-range hop).
The default 86.8 kHz bins of a whole-range sweep buried narrow lines under 12 dB more noise.
rtl_power reads 16 384 bytes per hop whatever the bin, so the finer sweep takes no longer, and
still averages 16 FFTs per bin; at 2048 bins it would average 4, and on synthetic noise a 6 dB
margin then finds thousands of lines over the whole range. Each entry in `spurs.json` records its
`bin_hz`; entries without one are from earlier versions and should be learnt again.

**Band plans and privacy** (`radio/bands.toml`, `radio/bandplan.py`). Each band has edges,
regions (`cept`, ITU 1-3, `jp`), a mode (and `region_mode` where a region's differs: CB is NFM
under CEPT, AM elsewhere; `region_bands()` and `lookup()` give the band as the region uses it),
a raster or channel list and a class: `broadcast`,
`amateur`, `utility`, `private` or `digital`. `lookup()` takes the narrowest band of the region;
a band any region marks digital counts as digital everywhere; an unlisted frequency is private.
A channel's class is the strictest of every band its edges reach (`channel_width()`: 200 kHz for
FM, else the band's channel or raster, 25 kHz unlisted; callers can pass `width_hz`), so a
channel straddling a boundary takes the stricter side's class, keeping its own band's raster,
mode and name. Unlisted spectrum doesn't count at the edges: FM's band ends on its outermost
channel's centre, and its edge mustn't make it private.
Each class has a `Policy` (`needs_ack`, `text_days`, `audio`, `export`, `quote_in_alerts`).
With no region chosen (`AUTO`), the page and `radio bands` list the bands of the locale's
region, but `lookup()` takes the band of any region with the strictest class: a locale says how
a Mac is set up, not where it is. The defaults are cautious; the user changes all but
`needs_ack`, in `[radio.policy.<class>]` of the stations file and on the page (`config/ui.toml`, laid over the file). `apply_policies()`
puts them into effect in place, at startup and on every reload, so every module below sees a
change at once. The policies are enforced where text goes:

| Where | What |
|---|---|
| `config.radio_policy()` | utility, private and digital need `acknowledge` (the user confirms they may receive it); what the page saves is the classes confirmed (`config.confirmed()`; `App._pin_acknowledgements()` turns a plain `true` into the current class), so a move to a class never confirmed asks again, while a class kept stricter after a change of region (`privacy_floor`) runs on the confirmation it had; digital is tuned with the analogue demodulators only: there is no digital decoder and no decryption |
| `Store.add_transcript()` | `text_days = 0`: nothing written; `audio` false: no clip; every row carries the class it was heard under (`segments.privacy`, `broadcast` included; NULL only in rows from before schema 6); with `text_days` > 0, `prune_short_lived()` removes it and its audio past the class's current retention, after a restart, a retune or the station's deletion, and also when the user shortens it later. Alerts of such classes lose quote, terms, reason and translations at the same age. The `log` alert action leaves such alerts' words out |
| `Outputs`, `/feed/v1`, remote followers | text only where `export`; alerts from the other stations go out as `redact_alert()` (rule, time, station; no words, terms or audio) |
| The `outbox` (alerts waiting for an output) | a row holds the event as it is sent, so nothing that doesn't leave anyway. Words go to disk only while the store would keep them: an alert quoting a class with `text_days = 0`, or past its class's days, waits in memory only, and `Store.prune_outbox()` removes a waiting row when its words pass their class's days (from the alert's first mention, as for the alert's own quote) or the store's text retention; the output goes on trying from memory. At start, a row whose words may no longer leave (the station is gone, or its class stopped exporting) is rewritten without them before it is sent |
| The page, for other machines (`--lan`) | stations without `export` don't exist for them: `WebHub.exports()` (`bandplan.Exports`) decides for every read path, from the row's stored class and the stations configured now (a deleted station's rows stay here), and leaves them out of the state, history, search, live events, live and archived audio and Ask, and their alerts arrive redacted. The Listen tab's receivers and spectrum monitors never reach another machine |
| `Alerter` | without `quote_in_alerts`, the quote and the spoken text are replaced by a fixed phrase |

**Catalogue** (`radio/catalog.py`). `data/radio/heard.json`, keyed by frequency: band, class,
level, first and last heard, how often, RDS for broadcast only, a name the user gave. Spurs and
digital signals are not kept. The last 20 scans' spectra are in `data/radio/scans/`, each with
its time, the receiver that made it (`device` id and `serial`), `what` it was asked to scan (the
band's name, "the whole tuner range", or "88 to 108 MHz"; `wavescribe radio scan` labels its
scans the same way), its length and the signals found. The page gets the latest as
`last_scan` in `GET /api/manage/radio`; one saved before scans recorded `device` and `what` has
them as null, and its `device` is filled in from its serial while a receiver has that serial.

**The page** (`radio/service.py`, `/api/manage/radio…`): one background scan at a time. A
receiver with running stations or a spectrum monitor is refused with what holds it, unless the
page sends `pause: true`: they stop for the scan and start again when it ends. Dongles on USB
that no receiver uses are listed with `rtl_test -d no-such-dongle`, which prints the list and
opens none. `busy` names what Wavescribe itself holds a receiver for; `held_elsewhere` (on each
receiver, and on each of those dongles) says when another program holds it: `{pid, program,
other_wavescribe, reason}`, else null. `busy_code` says it in one word for the page, the first
that applies: `scan`, `live` (the hand tuner), `watch` (a running watch, even beside stations),
`station`, `monitor` (the antenna test's spectrum monitor), `other_program` (`held_elsewhere`),
else null. Every view of a receiver (these, `GET /api/manage/receivers` and the hand tuner's
`/api/manage/live/<id>`, where the hand tuner may have moved it) gives `span_mhz` as `[lo, hi]`
in MHz: what it takes in, its centre ± half its sample rate (`span_mhz()`); a channel is heard
a margin inside those edges (120 kHz for broadcast FM, 60 kHz for a narrow channel). Each
receiver keeps its full `serial`, so Settings can match a stick to its letter;
the scan job (`scan`) names its receiver by `device` and its dongle by the serial's last
characters only (`masked_serial`: `"…042"`), since a job's words get pasted into reports; the
saved scans and `last_scan` keep the full serial. The job's `started` is when it began (Unix
seconds). `country` is the ISO 3166 code of the chosen region where that region is one country's
band plan (`jp` → `"JP"`), else null: the CEPT countries and ITU Regions 1 to 3 are many
countries, and a region left to the computer's setting is not one chosen. It comes from macOS's USB registry (`radio/holders.py`:
`ioreg -r -c IOUSBHostDevice -l`, read at most every 3 s, nothing opened): a `UsbExclusiveOwner`
of `pid N, name` on the dongle or one of its interfaces, or an interface user client's
`IOUserClientCreator`. Device user clients alone don't count (browsers make one for every USB
device they list). A holder whose parent is this process is one of its own rtl tools; one whose
parent's command line names wavescribe is another Wavescribe (`ps`). `lsof` can't see it: libusb
on macOS goes through IOKit, not a file. Elsewhere than macOS it is always null. Every 15 s, a page FM station still named after its frequency takes its RDS name
(`adopt_rds_names()`). `tune` makes a page
station from a frequency and starts it, in one call: mode from the band plan, the class's
acknowledgement, a name from the catalogue or the band. The receiver stays where it is if it
hears the frequency there; otherwise `_centre_for` picks a centre that hears the new station and
every station the receiver has (where the hand tuner has it, else 250 or 300 kHz below the
frequency, away from DC, else the nearest kHz that keeps them all). With no such centre nothing
changes and the answer is a 400 with code `retune` (`retune_question`: `unfit`, `holders`,
`center_mhz`); `retune: true` goes ahead, and `/api/manage/receivers/<id>` asks the same before a
hand-set centre leaves stations out. `POST /api/manage/stations` moves the receiver the same way
for a radio station or watch it doesn't hear where it is tuned (`add_station`, with the same
planner: `_centre_for_station` tries, for a watch, where a watch is tuned (`plan_centre`) and
then every kHz from which it hears the whole window): without asking when that leaves nothing
out, else the same question with nothing changed, until the same body comes with `retune: true`.
After such a move its answer adds `receiver`, `unfit`, `live_closed` and `start_error`, as
`tune`'s does. `POST /api/manage/stations/<id>/start` does the same for a station already made
that its receiver doesn't hear (`RadioService.start_station`, the same `_centre_for_station`
and `_place`): it moves the receiver and starts the station, or asks the same question (code
`retune`) with nothing changed until the body has `retune: true`; where no centre hears the
station at all, the refusal has code `out_of_reach`. Its answer is `{"ok": true}`, and after a
move also `receiver`, `unfit`, `live_closed` and `start_error`. A receiver tuned by hand is let
go first, and what it paused starts again where it is still heard.

**Codes beside refusals** (`refusal.py`). A refusal the page may act on carries a `code` beside
its sentence, in the 400's JSON, so the page needn't judge from the words (which stay as they
are). `Refusal` is the base of `ConfigError`, `ChangeError`, `RadioError` and `WatchError`;
where a refusal travels as its sentence alone (the page's config errors, `effective_config`),
`said()` keeps the code beside the words and `Refusal.of()` raises it again. Besides the
questions (`acknowledge` with `privacy`, `busy` with `holders`, `scanning`, `language`,
`retune`) and `refused` (a mode `/radio/tune` refuses), a radio station, a watch or a tune is
refused with: `bad_frequency` (not a frequency in MHz, or none given), `out_of_band` (a
frequency no receiver tunes, or one this receiver can't be tuned to hear), `bad_mode`,
`bad_channel` and `bad_squelch` (`channel_khz`, `squelch_db`), `no_receiver`, `out_of_reach` (a
change that would tune a station where its receiver doesn't hear it, or a start of one that no
centre of its receiver hears), and for a watch
`unknown_band`, `bad_window` (not two frequencies, or the start not below the stop),
`no_channels` (none on the band's raster in the range), `bad_threshold`, and `too_wide` with
`slice_mhz: [start, stop]`, the slice that fits that the sentence offers. A station in language
`auto` given a speech model that can't tell languages apart is refused with `cannot_detect`;
`GET /api/manage/models` marks each model that can (`detects_languages`: an ASR model on disk
that passes the test `plan_detecting` makes, so false for one not downloaded yet), for the
station editor to offer.

**Stations a receiver doesn't hear.** Whether a receiver hears a station is one rule for every
kind (`config.heard_by`): one of its frequencies inside the window and, for a narrow channel,
clear of the centre's spike; every channel of a watch inside the window. A station that fails it
is built anyway, with `Station.out_of_reach` saying why and where to centre the receiver, and the
app lists it unavailable with that reason, never starts it (`open_source` refuses it too), and
stops it if a retune leaves it out. It is not a config error: a stations file with one starts,
and `doctor` warns `out of reach` instead of counting a page change not applied. Switched-off
stations, and those already out of reach, never hold a retune back. A page change that would
tune a station where its receiver doesn't hear it is refused with the same reason (code
`out_of_reach`); a station added or started from the page moves the receiver instead, as above.

**Listening by hand** (`radio/live.py`, `/api/manage/live/<receiver>`, the page's **Tune by
hand**). A `LiveReceiver` is one more consumer of the dongle's shared `rtl_sdr` pipe, like a
station's channel, but nothing is segmented, transcribed or stored: its audio goes to the page's
audio tap as `live.<receiver>` (station ids can't contain a dot). Retuning assigns `tuning`; the
next block rebuilds the demodulator, so moving within what the dongle hears is instant and the
pipe never restarts. While the page polls (every 250 ms) or a scan runs, each block also gives a
spectrum: an 8192-point FFT averaged over 8 segments (about 293 Hz per bin at 2.4 MS/s), reduced
by maximum to at most 1024 bins for the requested span. The **scanner** works on that spectrum
rather than retuning channel by channel: every block it measures each listed channel's power over
the median noise, holds the strongest one above the threshold, and moves on `hold_s` after it
goes quiet; lockouts skip channels, and the receiver's learnt spurs are locked out from the
start. A frequency outside what the dongle hears, or another gain, needs the dongle to itself: it
reopens at a transient centre 300 kHz (WFM) or 250 kHz (otherwise) away from the frequency, off
the DC spike, and the receiver's settings are not touched. Stations on that dongle are paused
only when the page sends `pause: true`, and start again when listening ends; a background scan
takes the dongle back the same way. The privacy classes apply as for stations: utility, private
and digital need the acknowledgement once per receiver. A receiver nobody polls or listens to
closes after 60 s. **Keep** turns what is tuned into a page station (`radio.tune`, as above),
listen-only or transcribed, with the name, language, `languages` (those language `auto` may
be) and translation languages it is given, as a tune takes them. A tune may say which view it comes from (`from`: `find` or
`listen`), so that a station refused because the receiver is tuned by hand says where to let it
go.

**Watches** (experimental; `dsp/activity.py`, `sources/watch.py`, `radio/watch.py`; not the station
alerts' `StationWatch`, below). A station whose `SdrTuning` carries a `Watch` (the channels of one
window) gets a `WatchSource` instead of a channel: one more consumer of the shared pipe. On the
dongle's thread, each block goes to an
`IqHistory` (the last 2 s as the dongle's 8-bit samples, about 10 MB) and an `ActivityDetector`:
4096-point FFTs averaged into frames (29 of them, 49.5 ms, at 2.4 MS/s), each channel's power over
the middle 80 % of its width against a noise floor kept per bin, which follows the bin only while
its channel is quiet (down in about 2 s, up in about 20 s). The centre's bins (±1.5 kHz) are left
out, and so are a learnt spur's, as far again as half the bin it was learnt at (a list without
`bin_hz`, from an earlier version, isn't used). Two frames 8 dB over the floor start a transmission; 1.2 s under it less 3 dB end it;
under 0.3 s it is counted, not kept; after 120 s it ends with reason `carrier` and its bins' floors
jump to its level. A start 20 dB under a busy channel within three channels, or under its own mirror
about the centre (a receiver shows a weaker mirror of strong signals), is suppressed. While half the
channels (at least four) rise at once, and for 0.5 s after, nothing starts and the floors hold;
after 10 s of that without a break the floors are learnt afresh from the median of the next 0.5 s,
as at the start. Unless one channel stands 15 dB (`OVERLOAD_DB`) over every channel beyond its own
reach (three channels either side, those overlapping it, and those about its mirror and about the
third-order copy its I/Q clipping makes at −3 times its offset, folded into the band): that is
`overload`, a transmitter close by. That channel may start and nothing else does, the floors hold,
and once it has started the time doesn't count towards learning them afresh. The same holds while
1 % or more of a frame's 8-bit samples are at the converter's limits (`CLIPPING`; counted per FFT,
so the frames before a key-up in the same block don't clip) with a channel on top and no rise: the
copies then land on a few channels of their own. A catch busy during an
overload has `reason` `overload`, which wins over `carrier` and `stopped`. The margin is measured on
synthetic IQ: a walkie-talkie overloading the receiver stands 26 to 48 dB over the rest where the
converter clips (17.5 dB at worst without clipping); white, sloped and comb noise, USB clicks and a
carrier clipping from outside the window never more than 12 dB in a frame. A transmitter close by on
none of the channels leaves only its products on them, and one of those can stand over the rest. So
in every frame overloaded in that sense (the window risen, or clipping), the frame's FFT is searched
for the strongest signal in the whole window (`_strongest`): the power over 8 dB (the busy
threshold) above each bin's floor (so the dongle's spike at the centre, always there, and a signal
learnt into the floor are no signal) summed over a channel's width about each bin, its frequency the
power-weighted middle of the strongest such window. When that is more than a
quarter of a channel's width from every channel, stands 6 dB (`OFF_CHANNEL_DB`) over every other
signal in the window beyond its reach (its splatter, its mirror and its third-order copy: one
transmitter, not a comb of lines), and, with no channel on top beyond that reach, 15 dB over every
channel beyond it, nothing starts. Two such frames in a row make it the overload's transmitter, off
the channels, named to the kHz, or not named within 60 kHz of the window's edges, where signals from
beyond fold in. The converter clipping with no channel on top is an overload too, not
interference: when the strongest signal isn't on a channel, or isn't clear of the
rest, it is an overload off the channels with no frequency (a transmitter beyond the window, where
it isn't seen, clips the converter all the same); when it is on a channel but stands over nothing
(two transmitters close by at once), it is an overload with neither (`on_channel` null), and that
channel may still start if it comes to stand over the rest. A window rising without clipping and
with no channel on top stays `interference`. An overload nothing is caught from (off the channels,
or with neither) is learnt into the floors after 10 s without a break, as interference is, so a
transmitter that stays doesn't leave the watch deaf; learnt while the converter clips, the overload
stays noted (`overloaded_at`, so the status's `overload`) until it has stopped clipping for 0.5 s,
and starts nothing new as off the channels meanwhile.
Measured on synthetic IQ at 2.4 MS/s: such a transmitter stands 10 to 14 dB over every other signal
with a product 10 dB under it on a channel, and 25 to 33 dB without; the same noise, slopes, combs,
USB clicks and carrier clipping from outside the window never more than 2 dB (those that clip are
now overloads off the channels with no frequency, by the rule above); on-channel overloads, with the
radio up to 3 kHz off its channel, never a frame off the channels. It costs 30 to 60 µs per
overloaded frame (20 a second), a few per cent of the detector, and nothing otherwise; counting the
clipping per FFT costs nothing while the converter doesn't clip (16 ms per second of IQ for 16
channels, before and after). One overload is one transmitter: its channel
(when it starts) or its frequency off the channels (when sure) is fixed until the overload is over,
no other channel starts in it, and `deaf_reason` stays `overload` from then on, though a frame may
have no channel on top. Deafness of 3 s or more, and an overload off the channels at once (no catch
says it), is logged once (`watch_deaf` with `reason`, `overload` or `interference`, the `clipped`
share, and for an overload `on_channel` and its `mhz` where the class keeps text; `watch_hearing`
with the same `reason`), shown as the station's `warning` (a sentence) and `deaf` (the reason) in
the status, and published as a `Hearing` (with `reason`, and `off_channels`, the sentence saying
where it was) each way, which `radio watch` prints; for an overload it prints what to do, with
`--gain`, and an overloaded catch too short to make the watch deaf prints it with the catch. The
status also carries `overload` (see `status()`, above). Listening by hand (`radio/live.py`) gives the same `overload` object in
its view while the dongle's converter clips 1% or more of the samples, and for 10 minutes after,
with `freq_hz` and `on_channel` null: it doesn't tell which signal on the window clipped it. A start opens a `NarrowbandDemodulator` for its channel fed
from the history 0.3 s before the first frame above the floor (but never before the previous catch's
audio on that channel ended), at most four at once (`watch_missed` otherwise); an end gives a
`Catch` with the audio up to 0.3 s after. The dongle's thread queues catches for the station's (16
at most; beyond that `watch_behind` counts them, so that thread never writes to the store), and when
the watch stops, what is still being caught is cut where the IQ ends and published. For a class that
keeps no text, those log lines carry counts, not frequencies. The source's iterator hands catches to
the pipeline one at a time: quarter-second chunks with the catch's `freq_hz`, a discontinuity on the
first so the VAD starts afresh, and 0.8 s of silence after so its last utterance ends there.
`freq_hz` goes from chunk to utterance to `segments.freq_hz`. The pipeline tells the source which
chunks went through the VAD (`checked(chunk)`) and which utterances passed the gate (`speech(utt)`),
so once the catch's audio has been read it is published (`watch.publish`) with `voice` true, false,
or null where nothing looked for speech in it (a watch only listened to); the store and the command
line subscribe. In the ASR worker, language `auto` runs `transcribe_any` (`asr/language.py`) with a
model whose wrapper says it can tell languages (`MlxAudioTranscriber.tells_languages`: a
multilingual Whisper, by its config): Whisper's language probabilities (`identify_language`, as the
re-check has them), narrowed to the station's `languages` unless those together are under 10 %, then
the station is replaced by one in that language for the checks, translation, alerts and the store.
`plan_centre` puts a window narrower than about 0.9 MHz wholly above the centre (lowest channel −
250 kHz), so its mirror falls outside; a wider one is centred between two channels, and a window is
planned as it will be tuned, so a slice suggested for a band too wide fits after centring. That
slice is the longest run of the band's own class of channels, with none of another between them,
that fits (nearest the middle among equals), and the refusal names the parts of another class
(`other_kinds`). The class
of a watch is its strictest channel's, each channel's being the strictest its edges reach at the
watch's channel width, under whatever region is chosen (`SdrTuning.policed`).

### Alerts

```
transcript (source language) ─► RuleSet.evaluate ─► hits ─► Alerter ─► alert (stored)
                                                              │
                         translation queue, priority 0 ◄──────┤  wait ≤ translation_wait_s
                                                              ▼
                              language check ─► notify / speak:<lang> / log ─► delivery state
```

- **Keyword rules** (`rules/`) run on the source text the moment ASR finishes, on the ASR thread.
  Topics live in per-language packs (`config/keywords/<lang>.toml`, or `keywords/` beside an
  alerts file kept elsewhere, looked for first); a rule names topics, so one
  rule serves every language that has a pack. Matching (`rules/text.py`) folds case, and accents
  where they are optional (Latin, Greek, Cyrillic, Hebrew and Arabic points; a Thai tone mark,
  a Devanagari vowel sign or a Japanese voicing mark stays part of its word), stems with Snowball when it has the language (pack terms are base forms; plurals match), keeps
  word boundaries ("REN" never matches inside "renda"), matches terms in capitals exactly
  (acronyms), and falls back to substrings for scripts without spaces. A term starting with `=`
  matches as written, without stemming, for words whose stem a common word shares (Snowball
  stems *tornado* like *torna*, "becomes"). `near = ["local"]` raises a rule's severity one level
  when a place from the pack is named in the same utterance. Text the ASR checks flagged as
  another language never rises above `low` (Parakeet answering in English turned cured ham into
  "nuclear sabotage", and Snowball stems *sabotage* like *sabotagem*). The `repetition` flag
  doesn't cap severity. A flagged line Whisper is about to re-check (`Result.rechecking`) is
  read by the keyword and LLM rules once, when the verdict is in (`Pipeline(on_rechecked=…)`):
  replaced by Whisper's text in the station's language, it can raise an alert at full
  severity; kept, it stays capped at `low`. Rules never read both versions, so a re-checked
  line can't raise two alerts or count twice towards `min_mentions`, and nothing is lost by
  waiting: until then it could only have raised a `low` alert, which is never announced.
- **Regular expressions:** a keyword rule can have `regex = [...]` beside its topics and terms,
  matched on the source text ignoring case and nothing else (no folding, no stemming), in any
  language; `stations` narrows them down. A match is marked and highlighted like a term, and the
  hit's `terms` holds what was said. Python's `re` has no timeout, so the cost is bounded
  instead: patterns are compiled when the rules load and refused, with the rule's id, when
  invalid, longer than 200 characters, more than 20 in a rule, matching empty text, or repeating
  a group that itself repeats (`(a+)+`, `(\w+\s?)*`; found on the parsed pattern, and an atomic
  group or possessive repeat is allowed). Each reads one utterance, at most 2,000 characters,
  and a pattern that takes over 50 ms of CPU on one is named in the log. The cost is this
  thread's CPU time (`time.thread_time()`), not the clock: on a loaded machine the clock also
  counts the wait for a CPU, and the example pattern, a few microseconds of work, was named as
  taking 139 ms. Overlapping alternatives inside a repeat (`(a|ab)+`) are not detected.
- **Station alerts** (`kind = "silence"` or `"traffic"`, `alerts/watch.py`): no second monitor. Every 5 s
  `StationWatch` reads `Pipeline.status()`, the snapshot the page shows, and a running, transcribed
  station is silent while its source is down or reconnecting, has sent nothing for 15 s, or its
  audio level (the RMS of the latest chunk) is below `level_db` (default −50 dBFS) at every
  reading. After `after_minutes` (default 5) it raises one alert through the Alerter
  (`on_station`): cooldown, actions, CAP, webhooks and store as usual. A station that goes
  silent while the rule's alert is open (and within the cooldown) joins it unannounced, so a
  network outage that silences every stream is one alert. Once a station has had audio for
  10 s it is back; when every station in the alert is, `station_back` says so once ("Audio is
  back on … after 12 minutes", the alert's `reason`) through notify, log and webhooks, never
  spoken, and the alert closes, so the next silence is a new alert. Listen-only, switched-off
  and stopped stations never alert (a station stopped while silent ends its alert quietly), the
  `StationWatch` waits while the models load (sources aren't read then), and a narrowband channel
  or a watch is silent between calls by design, so only its outages count. The alert's text is
  Wavescribe's own, in English like the page, with no words heard: delivery translates it for
  `notify_language` and each `speak:<lang>` like a quote, and speaks it without the voice's
  `intro`. With no station rules the status isn't read at all.
- **Traffic announcements** (`kind = "traffic"`, opt-in): an FM station's RDS turning on TA with
  TP (TA alone means it relays another station's announcement, by EON), as `dsp/rds.py` decodes
  it into the station's health (`SourceHealth.rds`, in `Pipeline.status`) after seeing the flag
  twice in a row. Only the change from off to on alerts ("Traffic announcement on …"), also on
  a listen-only station, since RDS needs no model; announcements within the cooldown fold into
  the rule's open alert, unannounced. `stations` limits it to some stations.
- **Confirmation:** a rule with `min_mentions = N` holds its alert at `low` (stored, not
  announced) until N mentions within the cooldown, or one that names a `near` place. Mentions
  heard as another language don't count. Reaching N raises the alert, which is then announced
  through the usual "severity rose" path.
- **Grouping: one alert per incident.** Repeats of a rule (on any station) fold into its open
  alert as mentions and are not announced again, unless the severity rises. Once nobody has
  mentioned it for `cooldown_minutes` (default 15, sliding) the incident is over and the next
  mention is a new alert. While it goes on, a mention `remind_minutes` (default 60; 0 for never)
  after the last announcement brings one "still being mentioned" reminder ("STILL HIGH · water
  · …", "Mentioned 25 times since 09:40: …" with the translation already made): notification,
  log and webhooks, never spoken, and the alert's delivery state is left as it was. One
  utterance that trips several rules is announced once, by its most severe alert; the others are
  marked `covered` (the page shows them as "+ water, hazards" on the lead card). This also holds
  when an alert escalates while its previous delivery is still in flight, and for reminders. An
  escalation that comes just as a delivery has recorded its outcome is announced once, by
  itself: the finishing delivery sees the alert pending again and leaves it alone.
  Every alert event handed on (the page, `/feed/v1/events`, webhooks, the agent tools) carries
  `change`: `new` (its first announcement), `escalated` (announced again, more serious),
  `reminder`, `ended` (a station alert is over), or `update` (a mention folded in, a
  translation, a delivery outcome: nothing announced). Webhooks only ever get the first four;
  CAP sends the last three as an `Update` with its own identifier in the alert's `incidents`.
- **Robustness:** alerts are delivered even when the database can't be written. A delivery that
  couldn't run at all (its queue full, an error) shows as `failed`, and the rule's next mention
  announces it. One whose actions ran but reached nobody (every notify and speak failed: a
  notification that times out, a voice that isn't installed) is tried again at a mention 5
  minutes later, then 10, 20, 40 and 60, as `change: update`, without sending its webhooks
  again. Tried at every mention, as it first was, notifications that timed out announced one
  story 19 times in ten minutes in testing, webhooks included.
- **Delivery:** the severity's actions come from `[actions]` in `config/alerts.toml`. Translations
  for the notification language and each `speak:<lang>` are requested ahead of the station
  backlog (`Pipeline.request_translation`, queue priority 0), and delivery waits at most
  `translation_wait_s` (default 10 s), then goes out with what it has. Each translation must pass
  the language check (py3langid) or it's discarded; models list targets they were measured
  inventing content for in `unsafe_targets` (MADLAD: `uk`), and those are never requested. A
  spoken alert says `intro` plus the checked translation (or the words as said, when they are in
  the voice's language), or the language's fixed `fallback` phrase, so nothing unchecked is
  spoken and a voice always has words to say. The voice must be installed
  (`say -v Unknown` silently uses the default voice, which would read one language as
  another): with a voice that isn't, that `speak` action fails, nothing is spoken in that
  language, and the alert shows as partly delivered, or as failed when nothing else reached
  anyone (a warning at start, `alerts test` and `doctor` name the missing voice).
  Actions run one at a time on one thread, so speech never overlaps. Once an alert is above
  low, its quote is also translated into its stations' `alert_languages` (`stations.toml`),
  asked for after the delivery's own translations and never waited for, so the page, CAP and
  webhooks carry it in those languages whoever is notified in them.
- **Delivery state** (bandwatch's lesson): `pending`, then `delivered`, `partial`, `failed`,
  `logged` (log only) or `covered`. `delivered_at` is set only when an action that reaches a
  person confirmed it, and each action's outcome is stored (`actions`).
- **CAP 1.2:** `wavescribe alerts cap <id>` and `GET /api/alerts/<id>.cap` render an alert as a
  CAP document: one `<info>` in the source language with the quote, and one per checked
  translation, marked `machine_translation`. Certainty and urgency are always `Unknown`: a keyword
  on the radio says nothing about either. Severity maps critical/high/medium/low to
  Extreme/Severe/Moderate/Minor. A station alert's category is `Infra` (silence) or
  `Transport` (traffic), and its note says it was seen on the station's status, not in what was
  said.
- **Views:** the page has an alert feed (recent alerts above `low`, in the viewer's language when
  a checked translation exists; tap to open the station, ▶ to replay) and highlights the words
  that matched in the original text, or underlines the utterance when a translation is shown.
  `wavescribe alerts` lists alerts; `wavescribe alerts check "<text>" --language pt-PT` shows what
  a sentence would trigger (with the rules `serve` runs: alerts.toml plus the page's changes, as
  for `listen`, `test` and `replay`; `--settings` points `check`, `test` and `replay` at the page
  settings `serve --settings` uses); `wavescribe alerts test` sends a notification and speaks each voice;
  `wavescribe alerts replay --alerts <draft.toml>` runs every stored transcript through a draft
  rule file with the real `Alerter` (cooldown, folding, confirmation, one announcement per
  sentence), a clock that follows the recordings, and actions that do nothing. It prints what
  would have been announced, so rules are tuned on what the stations actually say. It sees only
  what was stored: a loop the ASR checks dropped reached the rules live but was never written
  down, so its low alert (never announced) isn't replayed, and where it folded into an open
  alert live, that alert's cooldown ran longer live than in the replay, which can then announce
  a later mention that live folded in.

## Data model (SQLite)

`data/wavescribe.db` (`store.py`):

- `stations(id, name, language)`
- `segments(id, station_id, t_start, t_end, kind DEFAULT 'speech', audio_path NULL, audio_bytes)`
- `transcripts(segment_id, lang, text, model, confidence, latency_ms, flags)`; `flags` holds the ASR checks' verdicts, e.g. `lang:es`, `rechecked` once Whisper listened again (schema 2; schema 1 is migrated on open), and `untranslated:<target>:<why>` for each translation the reply check left out, written with the line's translations (`Store.add_translations`). A re-check that replaced the text updates the row in place (`Store.update_transcript`: text, model, flags), and the FTS triggers follow
- `translations(id, segment_id, lang, text, model, latency_ms)`, unique per (segment, language)
- `transcripts_fts`, `translations_fts`: FTS5 external-content indexes kept in step by triggers

- `alerts(id, rule_id, severity, first_at, last_at, stations, language, quote, terms, mentions, segment_id, status, delivered_at, delivered_severity, actions)`: one per rule firing, repeats folded in; the quote keeps the source text after its transcript is pruned (schema 3)
- `alert_mentions(alert_id, segment_id, severity, terms, spans)`: each utterance's hit, with character spans for highlighting; deleted with the utterance
- `alert_summaries(alert_id, lang, text, model)`: checked translations of the quote, one row per language, so no language is hard-coded in the schema
- `alerts.kind` (`keyword` or `llm`) and `alerts.reason` (the LLM's one-line reason), schema 4. A station alert has kind `silence` or `traffic`, Wavescribe's own words as its quote (language `en`), no mentions rows, and "audio is back …" as its reason once it ended
- `deliveries(id, output_id, event, ref, t, ok, status, error, attempts, ms, delivery, after_restart)`: one row per output request, capped at 10,000 rows (schema 4); `delivery` is an alert's delivery id and `after_restart` says the alert had waited through a restart (schema 8). An alert given up as too old, or dropped from a full queue, is a row too, failed, with the reason
- `outbox(id, output_id, delivery_id, event, raised_at, attempts, next_at, privacy, first_at)`: one row per alert on its way to an output, until it is delivered, refused for good, older than the output's `alert_max_age_hours`, or its output is removed; at most 2,000 per output. `event` is the JSON as it is sent, `privacy` the classes of radio its words were heard on (NULL: no words), and `first_at` the alert's first mention, which `Store.prune_outbox()` counts a class's days from (schema 8)
- `segments.privacy` (the class the row was heard under), `alerts.privacy` (the classes of the alert's stations when heard, comma-separated), index `segments_class`, schema 6; at start, older rows take their station's current class if it isn't broadcast
- `catches(id, station_id, freq_hz, t_start, t_end, mode, band, label, snr_db, peak_db, voice, reason, privacy)`: one row per transmission a watch caught, kept and pruned like text of its class (not written at all where the class keeps no text); its words are the segments of the same station and frequency over the same moments, read with their translations under the same rules (`Store.catches`). `segments.freq_hz` (where radio was heard), schema 7
- `alerts.checked` (1 the LLM confirmed the match of a rule with `verify`, 0 the match was raised without its answer, NULL otherwise and in rows from before), schema 9

The schema version lives in `PRAGMA user_version`. The database runs in WAL mode, so `wavescribe
search` can read while `listen` or `serve` writes. Searches read through a second, read-only
connection, so a `LIKE` scan never delays a write. A segment row is written as soon as ASR
finishes; its translations are added when they arrive. Empty transcripts (silence) are not stored.

**Search** uses FTS5's `trigram` tokenizer with `remove_diacritics 1`. Trigrams need no word
boundaries, so search works the same in languages written without spaces (Japanese, Chinese,
Thai). It ignores case and accents, and it needs no per-language stemmer, which keeps the store
language-agnostic. Words shorter than 3 characters can't use a trigram index, so a query
containing one falls back to `LIKE`. Query text is always quoted, so FTS operators in a query are
searched as plain text. The index finds each word as a substring, which alone made "eau" find
"niveau"; `search.py` then keeps the rows where every word starts a word (after a space,
punctuation or the start), so a word also finds its inflections that add letters at the end
("agua" finds "águas"). It folds case and accents as the keyword rules do (`rules/text.py`):
accents only where they are optional, so a Devanagari nukta or a kana voicing mark still counts. `*word` asks for a fragment anywhere inside words (compounds), and in
scripts written without spaces every word matches anywhere. Words in double quotes are a
phrase: the index is asked for its words of three characters or more (so "corte de água" stays
an index lookup despite "de"), and `search.py` keeps the rows where they follow one another with
only spaces or punctuation between. The same code serves `wavescribe
search`, `/api/search` and everything built on it, and marks the matches in a snippet of about 80
characters.

**Speech audio** is archived as Opus (20 kbit/s, `-application voip`: about 2.5 KB per second of
speech) under `data/audio/<station>/<date>/<segment>.ogg`, encoded by an ffmpeg subprocess on its
own thread behind a bounded queue (a full queue skips the audio, never the text). Replay decodes
it back to 16 kHz PCM with ffmpeg.

**Retention.** Text, translations and their
index cost about 5.5 bytes per character (measured on real transcripts), so four talk stations
with one translation add about 11 GB a year. Forever would break the bounded-disk rule, so
text is kept for `--text-days` (default 365). Speech can't arrive faster than real time, so an age
limit is also a hard size limit, with no feedback loop. A byte cap was tried first and dropped:
FTS5 frees index pages only when it merges segments (even with `secure-delete`, which is on),
so the file size lags behind deletions and a "delete until under N GB" loop over-deletes. Speech
audio is kept for `--audio-days` (default 7) and capped at `--audio-max-gb` (default 2 GB), oldest
first. Both are checked every ten minutes, as utterances arrive and in the background. Settings →
Storage and privacy on the page changes all three while running (saved in `config/ui.toml`): 0 audio days stops
recording; 0 text days keeps text with no age limit, so watch the disk figure next to it. That is
the opposite of `text_days = 0` in `[radio.policy.<class>]`, which keeps no text at all
([RADIO.md](RADIO.md), *You decide what is kept*). Alerts are
kept with their quote (at most one per rule per cooldown, so they stay small). Music is never stored: the gate drops it before ASR.

## Key decisions (with rationale)

| Decision | Why | Revisit when |
|---|---|---|
| Python 3.12 via uv | ML wheels (MLX, onnxruntime, LiteRT, CTranslate2) lag new Pythons | Deps support 3.13+ |
| An `rtl_sdr` pipe, **never pyrtlsdr** | pyrtlsdr is GPL-3.0 (an import would bind us), and ≥0.4 fails against Homebrew librtlsdr | — |
| ffmpeg as a subprocess only | The Homebrew build is GPL-3.0-or-later | — |
| Own channelizer (numpy/scipy), not SDR++ multi-VFO or GNU Radio | SDR++ multi-VFO with audio sinks hangs on macOS (#1702); GNU Radio's Homebrew formula is deprecated | — |
| An **ASR chain per language** (MODELS.md), with Parakeet-TDT-0.6B-v3 first for European languages | CC-BY-4.0; ~60× real time on Apple Silicon; its training data includes European Portuguese. Whisper large-v3-turbo (MIT, about 100 languages) covers the rest, and other registered models can be put in the chain. | Bake-off results per language |
| Alerts on **source-language** text | Translation is the slowest and least reliable stage, so alerts mustn't wait for it or depend on it | Never |
| **Qwen3.5-9B** (4-bit, mlx-lm, GPU) for continuous translation, alerts and secondary languages; **MADLAD-400** (CTranslate2, CPU) as the fallback | MADLAD was the plan because CPU translation would leave the GPU to ASR, but Qwen3.5 measured 2.2× faster and better, and Parakeet keeps the GPU 95% idle anyway. Apache-2.0, 201 / 419 languages. The prompt names languages from CLDR, because bare codes are ambiguous. TranslateGemma is opt-in only (Gemma Terms of Use). | Power measured on battery; a better small model |
| Avoid **Hunyuan-MT, NLLB, SeamlessM4T, MMS** and unlicensed fine-tunes | Their licences exclude the EU, forbid commercial use, or don't exist (LICENSING.md) | — |
| Silero VAD, then **YAMNet** on each utterance | Silero alone treats singing as speech. inaSpeechSegmenter was the plan, but 0.7.6 no longer runs on current TensorFlow, Keras 3 or NumPy 2 (three separate breakages), and TensorFlow alone is a 1.6 GB install. YAMNet (Apache-2.0) on LiteRT is a 4 MB model and a ~60 MB install, classifies 3 minutes of audio in 0.16 s of one CPU core, and separated songs from talk on Portuguese radio (MODELS.md). Gating whole utterances after the VAD, instead of frames before it, keeps the segmenter unchanged and costs ~2–6 ms per utterance | Lyrics leak too often (the soak test logs every verdict) |
| SQLite FTS5 | Zero-ops, offline, searchable | — |
| **Live streams paced in the source**, not by ffmpeg's `-re` | An HLS stream arrives a whole segment (6–10 s) at a time, and HLS and Icecast servers send 10–20 s at once on connect. Passed on as it came, the page's player dropped most of each burst and utterances shared one time. `StreamSource` reads ffmpeg as fast as it writes and releases 0.512 s chunks at real time: audio that comes late moves the schedule on, and audio held longer than needed (at least 6 s in hand all through a 30 s window, or over 60 s at all) is let go at once, so a burst adds no lasting delay and nothing is dropped. With `-re` the backlog would sit in the socket, where it can't be bounded or logged and a server with a faster clock eventually drops the reader. On the example stations the hold settles at 2–3 s for Icecast and one segment for HLS (`source_pace` logs each let-go) | The delay matters more than smooth listening, or a stream needs more than 6 s of slack |
| **GPL tools only as separate processes** (ffmpeg's Homebrew build, `rtl_sdr`, `rtl_power`, `rtl_test`) | Keeps the code Apache-2.0. They run as subprocesses over pipes; never link or vendor them. | — |
| **Alerts as CAP 1.2** (Common Alerting Protocol) events | The standard emergency-alert format, so Wavescribe plugs into alert hubs, dashboards and any other CAP consumer | — |
| **One announcement per sentence; one alert per incident** | A real bulletin sentence tripped four rules and would have spoken four times. Repeats of a story fold into one alert; only a rise in severity speaks again, an hourly reminder notifies without speaking, and a quiet `cooldown_minutes` ends the incident. A delivery that reached nobody is retried with a growing wait, not at every mention (2026-11-27: 19 announcements of one story in ten minutes while notifications timed out) | The soak shows missed escalations or too much noise |
| **Confirm before announcing: `min_mentions`, weak topics, `=` terms** | From the soak: the first rules would have spoken 17 false critical alerts a day. A real event is repeated on every bulletin and names places; a passing reference is said once. Tuned with `alerts replay` on 35.6 h of four stations: 0 spoken, ~4 notifications a day, and synthetic emergencies still escalate | A real event is announced late or missed (replay it) |
| **Speak only checked translations, else a fixed phrase** | A translator that answers in the wrong language, or invents content (MADLAD into Ukrainian), must never be read aloud | — |
| **A fixed reply for "can't translate"; a remark is never stored as a translation** | Given garbled recognition, the LLM sometimes wrote about the text instead ("the meaning is unclear due to recognition errors", "(a term that may be a misrecognition)"): 3 of 134 lines of one poorly recognised station, read as if the station had said it. A line without a translation is honest; a remark in its place is not. The check uses the reply's shape first because the target can be any language; the phrase lists are a second guard for the languages that have one | A real translation is left out (the flag counts them per reason) |
| **Page changes in a separate, machine-written `config/ui.toml`** | Rewriting the hand-edited TOML would drop its comments and layout; an overlay keeps both, and a broken change can be skipped without losing the file's station | — |
| **Network only on a click on this machine, or to destinations the user entered** | Keeps the offline promise while letting the page download models, search catalogues and check streams. LAN viewers can't make the machine reach out | — |
| **The feed is text and metadata only; audio stays where it was recorded** | Broadcast audio is copyrighted, and text is ~1,000× smaller over a constrained uplink | — |
| **LLM rules batch per station every ~45 s, on source text** | One prompt per batch rather than per utterance keeps the LLM's share of the GPU small (not measured on a long run), and the alert path stays independent of translation | Missed or late events in the soak |
| **Overload is shown and shed, never hidden: urgent work displaces station backlog, translations over 5 minutes late are skipped** | Seen in testing with five translated stations on a loaded machine: the translation queue stayed full of station lines, LLM rule checks were turned away and logged once each, and nothing on the page said so. Displacing the least urgent station entry keeps rule checks and alert translations running whatever the backlog; skipping stale translations stops one slow model from pushing every line later; `behind_s`, `dropped` and `load` say what is happening, counted per station over ten minutes and logged once a minute | Pausing translation while text is behind, if displacement and skipping aren't enough on a soak |
| **The page is React + TypeScript + Vite, and the built page is committed** | A framework with a build was chosen over keeping plain JS or vendoring Preact + htm, and React because most contributors already know it; at this size its overhead doesn't matter if the transcript list is virtualised. Committing `src/wavescribe/web/dist/` keeps Node off the running machine. The build works offline from `package-lock.json` and a cache, bundles every asset (no CDN or web fonts), and every dependency is in LICENSING.md first. `scripts/commit_checks.py` enforces the built page and the licence entries on every commit | — |
| **Own NFM/AM demodulators and RDS decoder** (`dsp/`), not rtl_airband and redsea | A dongle opens in one process at a time: rtl_airband would own a whole dongle, while our channels share one `rtl_sdr` pipe with the FM stations. redsea has no Homebrew formula and pulls in liquid-dsp and LGPL libsndfile. Each part is a few hundred lines of numpy/scipy, tested on synthetic IQ; broadcast FM and RDS also on air, NFM and AM not yet | A band needs a demodulator we don't have (digital voice is out of scope) |
| **`rtl_power` for scans**, as a subprocess | GPL-2.0 like the other rtl tools, already installed with librtlsdr, and fast: the FM band in 3 s, the tuner's whole range in about 30 s | A device without rtl tools (Airspy) |
| **Privacy classes from the band plan, enforced in the store, feed, outputs and alerts; the user sets what is kept** | Laws on radio that isn't broadcast differ by country, and Wavescribe can't know which apply. The defaults are cautious (private channels live only; airband, marine and amateur text kept a few days; no audio, no words exported); any frequency can be tuned once the user confirms they may receive it, and what is kept is theirs to set and their responsibility (README, RADIO.md). There is no digital decoder and no decryption | A default turns out wrong for most users |
| **The station list comes from your own scans**, not a downloaded register | National registers and community lists mostly forbid redistribution or derived works (LICENSING.md, *Radio data*); a scan describes what this antenna actually receives | An openly licensed register worth importing |
| **The background service is a per-user LaunchAgent running serve through `caffeinate -i`** | An agent runs in the user's session, so notifications, `say` and the user's model cache work, and it needs no root; a LaunchDaemon has neither. `caffeinate` with serve as its child holds the idle-sleep assertion exactly as long as serve runs, passes SIGTERM on and returns serve's exit code. The project environment's Python runs directly, not `uv run`, so a restart never syncs or reaches the network | A Mac app (it would register itself as a login item) |
| **Doctor never opens a receiver and never uses the network** | Opening a dongle disturbs the Wavescribe using it, and the self-check must be safe to run at any time, offline. Dongles are read from the USB registry; models from the caches | — |
| **Alerts on their way to an output are kept in the store (the `outbox` table), delivered at least once with a delivery id, and given up after `alert_max_age_hours`** | An alert that was raised and not yet delivered was lost with the process: the receiver was away, or Wavescribe restarted in between. `Outputs` already has the store, for the delivery log and an alert's context, and the store has one connection behind a lock that the output threads write through, so the outbox is a table in it: one file, one writer, pruned with the rest, and nothing kept under `--no-store`, alerts included. Exactly once needs the receiver's help, so it gets an id to tell a repeat by. An old alert arriving as news does more harm than a missed one, hence the age limit, which with the per-output cap bounds the table. Utterances stay in memory: the pull feed's resume is there for gaps in the text | A receiver needs order or exactly-once delivery |
| Build the glue, reuse components | No existing project covers the use case | A project appears that does |
