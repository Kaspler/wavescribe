# Wavescribe as a component: the feed, outputs and remote stations

Wavescribe works fully on one machine with nothing else. It can also be part of something
bigger. Another program can read what it hears (the **feed**), it can push what it hears to
other programs (**outputs**: webhooks and live pushes), and one Wavescribe can follow the
stations of others (**remote stations**). That last one lets a central server show and alert
on many places' local radio.

The feed, outputs and remote stations carry text and metadata, never audio: an `audio_url`
points back at the machine that kept the recording. With `serve --lan`, devices on your network
can play that audio on the page, as they can read the text.

## Event format (version 1)

The pull feed and the pushes carry the same JSON objects.

```json
{
  "type": "utterance", "version": 1, "id": 4182,
  "node": {"name": "kitchen-mac", "url": "http://kitchen-mac.local:8765"},
  "station": {"id": "tsf", "name": "TSF", "language": "pt-PT", "privacy": "broadcast"},
  "time": "2026-11-26T14:03:12Z", "t_start": 1795701792.1, "duration": 6.2,
  "language": "pt-PT",
  "text": "A circulação na ponte está cortada nos dois sentidos.",
  "translations": {"en": "Traffic on the bridge is closed in both directions."},
  "flags": [], "rules": ["roads"],
  "audio_url": "http://kitchen-mac.local:8765/api/segments/4182/audio.wav"
}
```

Utterance fields:

- `id`: the segment id on the machine that heard it. It grows in the order lines are
  transcribed, which isn't always the order they finish (see *Resuming*).
- `text`: what the speech recogniser wrote, in the station's language.
- `translations`: the languages that machine translates this station into.
- `flags`: notes from the ASR checks. For example, `lang:es` means it sounded like Spanish,
  `repetition` that a phrase was repeated four or more times inside a longer message, and
  `rechecked` that Whisper listened again to a line heard in another language: without `lang:`
  beside it, Whisper heard the station's language and the text is its transcript. A
  translation that was left out says so too: `untranslated:<language>:<why>`, where why is
  `declined` (the model said it can't translate the line), `remark` (it wrote about the text
  instead), `too_long`, `empty` or `failed`; the line then comes without a translation into that
  language.
- `rules`: the alert rules this utterance tripped.
- `node.id`: the sending machine's id, random, made at its first start and kept in its
  `config/ui.toml`. It says nothing about the machine. The commands' `--json` carry it too,
  once Wavescribe has started in that folder.
- `"via"`: only on a line the sender took from another machine: the ids of the machines it
  passed through before the sender, the one that heard it first. Wavescribe refuses a pushed
  or followed line that already passed through it, or through more than 8 machines, so two
  machines that push to each other never echo a line back and forth.
- `audio_url`: appears only when the recording is kept and the machine's address is known.
  The page sets that address under Settings → Connections → *For other programs* (*Public URL*).
- `node.url`: the *Public URL* when one is set. Otherwise `http://localhost:<port>`, or with
  `serve --lan` the machine's private IP address on the LAN, looked up again every 10 minutes,
  and `null` when it has none. Never its hostname: that often holds the owner's name, and events
  travel. `node.name` is the name set on the page, `wavescribe` until then.
- `station.privacy`: the station's privacy class on the machine that heard it (`broadcast`,
  `amateur`, `utility`, `private`, `digital`; see [RADIO.md](RADIO.md)). A machine that takes
  the text in keeps it, quotes it in alerts and passes it on by its own rules for that class.
  The class is the sender's statement: a sender that leaves it out is taken to send broadcast,
  so a program that pushes radio that isn't broadcast must say so. A class the receiver doesn't
  know is handled as `private`. Once a sender states a stricter class, the station is handled
  by it from then on. On the receiving machine, `privacy` on a remote station in
  `stations.toml` (pulled or `remote = "push"`) sets the least strict class its text gets:
  what the sender says can make it stricter, never looser.

```json
{
  "type": "alert", "version": 1, "id": 77, "node": {"name": "kitchen-mac", "url": "…"},
  "rule": "water", "kind": "keyword", "severity": "high", "status": "delivered",
  "time": "2026-11-26T14:05:40Z", "first_at": 1795701740.0, "last_at": 1795701940.2,
  "mentions": 3, "terms": ["corte de água"], "reason": null, "checked": null,
  "station": {"id": "antena1", "name": "Antena 1", "language": "pt-PT", "privacy": "broadcast"},
  "stations": [{"id": "antena1", "name": "Antena 1", "language": "pt-PT", "privacy": "broadcast"}],
  "language": "pt-PT", "text": "…what was said…", "translations": {"en": "…"},
  "context": [{"id": 4180, "time": "…", "station": "antena1", "text": "…", "translations": {}}],
  "segment_id": 4181,
  "audio_url": "…/api/segments/4181/audio.wav", "cap_url": "…/api/alerts/77.cap",
  "change": "new"
}
```

Alert fields:

- `change` (live events and pushes; not in `/feed/v1/alerts`): what this event is about. An
  alert is one incident: a story repeated on the air is one alert whose `mentions` grow, and it
  ends once nobody mentioned it for the cooldown (15 minutes by default); the next mention is a
  new alert with a new `id`.
  - `new`: the alert was announced for the first time.
  - `escalated`: announced again because it got more serious (`severity` rose).
  - `reminder`: still being mentioned, at most once per `remind_minutes` (an hour by default).
  - `ended`: a station alert is over (the station's audio is back).
  - `update`: nothing was announced: another mention folded in, a translation added, a
    delivery outcome recorded, or an alert opened while it is still being checked (`pending`).

  `/feed/v1/events` sends every change, so a program that reacts to alerts should act on
  `change != "update"` (or only on `new`) and treat `update` as fresh numbers for an alert it
  has seen. Pushes to outputs only ever carry `new`, `escalated`, `reminder` and `ended`.

- `kind`: `keyword` or `llm` (a rule written in plain language and checked by the LLM), or a
  station alert: `silence` (a station gone silent) or `traffic` (an FM station's RDS traffic
  announcement). A station alert's `text` is Wavescribe's own wording, in English (`language`
  `en`), with no words heard and no `terms`; when a silent station's audio is back, a second
  event for the same alert carries "Audio is back on …" as its `text` and `reason`.
- `reason`: the LLM's one-line reason, when it was involved.
- `checked`: for a keyword rule whose matches the LLM confirms (`verify`), whether it did.
  `true`: the LLM confirmed the match the alert's `severity` comes from. `false`: that match
  was raised without the LLM's answer (none loaded, too busy, a failure, no answer in time),
  `reason` starts with "not checked by the LLM" and says why, and `severity` is one step below
  what a confirmed match gets, unless the rule has `unverified = "announce"`. `null`: nothing
  was to be checked (any other rule), or the answer hasn't come yet. It can change during an
  alert, with an `update` or an `escalated` event: a later match the LLM confirms makes it
  `true`, and one that raises the severity unchecked makes it `false`. The alert's CAP document
  (`cap_url`) has it as the parameter `checked`, and a `text` output's first line says "not
  checked" after the severity.
- `text`: what was announced, in the source language.
- `translations`: only translations checked to be in their target language.
- `context`: up to two utterances stored just before its first mention (`segment_id`), on
  the same station, oldest first, each with `id`, `time`, `station` (its id), `text` and
  `translations`. What may not leave this machine is left out, and a station alert has none.
  An output sends as many as its `context` setting says (0 to 10).
- `status`: where the alert stands on this machine. `pending` while it is checked or its
  translations awaited; once announced, `delivered` (every action worked), `partial` (some
  reached someone), `failed` (none did) or `logged` (it had no action but the log);
  `covered` when another alert announced the same sentence. An alert seen as `pending` comes
  again with its new status; only the feed sends those (`change`: `update`).
- `cap_url`: the same alert as a CAP 1.2 document. On the Wavescribe machine,
  `uv run wavescribe alerts cap <id>` prints it, with the number `wavescribe alerts` shows.

A batch, used by pushes that batch utterances, is
`{"type": "batch", "version": 1, "node": {…}, "events": [ … ]}`.

The sample an output's *Test* button sends, utterance or alert, also carries `"test": true`.
Real events never have it. Drop a test event or only log it: a Wavescribe that takes pushes
checks it like a real one and keeps nothing.

## Pull: `/feed/v1`

| Request | What it returns |
|---|---|
| `GET /feed/v1/events` | Server-Sent Events: `utterance` and `alert` by default. Filters: `?events=utterance,alert,status`, `?stations=a,b`, `?langs=en`. |
| `GET /feed/v1/utterances` | What was said, in the order it was stored (by `id`): `?since=<epoch>&until=<epoch>` or `?since_id=<id>` (a segment id, or a live stream's event id), `?stations=`, `?langs=`, `?limit=` (500 by default, up to 10,000; counts only what this machine may give), `?format=json\|ndjson\|csv`. A page cut at the limit goes on with `?since_id=<its last id>`, with nothing skipped. A line still being translated is there without its translations. |
| `GET /feed/v1/alerts` | The 30 newest alerts, newest first; with `?since=<epoch>`, those raised or repeated since then (the newest 500 at most). |
| `GET /feed/v1/catches` | What band watches caught, newest first, each with the words heard in it, in the shape `wavescribe radio catches --json` gives ([AGENT_TOOLS.md](AGENT_TOOLS.md)) but without your notes: `?hours=` (24; 0 for everything kept), `?mhz=` with `?width_khz=` either side, `?stations=`, `?limit=` (100 by default, up to 500). Only catches on radio whose text may leave this machine ([RADIO.md](RADIO.md)). |
| `GET /feed/v1/stations` | Stations: language, translation languages, running, connected, last heard, lag. |
| `GET /feed/v1/node` | This machine: `name`, `url`, `id`, `version`, `feed_version`, `stations` (how many it shows you), `translation_languages`, `store` (whether history is kept) and `ask` (whether Ask works). |
| `GET /healthz` | `{"ok": true}` once models are loaded (503 before). It needs no token. |
| `GET /metrics` | Prometheus text: stations running and connected, utterances, queues, drops, lag, outputs, memory footprint, free disk, power draw. |

**Resuming.** Utterances finish out of order: one whose translation waited behind a busier
station, or that Whisper re-checked, finishes after later ones. So each `utterance` event's
`id:` is a resume point rather than a segment id (the event's own segment id is its `id`
field): `<low>` or `<low>,<id>,<id>…`. Every utterance up to segment id `low` that the stream
may send has been sent, and so have the ids after it; `low` stays just below the oldest line
still in flight on the stations you follow. Treat it as text and send it back as it came.
To go on after a restart, keep the last event id, not the last `id` field: `since_id=197`
takes everything up to 197 as sent, so a line 196 that finished after 197 would never come.
`wavescribe follow` gives the event id on each line as `resume` and keeps it with `--resume FILE`
([AGENT_TOOLS.md](AGENT_TOOLS.md), *Stopping and going on*).

EventSource sends the last one back as `Last-Event-ID` when it reconnects (a client that opens a
new stream passes it as `?since_id=`). The feed then replays the lines that finished since (up
to 1,000), sends those still being translated when they finish, never unfinished, and goes on
live: each line once.

A resume can still send a few lines again: the id lists at most 64 lines sent past `low` (more
finish while an old one waits only on a very busy machine), and a replay cut at 1,000 sends the
rest on the next resume. **Skip an utterance whose `id` you already have** (with the same
text); `wavescribe follow` and remote stations do. Alerts carry no `id:` and aren't replayed:
ask `/feed/v1/alerts?since=` for those raised meanwhile. After a restart, a client that kept no
id can ask for `?since=<epoch>`. Pass `?events=status` to get a status event every 10 s.

```sh
curl -N "http://kitchen-mac.local:8765/feed/v1/events?stations=tsf"
curl "http://kitchen-mac.local:8765/feed/v1/utterances?since=$(date -v-1H +%s)&format=csv" > hour.csv
```

In CSV, a cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading
apostrophe, so a spreadsheet doesn't run it as a formula.

`since`, `until` and `limit` that aren't numbers, and a `since_id` that is neither a segment id
nor an event id, get a 400 that names the parameter: `{"error": "bad parameter: …"}`;
`/feed/v1/catches` says what a valid query is instead. A
`limit` out of range is held to 1–10,000. A `Last-Event-ID` header that isn't an event id is
ignored, and the stream starts live. Unknown parameters and `events` values are ignored.

**From a web page.** The feed sends no CORS headers, so a browser lets only a page from the
same origin read it: one Wavescribe serves itself, or one served through the same proxy.
A page elsewhere gets nothing, ticket or not; read the feed from your server, or have your
proxy add the CORS headers you want. Tickets are for clients that can't set a header, such as
an EventSource on such a page.

## Who can read what

`serve` prints one line when it starts saying which of these holds.

| Who | The page's data: what was said, alerts, catches, audio, live events | The feed: `/feed/v1`, `/metrics` | Asking questions and for digests (`/api/ask`, `/api/digest`, `/api/digest/<id>/cancel`) | Controls (`/api/manage/…`, start and stop, reading a digest aloud: `/api/digest/speak`, `/api/digest/speech/stop`) |
|---|---|---|---|---|
| This machine | yes, no token; behind a proxy as through it, unless the request carries the control key (`wavescribe token`, the agent tools) | yes, no token; behind a proxy likewise | yes; behind a proxy likewise | yes; behind a proxy only with the control key (`wavescribe token`, the agent tools) |
| The LAN (`serve --lan`) | yes, no token | no token until the first feed token is made; then a `read` token | only with `serve --allow-remote-ask` | never |
| Through a proxy (`serve --behind-proxy`, or a request carrying a proxy's header) | a `read` token, unless `serve --public-read` | always a `read` token | only with `--allow-remote-ask`, and a `read` token unless `--public-read` | never |

- **Everyone:** the page itself (its HTML, scripts and icons) and `/healthz` need nothing.
  Pushing to `/feed/v1/ingest` always needs a `push` token, even from this machine, as JSON
  (`Content-Type: application/json`): otherwise any web page open on the machine could post
  events and set off alerts.
- **The page's data** is every `GET` under `/api/` but the controls, and `/audio/`: among them
  `/api/state`, `/api/events`, `/api/history`, `/api/search`, `/api/alerts`,
  `/api/alerts/<id>.cap`, `/api/catches` (what radio watches caught: frequencies, times, words),
  `/api/segments/<id>/audio.wav` (the feed's `audio_url`) and `…/audio.pcm`, and
  `/audio/<station>.pcm`. Without a token where one is needed, the answer is 401 with
  `{"error": …, "token_required": true}` and `WWW-Authenticate: Bearer`.
- **On the LAN** there is no login: anyone on your network reads the page, what it heard and its
  recordings, as they could listen to the radio in the room. Use `--lan` only on a network you
  trust. Feed tokens guard the feed there, not the page.
- **Asking** runs this machine's LLM, which is then busy for everything else, so another machine
  may ask only when `serve --allow-remote-ask` lets it. Otherwise the page there shows Ask as
  unavailable (`ask` in `/api/state` says why, `ask_code` is `remote_off`) and `POST /api/ask`
  is 403; digests likewise (`digest` and `digest_code`).
- **The `X-Wavescribe` header.** Every request that asks, makes or cancels a digest, or uses a
  control must carry the header `X-Wavescribe: 1`, from this machine too. Without it the answer
  is 403 with `{"error": "missing X-Wavescribe header"}`. A form on another website can't set
  it, so a page open in your browser can't spend the model or press a control. The feed
  (`/feed/v1`) doesn't need it.
- **Through a proxy.** A request carrying `Forwarded`, `X-Forwarded-For`, `X-Real-IP` or `Via`
  came through a proxy, so it is another machine's even when it connects from this one: it is
  read-only, counts against the limits for other machines, and needs a token as in the table.
  The address in those headers is never trusted or used for anything else. A proxy that adds
  none of them makes every visitor look like this machine, controls included: start Wavescribe
  with `serve --behind-proxy` whenever a proxy or tunnel forwards to it.
- **Over the internet:** put Wavescribe behind a reverse proxy with TLS and start it with
  `serve --behind-proxy`. Set the proxy's public name as the *Public URL* first (*Settings →
  Connections → For other programs*): requests that name the machine any other way get 421.
  `--public-read` lets anyone who reaches the proxy read the page without a token; the feed
  still needs one.
- **What leaves this machine:** to every other machine, radio whose class keeps its text here
  (by default everything but broadcast and streams) is invisible, and its alerts reach them
  without words.
- **By the machine's name only:** every request must name the machine in its `Host` header
  (`localhost`, `127.0.0.1`, `[::1]`, a `.localhost` name, the machine's own name or an
  address); from other machines a `.local` name and the public URL's host count too. Anything
  else gets 421, so a website whose name is made to resolve to 127.0.0.1 (DNS rebinding) can't
  reach the page's controls.
- **Tokens** are made on the page (*Settings → Connections → For other programs → Feed
  tokens*), or with `wavescribe token` on the Wavescribe machine, with a scope:
  - `read`: the feed, and the page's data through a proxy;
  - `push`: `/feed/v1/ingest`.

  Behind a proxy the page's controls are off, even on the Wavescribe machine, so there the
  command is the way. Run it in the folder Wavescribe runs in, while it runs:

  ```sh
  uv run wavescribe token make --label "kitchen screen"   # prints the token once
  uv run wavescribe token make --scope push --label "valley node"   # one that may send events here
  uv run wavescribe token list                            # ids, scopes, labels
  uv run wavescribe token revoke 3f9c0a1b                 # by the id the list shows
  ```

  It reaches the running Wavescribe through `data/control.key`, a random key `serve` makes
  once, readable by you only. A request from this machine's own address that carries it counts
  as this machine's, proxy or not; the command sends it only to this machine (`--url` must be
  `localhost`). Keep the file as private as `config/ui.toml`, and delete it to make a new one
  at the next start.

  Send a token as `Authorization: Bearer <token>`. A token is shown once and only its SHA-256 hash
  is kept, in `config/ui.toml` (mode 0600). Deleting a token ends the streams opened with it
  at once (the feed, the page's live events and audio), and its unused tickets stop working.
- **Where a header can't be set** (a browser's EventSource), trade a `read` token for a stream
  ticket and put that in the URL: `POST /feed/v1/ticket` with the token in the
  `Authorization` header (never in the URL) returns `{"ticket": "wsk_…", "expires_in": 60}`,
  then `GET /feed/v1/events?ticket=wsk_…` (or `/api/events?ticket=…`) opens the stream. A
  ticket works once, within 60 s, and only for reading, so one found in a log is spent. An
  EventSource reconnects with the URL it was given, which no longer works: close it, get a new
  ticket and open a new one with `?since_id=<the last event id>` (EventSource keeps it as
  `lastEventId`). `?token=<token>` still works but is logged as a warning (at most once an
  hour): a token in a URL ends up in logs, here, in browsers and on any proxy.

  ```sh
  T=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" "http://kitchen-mac.local:8765/feed/v1/ticket" | jq -r .ticket)
  curl -N "http://kitchen-mac.local:8765/feed/v1/events?ticket=$T"
  ```

## Push: outputs

Alerts can be missed, late or wrong: don't connect them to anything where a missed or a false
alert causes harm.

An output is a URL plus filters, set on the page (*Settings → Connections*) and saved in
`config/ui.toml`. Its name gives its id; a new output with the name of one that exists is
refused (400), so it never replaces that one and its secret or headers:

| Field | Meaning |
|---|---|
| `events` | `alert`, `utterance`, or both |
| `rules`, `min_severity`, `stations` | which alerts go out (all rules, medium and up, all stations, by default) |
| `languages` | translations to include (all, by default) |
| `format` | `json` (the events above), `cap` (CAP 1.2 XML, alerts only), `text` (a readable line or two, for chat bots), `line` (an alert as one short line, alerts only; below) |
| `max_bytes` | `line` format: the longest line, in bytes of UTF-8: 200 by default, 40 to 4096 |
| `batch_s` | utterances: 0 sends each one; more sends one batch per window. Alerts are never batched: each goes when it is announced, which waits up to `translation_wait_s` (10 s by default, `[delivery]` in `alerts.toml`) for its translations: up to about 12 s after it was heard when the translator is busy |
| `headers` | extra headers, e.g. `Authorization` for the receiving end |
| `secret` | signs each request, see below |
| `context` | how many utterances before an alert's to include (2) |
| `alert_max_age_hours` | how long an alert that hasn't got there is still sent, counted from when it was raised: 24 by default, more than 0 and at most 168 (a week). The page's form has no field for it: it is read from the output's `[[output]]` in `config/ui.toml`, stays when the output is edited on the page, and is said under the output's *Advanced* ("Alerts older than 24 h aren't sent") |

**Delivery:**

- An alert goes out when it is announced (`change`: `new`), again only when it gets more
  serious (`escalated`), is still being mentioned an hour later (`reminder`) or, for a station
  alert, is over (`ended`); never for each mention. In `cap` format the first is a CAP `Alert`
  and the others are an `Update` with an identifier of their own, all in the same `incidents`.
  In `text` and `line` format the first line says "(more serious now)", "(still being
  mentioned)" or "(over)", and "not checked" after the severity when the model couldn't check
  the match; in `cap` format that is the parameter `checked`.
- On a network error, 408, 425, 429 or 5xx, an alert gets six tries, 2, 4, 8, 16 and 32 s
  apart (about a minute), and utterances three (2 and 4 s apart).
- If the other end is still away after those, nothing is dropped: the events wait at the head
  of the output's queue and go again after 5 s, then 10, 20, 40 and every 60 s, until it is
  back (a central server restarting, a router rebooting) or, for an alert, until it is too old
  (below). The page shows the output as waiting. Any other answer (400, 401, 404, a redirect)
  fails the delivery and is not tried again.
- Each output has its own queue (2,000 events, waiting ones included; when it is full the
  oldest utterances go first, and the oldest alert only when nothing but alerts waits), so a
  dead endpoint never slows anything else. An alert goes before any utterances waiting.
- **Alerts wait through a restart, utterances don't.** An alert that hasn't got there is also
  a row in the store (the `outbox` table of `data/wavescribe.db`), written before the first
  try and removed once the other end answered 2xx or refused it for good. When Wavescribe is
  restarted, or crashes, in between, the next run sends what is left: it counts on from the
  tries so far and doesn't try sooner than the next one was planned. Utterances wait in memory
  only: a restart empties them. With `--no-store` alerts wait in memory only as well, and
  those that hadn't got there are lost with the run.
- **At least once.** An alert can arrive twice: when the other end got it and its answer was
  lost (a timeout, a crash straight after), it is sent again. Each alert delivery has an id,
  32 hex digits, in the `X-Wavescribe-Delivery` header and, in `json` format, in the event's
  `delivery_id`. It is the same on every try of that alert to that output, also after a
  restart: keep the ids you have handled and ignore one you have seen. A new message has a
  new id: every output has its own, and so has each time an alert is announced (`new`,
  `escalated`, `reminder`, `ended`). The id is random; it says nothing about the alert or
  this machine. A *Test* alert has one too; utterances have none.
- **Not as news a day later.** An alert that still hasn't got there 24 hours after it was
  raised (`alert_max_age_hours`) is given up, not sent late. The delivery log says so (`not
  delivered within 24 h of the alert: given up`) and the output's status counts it. There is
  no "never": with the queue's 2,000 this bounds what waits. The page says, for each output
  under *Settings → Connections* and *Health*, how many alerts wait and how long the oldest
  has ("2 alerts waiting, oldest 3 min"), and, since the output started or was changed, how
  many were given up as too old or dropped from a full queue (`alerts_waiting`,
  `alerts_oldest_age_s`, `alerts_expired`, `alerts_dropped` in its status).
- **Outputs that change.** An output that is edited goes on with the alerts that waited for
  it, at its new URL and in its new format, when it still asks for them (rules, stations,
  severity), without translations or context it no longer includes; its waiting utterances
  are dropped. One that is switched off sends nothing and keeps its alerts until they are too
  old. What waited for an output that was removed is dropped, at once or at the next start.
- **What is on disk.** A row holds the event as it is sent and nothing more, so no words
  from radio that isn't exported. Words the store itself doesn't keep never go there: an
  alert quoting radio whose class keeps no text waits in memory only, and a waiting row is
  removed when its words pass their class's days or the text retention (the output goes on
  trying from memory). At a start, words whose station was removed or is no longer exported
  are taken out before the alert is sent. The store commits as the rest of it does: a crash
  of Wavescribe loses nothing, a power cut or a system crash can lose the last moments.
- Every request is logged, with its status, attempts and time, and for an alert its delivery
  id and whether it had waited through a restart (`after_restart`). An alert that was given
  up or dropped is logged as a failure with the reason. The page shows the log, an alert that
  waited through a restart marked "after a restart" and its delivery id on hover; it is also
  in the store's `deliveries` table.
- A redirect (any 3xx) is never followed: it is a failed delivery, not tried again, logged
  with its status and where it pointed (`HTTP 302: redirected to …; not followed`). The
  `Authorization` header, other headers you set and the signature go only to the URL you
  gave: set the final address.
- The page's *Test* button sends one sample event, once, with no retries. A sample alert has
  `"rule": "test"` and `"status": "test"`; in CAP it is `<status>Test</status>` and
  `<scope>Restricted</scope>` with a `<restriction>` saying it is a test, so a CAP consumer
  never shows it to the public.

**One line.** The `line` format is for channels that take small messages: an SMS gateway, a
pager, a radio. An alert is one line of text, at most `max_bytes` bytes of UTF-8 (200 by
default): the severity in brackets, with "not checked" when the model couldn't check the match,
the rule, the station, then the words as heard, in the station's language. It never carries the
translation: the rule matched those words, and they are the alert. Line breaks become spaces.
A longer line is cut, between whole characters (never inside a character's bytes, or between
a letter and its accent, or inside an emoji or a flag), and ends with "…":

```text
[HIGH] water · Radio Example: Corte de água amanhã entre as 9h e as 17h na zona norte.
[CRITICAL, not checked] fire (more serious now) · Радіо Приклад: Пожежа біля промислової зони, дим іде на північ; мешканців просят…
[MEDIUM] roads · 示例电台: 下午三点起桥附近道路封闭，请绕行市中心，并留意救援部门的最新通知。谢谢合作，祝大家平安，请转告家人和邻居，保持…
```

The last two are cut at 200 bytes.

200 bytes hold about 200 letters of a Latin script, 100 of Cyrillic or Greek, and 66 of
Chinese or Japanese.

**Headers.** Each request is a `POST` with `Content-Type` (`application/json`,
`application/xml; charset=utf-8` for CAP, `text/plain; charset=utf-8` for text and one line),
`User-Agent: wavescribe/<version>`, `X-Wavescribe-Event` (`alert`, `utterance` or `batch`),
the output's own `headers` (which can replace the three before), `X-Wavescribe-Delivery` on
an alert, and the signature headers below when a secret is set.

**Signatures.** With a secret, each request carries `X-Wavescribe-Timestamp` (Unix seconds)
and `X-Wavescribe-Signature: sha256=<hex>`, the HMAC-SHA256 of `"<timestamp>.<body>"` keyed
with the secret. To verify:

```python
import hashlib, hmac

expected = "sha256=" + hmac.new(secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
ok = hmac.compare_digest(expected, request.headers["X-Wavescribe-Signature"])
```

Reject timestamps older than a few minutes, to stop replays. A later round of tries, or one
after a restart, is signed again with the time it is sent at.

The signature covers the body, not the headers. In `json` format an alert's delivery id is
in the body (`delivery_id`), so it is signed: dedupe on that one. In `cap` format the body's
`<identifier>` is the same on every try and signed too. In `text` and `line` format the id is
in the `X-Wavescribe-Delivery` header only, which the signature doesn't cover.

## Many places, one central server

Each place runs Wavescribe on its own machine with its own antenna; one machine collects the
text. There are two ways to connect them.

**The central server pulls.** Use this when it can reach the places, on a LAN or a VPN. Add
a *remote station* on the central server, on the page under *Find → On the internet → Other
ways to add → From another Wavescribe*, or in its `stations.toml`:

```toml
[[station]]
id = "north-tsf"
name = "TSF (north node)"
language = "pt-PT"
translate_to = ["en", "es"]
remote = "http://north-node.local:8765"
remote_station = "tsf"
remote_token = "wst_…"      # if that node has feed tokens
privacy = "amateur"         # optional: handle its text at least this strictly
```

- The central server subscribes to the node's feed and catches up on the last hour the first
  time.
- After that it resumes after the last utterance it stored, going back a week at most. A node
  whose clock runs fast sends the last few minutes again, since their times were stored here
  as they arrived; what it sends again is recognised by its words and length and skipped.
- On a lost connection it reconnects, backing off from 2 s to 60 s.

**The places push.** Use this when they sit behind NAT. On the central server, make a token
with the `push` scope. On each place, add an output (Settings → **Connections** → **Webhooks
and live pushes** → **Push to a central Wavescribe** starts one, for you to give the address
and the token):

- URL: `http://central:8765/feed/v1/ingest`
- events: `utterance` (the central server runs its own rules; pushed alerts are ignored)
- batch: 5 s
- header: `Authorization: Bearer <that token>`

Each of a place's stations appears on the central server the first time it sends something, as
`<place>-<station>`, with `remote = "push"`. An id longer than 40 characters keeps the first 16
of the place's name and gets a short hash of both names, so two long names never share an id. A push only writes into stations that pushes made:
an event whose id would land on any other station here is refused. One place can make 25
stations and all places together 100. The answer is `{"accepted": n, "rejected": m}` with status
200; the log says why events were refused. When the push held test events (the *Test* button),
it is `{"accepted": 0, "rejected": m, "test": k}`, with `k` the test events that would have been
taken. Those make no station and store nothing. An event is refused whole if a value is out of
bounds: `t_start` older than 7 days or more than 10 minutes ahead of this machine's clock,
`duration` outside 0–600 s, text over 4,000 characters, a language that isn't a BCP-47 tag. A
`t_start` up to 10 minutes ahead is taken as a clock that runs fast and stored as the time the
event arrived.

**Either way:**

- The central server stores, searches and answers questions over the text like its own, by the
  privacy class each station's events carry: radio that isn't broadcast is kept, quoted and
  passed on by the central server's own rules for that class.
- It runs **its own** rules on the source-language text. Translation stays off the alert path.
- It translates only into languages the text didn't arrive with, so the place can translate
  into the local language and the central server into its own.
- No audio crosses. Each event's `audio_url` points back at the place that kept it.
- A line can go on from the central server to another, with `via` saying where it has been
  (*Event format*). It never comes back to a machine it passed through, so places and central
  servers can push to each other both ways. A machine learns a station's path from its lines
  as they come, in memory: just after a restart, a line stored before it can come back once.

## Using Wavescribe from another program

- **Dashboards:** read `/feed/v1/events` (or poll `/feed/v1/utterances?since_id=`).
- **Automations:** point an output at your webhook endpoint (Home Assistant, Node-RED, n8n, a
  script). It receives the alert JSON above.
- **Monitoring:** scrape `/metrics`; health checks use `/healthz`.
- **Archiving:** fetch `/feed/v1/utterances?format=ndjson` by time range.
