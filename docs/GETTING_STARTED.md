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

Every command used here, with all its options, is in [COMMANDS.md](COMMANDS.md).

## What you need

- **A Mac with Apple Silicon** (M1 or later). Intel Macs, Windows and Linux aren't supported.
- **Memory:** 16 GB or more to transcribe and translate. Listening alone needs very little.
  [HARDWARE.md](HARDWARE.md#the-computer) says how many stations fit on which Mac.
- **Disk:** about 11 GB to install: 8 GB of models (9.5 GB with Whisper, for languages the
  default model doesn't cover) and about 2 GB for the program's own environment. Then room
  for what's heard: about 30 MB a day for four busy stations, and up to 2 GB of speech audio by
  default.
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
country). Press **Start** beside one to start it. Start alone plays no sound: to hear the
station, press **Listen** at the top of its column, or play it from **Listen** at the top of the
page. Until the models are downloaded (next step) a station has no text yet, and its text
starts by itself once they are.
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

That's about 8 GB: about four minutes on a fast fibre line, half an hour at 35 Mbit/s and an
hour at 20 Mbit/s. It says the total first and checks that the disk has room before it
downloads anything, and its last line is the command that starts Wavescribe. These models need
no account and no token.

- **To stop it,** press **Ctrl-C**: it stops at once and says which models finished.
- **With no network, or when the download breaks off,** it ends with one sentence that says
  what failed. Nothing is broken: run the same command again. Models that finished are skipped,
  and a model of several files keeps the files that finished; a file that was cut off starts
  again from its beginning.
- **A certificate error** is in [Troubleshooting](#troubleshooting).

| Model | What it does | Size |
|---|---|---|
| Silero VAD | finds where someone is speaking | tiny |
| YAMNet | skips music, so most songs aren't transcribed | tiny |
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
- **On the internet → By address:** paste a station's stream address. **Check** tries it and
  says whether it plays. Tick **Also identify the language** first to have Whisper listen for
  20 seconds and name the language; that needs a Whisper model.
- **On the internet → Other ways to add:** an `.m3u` or `.pls` playlist, or a station that
  another machine transcribes ([FEED.md](FEED.md)).
- **On the air:** what your receiver finds; see [section 9](#9-use-a-radio-receiver-sdr).

Each station has a **language** (what it speaks) and a **mode**, both changed later under
Settings → **Stations**. When you don't know the language, or a station switches between
several, language `auto` has Whisper tell each line's; it needs a Whisper model and is slower
than a station in one known language. The modes:

- **Listen only:** you can play it; nothing is written down and no model is needed.
- **Transcribe:** written down and checked for alerts.
- **Transcribe and translate:** also translated into the languages under **Translate into**, as
  it speaks.

A station that already speaks your language isn't translated.

Two more settings, both under Settings:

- **Stations** → a station → *Advanced* → **Transcribe first when busy** (`priority` in the
  stations file): a number from 1, 5 when you set none. When the Mac falls behind, stations
  with lower numbers are written down first.
- **Languages and models** → **Translation languages** lists the languages your stations are
  translated into, with how many stations use each, and takes one away from every station at
  once. Every further language costs time on each line.

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
  Choose a station to read only that one, or **All live** (key `0`) for the running stations
  side by side: one column each, as many as fit, four at most. **Edit list** chooses which
  stations this device shows.
- **Listen** at the top of a station's column plays it. **Listen** at the top of the page lists
  all your stations to play: one click plays a station, starting it first if it is off, and one
  volume serves them all. Below them, **On the air here** lists what your receivers have found,
  each a click from playing.
- **The transcript** shows what was said, with its translation. Until a line's translation
  arrives, its words as heard are shown in grey. A line that isn't translated into the language
  you read in keeps its own words, with a small label naming their language.
- **"heard as …"** before a line means the speech recognition wrote it in another language
  than the station's: someone may really have spoken that language (a guest, a clip, a song),
  or the recognition slipped and the words may be wrong. It can't tell which, so read such
  lines with care; hovering over or focusing the label says the same.
- **When no new line is coming**, the foot of the column says why and since when, in place of
  the next line: *Music is playing, and music is skipped* (songs are never written down), *No
  speech since …* for silence or music without a voice, or that nothing was written down since
  a time while a voice is heard (singing and jingles are left out). The text goes on by itself
  when speech does. The lamp in the station list says the same: a green ring.
- **A stream that can't be reached** says so in its column (*This station's stream can't be
  reached. Wavescribe keeps trying.*) with when it was last heard; **Details** under it has
  the stream's own error and when the next try is. *Waiting for speech…* appears only while
  the station is being heard.
- **Click a time** to hear that moment again.
- **The Aa button** (Display) sets the text size, light or dark theme, which language you
  **read in**, and whether the original is shown beside the translation. These are yours alone:
  another person reading the same Wavescribe can choose differently.
- **The ⋯ button** beside a station has **Show only this station**, **Station settings** and
  **Stop the station**, which stops it for everyone reading this Wavescribe.
- **In the Terminal, without the page:** `uv run wavescribe listen <station id> …` prints the
  same lines as they come, each translation under its original, until you press Ctrl-C.

**Keyboard shortcuts** (`?` lists them on the page):

| Key | What it does |
|---|---|
| `/` | Search or ask |
| `1`–`9` | Read a live station, in the list's order |
| `Shift` + `1`–`9` | Add it as a further column, or take its column away |
| `0` | All live stations |
| `A` | Show or hide the alerts |
| `Esc` | Close the alerts, search, Ask, Listen, Find or Settings |
| `?` | The list of shortcuts |

While a radio receiver is tuned by hand (section 9), the arrow keys tune it, as on a radio:

| Key | What it does |
|---|---|
| `↑` or `→` | One step up |
| `↓` or `←` | One step down |
| `Shift` with them | Ten steps |
| `Page Up`, `Page Down` | The next or previous channel where the band has a channel list, else ten steps |

A text field, and a slider or list you reached with Tab, keep the arrow keys; one you used with
the mouse gives them back ([RADIO.md](RADIO.md#listening-by-hand)).

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

**What did I miss?** When you've been away, a summary (a *digest*) gives a few points the local
model picked out of what was said, each with the station that said it and the moments it rests
on. Digests are experimental: a point can still run two stories together, add a detail no one
said or cite the wrong moment, so read its moments before you rely on it.

On the page, press **Summary** at the top of Read and choose how far back: the last 15 minutes,
the last hour, since a time (or between two), last night (by day, on the Wavescribe machine), or
since you last looked when this browser knows. It covers every running station in your list,
whether or not its column is on screen ("Showing 2 of 3" changes nothing); **Choose stations**
in the same menu leaves some out until the page is loaded again, and the summary names the
stations it covers. A column's **⋯** menu asks for that station alone, or *What are they
talking about now?* in a line under its name. When the model is busy, **Try again at …** names
the second from which the button works. The points come beside the columns (on a phone, in their place); press a point's
time to read that line in the transcript. **Cancel** stops one being written, on the server too,
and so does leaving the page. On the Wavescribe machine, **Read it aloud** under a summary has
the Mac read it and **Stop** ends that; *Read it aloud on this Mac when it's ready* reads one as
soon as it is written.

From the command line, with Wavescribe running, in another Terminal window:

```sh
uv run wavescribe digest                                    # stations heard in the last hour
uv run wavescribe digest --stations tsf,antena1 --since 14:30 --language en
uv run wavescribe digest --now --stations tsf               # what is TSF on about right now?
```

```
TSF Rádio Notícias, RTP Antena 1, 14:30-16:00
Written by the local model from 140 of 512 lines, spread evenly between 14:30 and 16:00; it can miss things or get them wrong.

! high 15:12 TSF Rádio Notícias: Water cut tomorrow between nine and one o'clock

- TSF Rádio Notícias: Water will be cut tomorrow morning in the town centre for repairs. [1, 2]
- RTP Antena 1: The bus drivers' strike goes ahead on Thursday. [3, 4]

[1] TSF Rádio Notícias 15:12  Water cut tomorrow between nine and one o'clock.
…
```

The local model writes it in the language you ask for (by default, the one your stations are
translated into) from what was said, leaving out songs, station idents and repeats. When there
is more than it can take it reads an even sample of each station, from the start of the time
to its end, and the second line says so ("140 of 512 lines, spread evenly", or "a sample: 252
of 259,200 lines" when it is thin). A small model asked about a long stretch at once writes
about what it reads first, so a digest asks about it a part at a time (three parts for a busy
station) and then chooses the points that matter most, the latest first when two matter as
much. What it doesn't choose stays out, so a digest can have fewer than five points. The model
is told to leave out advertising, sponsorship and a station's promotion of
itself (its programmes, trailers, jingles, contests, its events), and to say that nothing
stood out when that is all there was, and again when it chooses the points. It doesn't always
listen when it reads an ad break, and nothing checks it, so an advert can still slip in.
The alerts announced in that time (notified or spoken, not only logged)
come first, each once, in the words they were announced in. A point the model gave without the
moments it rests on isn't shown, nor one that shares no word with the moments it cites, nor
one with a name or a number that none of the moments it shows says, in any of their
languages: "funded by" two companies no moment names, or "above 2.9%" when the moments say
2.8 ("2,9" and "2.9" are the same number). These checks are cheap and catch a point that
cites another story or adds a name or a figure. They don't catch a detail taken from another
story told in the same moment, a relation turned round ("after blocking the reform" when it
was his opponents who blocked it), or a number written in words ("three" isn't checked).
In a language that writes its nouns with a capital, such as German, only acronyms (EU) count
as names; a name whose spelling changes when it is inflected can make a good point be left
out. A point shows three of its moments at most: those that say its names and numbers first,
then those that share words with it, spread over all it cited. A watch ([RADIO.md](RADIO.md), *Catching radio that only talks now and then*) is in a
digest only when you name it with `--stations`: its lines are short, from many speakers on many
channels.

`--since` takes a time of day, today's, or yesterday's when it is still to come: at 00:15,
`--since 23:30` is the last three quarters of an hour, and at 02:43, `--since 02:50` is nearly a
day. The first line then says so: "yesterday 02:50 to today 02:43".

With no points, the digest says why in Wavescribe's own words: nothing was said, or nothing
said was written down because the station played music (a voice over a song goes with it);
only music, idents and repeats were written down; a station keeps no text (walkie-talkies,
digital voice) or isn't running; nothing stood out in the lines it read (only advertising and
the station's own promotion, say); or the model's reply
cited nothing it read, or nothing that backs its points, words, names and numbers. When nothing was heard for part
of the time asked for, it says that too ("Nothing was heard between 16:50 and 17:05", or
"before 14:55"). It goes by what was written down, from any of your stations, not by when
Wavescribe was last started: after a restart, a digest still reads what was heard before it.
A station that plays music while another talks leaves no such gap, and a stretch has to last
longer than your stations are known to go quiet: ten minutes at least, half an hour for a
station it has heard little of. A restart of a few minutes isn't mentioned.

It needs the Qwen3.5 model and waits behind live translation: an hour of two busy stations
takes a minute or two, `--now` from a few seconds to a minute. When two digests are already being
made, it is refused at once; try again in a minute. `--speak` also reads its points aloud, in
the voice your spoken alerts use for that language ([section 7](#7-get-alerts)): it opens by
saying that this is a Wavescribe summary written by the local model, and an alert stops it at
once. One that comes while an alert is being spoken waits for it to end (a minute at most;
after that it is only shown).

Like alerts, a digest is a help, not a warning system: it comes from a small model reading
speech recognition, so it can miss things or get a name or a number wrong, and one without
points doesn't mean that nothing happened. The numbers in brackets are the moments listed under
it, worth reading before you act on a point.

## 7. Get alerts

Alerts tell you when something you asked to hear about is said. Rules run on what was said in
the station's own language, never on the translation, so a poor translation can't hide an
alert.

Alerts are a help, not a warning system: one can be missed, come late or be raised for
something that wasn't said. In an emergency, follow official warnings and the emergency services.

**What runs before you set anything.** Until you make a `config/alerts.toml`, the example
rules in `config/alerts.example.toml` run. Most of them are topic rules (water, power, civil
protection, hazards), and a topic's words come from a *keyword pack*: a word list for one
language, in `config/keywords/`. Wavescribe ships one pack, as an example, for one language
(`pt.example.toml`, Portuguese). So as installed:

- stations in that language get the topic alerts;
- stations in any other language get only the example's *station silent* alert, until you
  give them rules that work in their language.

Nothing warns you of this while Wavescribe runs: a topic rule simply never matches a station
whose language has no pack. (A warning is logged, once, only for a rule whose topic is in no
pack at all.) To see where you stand:

```sh
uv run wavescribe alerts check --language es "Hiervan el agua antes de beberla"
```

says when no rule matches and that topic rules have no word list in that language;
`uv run wavescribe doctor` names the languages that have one (`keyword packs: pt`), and so does
the rule editor on the page, under *Ready-made word lists*.

Two kinds of rule work in any language, with no pack:

- **words of your own**, typed for each language (the simplest rule, below);
- **a description in plain language**, which the local model reads against what each station
  said, whatever the language (*Describe it in your words*, below).

**A pack for another language** is one file. Copy `config/keywords/pt.example.toml` to
`config/keywords/<code>.toml` (`es.toml`, `ja.toml`), set `language` to that code, and under
each topic put the words and phrases the radio uses in that language. Keep the topic names
(`water`, `power`, `civil_protection`…) so that the same rules use them, and put your own
place names under `[places]`. The comments in the example say how words match. Have someone
who knows the language well read the list: a word that is also an everyday word raises alerts
all day, and a phrase left out misses the announcement, which is why no other pack is
shipped. Then try sentences with `alerts check`, run `uv run wavescribe alerts replay` over
what was already heard, and start Wavescribe again.

**Your own rules file:** copy `config/alerts.example.toml` to `config/alerts.toml` and change
it. Rules and settings made on the page are kept apart, in `config/ui.toml`, and laid over it.
A key the file doesn't take (a misspelt `min_mention`, say) is refused by name when Wavescribe
starts, and `alerts check` says it without starting anything.

**The simplest rule:** Settings → **Alerts** → **Words to listen for**. Give it an **Id** (a
short name in lowercase letters, digits and dashes, such as `road-closed`), type the
**words and phrases** to watch for in your language (*evacuation*, *boil water*, *road
closed*), and press **Suggest in each language**: the local model proposes the same terms in
each station's language. Read them, fix any that are wrong, and save.

- **Severity** decides what happens. *Low* is only highlighted in the transcript. *Medium*
  shows a notification on the Mac. *High* and *critical* are also **spoken aloud**, in the
  languages you choose. You can change what each level does.
- **Ready-made word lists** are the topics of the keyword packs, for the languages that have
  one. **More severe near** raises a match one level when a place from the pack's `[places]` is
  named in the same sentence.
- Under **Advanced**:
  - **Mentions before alerting:** with 2 or more, a first mention is kept as *low* and not
    announced, since a real event gets repeated (`min_mentions` in the file).
  - **Only on these stations** keeps the rule to the stations you tick (`stations`).
  - **Let the model confirm** each match (`verify = true`): hits are checked by the local
    model in context before they're announced. It filters out songs, adverts, sport and other
    meanings of the word. A match the model couldn't check still goes out, marked
    *not checked* and one severity lower (*When a match can't be checked*, below).
  - **Patterns (regular expressions)** match as well as words, such as a road and its number:
    `\bA-?\s?\d{1,3}\b.{0,30}\bclosed\b`. They ignore case and work in any language, but match
    the text exactly as written otherwise (no accents folded, no plurals). `\b` marks the edge
    of a word, which Chinese, Japanese or Thai don't have, so leave it out there. A pattern
    that could take very long (a repeated group that repeats inside, like `(\w+ )+`) is
    refused, with the rule's name; a rule takes 20 patterns at most, of 200 characters each.
- **Describe it in your words:** say what you want to hear about in one sentence (*"residents
  are told to leave their homes"*), with no keywords at all. The model reads each station's recent speech
  about every 45 seconds.
- **Try a sentence**, as the radio would say it, to see which word rules would fire. Nothing is
  raised. In the Terminal: `uv run wavescribe alerts check "la carretera está cortada"
  --language es-ES`; `uv run wavescribe alerts replay` runs everything heard so far through the
  rules and counts what they would have announced.
- **In `config/alerts.toml`** every rule is a `[[rule]]` with an `id` and a `severity` (medium
  when left out). A keyword rule has `topics` from the packs, its own words per language
  (`terms = { es = ["corte de agua"], de = ["Wasser abgestellt"] }`, for stations whose
  language starts with that code; case, accents and plurals don't matter) or `regex` (in
  'single quotes', so backslashes stay as they are). A rule in plain language is
  `kind = "llm"` with a `prompt`. The example file shows each of them.

**When a match can't be checked.** A rule with *Let the model confirm* asks the local model
about every match. Sometimes the model doesn't answer: none is loaded, it is too busy, it
fails, or 90 seconds pass. The match is raised all the same, and the alert says it was **not
checked**: the notification's title has it after the severity ("MEDIUM, not checked ·
water-cut · …"), and the alert's reason says why ("not checked by the LLM: it was busy; raised
as medium, not high"). *Not checked* means the words were heard and nothing looked at what
they were about: it can be the event, a song or a football team. What becomes of such a match
is the rule's `unverified`, in `config/alerts.toml` (a rule made on the page has `"lower"`):

- `"lower"`, the default: raised one severity lower than it would have been. A *critical* match
  comes as *high*, a *high* one as *medium*, and a *medium* one as *low*, which is only
  highlighted unless you changed what *low* does. You asked for matches to be confirmed, so one
  that wasn't counts for less, and it still reaches you.
- `"announce"`: raised at the rule's own severity, checked or not.
- `"hold"`: kept back while the check is tried again, 30 seconds later and then twice as long
  each time. When the model answers, the match is raised as confirmed, or dropped. If there is
  still no answer 10 minutes after the words were heard, it is raised as `"lower"` would raise
  it, so a held match is never lost. At most 32 matches wait at a time: one more raises the
  oldest, and stopping Wavescribe raises them all. While it waits, the match is on the page as
  a *low* alert.

A match the model looked at and found not real is dropped, whatever `unverified` says: this is
only about checks that didn't happen. Under `wavescribe listen` there is no model to ask, so
`verify` does nothing there and a match is raised at the rule's severity.

**A story told again and again is one alert.** Further mentions add to it (the page counts
them) and are not announced again, unless it gets more serious. After 15 minutes without a
mention (`cooldown_minutes`) it is over, and the next mention is a new alert. While it goes on,
you get a reminder notification at most once an hour, titled with its severity, rule and
stations ("STILL HIGH · water · …") and saying how often it was mentioned since when
(`remind_minutes` under `[delivery]`; 0 turns it off). Reminders are never spoken.

**When a station goes silent:** the example rules (`config/alerts.example.toml`) have one
(`kind = "silence"`), and Settings → **Alerts** → **Station silent** makes or changes one: a
station that sends no audio, or only silence, for 5 minutes (from half a minute to a day)
raises an alert, and you get a notification when its audio is back. It's how you find out that
the stream, the receiver or the network went down. It says the station went quiet, not that
anything happened. Stations you only listen to, and stations switched off, never raise it.

**Traffic announcements:** FM stations flag their traffic news in their broadcast data (RDS).
Add a rule with **Traffic announcement** on the same page, or with `kind = "traffic"` in the
file (there is one to uncomment in the example rules), and you get an alert when one starts on
a station you receive with an SDR, at most once per cooldown. It works on stations you only
listen to as well, since nothing has to be transcribed.

**Alerts in more languages:** notifications use the language set as **Notifications in** under
Settings → Alerts → **What happens on an alert** (`notify_language`; with none set they are in
the station's language), and voices speak theirs. To have a station's alerts translated into
other languages as well, for everyone reading the page or receiving webhooks, set **Translate
alerts into** for it under Settings → **Stations**, or `alert_languages = ["es", "fr"]` in
`config/stations.toml`. Alerts above *low* are translated into them.

**Quiet notifications:** an alert's notification plays the Mac's *Glass* sound (a digest's
never does). To keep them but lose the sound (for the night, say), set `notify_sound = ""` under
`[delivery]` in `config/alerts.toml`, then start Wavescribe again; another name from
`/System/Library/Sounds` (`"Ping"`, `"Submarine"`) plays that sound instead. Spoken alerts still
speak: to quiet them too, take the voices out of **What happens on an alert**.

Check that notifications and voices work: `uv run wavescribe alerts test`. Spoken alerts use
macOS voices; download more in System Settings → Accessibility → Spoken Content → System voice
→ Manage voices.

**Reading them:** the bell at the top of the page (key `A`) shows the alerts, those above *low*
from the last 12 hours under **Important** and every one under **All**, each with what was said,
**Play** and **Show in the transcript**; `uv run wavescribe alerts` lists the latest in the
Terminal.

**A summary every hour:** under Settings → Alerts, **Summaries on a schedule**, switch on *A
summary every hour*: on the hour, a summary of what was said since the one before
([section 6](#6-search-and-ask)) comes as a quiet notification titled "Wavescribe summary".
Like any summary it is experimental, and no warning. Folded under the switch are how often,
which stations, the language, reading it aloud (where a voice reads that language) and quiet
hours. The page's schedule takes effect at once and is kept in `config/ui.toml`, in place of any
`[digest]` table in `config/alerts.toml`; *Use alerts.toml's instead* gives it back to the file.

The same schedule can be written in the file, as a `[digest]` table:

```toml
[digest]
every_minutes = 60                # 30 is on the hour and half past; any number from 5 to 1440
                                  # that divides a day evenly (10, 15, 45, 90, 120, 180...)
stations = ["tsf", "antena1"]     # leave out for every station
language = "en"                   # default: notify_language
speak = true                      # also read its points aloud, in the [speak.en] voice
quiet_hours = ["22:00", "07:00"]
```

`every_minutes` takes a whole number of minutes that divides a day evenly: 5, 6, 8, 9, 10, 12,
15, 16, 18, 20, 24, 30, 32, 36, 40, 45, 48, 60, 72, 80, 90, 96, 120, 144, 160, 180, 240, 288,
360, 480, 720 or 1440 (once a day, at midnight). Every digest is shown, whatever became of it:
its points, with who wrote them from how much; that nothing stood out, or that nothing was
said; or why none could be made (the model was busy, say: the next one then covers that time
too). A quiet notification centre would otherwise look like a quiet radio. It covers only radio
that may leave the Mac ([section 10](#10-what-wavescribe-keeps-and-why)): nobody chose to hear
it at that moment, and anyone in the room does.

Read aloud, a digest opens by saying what it is, names the earlier alerts only by time and
rule, then reads its points; an alert stops it at once, and one being spoken is waited for.
For a language other than English, give its `[speak.<lang>]` a `summary` and an `earlier`
sentence, as `alerts.example.toml` shows. Digests keep to the clock on the days it changes too.
Restart Wavescribe after changing the table.

**Elsewhere:** Settings → **Connections** → **Webhooks and live pushes** sends alerts to your
own automations, a chat bot or a home-automation system ([FEED.md](FEED.md)).

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
2. **Find** → **On the air**. It says a dongle is plugged in and not in use yet: **Use it and
   find stations here** makes it a receiver (Receiver A) and scans the FM band with it, in one
   press. With several dongles plugged in, **Choose by serial** leads to Settings →
   **Receivers**, which lists them, each with its own **Use it**. **Add by serial**, under
   *Advanced* there, is for a dongle the list doesn't show, or to choose its id and centre
   yourself; **Look for dongles** reads the list again.
3. In about two minutes you get the stations it hears, on the dial and in a list, named from
   their own broadcast data (RDS) when the signal is good enough. A receiver made under
   Settings → **Receivers** hasn't scanned yet: **Find FM stations with Receiver A**, above the
   dial, does it.
4. **Listen** beside a station tunes the receiver to it, there on the page: a spectrum and
   waterfall show what the receiver hears, with the stations marked by name, and a click on a
   signal tunes it. **Listen** → **Tune by hand** has the same receiver, with seek and a
   scanner; the arrow keys, or the up and down buttons beside the frequency, step it by the
   band's raster or channel by channel. Nothing you listen to there is written down.
5. **Add as a station** beside a station keeps it: it runs all the time, written down,
   translated and watched by your alerts, like any other station.

Choose another band on the dial to read what it is and find what's on it there
([RADIO.md](RADIO.md#finding-stations)). On a band whose channels talk now and then, such as
walkie-talkies or the 2 m amateur band, **Watch this band** writes down whatever is said on any
of its channels and lists it under **Caught here**
([RADIO.md](RADIO.md#catching-radio-that-only-talks-now-and-then)).

One dongle hears a window about 2.2 MHz wide, so it can carry two to five FM stations that sit
close together; the line under the dial says how much of the band that is. For stations spread across the band, use more dongles
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
**Last 24 h as NDJSON**, on the same page, saves the last day's text as a file, one JSON
object per line.
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
| `models fetch` says *no connection* or *the connection broke off* | The Mac isn't online, or the download was cut off. Nothing is broken and the files that finished are kept: check the network and run the same command again (on the page, start the download again). It downloads only what is missing. |
| `models fetch` says *this computer doesn't trust the certificate* | Downloads check the server's certificate against the certificates macOS trusts (the Keychain), as a browser does. Check the Mac's date and time. If a company proxy or a security program inspects HTTPS, add its certificate to the System keychain. Then run the same command again. |
| `models fetch` says *not enough room on the disk* | It says how much is missing, and nothing was downloaded. Make that much room, or name fewer models (`uv run wavescribe models list` shows their sizes), then run it again. |
| A station says *No text yet* | Its speech model isn't downloaded. Download it under Settings → **Languages and models**; the text starts when it finishes. Meanwhile the station can be played. |
| No sound | **Start** only starts a station. Press **Listen** at the top of its column, or play it from **Listen** at the top of the page, and check the Mac's volume. *Channel quiet* on a radio channel means nobody is transmitting right now. |
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
