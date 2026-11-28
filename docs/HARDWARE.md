# Hardware: what to buy and how to connect it

You can start with no hardware at all: internet radio stations need only the Mac. A radio
receiver lets Wavescribe hear FM radio with no internet, and other radio too. This page says
which Mac, which receiver and antenna, how to connect them, and what it can't do. The
second half has the detailed setup and the measurements behind it.

Prices are rough 2026 figures; they vary a lot between shops and countries.

## The computer

**A Mac with Apple Silicon** (M1 or later). The speech and translation models run on its
graphics processor through Apple's MLX. Intel Macs, Windows and Linux aren't supported.

| Mac | What to expect |
|---|---|
| **M1 Max, 64 GB** | Four news stations transcribed and translated around the clock: 6.6 GB of memory, peaks of 8.5 GB, translation busy 28% of the time and transcription 6% (a 34-hour run; [MEASUREMENTS.md](MEASUREMENTS.md)). More may fit, depending on how much is said: watch Settings → Health |
| Max or Ultra, 32 GB or more | As above |
| Pro, 18–32 GB | About half the M1 Max's graphics cores: expect two to four translated stations, more transcribed only |
| Any M chip, 16 GB | Everything fits in memory, with little left for other apps. The base chips have a quarter of the M1 Max's graphics cores: expect one or two translated stations, more transcribed only |
| 8 GB | Listening, and probably transcription of a few stations. The translation model doesn't fit beside macOS |

The M1 Max row is measured; the others are estimates from memory and graphics cores.

- **Transcription is light, translation is heavy.** Speech recognition takes a few percent of
  the M1 Max for four stations; translation takes most of the work. Stations that are only
  transcribed, or only listened to, cost little. Past what the Mac can translate, translations
  fall behind, by minutes on a busy machine: translate fewer stations, or fewer languages.
- **Settings → Languages and models** estimates whether each model fits this Mac, from its
  memory and graphics limits.
- **Disk:** about 8 GB of models (10 GB with Whisper for languages outside Europe), plus about
  30 MB a day of text for four busy stations (about 11 GB after a year, the default limit) and
  at most 2 GB of speech audio by default.
- **Power:** roughly 30–60 W for an M1 Max under this load (an estimate); three dongles and a
  hub add about 4–5 W.

Numbers from other Macs are welcome: open an issue with the figures from Settings → Health.

## What to buy

| Setup | What it does | Rough cost |
|---|---|---|
| **Nothing** | Internet radio stations only | €0 |
| **One dongle and its antenna** | Two to five FM stations that sit within about 2.2 MHz of each other, or finding stations and listening to any frequency by hand | €40–60 |
| **Three dongles in one box** (the reference kit, experimental) | Two windows of FM stations around the clock, plus a third receiver for scanning, walkie-talkies, aircraft or radio amateurs. One antenna, one cable to the Mac | €150–250 |

The one-dongle setup is the place to start: everything else is the same dongle, more times.
The three-dongle box is described [below](#the-reference-kit-in-detail).

**Which dongle.** The **RTL-SDR Blog V4** family: the V4 and the V4L ("Lite", the one used
here). They have a good tuner, a stable crystal, an aluminium case and a built-in bias tee.
Buy from the official reseller list on rtl-sdr.com: **many marketplace "RTL-SDR Blog" dongles
are fakes.** Wavescribe is tested with the RTL-SDR Blog V4 family.

Other dongles with the RTL2832U chip work through the same driver; what differs is the tuner
chip beside it, which `rtl_test` names ("Found … tuner"):

| Tuner | What happens |
|---|---|
| R820T, R820T2 (most RTL-SDR dongles) | The same as the V4 family: the same range and gain steps. |
| E4000 (about 52–2200 MHz, with a gap around 1100–1250 MHz) | It runs, but the page shows the R820T range (24–1766 MHz) and gain steps for it: it offers 24–52 MHz, which the stick can't tune, doesn't offer 1766–2200 MHz, and its gain words land on the stick's nearest step, at most 42 dB. |
| FC0012, FC0013 (about 22–948 and 22–1100 MHz) | It runs, but the page shows the R820T range and gain steps for it: bands above the stick's top look tunable, and every gain word lands on its top step, about 19 dB. |

An **Airspy R2** handles strong nearby stations better and hears a wider window, but it isn't
supported; nor are HackRF, SDRplay or receivers reached over the network (`rtl_tcp`).

## Antennas

The dongle kits usually come with a telescopic whip or a small dipole; that's enough to start.
Indoors near a window, a whip on 3 m of thin cable found 20–25 FM stations in one test.

**How long to extend it:** a quarter of the wavelength works well.

| Band | Frequency | Whip length |
|---|---|---|
| FM broadcast | 88–108 MHz | about 75 cm |
| Aircraft | 118–137 MHz | about 55–60 cm |
| Amateur 2 m, marine | 144–174 MHz | about 50 cm |
| Amateur 70 cm, PMR446 walkie-talkies, 433 MHz | 430–446 MHz | about 17 cm |

The length isn't critical for strong FM: 10 cm either way makes little difference.

- **Placement:** vertical (FM broadcast is mostly vertically or mixed polarised), near a window, and away from the Mac, the hub and other electronics,
  which make noise the antenna hears. Higher and outdoors is better if signals are weak.
- **No antenna yet?** About 75 cm of thin solid wire (one conductor from an old network cable)
  pushed into the dongle's centre socket hears strong FM stations. Nothing thicker than the
  0.9 mm pin of an SMA plug, or it spreads the socket. Keep the bias tee off (it is by default).
- **Walkie-talkie antennas** fit too, through an adapter (next section).

## Connecting it: the SMA trap

Dongles and small antennas use **SMA** connectors, which come in two genders that are easy to
mix up:

- **SMA male:** a **centre pin**, with the thread **inside** the nut.
- **SMA female:** a **socket** (a hole), with the thread **outside**.
- **RP-SMA** ("reverse polarity", often sold for Wi-Fi) swaps the pin. It doesn't work with
  these dongles.

The dongle has an **SMA female** socket, so the antenna or cable needs an **SMA male** end.
**Check the pin itself, not the listing:** telescopic whips sold as "SMA male" often turn out to
be female, like the dongle, and then the two don't screw together. Walkie-talkie antennas are
usually female too. A small **SMA adapter kit** (male–male, female–female, male–female, a few
euros) solves every mismatch; buy one with the first dongle.

**An extension cable** lets the antenna stand by the window while the dongle stays by the Mac.
3 m of thin RG174 coax loses about 1 dB at 100 MHz and 2 dB at 446 MHz, which is harmless.

**Several dongles** need a **USB hub**. A bus-powered USB-C hub with four USB-A ports carries three
dongles: an RTL-SDR typically draws 250–300 mA, about 4–4.5 W for three with the hub (typical
figures). Short USB extensions (about 15 cm) keep the wide dongles from blocking each other's ports and
from heating each other.

## Sharing one antenna

A **4-way TV splitter** (5–2400 MHz, F connectors) lets one antenna feed several dongles, with
an **SMA-male to F-male pigtail** for each dongle and an adapter for the antenna cable. It's a
75 Ω part on 50 Ω dongles; for receiving, the mismatch costs little.

- Each output gets about 7 dB less signal (6 dB for a four-way split, plus about 1 dB of loss).
  That's fine for strong FM, but it hurts weak signals such as walkie-talkies and the 70 cm
  amateur band. Better: the FM dongles share the antenna through the splitter, and the dongle
  for those bands gets its own short antenna.
- TV splitters don't pass medium wave, which Wavescribe doesn't receive anyway.

## Limits today

- **Macs with Apple Silicon only.** Not Linux, Intel Macs or Windows.
- **RTL-SDR dongles only,** tested with the RTL-SDR Blog V4 family ([which dongle](#what-to-buy)).
  Airspy, HackRF, SDRplay, SoapySDR devices and receivers over the network aren't supported.
- **A window of about 2.2 MHz per dongle.** At 2.4 million samples a second (above that, RTL-SDR
  dongles tend to drop samples) a dongle takes in 2.4 MHz, and its filter fades the edges.
  Wavescribe follows FM stations up to 1.08 MHz either side of the dongle's centre (2.16 MHz)
  and narrow channels up to 1.14 MHz (2.28 MHz): stations further apart need more dongles.
- **24–1766 MHz.** Medium wave and shortwave (below 24 MHz) aren't received.
- **No digital radio:** DAB, DMR and other digital voice aren't implemented, and protected
  services (TETRA) are never decoded or decrypted.
- **One program per dongle.** A second program that opens a dongle in use fails with
  `usb_claim_interface error -3`. A forgotten `rtl_fm` or `rtl_power` can hold a dongle too.
- **Unplugging a dongle** stops its stations; when it's plugged back in they start again by
  themselves (the receiver retries with a growing delay, at most 30 s).
- **macOS asks first** before a new USB device may connect ("Allow accessory to connect?").
  Approve it once per dongle and hub.

## The reference kit in detail

**Start lean, measure, then upgrade.** Where internet streams still work, they cover the wider
station list; the receivers only have to carry the priority FM clusters plus one flexible
receiver for when there is no internet.

**One box, one antenna, one cable to the computer.** Three dongles, a USB hub and an RF splitter go
in a single vented enclosure (a 3D-printed PETG box works). The telescopic whip stands apart, in a
small weighted stand by a window, and reaches the box over 3 m of thin coax, so the box can sit
wherever is convenient. Nothing has to go outdoors if the main FM transmitters are strong where
you are; add an outdoor antenna only if measurements show weak signals.

```
 telescopic whip, ~75 cm (SMA-female base, like the dongles)
        │
 weighted stand by the window: holds the whip; the cable's SMA-male end
 screws into the whip's base
        │
        └── 3 m RG174, SMA-M → SMA-F ─┐
 ┌────────────────────────────────────┴─── vented box ─────────┐
 │  splitter IN (back wall): SMA-F→F-M adapter + SMA M-M       │
 │  4-way splitter, 5–2400 MHz                                 │
 │    OUT1 ─ F→SMA pigtail ─ V4L A ─┐                          │
 │    OUT2 ─ F→SMA pigtail ─ V4L B ─┼─ USB-A ext ─ 4-port hub ─┼── USB-C (60 cm) ── computer
 │    OUT3 ─ F→SMA pigtail ─ V4L C ─┘                          │
 │    OUT4   spare                                             │
 └─────────────────────────────────────────────────────────────┘
```

| Item | Role |
|---|---|
| **3 × RTL-SDR Blog V4L** (R828S, 1 ppm TCXO, HF upconverter, bias tee, aluminium case, USB-A plug) | **A** and **B** each park on one FM cluster about 2.2 MHz wide. **C** is the flexible one: PMR446, 2 m/70 cm, or a third FM cluster. Identical devices, one driver. Buy from rtl-sdr.com's official reseller list: **most marketplace "RTL-SDR Blog" units are fakes.** |
| Bus-powered USB hub, USB-C upstream, 4 × USB-A, over-current protection | One cable to the computer |
| 3 × short USB-A extensions (~15 cm) | The dongles are too wide for adjacent hub ports; the extensions also space them apart for heat and USB noise |
| 4-way splitter, 5–2400 MHz, F connectors | One antenna feeds all three receivers. ~6.7 dB insertion loss at 40–1000 MHz, harmless for strong FM. Doesn't pass MW |
| SMA-female → F-male adapter | On the splitter input, with an SMA male–male adapter on top, so the cable's SMA-female end screws on |
| SMA-male → F-male pigtails, RG316, ~15 cm | Splitter outputs to the dongles |
| Telescopic whip, 70–1000 MHz, SMA | Test-first antenna. Extend it to ~75 cm, a quarter wavelength at FM. **Check the connector itself, not the listing**: male and female SMA are often mislabelled |
| 3 m SMA extension, RG174 | Stand to box. RG174 loses under 1 dB over 3 m at 100 MHz |
| *Optional:* FM band-stop filter | For PMR446 and the amateur bands, so strong FM doesn't swamp them |

Roughly €150–250 in total (2026 prices; they vary a lot by shop and country).

**Why it works, and what to watch:**

- **Power.** An RTL-SDR typically draws 250–300 mA, so three draw under 1 A (~4 W). A laptop's
  USB-C port supplies that without a powered hub.
- **Bandwidth.** 3 dongles × 2.4 MS/s × 2 bytes (8-bit I and Q) ≈ 115 Mbit/s, well within one
  USB 2.0 tree (480 Mbit/s).
- **Heat.** The dongles get warm. Vent the box at the bottom and the top so air rises past them,
  and leave a gap between dongles. The TCXO keeps the frequency stable as they warm up, but the
  box mustn't trap heat.
- **Counterpoise.** A whip is a monopole: it needs an electrical "other half", and here the
  extension's coax shield plays that role, imperfectly. If signals are weak, try a small metal
  plate or a few radial wires at the stand, bonded to the coax shield at the whip's base, before
  buying an outdoor antenna.
- **Splitter loss** matters for weak signals, not for strong FM. PMR446 and 70 cm traffic is weak:
  if receiver C struggles there, give it its own antenna rather than amplifying the whole tree.
- **The spare output.** A 75 Ω F terminator on OUT4 is optional. An open port slightly reduces the
  isolation between outputs, which lets one dongle's local-oscillator leakage reach the others a
  little more.
- **Keep the bias tee off** (the default). Nothing in this chain needs power.

**Strong nearby FM is the RTL-SDR's known weakness.** If the 8-bit dongles overload near a
strong transmitter even with manual gain, lower the gain or add the band-stop filter for the
other bands. A 12-bit receiver such as the Airspy R2 handles it much better
([comparison](https://www.rtl-sdr.com/comparison-several-sdrs-degradation-broadcast-fm-frequencies/)),
but Wavescribe supports only RTL-SDR dongles.

## Planning frequencies

An RTL-SDR at 2.4 MS/s hears a window of about 2.2 MHz, so group the stations you care about into
clusters that wide and give each cluster a receiver:

1. List your priority stations and their local FM frequencies: scan the band
   (`uv run wavescribe radio scan a fm`, or Find → On the air; [RADIO.md](RADIO.md)), or look them up on
   the broadcasters' sites.
2. Find the windows of about 2.2 MHz that hold the most priority stations; park receiver A and B on
   their centres.
3. Give receiver C whatever is left: a third cluster, PMR446 (446.0–446.2 MHz) or the amateur
   bands.
4. Verify everything from the antenna with a band survey (below), and keep streams for the rest.

| Receiver | Centre | Stations (example) |
|---|---|---|
| A | ~96.0 MHz | three stations between 95.2 and 96.8 MHz |
| B | ~105.0 MHz | a news station and one more within ±1 MHz |
| C | as needed | PMR446, 2 m, 70 cm, or a third FM window |

## RF and driver notes

- Use **manual gain, not AGC**: strong FM overloads 8-bit dongles.
- An RTL-SDR at **2.4 MS/s** gives about 2.2 MHz usable ([Limits today](#limits-today)), which is 2–5 real stations per dongle.
- **librtlsdr 2.0.3 or newer** is needed for the V4L/V4Lc's R828S. The Homebrew bottle is 2.0.3,
  so no source build is needed on macOS (rtl-sdr.com's V4L guide describes one; it predates that
  release).
  radioconda bundles 2.0.2, which doesn't know the V4L. The driver recognises the V4L by its
  EEPROM strings `RTLSDRBlog` / `Blog V4L` (an exact match): never change them.
- **pyrtlsdr ≥0.4 won't import against Homebrew librtlsdr** (missing `rtlsdr_set_dithering`), and it's GPL-3.0 anyway: never import it.
- **SDR++** (nightly arm64) is fine for surveying, but multi-VFO with audio sinks hangs on macOS ([#1702](https://github.com/AlexandreRouma/SDRPlusPlus/issues/1702)). Its macOS app bundles its own librtlsdr, and rtl-sdr.com's V4L guide listed it as not supporting the V4L yet, so the first checks use the command-line tools instead (`rtl_power` plus `scripts/fm_survey.py` for a band survey).
- **GQRX** 2.17.7 (Homebrew cask) bundles a librtlsdr that knows the V4 but not the V4L (no
  `Blog V4L` in `Gqrx.app/Contents/Frameworks/librtlsdr.0.dylib`), despite the datasheet saying
  GQRX ships current drivers. Opened directly, a V4L is driven as a plain R820T: the V4L guide says
  that gets no signal. Let Homebrew's driver own the dongle and feed GQRX over loopback instead:
  `rtl_tcp -d 00001234 -a 127.0.0.1 -p 1234`, then the GQRX device string
  `rtl_tcp=127.0.0.1:1234,psize=16384`. Only one program can hold a dongle at a time.
- **De-emphasis is 50 µs in Europe** (75 µs in the Americas).

## First steps with the dongles

The V4L has a **USB-A** plug and an **SMA-female** antenna socket. On a Mac with only USB-C ports,
use a USB-C→A adapter until the hub is in. Steps 1 and 2 need no antenna.

**macOS asks first.** On Apple silicon a new USB device stays disconnected until you approve the
"Allow accessory to connect?" prompt (each dongle, and the hub). Until then the hub shows up in
`ioreg` with nothing behind it and `rtl_test` says `No supported devices found`. The policy is in
System Settings → Privacy & Security → "Allow accessories to connect". For unattended 24/7 use,
check that an approved dongle reconnects with no prompt after a replug or a reboot while locked.

1. **Driver and tools.** librtlsdr brings `rtl_test`, `rtl_sdr`, `rtl_fm`, `rtl_power`,
   `rtl_eeprom` and `rtl_biast`:

   ```sh
   brew install librtlsdr        # 2.0.3 or newer
   rtl_test -t                   # lists every dongle with its tuner
   ```

   A V4L shows up as `RTLSDRBlog, Blog V4L`, then `Found Rafael Micro R820T tuner` (the R828S
   identifies itself as an R820T) and `RTL-SDR Blog V4 Lite Detected`. `-t` is a benchmark for an
   older tuner, so it ends with `No E4000 tuner found, aborting.`: that's expected.

   SoapySDR (`brew install soapysdr soapyrtlsdr`) isn't needed. Wavescribe uses an `rtl_sdr`
   pipe: it needs nothing but librtlsdr, and a subprocess is the cleanest licence boundary
   (librtlsdr is GPL).

2. **A unique serial per dongle.** They usually ship with the same serial (`00000001`), and with
   three plugged in there's no telling A from B. Plug in **one at a time**:

   ```sh
   mkdir -p data/sdr                                 # where these steps keep their files
   rtl_eeprom -d 0                                   # shows the current strings and serial
   rtl_eeprom -d 0 -r data/sdr/eeprom-A-orig.bin -s 00001234
                                                     # backs up, then sets A's serial:
                                                     # confirm with y, then unplug and replug
   ```

   Use another serial for each dongle (for example `00001235` and `00001236`) and label it. Change **only** the serial: the manufacturer and product
   strings must stay `RTLSDRBlog` / `Blog V4L`, or the driver stops treating it as a V4L
   (`rtl_eeprom -w <backup>` restores it). `rtl_eeprom -d` takes an index, not a serial, which is
   another reason to plug in one dongle at a time. Wavescribe addresses dongles by serial
   (`rtl_sdr -d 00001234`), so the hub port doesn't matter. The rtl tools try `-d` as a device
   index first (`00000001` *is* index 1, and a leading 0 means octal), and only then as a
   serial; for a serial like that, Wavescribe looks up the dongle's index on USB each time it
   opens it. A single dongle works with its factory serial; several need serials of their own.

   **Bias tee off.** The bias tee puts 4.5 V on the antenna socket to power an amplifier; with a
   passive whip it does nothing useful, and into a DC-short antenna it's a short circuit. The
   osmocom driver never switches it on by itself (it ignores the RTL-SDR Blog driver's EEPROM
   "always on" flag), but make sure once per dongle: `rtl_biast -d 0 -b 0`. A small red LED next
   to the antenna socket means it's on.

3. **A first listen.** A quick ear check that the antenna and each dongle work, on a strong local
   station:

   ```sh
   rtl_fm -d 00001234 -M wbfm -f 99.0M -g 20 - | ffplay -nodisp -f s16le -ar 32000 -ch_layout mono -
   ```

   **The antenna for this check.** Best: the whip on the dongle through an SMA male–male adapter.
   Also fine: whip → pigtail → splitter IN, then splitter OUT → pigtail → dongle. With neither at
   hand, ~75 cm of thin solid wire pushed into the dongle's centre socket hears strong local
   stations: one conductor from an old Ethernet cable is right. Nothing thicker than the 0.9 mm pin
   of an SMA plug, or it spreads the socket's contacts; keep the wire off the threaded shell. Bias
   tee off first (step 2).

   `-g` is the manual gain in dB. If other stations bleed in or the sound distorts, the dongle is
   overloaded: lower it. If it hisses, raise it. Note the value that sounds clean for each
   receiver. A manual gain also means no AGC: the tuner's gain stays fixed, and `rtl_fm` never
   turns on the RTL2832's digital AGC. With the whip straight on the dongle, signals are ~7 dB
   stronger than through the splitter, so the final gain will be higher; with a bare wire, they're
   weaker. The gain that counts is the one measured on the final chain. (`rtl_fm` de-emphasises at
   75 µs, the American value; Europe uses 50 µs, so treble sounds a little dull there. Fine for an
   ear check. Wavescribe's own demodulator uses 50 µs unless a station sets `deemphasis_us = 75`.)

4. **A band survey.** Which stations each receiver actually hears, and how strongly:

   ```sh
   rtl_power -d 00001234 -f 87.5M:108M:10k -g 20 -c 20% -i 10 -1 data/sdr/survey-A-g20.csv
   uv run python scripts/fm_survey.py data/sdr/survey-A-g20.csv
   ```

   Repeat at `-g 10`. Stations stay put at both gains; peaks that disappear at the lower gain are
   overload products.

5. **Equal load: heat and USB.** All three at Wavescribe's sample rate for 10 minutes, through the
   hub:

   ```sh
   for s in 00001234 00001235 00001236; do
     rtl_test -d $s -s 2400000 > data/sdr/load-$s.log 2>&1 &
   done
   sleep 600; pkill -x rtl_test; grep -c "lost at least" data/sdr/load-*.log
   ```

   `rtl_test` just streams and counts samples, so the load is identical. Zero "lost" lines means
   the hub and USB carry all three. Then compare the cases by hand right away: they should be
   about equally warm. Each has a thermal pad from the board to the aluminium case, so a case that
   stays clearly **cooler** than the others is the suspicious one (the heat is staying on the
   board).

6. **Through Wavescribe.** The same station, transcribed and translated:

   ```sh
   uv run wavescribe listen antena1 --source "sdr:00001234/99.0?gain=20" -v
   ```

   `-v` shows the `sdr_level` lines every 30 s: the station's power in dBFS and `clipped_max`, the
   share of samples at the ADC's limits. More than about 0.1% clipped means too much gain. For
   `serve` and the web page, describe the dongles as `[[device]]` entries in
   `config/stations.toml` (the example file shows how) and give each station its `device`.

## What a first scan finds

With one V4L and its whip indoors (gain 30 dB):

- **FM:** about 25 stations in under two minutes, transcribed and translated like internet
  streams. RDS names the strongest few; an outdoor antenna names more.
- **The whole range** (24–1766 MHz) takes about 30 s to sweep. Digital services and mobile
  networks show up and are listed as digital (Wavescribe has no digital decoder).
- **Walkie-talkies and the amateur bands** are usually quiet in a single sweep: watch them for a
  while (`--watch`).
- **With no antenna**, a receiver hears only its own spurs: `wavescribe radio spurs` learns them,
  and later scans mark them.

## Power

An M1 Max under a mixed ML load draws roughly 30–60 W (an estimate). The dongles add about
1.5 W each, and a hub a little more. On battery, budget for the Mac: a 1 kWh battery runs this
setup for roughly 15–25 hours.
