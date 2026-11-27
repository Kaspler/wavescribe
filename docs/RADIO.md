# Radio: finding stations and listening to any frequency

With an RTL-SDR dongle, Wavescribe can find what is on the air where you are, name FM stations
from what they broadcast about themselves, and turn any frequency into a station that is
transcribed, translated and watched by your alert rules. Everything happens on your own machine,
from your own receptions: no station list is downloaded.

> **You're responsible for how you use radio.** Broadcast radio is meant for everyone. Other
> radio (walkie-talkies, aircraft, ships, radio amateurs) is often protected by law: where you
> are, listening to it, keeping what is said or passing it on may be restricted or forbidden.
> Check your local rules before you tune it. Wavescribe only receives. It has no digital
> decoder and never decrypts. See [The law where you are](#the-law-where-you-are).

## Your first receiver

Plug in an RTL-SDR dongle with an antenna and open **Settings** → **Receivers**. Dongles on USB
that no receiver uses yet are listed there (once a receiver is set up, under **Look for
dongles**): **Use it** makes one a receiver (gain 30 dB; the same page changes its centre and
gain, and its sample rate under *Advanced*, where *Test the antenna* shows its live spectrum).
Listing the dongles opens none of them, so a running station is never disturbed. In a stations file, a
receiver is a `[[device]]` with an `id` and the dongle's `serial` (docs/HARDWARE.md).
Messages and the `radio` commands' output name a receiver by its id (`receiver A`), never by
its serial, so you can paste them anywhere; a dongle no receiver uses shows only the last
digits of its serial (`…1234`). The full serials are under Settings → **Receivers**, in
`wavescribe doctor` and in the logs.

## Finding stations

On the page: **Find** → **On the air**. At the top is the dial: everything a receiver can tune,
24 to 1766 MHz, with the bands named in everyday words. A filled band carries voices Wavescribe
can turn into text, an outlined one is digital (shown, not decoded), and a fine tick marks each
frequency heard here. Each receiver stands on the dial as the window it hears, with its letter
and a lamp; the line under the dial says in words what each one is doing: idle, tuned by hand,
following stations, scanning, or not answering.

Choose a band to read what it is, when it is busy and the antenna it wants. Then:

- **Find stations here** sweeps the band with the receiver chosen under *Receiver*: once for
  broadcast, again and again for a few minutes where channels talk now and then. What it finds
  is listed under the dial, strongest first, and kept under *Heard here*.
- **Look here with Receiver A** tunes the receiver by hand to the strongest station heard in the
  band (with nothing heard yet, a channel at its middle) and shows the slice of the band around
  it, about 2.4 MHz wide (what one receiver hears at once; a wider band takes several looks),
  as spectrum and waterfall live, with what was heard marked by name. Click a signal, or
  **Listen** in the list, to hear it there. You can also drag a receiver's letter along the dial.
  A receiver that is following stations and has to move for it asks first, says which would
  pause, and offers a free receiver instead.
- **Add as a station** makes what you are hearing a station that runs all the time, in one step:
  the receiver is let go from tuning by hand and stays where it hears the new station and the
  stations it already has, moving if it must; then the station starts. If no place hears them
  all, nothing changes until you answer: the page names the stations that would be left out
  (see [Keeping a frequency as a station](#keeping-a-frequency-as-a-station)).
- **Watch this band with Receiver A**, on a band whose channels talk now and then
  (walkie-talkies, 2 m, aircraft), turns whatever is said on any of its channels into text; see
  [Catching radio that only talks now and then](#catching-radio-that-only-talks-now-and-then).

One receiver is live at a time. For a band without it, the page shows the band from the newest
scan that covered it, with how long ago that was. With nothing found yet, the page offers one
button: FM, the easiest radio to hear; after that it suggests a band to try next. On a phone the
dial is a list of bands, with the receivers on their rows. Another range, and how long to watch,
are under *Advanced*; while a receiver is live, its frequency, mode, step and gain are under
*Tuning*. From a terminal:

```sh
uv run wavescribe radio bands                 # the band plan for your region
uv run wavescribe radio scan a fm             # receiver `a`, the FM broadcast band, names from RDS
uv run wavescribe radio scan a airband --watch 5   # sweep again and again for 5 minutes
uv run wavescribe radio scan a 150:175        # any range, in MHz
uv run wavescribe radio scan a all            # everything the tuner reaches (24-1766 MHz)
uv run wavescribe radio heard                 # everything found here so far
uv run wavescribe radio rds a 98.6            # live RDS from one FM station
```

A scan works like this:

1. `rtl_power` sweeps the range and gives one power spectrum. The resolution follows the band:
   about a twentieth of an FM channel and a fifth of a narrow channel. The dongle sees a spike
   at the centre of each 2 MHz it tunes, which hides whatever is right there; for narrow
   channels a second, shorter sweep with the dongle tuned elsewhere fills those spots in (about
   2 s more).
2. Signals are the peaks a margin (8 dB by default) above the local noise floor. Each is snapped
   to its band's channel raster (100 kHz for FM, 8.33 kHz in the airband, the PMR446 channel
   list), and the band plan says what it is.
3. For FM, a few seconds of the band are recorded per 2 MHz and every station in it is
   demodulated at once. Its **RDS** gives the station's name, programme type and radiotext.
4. Everything found goes to `data/radio/heard.json`, merged with earlier scans. A station not
   heard in a later scan is kept, marked with when it was last heard.

**Watching.** Walkie-talkies, aircraft and amateurs talk now and then, so one sweep rarely
catches them. `--watch MINUTES` (or *How long* on the page) sweeps again and again, keeps the
strongest level each frequency reached, and counts how many sweeps each channel was busy in.
Real traffic comes and goes. A channel busy in every sweep is a beacon, a recorded weather
loop, or interference. To hear what is said as well, see
[Catching radio that only talks now and then](#catching-radio-that-only-talks-now-and-then).

**When the tuner can't tune.** Now and then a dongle's tuner fails to lock onto a frequency
(`[R82XX] PLL not locked!` from `rtl_power`), and what it reports there comes from somewhere
else. When the message names the frequency, those hops are left out of the scan: no reading,
no signals. The usual message names none, so the scan warns that what it found in the range may
be misplaced or missing (in the terminal, and with the scan's spectrum): sweep again. The same
message once at start-up, before the sweep, is the tuner calibrating itself and is harmless.

**A receiver's own signals.** Every dongle picks up a few signals of its own: harmonics of its
crystal (28.8 MHz on the RTL-SDR Blog V3/V4), and noise from the computer's USB. Disconnect the
antenna, then run `uv run wavescribe radio spurs a --learn` (or *Learn* under
*Each receiver's own signals* on the Receivers page). It sweeps in steps of about 5 kHz, fine
enough to see narrow lines, in about a minute for the whole range. Lines seen in
both of two sweeps are kept for that receiver's serial (`data/radio/spurs.json`), and later
scans mark them and leave them out. Spurs learnt with versions up to 0.1.0a1 were learnt in
steps of about 87 kHz, which hide narrow lines (their entries in `spurs.json` have no
`bin_hz`): learn them again. A signal that disappears when only the antenna is
disconnected comes in through the antenna: it is either real or interference near it. Narrow
lines that sit on multiples of 2.4 MHz are another receiver's clock; the scan marks them and
keeps them under Show more, and `radio scan` lists them marked but counts them, and spurs,
apart from the signals it found.

**One program per dongle.** A dongle opens in one process at a time. A scan with a receiver
that is running stations (or the spectrum monitor) pauses them for the scan and starts them again
when it ends; the page says which before you start the scan. From a terminal,
`wavescribe radio scan` and `radio watch` need the receiver free. When `serve` or another program
has it open, the command says so at once, names the program where it can tell, and exits with an
error: scan from the page instead (Find → On the air), stop `serve`, or use another dongle. A
watch whose receiver stops sending says so when it stops and when it is back, and then exits with
an error too: it never reports a band it didn't hear.

## Listening by hand

**Listen** → **Tune by hand** is a receiver you tune yourself, like a hand-held scanner:
nothing is written down, stored or checked for alerts. Pick a receiver and type a frequency,
press a band (FM, airband, marine, amateur, PMR446...) or **Listen** beside anything on
**Find**. Then:

- **The spectrum and waterfall** show what the dongle hears; the orange line is where you are
  tuned and the shaded band the channel you hear. Click a signal to tune it, scroll or use the
  arrow keys to step, and zoom to 400, 100 or 25 kHz to see narrow channels. The waterfall shows
  radio that comes and goes: walkie-talkies and aircraft are short bursts.
- **‹ and ›** seek to the next signal down or up the band; **−** and **+** step one channel.
- **Squelch** silences the channel between transmissions. Left at *auto*, it opens on any
  carrier by itself; move it to set a level. **Gain** changes the dongle's amplification (try
  lower if strong stations bleed into others).
- **Scan** watches every channel of a list at once (the band plan's channels, those heard here,
  or every step in view) and stops on the strongest that comes alive, then moves on a moment after
  it goes quiet. **Skip** locks out a channel that is always busy (interference, a beacon).
- **Keep it as a station** makes what you hear a station that runs all the time: listen-only,
  transcribed, or transcribed and translated. As with Add as a station on Find, the receiver is
  let go once the station is made, and what it paused starts again where it is still heard.

A frequency the dongle can't hear from where it is tuned moves the dongle, if it is idle. If it
is running stations, the page asks first and pauses them while you listen; they start again when
you press *Stop listening*. Utility, private and digital frequencies ask you to confirm, once per
receiver, that you may receive it where you are; broadcast and amateur bands don't.

## Listen only, transcribe, or translate

Every station, from the internet or from radio, has three modes, under Settings → **Stations**:

- **Listen only:** you can play it; nothing is written down, stored or checked for alerts, and no
  model is needed. A new install can listen before it downloads anything.
- **Transcribe:** written down and checked by your alert rules, not translated.
- **Transcribe and translate:** as above, and translated into your languages as it speaks.

A station can also have its own speech model and translation model; *Automatic* takes the first
installed model that fits its language, and the shared translator. In a stations file,
`transcribe = false` or `translate = false`; the models are chosen on the page.

## Keeping a frequency as a station

On the page: **Add as a station** beside a scan result or a heard station, under a receiver
tuned by hand on the dial, or click anywhere on the scan's spectrum (the nearest channel of the
band there). For a frequency you type, tune it under **Listen** → **Tune by hand** and press
**Keep it as a station**. An FM station tuned by its frequency alone takes its RDS name once RDS
has it.

**When the receiver has to move.** A receiver stays where it is if it hears the new frequency
there. If not, it moves to where it hears the new station and every station it already has
(running ones restart within seconds). If no place hears them all, nothing changes until you
answer: the page names the stations that would be left out, running or not, and says which would
stop. A station left out isn't deleted: it stays in your stations, unavailable, with a reason
that says where to tune the receiver to hear it again. Settings → **Receivers** asks the same
question when you change a receiver's centre by hand. On the dial, the receiver's line counts
such stations, and the card of each one's band names it with **Move Receiver A there** (so does
Settings → **Stations**), which moves the receiver back and starts it, asking first if that
leaves others out. A station that is switched off is never in the way. A receiver can't hear a
station when the frequency is outside the 2.4 MHz or so it hears, or (narrowband) on its very
centre, where every dongle has a spike of its own; for a watch, when any channel of its window
is outside.

From a terminal:

```sh
uv run wavescribe radio tune a 446.00625 --language es-ES --acknowledge
uv run wavescribe radio tune a 145.5 --language de-DE        # amateur: no confirmation needed
```

In a stations file:

```toml
[[station]]
id = "repeater"
name = "Local repeater"
language = "de-DE"
device = "a"
mhz = 145.6            # any frequency; `fm_mhz` is for broadcast FM
# mode = "nfm"         # wfm, nfm or am; by default from the band plan
# channel_khz = 12.5   # the channel width; 12.5 for NFM and 8.33 for AM unless set
# squelch_db = -40     # a fixed squelch level; by default it opens on a carrier by itself
```

`ppm = 52` on a `[[device]]` corrects a dongle's crystal error (`rtl_test -p` measures it);
`--ppm` does the same for the `radio` commands and `?ppm=` in an `sdr:` source.

The demodulators are Wavescribe's own (numpy and scipy, `dsp/`):

- **WFM** for broadcast FM: a channel about 200 kHz wide, de-emphasis, and the RDS decoder fed from the
  multiplex. A rule with `kind = "traffic"` alerts when a station's RDS starts a traffic
  announcement (ARCHITECTURE.md, *Alerts*).
- **NFM** for walkie-talkies, marine and amateur radio: a 12.5 or 25 kHz channel, de-emphasis,
  CTCSS tones removed, a noise squelch that opens when someone transmits whatever their level
  and shuts with the carrier.
- **AM** for aircraft (and for medium wave once it can be tuned): an envelope detector with automatic gain, and a carrier
  squelch.

When a channel's squelch is closed the page shows "channel quiet" instead of silence.

## Catching radio that only talks now and then

*Experimental.* Walkie-talkies, repeaters and aircraft are quiet most of the time, and when
someone speaks you rarely know on which channel. A **watch** listens to every channel of a band
at once and transcribes whatever is said on any of them, though weak, very short or overlapping
transmissions can be missed (see Limits, below). Each transmission is caught on its own, from its
first syllable, with the channel it was on.

On the page: choose the band on the dial and press **Watch this band with Receiver A**. It asks
for the confirmation the band's kind of radio needs and names the stations the receiver would
stop. A receiver that doesn't hear the band is moved where it does, and if that leaves stations
out the page asks first, as when adding a station. A band wider than one receiver hears is
refused with a slice that fits, offered as a button. The receiver's line under the dial then
says what it watches, and **Stop watching** frees it. What is caught and kept is listed under
**Caught here**, by frequency, newest first: when, how long, how far above the noise, and
whether anyone spoke, with the words underneath. Give a frequency a name, tags and a note there,
**Listen there**, or **Add as a station**; the list can be narrowed to the last hour, day or
week, to one band, or to catches with speech. A kind of radio that keeps nothing
(walkie-talkies, by default) shows only in Read while the watch runs, and Caught here says so.
From a terminal:

```sh
uv run wavescribe radio watch a pmr446 --acknowledge     # all 16 PMR446 channels
uv run wavescribe radio watch a 2m --languages de en      # the 2 m band, German or English
uv run wavescribe radio watch a 446.0:446.1 --acknowledge --minutes 30   # a range in MHz
uv run wavescribe radio catches                           # what was caught and kept, with its words
uv run wavescribe radio note 446.00625 --name "Family" --tag home
```

`radio watch` prints a line for each transmission as it ends: when, the channel, how long and how
far above the noise it was, and whether anyone spoke in it. What was said comes underneath. In a
stations file, a watch is a station with `watch` in place of `mhz`:

```toml
[[station]]
id = "walkies"
name = "PMR446"
device = "a"             # tuned where it hears the whole band, or the watch can't run
watch = "pmr446"         # a band id from `radio bands`, or a range: [446.0, 446.1]
language = "auto"
languages = ["es-ES", "en"]   # optional: the languages it may be
acknowledge = true
# threshold_db = 8       # how far above a channel's noise counts as a transmission
```

A watch doesn't hold its receiver where it is. If the receiver is tuned where it doesn't hear
the whole window, from the stations file or because it was moved on the page later, Wavescribe
starts as usual and lists the watch as unavailable, with where to centre the receiver (as
`wavescribe serve` prints and `wavescribe doctor` warns). A move on the page that would leave a
watch out asks first, as for any station ([When the receiver has to move](#keeping-a-frequency-as-a-station)).

How it works:

- Every channel has a noise floor of its own, learnt while it is quiet. A channel 8 dB above
  its floor for a tenth of a second is a transmission. A pause shorter than 1.2 s (a breath, the
  reply on the same channel) stays in the same catch.
- The receiver's last two seconds are kept, so each catch is demodulated from a moment before
  it was noticed: the first word isn't lost to the time it takes to notice.
- Whatever is always there (the dongle's spike at its centre, its own signals learnt with
  `radio spurs --learn`, a beacon) becomes part of the floor. A channel busy for two minutes
  ends as one catch and is then taken as noise.
- Noise rising on half the channels or more at once is interference, not traffic: nothing is
  caught while it lasts. If it stays for ten seconds (a charger switched on nearby), it is taken
  as the new floor. The station's status says the watch is deaf while it lasts, and
  `radio watch` prints a line when that starts and when it ends.
- Up to four transmissions are demodulated at once. Transmissions on different channels at the
  same moment are transcribed one after the other, never mixed.

**Language `auto`** (the default for `radio watch`) lets Whisper tell the language of each
utterance: not everyone on a band speaks the same one. `languages` narrows the choice to the
ones you expect, which helps with short and noisy utterances. Speech clearly in none of them is
still taken as the language heard, because told the wrong language Whisper translates instead of
transcribing. Language `auto` needs a Whisper model (`uv run wavescribe models fetch
whisper-large-v3-turbo`). A watch with one language works with any model.

**What is kept.** A watch takes the strictest class of any channel in its window, with the same
confirmation. Its catches are kept as long as text of that class, and leave this machine only
where that class's text may: `/feed/v1/catches` ([FEED.md](FEED.md)) and the agent tool
`catches` ([AGENT_TOOLS.md](AGENT_TOOLS.md)) give no others. With the defaults, a PMR446 watch keeps nothing: its catches and
words are shown while `radio watch` runs and never written down, so `radio catches` stays empty.
A 2 m watch keeps both for 7 days, on this machine only. Notes on frequencies (`radio note`) stay
on this machine and are its heard list's names too.

**Alerts.** Alert rules read a watch's words like any station's. A silence alert
(`kind = "silence"`) on a watch goes off only when the receiver stops sending: a quiet band is
what a watch hears most of the time.

Limits:

- **A watch covers what one receiver hears at once**, about 2.25 MHz from the first channel to
  the last (at 2.4 MS/s). A wider band is refused with a slice that fits: the one with the most
  of the band's own channels, so marine VHF's slice is the ships' channels, not the land mobile
  radio in its middle. The refusal also says which parts of the band are another kind of radio:
  a watch is kept as the strictest kind in it.
- **NFM and AM only**: no broadcast FM, which talks all the time anyway.
- **Up to four transmissions at once.** A fifth while four are running isn't caught; the log
  (`data/logs`) counts it as `watch_missed`, without its frequency for a class that keeps
  nothing.
- **Shorter than 0.3 s isn't kept** (data bursts, key clicks).
- **A weak signal next to a strong one can be missed:** a channel 20 dB weaker than a busy one
  within three channels is taken for that signal's splatter. The receiver also shows a weaker
  mirror of strong signals about its centre, which the watch ignores, so a channel 20 dB weaker
  than the one opposite it about the centre is missed too.
- **In a window centred between two channels** (a band wider than about 0.9 MHz), the two
  channels next to the centre lie close to the dongle's spike and are the least sensitive.
- **Busy bands queue up.** Catches go through speech recognition one after another, so on a band
  with many transmissions their words come later. Beyond 16 waiting, more are counted in the log
  (`watch_behind`) and not kept.
- **Tried on the air only briefly**, with an indoor whip on 2 m, where short transmissions on an
  amateur channel were caught and logged; before that on synthetic signals and with no antenna
  (no false catches in 10 minutes on PMR446 and on 2 m). Speech from a walkie-talkie nearby
  hasn't been tried yet.

## What Wavescribe does with each kind of radio

The band plan gives every frequency a class. The class's rules apply everywhere Wavescribe
handles the text: the store, the feed, webhooks, remote followers and alerts. These are the
defaults:

| Class | Examples | Asks you first | Text kept | Audio kept | Leaves this machine | Words in alerts |
|---|---|---|---|---|---|---|
| broadcast | FM, medium wave, weather radio | no | like any station | yes | yes | yes |
| amateur | 2 m, 70 cm | no | 7 days | no | no | yes |
| utility | airband, marine VHF | yes | 3 days | no | no | yes |
| private | PMR446, FRS, CB, 433 MHz, VHF land mobile (157.45-160.6 MHz, between the marine channels), anything no plan lists | yes | never (shown live only) | no | no | no |
| digital | DAB, DVB-T, digital emergency networks, mobile networks, ADS-B, satellite navigation | yes | never (shown live only) | no | no | no |

- **Asks you first**: before a station on such a frequency is created, you confirm that you may
  receive it where you are (the checkbox on the page, `--acknowledge`, or `acknowledge = true`).
  The confirmation holds for that class only: the page saves the classes you confirmed for the
  station (`acknowledge = "utility"`, or `["utility", "private"]` after a second one), and a
  station that later lands in a class you haven't confirmed (another frequency, another
  region) is asked again. In `stations.toml`, `acknowledge = true` confirms the class the
  frequency is in when the file is read; write the class instead to be asked again if that
  ever changes.
- **Leaves this machine** covers the `/feed/v1` API, webhooks and live pushes, and other
  Wavescribe nodes following this one. An alert raised on radio that stays here still goes out,
  but only which rule matched, when and on which station: no words, matched terms or audio.
  Text that does leave carries its class, and the machine that receives it applies its own
  rules for that class.
- **Never (shown live only)**: the page shows the words as they are heard, and nothing is
  written down. Alerts without *words in alerts* say that a rule matched, not what was said.
- **Digital signals aren't decoded.** Public ones (DAB, DVB-T, ADS-B) simply aren't
  implemented; protected ones (digital emergency networks such as TETRA, mobile networks) are
  never decoded or decrypted. Tuning any of them with the analogue demodulators gives noise. You
  can still tune them, because a band plan can be wrong where you are.
- Wavescribe only receives. It has no transmit code.

### You decide what is kept

The defaults are cautious, and you can change every one except *asks you first*: on the page
(Settings → **Storage and privacy** → *What's kept of each kind of radio*), or in the stations
file:

```toml
[radio.policy.amateur]
text_days = 30          # days, 0 for never, or "keep" for the store's own retention
audio = true            # keep the audio of what was said
export = false          # let the text leave this machine (feed, webhooks, followers)
quote_in_alerts = true  # alerts carry the words heard
```

The page's changes are laid over the file's. They apply at once, also to what is already kept:
a shorter time removes older text, and the words kept in alerts, within a minute, also while
nothing is being received.

## The law where you are

Laws on receiving radio differ by country, and **you are responsible for following yours**.
Wavescribe is software: it can't know where you are or what you are allowed to do, so it leaves
the decisions to you and makes them explicit. In many countries:

- broadcast radio is meant for the public: listening is free, and recordings are for personal
  use (broadcasts are copyrighted);
- amateur radio is open to listeners;
- other radio (walkie-talkies, aviation and maritime traffic) may be received, but what is said
  may not be recorded, used, or passed on to anyone else;
- private communications are protected, and listening on purpose may be an offence in itself;
- decrypting encrypted or protected services is forbidden;
- what people say on the radio can identify them, so sharing transcripts beyond your household
  may bring in data-protection law (such as the GDPR in the EU).

Wavescribe's defaults are cautious, but they are not the strictest reading: it asks before it
listens to radio that is neither broadcast nor amateur, shows walkie-talkie and unlisted channels
live only, keeps aircraft and marine text for 3 days and amateur text for 7, keeps no audio of
any of them, and sends none of their words off the machine. Where what is said may not be written
down at all, set `text_days = 0` for that class before you tune it. You can change all of this
(above), within what the law where you are allows. If you're unsure, ask your national radio
regulator. Nothing here is legal advice.

## Band plans and regions

`src/wavescribe/radio/bands.toml` lists the bands, each with its edges, regions, demodulation,
channel raster or channel list, and class. Where a region demodulates a band differently, the
band says so: CB is narrowband FM in the CEPT countries and AM elsewhere. Regions follow the ITU:

| Region | Where |
|---|---|
| `cept` | Europe (CEPT countries): Region 1 plus European-only bands such as PMR446 |
| `1` | Africa, the Middle East, the rest of Region 1 |
| `2` | The Americas |
| `3` | Asia-Pacific |
| `jp` | Japan (its own FM band) |

Until you choose a region, the band list follows this computer's region setting, but each
frequency takes the strictest class any region gives it (FM broadcast excepted): a computer set
to US English may well be in Europe, where 446 MHz is walkie-talkies rather than amateur radio.
Set your region with `[radio] region = "2"` in the stations file, or on the page. Where a region
has its own version of a band, the usual name finds it: `airband` is the Americas' `airband-25`
(a 25 kHz raster), `fm` their `fm-americas`, `2m` their `2m-wide`. To add a band,
add a `[[band]]` to `bands.toml`: it's data, not code. A frequency that no plan for your region
lists counts as private. One that any region marks as digital counts as digital everywhere. A
channel is as wide as its station's `channel_khz`, or the width chosen under Listen, or else its
band's channels (200 kHz for FM, 25 kHz where no plan says); one whose edge reaches a stricter
band takes that band's class: a channel just below 380 MHz, where digital services start,
counts as digital.

Changing the region never makes a station less strict by itself. If the new region puts a
station's frequency in a class that keeps more (446 MHz is walkie-talkies in Europe and amateur
radio in the Americas), the station keeps its old class until you confirm on the page that you
may receive it as the new one.

## Why there is no downloaded station list

Wavescribe can't ship a list of every station in the world. National frequency registers are
often published under terms that forbid redistribution or derived works. Community lists are
usually for private use only, and open data covers very few stations with their frequencies.
The list under **Listen** → **On the air here** is therefore built only from your own scans: it describes what *you*
can receive, and needs no data from outside.

## Known limits

Corrections and pull requests on any of these are welcome.

- **Narrowband FM and AM are experimental**, and so are several dongles sharing one antenna
  through a splitter. Broadcast FM with RDS is the part that has run on air longest.
- **Narrowband FM is set up for 12.5 kHz channels** (PMR446, most European repeaters). Wider
  signals (±5 kHz deviation: marine, NOAA weather, GMRS, amateur FM in the Americas) come out
  twice as loud and distorted in a 12.5 kHz channel (29–41 dB SINAD against 48–63 dB with
  `channel_khz = 25`, on synthetic signals). The band plan gives the amateur bands of Regions 2
  and 3 and marine VHF 25 kHz channels under Listen and Find; for a station in the
  stations file, set `channel_khz = 25`.
- **Frequency correction is manual.** An ordinary 50 ppm dongle is about 22 kHz off at 446 MHz:
  measure it with `rtl_test -p` and set `ppm`. A dongle with a TCXO (RTL-SDR Blog V3 or V4)
  needs none.
- **Programme type names follow the European RDS table.** North American stations (RBDS) number
  them differently, so their type can be named wrongly.
- **No SSB or CW**, so amateur radio means FM repeaters and simplex. No DAB, HD Radio, DMR,
  TETRA or other digital voice.
- **Speech recognition on aircraft and handheld radio audio is likely poor:** the models were
  trained on cleaner speech.
- **24-1766 MHz only.** Medium wave and shortwave (below 24 MHz) need the V4's built-in HF
  upconverter and a long wire antenna; Wavescribe doesn't support them (nor the V3's direct
  sampling).
- RDS needs a stronger signal than audio does. A whip antenna indoors names the strongest
  stations only; an outdoor antenna names most of them.
- Scans are a snapshot. Watch for a few minutes, and scan at different times of day.
