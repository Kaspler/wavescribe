# Getting started

This guide takes you from nothing to radio stations transcribed, translated and watched for
alerts on your Mac. It's organised by what you want to do, not by screen. No radio knowledge
is needed. Allow about half an hour, plus the time to download the models.

> Wavescribe is an early alpha, installed from the Terminal.

- [What you need](#what-you-need)
- [1. Install](#1-install)
- [2. First run](#2-first-run)
- [3. Download the models](#3-download-the-models)
- [4. Choose your stations](#4-choose-your-stations)
- [5. Read along, listen, translate](#5-read-along-listen-translate)
- [6. Search and ask](#6-search-and-ask)
- [7. Get alerts](#7-get-alerts)
- [8. Read along on your phone](#8-read-along-on-your-phone)
- [9. Use a radio receiver (SDR)](#9-use-a-radio-receiver-sdr)
- [10. What Wavescribe keeps, and why](#10-what-wavescribe-keeps-and-why)
- [11. Keep it running](#11-keep-it-running)
- [Troubleshooting](#troubleshooting)
- [Questions](#questions)

## What you need

- **A Mac with Apple Silicon** (M1 or later). Intel Macs, Windows and Linux aren't supported.
- **Memory:** 16 GB or more to transcribe and translate. Listening alone needs very little.
  [HARDWARE.md](HARDWARE.md#the-computer) says how many stations fit on which Mac.
- **Disk:** about 10 GB for the models, plus room for what's heard: about 30 MB a day for four
  busy stations, and up to 2 GB of speech audio by default.
- **Internet during setup**, to download Wavescribe and its models. After that it works
  offline, except for internet radio stations, which need the internet to play.
- **Optional: a radio receiver.** An RTL-SDR dongle and an antenna (about €40–60) let
  Wavescribe hear FM radio with no internet at all. You can start without one, with internet
  stations.

## 1. Install

1. Install [Homebrew](https://brew.sh) if you don't have it: it installs tools on a Mac. Open
   the **Terminal** app and paste the command from its home page.
2. Get Wavescribe: on its GitHub page, **Code → Download ZIP** and unzip it, or `git clone` it.
3. In the Terminal, go into its folder and install what it needs:

   ```sh
   cd wavescribe                        # the folder you unzipped or cloned
   brew install ffmpeg uv librtlsdr     # librtlsdr is only for a radio receiver
   uv sync                              # Python and Wavescribe's libraries, in this folder only
   ```

   **ffmpeg** reads internet radio. **uv** installs the right Python and Wavescribe's
   libraries without touching the rest of your Mac. **librtlsdr** drives an RTL-SDR receiver.
   Already have ffmpeg and uv? Any recent version works.

## 2. First run

```sh
uv run wavescribe serve --open
```

Your browser opens Wavescribe's page at <http://localhost:8765>. It keeps running as long as
this Terminal window does; press **Ctrl-C** there to stop it.

Until you choose your own stations, it shows an example list (four national stations of one
country). Press **Start** beside one to start it and listen right away: until the models are
downloaded (next step) it plays without text, and its text starts by itself once they are.
Stations that were running when you stopped Wavescribe start again next time. To start some
from the Terminal, name them: `uv run wavescribe serve <station id> …` (the ids are in the
station list, or in `config/stations.example.toml`).

## 3. Download the models

Speech recognition and translation run on your Mac, with open models you download once from
their publishers. Wavescribe never ships or uploads them.

**On the page:** the gear button at the top right opens **Settings**. Its **Languages and
models** page lists every model with its licence, its size, and whether it fits this Mac.
**Recommended for your stations** shows what your stations' languages need; download those.

**Or in the Terminal:**

```sh
uv run wavescribe models fetch
```

That's about 8 GB:

| Model | What it does | Size |
|---|---|---|
| Silero VAD | finds where someone is speaking | tiny |
| YAMNet | skips music, so songs aren't transcribed | tiny |
| Parakeet TDT 0.6B v3 | speech to text, 25 European languages | 2.3 GB |
| Qwen3.5 9B | translation, alert checks, answers to your questions | 5.5 GB |

For other languages (Arabic, Hebrew, Japanese, Chinese, Hindi and about 70 more), add Whisper:
`uv run wavescribe models fetch whisper-large-v3-turbo` (1.5 GB), or download it under Settings →
**Languages and models**. [MODELS.md](MODELS.md) lists the models per language.

Every model on the built-in list is freely licensed. Models you add from the online catalogue
may have terms to accept first; the page asks before downloading them. Stations waiting for a model get their text by
themselves when its download finishes.

## 4. Choose your stations

**On the page:** **Find** at the top of the page (or **Add a station** below the station list)
is where stations are found and added:

- **On the internet → Station directory:** search thousands of internet stations by name,
  country or language ([Radio Browser](https://www.radio-browser.info), a public directory).
  **Download the directory** once and you can search it offline too.
- **On the internet → By address:** paste a station's stream address. **Check** plays a few
  seconds to make sure it works and can identify the language.
- **On the internet → Other ways to add:** an `.m3u` or `.pls` playlist, or a station that
  another machine transcribes ([FEED.md](FEED.md)).
- **On the air:** what your receiver finds; see [section 9](#9-use-a-radio-receiver-sdr).

Each station has a **language** (what it speaks) and a **mode**, both changed later under
Settings → **Stations**:

- **Listen only:** you can play it; nothing is written down and no model is needed.
- **Transcribe:** written down and checked for alerts.
- **Transcribe and translate:** also translated into the languages under **Translate into**, as
  it speaks.

A station that already speaks your language isn't translated.

**A ready-made world list:** 18 public broadcasters' news stations in eight languages, chosen
for slow connections:

```sh
uv run wavescribe serve --config config/stations.world.example.toml --open
```

**From a file:** copy `config/stations.example.toml` to `config/stations.toml` and edit it.
What you change on the page is saved separately, in `config/ui.toml`, so your file is never
rewritten.

## 5. Read along, listen, translate

- **The station list** (left) shows each station's state with a lamp. **Start** starts a
  stopped one.
  Choose a station to read only that one, or **All live** (key `0`) for every running station
  side by side. **Edit list** chooses which stations this device shows.
- **Listen** at the top lists your stations to play: one click plays a station, starting it
  first if it is off, and one volume serves them all. Below them, **On the air here** lists what
  your receivers have found, each a click from playing.
- **The transcript** shows what was said, with its translation. Lines dimmed as another
  language are usually songs or a guest speaking another language. Music is shown as *music*
  and skipped.
- **Click a time** to hear that moment again.
- **The Aa button** (Display) sets the text size, light or dark theme, which language you
  **read in**, and whether the original is shown beside the translation. These are yours alone:
  another person reading the same Wavescribe can choose differently.

## 6. Search and ask

Type in the field at the top (**Search or ask**):

- **Search** finds words in everything heard, in the original and in the translations, in any
  language and with or without accents. A word is found at the start of a word, so *flood*
  finds *floods* and *flooding* but *eau* doesn't find *niveau*; put a `*` in front (`*eau`) to
  find it anywhere inside words. Put words in double quotes to find them as a phrase, in that
  order: `"boil water"` finds *boil water notice* but not *water to boil*. In languages written
  without spaces (Chinese, Japanese, Thai) every word is found anywhere. Click a result to read
  around it or hear it.
- **Ask** answers a question in plain language from what was said, such as *"what did they say
  about the trains this morning?"*. The answer comes in the language you asked in, with the
  stations and times it's based on, which you can play. It takes from a few seconds to about a
  minute, and needs the Qwen3.5 model.

The answers come only from what your stations said. If they didn't cover something, Ask says
so rather than guessing, but check the quoted moments before acting on an answer.

**What did I miss?** When you've been away, a digest gives a few points the local model picked
out of what was said, each with the station that said it and the moments it rests on.
Digests are experimental: a point can still run two stories together, add a detail no one said
or cite the wrong moment, so read its moments before you rely on it. With Wavescribe running,
in another Terminal window:

```sh
uv run wavescribe digest                                    # stations heard in the last hour
uv run wavescribe digest --stations tsf,antena1 --since 14:30 --language en
uv run wavescribe digest --now --stations tsf               # what is TSF on about right now?
```

```
TSF Rádio Notícias, RTP Antena 1, 14:30-16:00
Written by the local model from 140 of 512 lines heard between 14:30 and 16:00; it can miss things or get them wrong.

! high 15:12 TSF Rádio Notícias: Water cut tomorrow between nine and one o'clock

- TSF Rádio Notícias: Water will be cut tomorrow morning in the town centre for repairs. [1, 2]
- RTP Antena 1: The bus drivers' strike goes ahead on Thursday. [3, 4]

[1] TSF Rádio Notícias 15:12  Water cut tomorrow between nine and one o'clock.
…
```

The local model writes it in the language you ask for (by default, the one your stations are
translated into) from what was said, leaving out songs, station idents and repeats. When there
is more than it can take it reads an even sample, and the second line says so ("a sample: 252
of 259,200 lines"). The alerts announced in that time come first, each once, in the words they
were announced in. A point the model gave without the moments it rests on isn't shown, nor one
that shares no word with the moments it cites (a cheap check: it catches a point that cites
another story, not one that gets a detail wrong). A point shows three of its moments at most,
those that share words with it first, spread over all it cited. A watch
([RADIO.md](RADIO.md), *Catching radio that only talks now and then*) is in a digest only when
you name it with `--stations`: its lines are short, from many speakers on many channels.

With no points, the digest says why in Wavescribe's own words: nothing was said; a station
keeps no text (walkie-talkies, digital voice) or isn't running; nothing stood out in the lines
it read; or the model's reply cited nothing it read, or nothing that backs its points.

It needs the Qwen3.5 model and waits behind live translation: an hour of two busy stations
takes about a minute, `--now` a few seconds. When two digests are already being made, it is
refused at once; try again in a minute. `--speak` also reads its points aloud, in the voice
your spoken alerts use for that language ([section 7](#7-get-alerts)): it opens by saying that
this is a Wavescribe summary written by the local model, and an alert stops it at once.

Like alerts, a digest is a help, not a warning system: it comes from a small model reading
speech recognition, so it can miss things or get a name or a number wrong, and one without
points doesn't mean that nothing happened. The numbers in brackets are the moments listed under
it, worth reading before you act on a point.

## 7. Get alerts

Alerts tell you when something that matters is said, so you don't have to read all day. Rules
run on what was said in the station's own language, never on the translation, so a poor
translation can't hide an alert.

Alerts are a help, not a warning system: one can be missed, come late or be raised for
something that wasn't said. In an emergency, follow official warnings and the emergency services.

**The simplest rule:** Settings → **Alerts** → **Words to listen for**. Name it, type the
**words and phrases** to watch for in your language (*evacuation*, *boil water*, *road
closed*), and press **Suggest in each language**: the local model proposes the same terms in
each station's language. Read them, fix any that are wrong, and save.

- **Severity** decides what happens. *Low* is only highlighted in the transcript. *Medium*
  shows a notification on the Mac. *High* and *critical* are also **spoken aloud**, in the
  languages you choose. You can change what each level does.
- **Let the model confirm** each match: hits are checked by the local model in context before
  they're announced. It filters out songs, adverts, sport and other meanings of the word. If the model
  can't answer in time, the alert still goes out, marked *not checked*.
- **Describe it in your words:** say what you want to hear about in one sentence (*"residents
  are told to leave their homes"*), with no keywords at all. The model reads each station's recent speech
  about every 45 seconds.
- **Try a sentence**, as the radio would say it, to see which word rules would fire. Nothing is
  raised.
- **Its own words:** in `config/alerts.toml`, a keyword rule can list its own words per language
  instead of (or beside) topics from a keyword pack:
  `terms = { es = ["corte de agua"], de = ["Wasser abgestellt"] }`. They match as pack words do
  (case, accents and plurals don't matter), on stations whose language starts with that code.
- **Patterns:** in `config/alerts.toml`, a keyword rule can match regular expressions as well as
  words, such as a road and its number: `regex = ['\bA-?\s?\d{1,3}\b.{0,30}\bclosed\b']`. They
  ignore case and work in any language, but match the text exactly as written otherwise (no
  accents folded, no plurals). `\b` marks the edge of a word, which Chinese, Japanese or Thai
  don't have, so leave it out there. A pattern that could take very long (a repeated group that
  repeats inside, like `(\w+ )+`) is refused when the file loads, with the rule's name.

**A story told again and again is one alert.** Further mentions add to it (the page counts
them) and are not announced again, unless it gets more serious. After 15 minutes without a
mention (`cooldown_minutes`) it is over, and the next mention is a new alert. While it goes on,
you get a "still being mentioned" notification at most once an hour (`remind_minutes` under
`[delivery]`; 0 turns it off). Reminders are never spoken.

**When a station goes silent:** the example rules (`config/alerts.example.toml`) have one
(`kind = "silence"`): a station that sends no audio, or only silence, for 5 minutes raises an
alert, and you get a notification when its audio is back. It's how you find out that the
stream, the receiver or the network went down. It says the station went quiet, not that
anything happened. Stations you only listen to, and stations switched off, never raise it.

**Traffic announcements:** FM stations flag their traffic news in their broadcast data (RDS).
Add a rule with `kind = "traffic"` (there is one to uncomment in the example rules) and you get
an alert when one starts on a station you receive with an SDR, at most once per cooldown. It
works on stations you only listen to as well, since nothing has to be transcribed.

**Alerts in more languages:** notifications and voices use the languages set under Settings →
Alerts → **What happens on an alert**. To have a station's alerts translated into other
languages as well, for everyone reading the page or receiving webhooks, give the station
`alert_languages = ["es", "fr"]` in `config/stations.toml`. Alerts above *low* are translated
into them.

**Quiet notifications:** an alert's notification plays the Mac's *Glass* sound (a digest's
never does). To keep them but lose the sound (for the night, say), set `notify_sound = ""` under
`[delivery]` in `config/alerts.toml`, then start Wavescribe again; another name from
`/System/Library/Sounds` (`"Ping"`, `"Submarine"`) plays that sound instead. Spoken alerts still
speak: to quiet them too, take the voices out of **What happens on an alert**.

Check that notifications and voices work: `uv run wavescribe alerts test`. Spoken alerts use
macOS voices; download more in System Settings → Accessibility → Spoken Content → System voice
→ Manage voices.

**A digest every hour:** a `[digest]` table in `config/alerts.toml` brings, on the hour, a
digest of what was said since the one before ([section 6](#6-search-and-ask)), as a quiet
notification titled "Wavescribe summary", and read aloud too if you like:

```toml
[digest]
every_minutes = 60                # 30 is on the hour and half past; 15, 120 and 180 work too
stations = ["tsf", "antena1"]     # leave out for every station
language = "en"                   # default: notify_language
speak = true                      # also read its points aloud, in the [speak.en] voice
quiet_hours = ["22:00", "07:00"]
```

Every digest is shown, whatever became of it: its points, with who wrote them from how much;
that nothing stood out, or that nothing was said; or why none could be made (the model was
busy, say: the next one then covers that time too). A quiet notification centre would
otherwise look like a quiet radio. It covers only radio that may leave the Mac
([section 10](#10-what-wavescribe-keeps-and-why)): nobody chose to hear it at that moment, and
anyone in the room does.

Read aloud, a digest opens by saying what it is, names the earlier alerts only by time and
rule, then reads its points; an alert stops it at once. For a language other than English,
give its `[speak.<lang>]` a `summary` and an `earlier` sentence, as `alerts.example.toml`
shows. Digests keep to the clock on the days it changes too. Restart Wavescribe after changing
the table.

**Elsewhere:** **Webhooks and pushes** send alerts to your own automations, a chat bot or a
home-automation system ([FEED.md](FEED.md)).

## 8. Read along on your phone

```sh
uv run wavescribe serve --lan
```

Then on a phone or tablet on the same Wi-Fi, open `http://<your Mac's name>.local:8765`. The
name is in System Settings → General → Sharing → Local hostname. Open the page by that `.local`
name or the address `serve --lan` prints; for any other name (a router's, a VPN's), set it as
the *Public URL* under Settings → **Connections** → *For other programs* first, or the phone
gets an error. Other devices can
read and listen, but can't change anything; only the Mac itself can. They can't use Ask either,
which keeps the Mac's model busy: `serve --lan --allow-remote-ask` lets them. `serve` says at
start who can read and ask ([FEED.md](FEED.md), *Who can read what*).

> **Only on a network you trust.** The page is plain HTTP with no login: anyone on the same
> network can read your broadcast stations' transcripts. Radio that isn't broadcast never
> reaches other devices ([section 10](#10-what-wavescribe-keeps-and-why)).

## 9. Use a radio receiver (SDR)

An SDR ("software-defined radio") dongle is a USB radio receiver: plugged into the Mac with an
antenna, it lets Wavescribe hear FM stations with no internet, and other radio too.
[HARDWARE.md](HARDWARE.md) says what to buy and how to connect it.

Wavescribe opens a receiver only for a station that uses it, a scan, or listening (under
**Listen** → **Tune by hand**, or looking at a band on the dial). Listing receivers (Find →
**On the air**, Settings → **Receivers**) opens none, so a dongle another program is using is
left alone.

1. **Plug it in.** macOS asks **"Allow accessory to connect?"**: allow it. Until you do, the
   dongle is invisible.
2. **Find** → **On the air**. The dongle is listed; **Use it** makes it a receiver.
3. **Find FM stations with Receiver A**, above the dial. In about two minutes you get the
   stations it hears, on the dial and in a list, named from their own broadcast data (RDS) when
   the signal is good enough.
4. **Listen** beside a station tunes the receiver to it, there on the page: a spectrum and
   waterfall show what the receiver hears, with the stations marked by name, and a click on a
   signal tunes it. **Listen** → **Tune by hand** has the same receiver, with seek and a
   scanner. Nothing you listen to there is written down.
5. **Add as a station** beside a station keeps it: it runs all the time, written down,
   translated and watched by your alerts, like any other station.

Choose another band on the dial to read what it is and find what's on it there
([RADIO.md](RADIO.md#finding-stations)).

One dongle hears a window about 2 MHz wide, so it can carry two to five FM stations that sit
close together. For stations spread across the band, use more dongles
([HARDWARE.md](HARDWARE.md#planning-frequencies)).

Wavescribe can tune any frequency: aircraft, ships, radio amateurs, walkie-talkies. Before it
listens to aircraft, ships, walkie-talkies or any frequency it can't classify, it asks you to
confirm that you may receive it where you are; amateur bands are open to listeners by default. **Laws on this differ by country; you are responsible for following yours** (README,
*Using it responsibly*, and [RADIO.md](RADIO.md)).

## 10. What Wavescribe keeps, and why

Everything stays on your Mac, in the `data` folder inside the Wavescribe folder. What's kept
depends on the kind of radio, because what people say on the radio can identify them, and
radio that isn't broadcast is often protected by law:

| Kind of radio | Text kept | Audio kept | Leaves the Mac | You confirm first |
|---|---|---|---|---|
| Broadcast radio and internet stations | a year | a week, at most 2 GB | yes, where you send it | no |
| Radio amateurs | 7 days | no | no | no |
| Aircraft, ships | 3 days | no | no | yes |
| Walkie-talkies, CB, anything unlisted | nothing | no | no | yes |
| Digital and encrypted services (no decoder: you hear noise) | nothing | no | no | yes |

"Leaves the Mac" means the phone view, the text feed, webhooks and other Wavescribes. Alerts
from radio that stays on the Mac reach them without the words that were said.

These are cautious defaults. You can change them under Settings → **Storage and privacy**: the
broadcast limits under *How long it's kept*, the rest under *What's kept of each kind of radio*.
To delete everything heard, stop Wavescribe and delete the `data` folder. Models live in the `models` folder and in
`~/.cache/huggingface`.

## 11. Keep it running

**In the background.** To run Wavescribe without a Terminal window, start it when you log in
and restart it by itself after a crash, install it as a background service. In the Wavescribe
folder:

```sh
uv run wavescribe service install --lan    # takes the same options as serve
uv run wavescribe service status           # is it running? where are its logs?
uv run wavescribe service uninstall        # stop it and remove it
```

- It runs `serve` in this folder with this folder's Python, so starting needs neither the
  Terminal nor the internet. It keeps the Mac from going to sleep while it runs, and after a
  crash it starts again within 30 seconds.
- Before installing, it checks your settings and that the port is free, so a mistake shows
  straight away rather than as a service that keeps stopping. `--dry-run` shows what it would
  install.
- **To change its options,** run `service install` again with the new ones; it replaces the
  running one. Do the same after changing `config/stations.toml` or `config/alerts.toml` by
  hand, so it reads them again (changes made on the page apply at once).
- **Logs:** start-up messages and errors are in `data/logs/service.log`, everything in
  `data/logs/wavescribe.jsonl`.
- macOS may tell you a background item was added. It's listed under System Settings → General
  → Login Items & Extensions; leave it switched on there.
- The service runs while you're logged in. For a Mac that should carry on by itself after a
  restart, turn on automatic login (System Settings → Users & Groups; not available while
  FileVault is on).

**A closed lid still puts a MacBook to sleep,** with or without the service. Keep the lid open,
or, for a machine that stays on all the time, run `sudo pmset -a disablesleep 1` (and `0` to
undo it). Without the service, Wavescribe runs only while its Terminal window is open and
doesn't keep the Mac awake: start it with `caffeinate -i uv run wavescribe serve --open`.
Started from a script rather than a Terminal window, stop it with `kill <pid>` (SIGTERM): `uv run`
passes that on to Wavescribe, but not the SIGINT that Ctrl-C sends in a Terminal.

- Settings → **Health** shows memory, processor and graphics load, battery, disk, and how far
  behind each station is. If the Mac struggles, translate fewer stations: translation is the
  heaviest part.
- **Check everything** with `uv run wavescribe doctor` ([Troubleshooting](#troubleshooting)).
- **Update:** stop Wavescribe, then `git pull` (or download the ZIP again) and `uv sync`. With
  the service, run `uv run wavescribe service install` again afterwards to restart it on the
  new version.

## Troubleshooting

**Start with the self-check:**

```sh
uv run wavescribe doctor
```

It checks what Wavescribe needs on this Mac: ffmpeg and the radio tools, Python and its
libraries, the models your stations and languages need, the receivers plugged in, free disk
against what Wavescribe may keep, your settings files, whether the page's port is free, the
voices for spoken alerts, and the background service. Each line says **ok**, **warn** (works,
but worth knowing), **FAIL** (won't work) or **skip** (doesn't apply), and each problem comes
with what to do next. It works offline, changes nothing and never opens a receiver, so it's
safe to run while Wavescribe is running.

- With the background service installed, it checks the service's options. Give it serve's
  options to check others: `uv run wavescribe doctor --config config/stations.world.example.toml`.
- `--json` prints one JSON object per line (each check, then a summary) for scripts and other
  programs. The exit code is 1 when something is broken, 0 otherwise.

| Problem | What to do |
|---|---|
| The page doesn't open | Is the Terminal window still running Wavescribe? Open <http://localhost:8765> by hand. `uv run wavescribe doctor` says whether another program holds the port; if so, start it with another, e.g. `--port 8780`. |
| The background service doesn't run | `uv run wavescribe service status` shows its last exit, and `data/logs/service.log` says why it stopped. Fix that (`uv run wavescribe doctor` helps), then `uv run wavescribe service install` again. |
| A station plays without text and says it's waiting for a model | Download it under Settings → **Languages and models**; the text starts when it finishes. |
| No sound | Press **Start** beside the station, and check the Mac's volume. *Channel quiet* on a radio channel means nobody is transmitting right now. |
| Nonsense text, or the wrong language | Check the station's language under Settings → **Stations**. Some songs still get through the music filter; heavy accents, phone-ins and weak signals transcribe worse. |
| The dongle isn't found | Allow it in System Settings → Privacy & Security → *Allow accessories to connect*, then unplug it and plug it in again. `uv run wavescribe doctor` lists the dongles macOS sees, with their serials, and says whether each receiver in your settings is plugged in. |
| The dongle is found but busy | Only one program can use a dongle at a time. Close other radio programs (GQRX, SDR++), and stop any `rtl_fm` or `rtl_power` left running. |
| A scan finds few stations | Extend the antenna to about 75 cm, stand it upright near a window and away from the Mac. |
| A scan finds stations everywhere, or they sound distorted | The gain is too high: lower it under Settings → **Receivers**. |
| Too many alerts | Raise the rule's severity threshold, turn on **Let the model confirm**, or use more specific phrases. `uv run wavescribe alerts replay` shows what a draft rule would have announced on everything heard so far. |
| No notifications | Check System Settings → Notifications for Script Editor or your terminal app, and run `uv run wavescribe alerts test`. |
| Internet stations keep stopping | They need the internet; Wavescribe reconnects by itself when it comes back. |

## Questions

**Does it send anything over the internet?** No. There's no telemetry and no cloud service.
The internet is used only for downloads you start, internet stations you add, and webhooks or
other machines you set up yourself.

**Which languages?** Speech: Parakeet covers most European languages and Whisper about 100 in
all. Translation: Qwen3.5 handles most widely spoken languages. A station's language is a
setting ([MODELS.md](MODELS.md)).

**How accurate is it?** Good on clear speech such as news; worse on music, phone-ins and weak
signals. Translation is usually natural, but it can slip. Use alerts as a prompt to listen and
check, not as the only source for a safety decision.

**Can it work with no internet at all?** Yes, once set up and with an SDR receiver: FM radio
reaches the antenna even when power and phone networks are down elsewhere.

**Is it legal?** Broadcast radio is meant for everyone. Other radio depends on where you are:
see the README's *Using it responsibly* and [RADIO.md](RADIO.md#the-law-where-you-are). This
isn't legal advice.

**Can other programs or AI agents use it?** Yes: a live text feed, webhooks and remote stations
([FEED.md](FEED.md)), and experimental agent tools, an MCP server and JSON output
([AGENT_TOOLS.md](AGENT_TOOLS.md)).
