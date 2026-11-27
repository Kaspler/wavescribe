<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/lockup-on-dark.svg">
    <img alt="Wavescribe" src="assets/lockup-on-light.svg" height="56">
  </picture>
</h1>

**Wavescribe listens to the radio for you.** It follows several stations at once, writes down
what is said, translates it into your language, and **alerts you when something you care about
comes up**: a road closure, a strike, a water cut, a wildfire. It works with internet radio, and
with FM and other radio through an inexpensive RTL-SDR receiver. Everything runs on your Mac,
with no cloud and no account.

![The Wavescribe page following three synthetic test broadcasts (text-to-speech, not real stations) in French, German and Spanish, each sentence shown with its machine translation into English, and a "Road closed" alert raised by the German traffic report](assets/demo.gif)

*Test broadcasts made with text-to-speech, not real stations. The recognition, the translation
and the alert are the app's own output.*

Early alpha, for macOS on Apple Silicon.
**[Getting started](docs/GETTING_STARTED.md)** · [Documentation](#documentation)

**Not a safety system.** Wavescribe can miss an announcement, or raise an alert for something
that wasn't said: reception drops, speech recognition and machine translation make mistakes, and
this is early software. Use it to stay informed, never as your only source where life, health or
property is at stake: follow official warnings and the emergency services.

## What it's for

Radio is live, in one language, one station at a time, and gone once it's said. Wavescribe
writes it down, translates it and keeps it, so it can be read, searched and acted on.

**To understand it**

- **Where you don't speak the language.** Read local news and official announcements in your own
  language, and get an alert for what matters to you: a wildfire, a flood warning, a strike, a
  road closure, a power or water cut.
- **Learning a language, or keeping up with home.** Real radio, each sentence next to its
  translation, and any sentence can be played again.
- **Radio you can read.** For deaf and hard-of-hearing people, radio as text, sentence by
  sentence, a few seconds behind the broadcast, on the Mac or on a phone. Speech recognition
  makes mistakes; this is not a certified captioning service.
- **Out of curiosity.** Plug in a receiver and see what is on the air around you: one scan lists
  the stations it hears, with their names where they send one.

**To keep track and act**

- **Many stations, one reader.** Journalists, researchers, OSINT analysts and media monitors
  follow several stations in several languages, search everything that was said, and ask
  questions that are answered with the quoted moments.
- **Automations.** An alert can call a webhook, so a home-automation system or a chat bot can act
  on it: flash a lamp, post a message.
- **Give your AI agent a radio.** An AI agent can search what was said, ask about it, and wait for
  alerts to act on, through an MCP server or JSON on the command line; experimental, see
  [Agent tools](docs/AGENT_TOOLS.md).
- **One listener, many readers.** One Mac follows the radio and everyone on the same Wi-Fi reads
  along on a phone: a household, a club, a community centre.

**Where there's no internet**

- **Off the grid.** On a boat, in a campervan or a cabin: with a receiver it needs no internet.
- **When the power and the networks go down.** Broadcast radio keeps working. During the power
  cut of 28 April 2025 in Spain and Portugal, radio was how people learned what was going on, in
  Spanish and Portuguese only.

## What it does

What is implemented today, one line each, with a link to how to use it. *(experimental)* marks
what is newer and less tried.

**Stations and sources**

- Follows several stations at once, each in its own language.
  [Choose your stations](docs/GETTING_STARTED.md#4-choose-your-stations)
- Internet radio from a station directory, searched by name, country or language, offline too
  once downloaded. [Choose your stations](docs/GETTING_STARTED.md#4-choose-your-stations)
- An internet station by its stream address (URL), or from an `.m3u` or `.pls` playlist.
  [Choose your stations](docs/GETTING_STARTED.md#4-choose-your-stations)
- A ready-made list of news stations in eight languages.
  [Choose your stations](docs/GETTING_STARTED.md#4-choose-your-stations)
- FM broadcast radio through an RTL-SDR receiver (a USB dongle), with no internet.
  [Use a radio receiver](docs/GETTING_STARTED.md#9-use-a-radio-receiver-sdr)
- Several FM stations on one receiver when they sit close together.
  [Planning frequencies](docs/HARDWARE.md#planning-frequencies)
- Several receivers at once, also sharing one antenna through a splitter (experimental).
  [Sharing one antenna](docs/HARDWARE.md#sharing-one-antenna)
- Walkie-talkie (PMR446), amateur, marine and aviation channels as stations, in NFM and AM
  (experimental). [Keeping a frequency as a station](docs/RADIO.md#keeping-a-frequency-as-a-station)
- Each station is listen only, transcribed, or transcribed and translated; listen only needs no
  model. [Listen only, transcribe, or translate](docs/RADIO.md#listen-only-transcribe-or-translate)
- Language `auto`, for a station whose language you don't know or that switches between several.
  [Choose your stations](docs/GETTING_STARTED.md#4-choose-your-stations)
- Stations in a file, `config/stations.toml`; what you change on the page is kept apart from it.
  [Choose your stations](docs/GETTING_STARTED.md#4-choose-your-stations)
- Remote stations: what another Wavescribe transcribes, followed as text.
  [Many places, one central server](docs/FEED.md#many-places-one-central-server)

**Reading and listening**

- A page in the browser, `wavescribe serve`: the live transcript of one station, or of all side
  by side. [Read along, listen, translate](docs/GETTING_STARTED.md#5-read-along-listen-translate)
- Transcribes 25 European languages with the default model; about 100 with Whisper, a second
  download. [Speech recognition chain](docs/MODELS.md#speech-recognition-chain-default-order)
- Skips most music, so songs aren't written down.
  [Speech vs music](docs/MODELS.md#speech-vs-music)
- Plays any station live, and any line again from its time.
  [Read along, listen, translate](docs/GETTING_STARTED.md#5-read-along-listen-translate)
- Per device: text size, light or dark theme, and which stations are shown.
  [Read along, listen, translate](docs/GETTING_STARTED.md#5-read-along-listen-translate)
- Keyboard shortcuts on the page; `?` lists them.
  [Read along, listen, translate](docs/GETTING_STARTED.md#5-read-along-listen-translate)
- The same transcript in the Terminal, without the page: `wavescribe listen`.
  [Read along, listen, translate](docs/GETTING_STARTED.md#5-read-along-listen-translate)

**Translation**

- Translates each station into one or more languages as it is said, with a model on your Mac.
  [Choose your stations](docs/GETTING_STARTED.md#4-choose-your-stations)
- Each reader chooses the language to read in, with or without the original beside it.
  [Read along, listen, translate](docs/GETTING_STARTED.md#5-read-along-listen-translate)
- Alerts translated into further languages, for readers and webhooks (`alert_languages`).
  [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)
- A station can have its own speech model and translation model.
  [Listen only, transcribe, or translate](docs/RADIO.md#listen-only-transcribe-or-translate)

**Alerts**

- Keyword alerts: words and phrases per language, matched on the station's own words, not on
  the translation. [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)
- The local model suggests your words in each station's language, and can confirm each match in
  context. [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)
- Regular expressions (regex) in a keyword rule, such as a road and its number.
  [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)
- Rules in plain language: describe what you want to hear about, and the local model checks for
  it (LLM rules). [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)
- A rule's severity decides what happens: a highlight in the transcript, a Mac notification, a
  spoken alert. [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)
- Spoken alerts in the languages you choose, each with its own macOS voice.
  [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)
- A story repeated on air is one alert: further mentions are counted, with a reminder while it
  goes on. [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)
- Silence alert: a station that sends no audio for some minutes, and a notification when it is
  back. [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)
- Traffic alert: an FM station flags a traffic announcement in its RDS.
  [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)
- Recent alerts on the page, to play or to find in the transcript; `wavescribe alerts` lists
  them. [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)
- Webhooks: an alert sent to your own program, a chat bot or home automation, as JSON, CAP 1.2
  XML or text. [Push: outputs](docs/FEED.md#push-outputs)
- A test of the notification and of each voice: `wavescribe alerts test`.
  [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)
- Rules tried on a sentence (`alerts check`) or on everything heard so far (`alerts replay`);
  nothing is raised. [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)

**Finding radio and watching bands**

- Scans a band and lists the signals it finds, strongest first: `wavescribe radio scan`.
  [Finding stations](docs/RADIO.md#finding-stations)
- Names FM stations from their RDS; `radio rds` shows one station's name, programme type and
  radiotext live. [Finding stations](docs/RADIO.md#finding-stations)
- The dial on the page: the bands a receiver can tune, what each one is, and what was heard on
  it. [Finding stations](docs/RADIO.md#finding-stations)
- Band plans by region (Europe, Africa and the Middle East, the Americas, Asia-Pacific, Japan):
  `radio bands`. [Band plans and regions](docs/RADIO.md#band-plans-and-regions)
- A list of everything your scans heard, kept on the Mac: `radio heard`.
  [Finding stations](docs/RADIO.md#finding-stations)
- Learns a receiver's own signals (spurs) with no antenna, so scans leave them out:
  `radio spurs`. [Finding stations](docs/RADIO.md#finding-stations)
- Receivers set up on the page: gain, centre frequency, and a live spectrum to test the antenna.
  [Your first receiver](docs/RADIO.md#your-first-receiver)
- Listening by hand: spectrum and waterfall, click to tune, seek, squelch, gain; nothing is
  stored. [Listening by hand](docs/RADIO.md#listening-by-hand)
- A scanner over a band's channels that stops where someone talks, and can skip a busy channel.
  [Listening by hand](docs/RADIO.md#listening-by-hand)
- Any frequency kept as a station, or transcribed from the Terminal with `radio tune`.
  [Keeping a frequency as a station](docs/RADIO.md#keeping-a-frequency-as-a-station)
- Band watch (experimental): transcribes every channel of a band at once, such as
  walkie-talkies: `radio watch`.
  [Catching radio that only talks now and then](docs/RADIO.md#catching-radio-that-only-talks-now-and-then)
- Caught here (experimental): what a watch caught, by frequency and time, with its words:
  `radio catches`.
  [Catching radio that only talks now and then](docs/RADIO.md#catching-radio-that-only-talks-now-and-then)
- A name, a note and tags for a frequency: `radio note`.
  [Catching radio that only talks now and then](docs/RADIO.md#catching-radio-that-only-talks-now-and-then)
- Asks before listening to radio that isn't broadcast or amateur: aircraft, ships,
  walkie-talkies.
  [What Wavescribe does with each kind of radio](docs/RADIO.md#what-wavescribe-does-with-each-kind-of-radio)
- Radio that isn't broadcast is kept for a few days or not at all by default; you set what is
  kept of each kind. [You decide what is kept](docs/RADIO.md#you-decide-what-is-kept)
- Receive only: no transmitting, no digital decoder, no decryption.
  [What Wavescribe does with each kind of radio](docs/RADIO.md#what-wavescribe-does-with-each-kind-of-radio)

**Search, Ask and summaries**

- Search everything heard, originals and translations, in any language: on the page or with
  `wavescribe search`. [Search and ask](docs/GETTING_STARTED.md#6-search-and-ask)
- Ask a question in plain language: the answer cites the stations and moments it rests on, which
  can be played. [Search and ask](docs/GETTING_STARTED.md#6-search-and-ask)
- Summary (experimental): a few points from what was said while you were away:
  `wavescribe digest`. [Search and ask](docs/GETTING_STARTED.md#6-search-and-ask)
- "What are they talking about now?" for one station (experimental): `wavescribe digest --now`.
  [Search and ask](docs/GETTING_STARTED.md#6-search-and-ask)
- Summaries on a schedule (experimental), as a quiet notification, read aloud if you like.
  [Get alerts](docs/GETTING_STARTED.md#7-get-alerts)
- You set how long text and speech audio are kept; the page downloads the last 24 hours as CSV.
  [What Wavescribe keeps, and why](docs/GETTING_STARTED.md#10-what-wavescribe-keeps-and-why)

**Phones and other readers**

- Phones and tablets on the same Wi-Fi read along and listen, read-only:
  `wavescribe serve --lan`.
  [Read along on your phone](docs/GETTING_STARTED.md#8-read-along-on-your-phone)
- Other devices can use Ask only when you allow it: `--allow-remote-ask`.
  [Read along on your phone](docs/GETTING_STARTED.md#8-read-along-on-your-phone)
- Reading over the internet through your own reverse proxy: `serve --behind-proxy`, with read
  tokens. [Who can read what](docs/FEED.md#who-can-read-what)
- Radio that isn't broadcast stays on the Mac by default: other devices don't see it.
  [What Wavescribe keeps, and why](docs/GETTING_STARTED.md#10-what-wavescribe-keeps-and-why)

**For other programs and agents**

- A text feed over HTTP, `/feed/v1`: live events, and what was said as JSON, NDJSON or CSV.
  [Pull: /feed/v1](docs/FEED.md#pull-feedv1)
- Webhooks and live pushes of alerts or of every line, with filters, retries, signatures and a
  delivery log. [Push: outputs](docs/FEED.md#push-outputs)
- Feed tokens, to read or to push, made on the page or with `wavescribe token`.
  [Who can read what](docs/FEED.md#who-can-read-what)
- One central Wavescribe that collects the text of many places, by pulling or by taking pushes.
  [Many places, one central server](docs/FEED.md#many-places-one-central-server)
- Every alert as a CAP 1.2 (Common Alerting Protocol) document: `wavescribe alerts cap`.
  [Event format](docs/FEED.md#event-format-version-1)
- Monitoring: `/healthz` and Prometheus `/metrics`. [Pull: /feed/v1](docs/FEED.md#pull-feedv1)
- Agent tools (experimental): an MCP server, `wavescribe mcp`, to search, ask, summarise and
  wait for an alert. [MCP](docs/AGENT_TOOLS.md#1-mcp-tools-your-agent-can-call)
- An agent can add, start and stop stations only when you start it with `--allow-control`.
  [MCP](docs/AGENT_TOOLS.md#1-mcp-tools-your-agent-can-call)
- JSON on the command line (experimental): `--json` on commands, and `wavescribe follow` for the
  live feed. [The command line](docs/AGENT_TOOLS.md#2-the-command-line---json-and-follow)

**Running it**

- Models are downloaded once from their publishers, with `wavescribe models fetch` or on the
  page. [Download the models](docs/GETTING_STARTED.md#3-download-the-models)
- The page lists each model's licence and size and whether it fits this Mac, and can search for
  more online. [Will it fit?](docs/MODELS.md#will-it-fit-settings--languages-and-models)
- Models, their licences and what is installed: `wavescribe models list`; their attribution:
  `models credits`. [Disclosure to users](docs/LICENSING.md#disclosure-to-users)
- A model with terms to accept is used only once you accept them: `wavescribe models accept`.
  [Opt-in and restricted models](docs/MODELS.md#opt-in-and-restricted-models)
- A model of your own, through `config/models.local.toml`.
  [Bring your own model](docs/MODELS.md#bring-your-own-model)
- A background service on macOS (launchd) that starts at login and restarts after a crash:
  `wavescribe service`. [Keep it running](docs/GETTING_STARTED.md#11-keep-it-running)
- A self-check of tools, models, receivers, disk, settings and voices: `wavescribe doctor`.
  [Troubleshooting](docs/GETTING_STARTED.md#troubleshooting)
- A Health page: memory, processor, battery and disk, and how far behind each station is.
  [Keep it running](docs/GETTING_STARTED.md#11-keep-it-running)
- Reconnects by itself when an internet stream comes back.
  [Troubleshooting](docs/GETTING_STARTED.md#troubleshooting)
- A receiver that is unplugged and plugged back in starts its stations again.
  [Limits today](docs/HARDWARE.md#limits-today)
- Speech models compared on a recording of your own station: `wavescribe record`, then
  `wavescribe bakeoff`. [Speech recognition chain](docs/MODELS.md#speech-recognition-chain-default-order)
- **Local by design**: every model runs on your Mac; no account, no telemetry, nothing sent
  unless you set it up. [Questions](docs/GETTING_STARTED.md#questions)

Limits: it runs on macOS on Apple Silicon only. With an RTL-SDR receiver it tunes 24 to
1766 MHz, which holds FM broadcast and the VHF and UHF bands, not medium wave or shortwave.
Ideas and requests are welcome as [issues](https://github.com/Kaspler/wavescribe/issues).

## Quick start

You need a Mac with Apple Silicon, 16 GB of memory or more, about 10 GB of disk for the models,
and [Homebrew](https://brew.sh). No receiver is needed to try it: start with internet radio.

```sh
git clone https://github.com/Kaspler/wavescribe.git
cd wavescribe
brew install ffmpeg uv librtlsdr        # librtlsdr only for an RTL-SDR receiver
uv sync
uv run wavescribe models fetch          # about 8 GB of models
uv run wavescribe serve --open          # opens the page: pick stations and read along
```

Already have ffmpeg and uv? Any recent version works. Those models transcribe 25 European
languages; for others, also run `uv run wavescribe models fetch whisper-large-v3-turbo` (1.5 GB).

For news stations in eight languages, ready to try:

```sh
uv run wavescribe serve --config config/stations.world.example.toml --open
```

A few more commands:

```sh
uv run wavescribe serve --lan           # phones on the same Wi-Fi read along (read-only, no
                                        # login: only on a network you trust)
uv run wavescribe search "água"         # search everything heard
uv run wavescribe alerts test           # check notifications and the spoken-alert voices
uv run wavescribe radio scan a fm       # find FM stations with receiver `a`
```

[Getting started](docs/GETTING_STARTED.md) goes through stations, alerts, phones and a radio
receiver step by step.

## Hardware

- **None:** internet radio needs only the Mac.
- **Starter:** one RTL-SDR Blog V4 receiver and its antenna (about €40–60): a few FM stations
  that sit close together, station finding, and listening to any frequency.
- **More stations:** three receivers behind one antenna and a splitter (about €150–250).

[docs/HARDWARE.md](docs/HARDWARE.md) says which Mac, what to buy, which antenna and how to
connect it.

## How it works

```
antennas ─► SDR(s) ─► channelizer ─► audio per station
   ─► speech/music gate ─► speech recognition ─► alert rules ─► notify / speak / log / webhooks
                                              └► translation ─► transcript, web page, text feed
```

Internet streams enter at "audio per station". Details are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Documentation

| | |
|---|---|
| [Getting started](docs/GETTING_STARTED.md) | Install, stations, reading, search and Ask, alerts, phones, radio receivers, troubleshooting |
| [Hardware](docs/HARDWARE.md) | Which Mac, what to buy, antennas, connectors, setup |
| [Radio](docs/RADIO.md) | Finding stations, listening by hand, what's kept of each kind of radio, the law |
| [Feed and outputs](docs/FEED.md) | The text feed, webhooks, remote stations, using Wavescribe from another program |
| [Agent tools](docs/AGENT_TOOLS.md) | Give your AI agent a radio: the MCP server, JSON output and `follow` (experimental) |
| [Models](docs/MODELS.md) | The speech and translation models per language |
| [Licensing](docs/LICENSING.md) | The licence of every library, tool and model |
| [Architecture](docs/ARCHITECTURE.md) | How it works inside, and the decisions behind it |
| [Measurements](docs/MEASUREMENTS.md) | Speed, memory and long-run figures |

## Using it responsibly

**Don't rely on it for safety.** Wavescribe is not an emergency alert system and no substitute
for official warnings: sirens, cell broadcast, civil protection, the emergency services. An alert
may come late, be wrong, or not come at all, and a quiet page doesn't mean nothing is happening.
Transcripts and translations are made by machine: check the original before you act on one.

**You are responsible for using Wavescribe lawfully where you are.** Laws on receiving radio
differ by country and by kind of radio. Broadcast radio is meant for the public; transcripts and
recordings are for your personal use, and broadcasts are copyrighted. Internet streams also come
with their broadcaster's terms of use, which can limit where and how they may be played. Other
radio (aircraft, ships, walkie-talkies) is often protected: in many countries it may be received
but not recorded or passed on. Wavescribe asks before it listens to such radio and keeps little
of it by default; you set what is kept. It only receives: it cannot transmit, and it has no
digital decoder and no decryption.

[The law where you are](docs/RADIO.md#the-law-where-you-are) has the details. This is not legal
advice. Wavescribe is provided "as is", without warranty of any kind (Apache License 2.0,
sections 7 and 8).

## Contributing

Issues and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Wavescribe is
written with AI coding tools; if you know radio and something in the signal processing, the band
plan or the hardware guide looks wrong, an issue is the best way to say so. To report a security
problem, see [SECURITY.md](SECURITY.md).

## License

Copyright 2026 Vitali Kaspler. Licensed under the [Apache License 2.0](LICENSE).

Models aren't part of Wavescribe and are never shipped with it: you download each one from its
publisher, under its own licence ([docs/LICENSING.md](docs/LICENSING.md)). The built web page
bundles React and Lucide icons; their notices are in
[`src/wavescribe/web/dist/licenses.txt`](src/wavescribe/web/dist/licenses.txt).
