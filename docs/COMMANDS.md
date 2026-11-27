# Command reference

Every `wavescribe` command and every option it takes, as `--help` prints them. The guides say
what the commands are for: [GETTING_STARTED.md](GETTING_STARTED.md) for stations, alerts and
keeping it running, [RADIO.md](RADIO.md) for receivers, [FEED.md](FEED.md) and
[AGENT_TOOLS.md](AGENT_TOOLS.md) for other programs.

Run a command from the Wavescribe folder, as `uv run wavescribe <command>`. `--help` after any
command prints its options (`uv run wavescribe radio scan --help`), and `wavescribe --version`
the version. A test keeps this page complete: it fails when a command or an option is missing
here.

- [`models`](#wavescribe-models): list and download models
- [`serve`](#wavescribe-serve): the page
- [`listen`](#wavescribe-listen): stations in the Terminal
- [What is kept and which models run](#options-for-what-runs): options `serve`, `listen`,
  `radio tune` and `radio watch` share
- [`alerts`](#wavescribe-alerts): the alerts raised, and trying rules and voices
- [`search`](#wavescribe-search): what was said
- [`radio`](#wavescribe-radio): receivers
- [`service`](#wavescribe-service) and [`doctor`](#wavescribe-doctor): the background service
  and the self-check
- [`follow`](#wavescribe-follow), [`digest`](#wavescribe-digest), [`mcp`](#wavescribe-mcp) and
  [`token`](#wavescribe-token): a running Wavescribe, from the Terminal or another program
- [`record`](#wavescribe-record) and [`bakeoff`](#wavescribe-bakeoff): comparing speech models
- [Exit codes](#exit-codes)

Only `models fetch` downloads anything. Every other command runs offline.

## `wavescribe models`

List and download models ([MODELS.md](MODELS.md), [LICENSING.md](LICENSING.md)).

### `wavescribe models list`

The registered models with their licences and whether each is installed and accepted.

| Option | What it does |
|---|---|
| `--json` | one JSON object per line, for scripts and AI agents (experimental) |

### `wavescribe models fetch`

`models fetch [ID …]` downloads models from their rights holders: the ones named, or with no
id the first model of each chain (about 8 GB).

| Option | What it does |
|---|---|
| `--accept-license LICENCE` | accept an opt-in or restricted model's licence, by the licence id `models list` shows |
| `--non-commercial` | acknowledge that a restricted model is for non-commercial use only |

### `wavescribe models accept`

`models accept ID --license LICENCE` accepts the licence of an opt-in or restricted model
whose weights are already on disk.

| Option | What it does |
|---|---|
| `--license LICENCE` | the licence id, as `models list` shows it (required) |
| `--non-commercial` | acknowledge that a restricted model is for non-commercial use only |

### `wavescribe models credits`

Prints the attribution for the installed models, as Markdown. No options.

## `wavescribe serve`

`serve [STATION …]` runs Wavescribe and its page, at <http://localhost:8765>. Stations named
here start right away; with none, the ones that ran last time do. It also takes
[the options for what runs](#options-for-what-runs).

| Option | What it does |
|---|---|
| `--config FILE` | the stations file (default `config/stations.toml`, else the example) |
| `--settings FILE` | where the page keeps its changes (default `config/ui.toml`) |
| `--open` | open the page in the default browser |
| `--port PORT` | the page's port (default 8765) |
| `--host ADDRESS` | the address to listen on (default `127.0.0.1`: this machine only). Another machine that reaches it reads only, as with `--lan` |
| `--lan` | listen on every address, so other devices on the network read along (read-only); `--host` is then ignored |
| `--behind-proxy` | a reverse proxy or tunnel forwards to this server: every request counts as another machine's (read-only, feed tokens required) |
| `--public-read` | through a proxy, let anyone read the page (what was said, alerts, audio) without a feed token |
| `--allow-remote-ask` | let other machines ask the local model questions (it is busy meanwhile) |

Who can read and do what with each of these is in [FEED.md](FEED.md) and
[SECURITY.md](../SECURITY.md).

## `wavescribe listen`

`listen STATION [STATION …]` writes stations down in the Terminal, without the page: one line
per utterance (`[time] station language text`), its translations underneath, the words that
tripped a rule marked, and a `!!` line for each alert announced. What is said is stored and
checked for alerts as under `serve`, except that rules in plain language (`kind = "llm"`) and
`verify` need `serve`. A station that is only listened to, or that another Wavescribe
transcribes, is refused: there is no text to print. It also takes
[the options for what runs](#options-for-what-runs).

| Option | What it does |
|---|---|
| `--config FILE` | the stations file (default `config/stations.toml`, else the example) |
| `--duration SECONDS` | stop after this long (default: until Ctrl-C) |
| `--source SOURCE` | hear one station from somewhere else: a stream address, an audio file, or a receiver as `sdr:SERIAL/MHZ` (below). One station only |
| `--realtime` | with a file as `--source`: play it at its own pace, not as fast as it reads |

**A receiver as `--source`** is `sdr:SERIAL/MHZ`, with options after a `?`, joined by `&`:
`sdr:00001234/99.0?gain=20&ppm=1`.

| Option | What it does |
|---|---|
| `gain` | tuner gain in dB (default 20) |
| `ppm` | the dongle's crystal error in parts per million (default 0) |
| `center` | where the dongle tunes, in MHz (default: a little below the station, 0.3 MHz for broadcast FM and 0.25 MHz otherwise, since every dongle has a spike at its centre) |
| `rate` | samples per second (default 2400000) |
| `deemphasis` | broadcast FM's time constant in µs: 50 (the default) or 75, as in the Americas |
| `mode` | `wfm` (broadcast FM), `nfm` or `am` (default: what the band plan says for the frequency) |
| `channel` | the channel's width in kHz for `nfm` and `am`, 2.5 to 30 (default: the band plan's) |
| `squelch` | a fixed squelch level in dBFS (default: automatic) |
| `ack` | `ack=1` confirms that you may listen to this radio where you are, as `--acknowledge` does for `radio tune` ([RADIO.md](RADIO.md#the-law-where-you-are)) |

## Options for what runs

`serve`, `listen`, `radio tune` and `radio watch` take these too.

| Option | What it does |
|---|---|
| `--no-translate` | transcribe only |
| `--asr MODEL` | the speech model's id, for every station (default: the first in the chain that covers the station's language) |
| `--translator MODEL` | the translation model's id |
| `--no-gate` | transcribe songs too: no speech/music gate |
| `--no-store` | keep nothing in `data/wavescribe.db`: no search, no questions, no alert history |
| `--audio-days DAYS` | keep speech audio this long (default 7; 0: never) |
| `--audio-max-gb GB` | the cap on the speech audio kept (default 2) |
| `--text-days DAYS` | keep text and translations this long (default 365; 0: no age limit). About 11 GB a year for four talk stations |
| `--alerts FILE` | the alert rules file (default `config/alerts.toml`, else the example) |
| `--no-alerts` | no alerts: no rule runs |
| `-v`, `--verbose` | log pipeline events to the Terminal |

Under `serve`, what Settings → **Storage and privacy** has saved replaces the three limits.
They are for broadcast radio and internet stations; other radio keeps what its class says
([RADIO.md](RADIO.md#what-wavescribe-does-with-each-kind-of-radio)).

## `wavescribe alerts`

The alerts raised, and ways to try rules and voices
([GETTING_STARTED.md](GETTING_STARTED.md#7-get-alerts)). `alerts` alone is `alerts list`.

| Option | What it does |
|---|---|
| `--json` | one JSON object per line, for scripts and AI agents (experimental; [AGENT_TOOLS.md](AGENT_TOOLS.md)) |
| `--config FILE` | the stations file that `--json` names stations from (default: the one `serve` reads; a station not in it goes by the name it was stored with) |

### `wavescribe alerts list`

The latest alerts, oldest first: number, when, severity, rule, stations, whether it was
delivered, and what was said, with `(+2)` for further mentions. Takes `--json` and `--config`
as above.

| Option | What it does |
|---|---|
| `--limit N` | at most this many (default 20) |

### `wavescribe alerts cap`

`alerts cap ID` prints one alert as a CAP 1.2 (Common Alerting Protocol) XML document, for
programs that take CAP. `ID` is the alert's number, as `alerts list` shows it (`#12` is `12`).
No options.

### `wavescribe alerts check`

`alerts check "SENTENCE"` says which word rules a sentence would trigger, at what severity and
with which actions; nothing is raised. Exit code 1 when no rule matches. It also says when the
rules file has a key it doesn't take, and when topic rules have no word list in that language.

| Option | What it does |
|---|---|
| `--language TAG` | the sentence's language, as BCP-47 (`es-ES`); required |
| `--alerts FILE` | the alert rules file (default `config/alerts.toml`, else the example) |
| `--settings FILE` | where the page keeps its rules and changes (default `config/ui.toml`) |

### `wavescribe alerts replay`

Runs everything stored through the rules and says what would have been announced: how many
alerts at each severity, per day, and per rule with its commonest words. For trying a draft
before using it.

| Option | What it does |
|---|---|
| `--alerts FILE` | the rules to try (default `config/alerts.toml`, else the example) |
| `--settings FILE` | where the page keeps its rules and changes (default `config/ui.toml`) |
| `--hours HOURS` | only the last hours (default: everything stored) |
| `--examples N` | also show the last N announcements |

### `wavescribe alerts test`

Sends a test notification and speaks each voice's fixed phrase, naming the voices that
aren't installed.

| Option | What it does |
|---|---|
| `--language TAG` | only this voice's language |
| `--alerts FILE` | the alert rules file (default `config/alerts.toml`, else the example) |
| `--settings FILE` | where the page keeps its voices and changes (default `config/ui.toml`) |

## `wavescribe search`

`search WORDS` searches everything heard so far, originals and translations, oldest match
first. A word matches at the start of a word (`*word`: anywhere in one); words in double
quotes are a phrase, quoted again for the shell: `'"boil water"'`. Exit code 1 with no match.

| Option | What it does |
|---|---|
| `--station ID` | only this station |
| `--limit N` | at most this many (default 30) |
| `--json` | one JSON object per line, for scripts and AI agents (experimental; [AGENT_TOOLS.md](AGENT_TOOLS.md)) |
| `--config FILE` | the stations file that `--json` names stations from (default: the one `serve` reads) |

## `wavescribe radio`

Receivers: scan bands, see what was heard, tune a frequency, watch a band
([RADIO.md](RADIO.md)). `RECEIVER` is a receiver's id from the stations file (`a`) or a
dongle's serial. A command that opens a receiver is refused while another program has it
open, a running Wavescribe included; the message says where the page does the same.

These three options mean the same wherever a `radio` command takes them:

| Option | What it does |
|---|---|
| `--config FILE` | the stations file (default `config/stations.toml`, else the example) |
| `--region REGION` | the band plan's region: `cept` (Europe), `1`, `2`, `3` (the ITU regions) or `jp` (default: `[radio] region` in the stations file, else from this machine's locale) |
| `--gain DB`, `--ppm PPM` | tuner gain in dB, and the dongle's crystal error in parts per million (default: the receiver's own; for a dongle named by its serial, 30 dB and 0) |

### `wavescribe radio bands`

The band plan for your region: what is where, how it is demodulated and its privacy class.
Takes `--config` and `--region`.

### `wavescribe radio scan`

`radio scan RECEIVER [RANGE]` finds signals. `RANGE` is a band's id from `radio bands`
(default `fm`), `START:STOP` in MHz, or `all`. Takes `--gain`, `--ppm`, `--config` and
`--region`.

| Option | What it does |
|---|---|
| `--no-rds` | don't listen for FM stations' names |
| `--rds-seconds SECONDS` | the longest it listens for names at each stretch of FM it tunes (default 12) |
| `--min-snr DB` | how far above the noise a signal must be to count (default 8) |
| `--watch MINUTES` | sweep again and again for this long, to catch radio that talks now and then |

### `wavescribe radio spurs`

`radio spurs RECEIVER [RANGE]` lists the receiver's own signals (spurs), which scans then
mark. `RANGE` is where to look (default `all`). Takes `--gain`, `--ppm`, `--config` and
`--region`.

| Option | What it does |
|---|---|
| `--learn` | sweep now, with the antenna disconnected, and keep what shows up |
| `--forget` | forget the spurs learnt for this receiver |

### `wavescribe radio heard`

Everything scans have found here.

| Option | What it does |
|---|---|
| `--config FILE` | the stations file (default `config/stations.toml`, else the example) |
| `--json` | one JSON object per line (experimental) |

### `wavescribe radio rds`

`radio rds RECEIVER MHZ` prints one FM station's RDS as it arrives: name, programme type,
radiotext. Takes `--gain`, `--ppm`, `--config` and `--region`.

| Option | What it does |
|---|---|
| `--seconds SECONDS` | how long to listen (default 30) |

### `wavescribe radio tune`

`radio tune RECEIVER MHZ` listens to one frequency and prints what is said, as `listen` does
for a station; nothing is saved as a station. It takes `--gain`, `--ppm`, `--config`,
`--region` and [the options for what runs](#options-for-what-runs).

| Option | What it does |
|---|---|
| `--language TAG` | what is spoken, as BCP-47 (`es-ES`) |
| `--translate TAG …` | translate into these languages |
| `--mode MODE` | `wfm`, `nfm` or `am` (default: the band plan's) |
| `--squelch DBFS` | a fixed squelch level in dBFS (default: automatic) |
| `--acknowledge` | confirm that you may listen to this radio where you are; radio that isn't broadcast or amateur asks for it ([RADIO.md](RADIO.md#the-law-where-you-are)) |
| `--duration SECONDS` | stop after this long (default: until Ctrl-C) |

### `wavescribe radio watch`

`radio watch RECEIVER WINDOW` watches every channel of a band at once and writes down whatever
is said on any of them (experimental). `WINDOW` is a band's id or `START:STOP` in MHz, about
2.2 MHz at most. It takes `--gain`, `--ppm`, `--config`, `--region` and
[the options for what runs](#options-for-what-runs).

| Option | What it does |
|---|---|
| `--language TAG` | what is spoken, as BCP-47, or `auto` (the default): each utterance in its own language |
| `--languages TAG …` | with `auto`: the languages it may be (`de en`) |
| `--translate TAG …` | translate into these languages |
| `--mode MODE` | `nfm` or `am` (default: the band plan's) |
| `--threshold DB` | how far over a channel's noise floor counts as a transmission (default 8) |
| `--acknowledge` | confirm that you may listen to this radio where you are ([RADIO.md](RADIO.md#the-law-where-you-are)) |
| `--minutes MINUTES` | stop after this long (default, or 0: until Ctrl-C) |

### `wavescribe radio catches`

What watches caught and kept, with what was said.

| Option | What it does |
|---|---|
| `--mhz MHZ` | one channel only |
| `--width KHZ` | with `--mhz`: this many kHz either side of it as well |
| `--hours HOURS` | this far back (default 24; 0: everything) |
| `--limit N` | at most this many (default 100, up to 500) |
| `--json` | one JSON object per line (experimental) |

### `wavescribe radio note`

`radio note MHZ` names a frequency, notes something about it or tags it. Takes `--config` and
`--region`.

| Option | What it does |
|---|---|
| `--name NAME` | what to call it (also its name in `radio heard`) |
| `--note TEXT` | anything worth remembering about it |
| `--tag TAG …` | tags, such as `family hiking` |
| `--clear` | forget what was noted for it |

## `wavescribe service`

Runs `serve` in the background on macOS
([GETTING_STARTED.md](GETTING_STARTED.md#11-keep-it-running)).

### `wavescribe service install`

Starts `serve` now and at every login, restarted after a crash, and keeps the Mac from idle
sleep while it runs. It takes [`serve`'s options](#wavescribe-serve) and
[the options for what runs](#options-for-what-runs), and runs `serve` with them; run it again
to change them, from the Wavescribe folder.

| Option | What it does |
|---|---|
| `--dry-run` | print the LaunchAgent's file without installing it |

### `wavescribe service uninstall`

Stops the background service and removes it. No options.

### `wavescribe service status`

Whether it is installed and running, its pid, its last exit and where its logs are. Exit code
0 when it runs and 3 when it doesn't (not installed, or stopped).

| Option | What it does |
|---|---|
| `--json` | one JSON object per line, for scripts and AI agents (experimental) |

## `wavescribe doctor`

Checks tools, Python, models, receivers, disk, configuration, the page's port, voices and the
background service, and says what to fix. It works offline and opens no receiver. It takes
[`serve`'s options](#wavescribe-serve) and
[the options for what runs](#options-for-what-runs), to check what `serve` with them needs;
with none, it checks the options of the installed background service. Exit code 1 when
something is broken.

| Option | What it does |
|---|---|
| `--json` | one JSON object per line, for scripts and AI agents (experimental) |

## `wavescribe follow`

Prints the live feed of a running Wavescribe, one JSON object per line, until interrupted
(experimental; [AGENT_TOOLS.md](AGENT_TOOLS.md)).

| Option | What it does |
|---|---|
| `--url URL` | the running `wavescribe serve` (default `http://localhost:8765`) |
| `--token TOKEN` | a feed token, which another machine needs once tokens exist (default: `$WAVESCRIBE_TOKEN`) |
| `--stations IDS` | only these stations, comma-separated |
| `--langs LANGS` | only these translations, comma-separated |
| `--alerts-only` | alerts, no utterances |
| `--since-id ID` | first, the utterances after this point (up to 1,000): the `resume` of the last line handled |
| `--since EPOCH` | first, the utterances heard since this time, in seconds since 1970 (up to 1,000) |
| `--resume FILE` | keep the resume point in `FILE` after each line printed, and start from it; `--since-id` and `--since` count only while the file holds none |

## `wavescribe digest`

A few points on what some stations said while you weren't listening, picked by the running
Wavescribe's local model (experimental;
[GETTING_STARTED.md](GETTING_STARTED.md#6-search-and-ask)).

| Option | What it does |
|---|---|
| `--url URL` | the running `wavescribe serve` (default `http://localhost:8765`) |
| `--token TOKEN` | a feed token, which another machine needs once tokens exist (default: `$WAVESCRIBE_TOKEN`) |
| `--stations IDS` | only these stations, comma-separated |
| `--minutes MINUTES` | how far back to look (default 60; with `--now`, 5) |
| `--since TIME` | from this time on: a time of day like `21:30`, or seconds since 1970 |
| `--until TIME` | up to this time rather than now (not with `--now`); `--minutes` then counts back from it |
| `--language LANG` | write it in this language, as BCP-47 (default: the stations' translations) |
| `--now` | what one station is on about now: the one `--stations` names, else the station heard last |
| `--speak` | also read its points aloud on the Wavescribe machine; an alert stops it at once |
| `--json` | the server's answer, as one line of JSON |

## `wavescribe mcp`

A Model Context Protocol server on stdin and stdout, for an AI agent, in front of a running
Wavescribe on this machine (experimental; [AGENT_TOOLS.md](AGENT_TOOLS.md)).

| Option | What it does |
|---|---|
| `--url URL` | the running `wavescribe serve` (default `http://localhost:8765`) |
| `--token TOKEN` | a feed token (default: `$WAVESCRIBE_TOKEN`) |
| `--allow-control` | also offer tools that start and stop stations and add an internet station, and let `digest` and `now` read their answer aloud |

## `wavescribe token`

Makes, lists and revokes the feed tokens other machines and programs read with
([FEED.md](FEED.md#who-can-read-what)). Run it on the Wavescribe machine, in the folder Wavescribe runs
in, while it runs.

| Option | What it does |
|---|---|
| `--url URL` | the running `wavescribe serve` on this machine (default `http://localhost:8765`); `make`, `list` and `revoke` take it too |

### `wavescribe token make`

Makes a token and prints it once. Takes `--url`.

| Option | What it does |
|---|---|
| `--label TEXT` | what it is for, shown in the list |
| `--scope SCOPE` | `read` (read the feed and the page; the default) or `push` (send events here); give it twice for both |

### `wavescribe token list`

The tokens made, with their ids, scopes and labels, without their secrets. Takes `--url`.

### `wavescribe token revoke`

`token revoke ID` revokes a token now, by the id `token list` shows; its streams end. Takes
`--url`.

## `wavescribe record`

`record STATION` saves a station's internet stream to a WAV clip (16 kHz, mono) for `bakeoff`.
Broadcast audio is copyrighted: a clip is for your own comparison, on this machine.

| Option | What it does |
|---|---|
| `--minutes MINUTES` | how long to record (default 15) |
| `--out FILE` | where to save it (default `data/bakeoff/<station>-<time>.wav`) |
| `--config FILE` | the stations file (default `config/stations.toml`, else the example) |

## `wavescribe bakeoff`

`bakeoff CLIP [CLIP …]` runs several speech models over clips from `record` and prints a
comparison: each model's speed, its slowest utterance, the time it takes to load, and how far
the models' texts are apart. The report is also saved under `data/bakeoff/`, and each model's
text beside the clip. Agreement between models is not accuracy: for a word error rate, correct
the `<clip>.ref-template.txt` it writes, save it as `<clip>.ref.txt` and run it again.

| Option | What it does |
|---|---|
| `--models IDS` | the models' ids, comma-separated (required) |
| `--language TAG` | the clips' language, as BCP-47 (required) |

## Exit codes

| Code | When |
|---|---|
| 0 | done |
| 1 | it couldn't be done, with the reason after `wavescribe:`; also `search` and `alerts check` with no match, and `doctor` with something broken |
| 2 | a command or an option it doesn't take, or a value it refuses |
| 3 | `service status`: the service isn't running |
| 130 | stopped with Ctrl-C |
